"""Offline unit tests for the Fusion orchestration in claude_runner (F1.3–F1.5,
F1.8) and spawn.ensure_fusion_providers (F1.7a subset).

NO NETWORK: the provider subprocess and the `claude` judge are both mocked, so
this exercises the panel→judge→cost wiring and the fallback dispatcher without a
single API call. The live end-to-end (real Gemini panel + Opus judge) is run
separately once a key is configured.

Usage:
    python -m unittest tests.test_fusion_runner -v
    python tests/test_fusion_runner.py
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orchestrator.lib import claude_runner, spawn
from orchestrator.lib.claude_runner import ClaudeRun

PROV = {"script": "providers/gemini.py", "model": "gemini-2.5-flash",
        "price_in": 0.30, "price_out": 1.50}


def _fake_proc(stdout):
    return mock.Mock(stdout=stdout, stderr="", returncode=0)


# ───────────────────────────── F1.3: _panel_answer ─────────────────────────

class TestPanelAnswer(unittest.TestCase):
    def test_ok_computes_cost_from_registry(self):
        out = json.dumps({"ok": True, "text": "hi", "model": "gemini-2.5-flash",
                          "prompt_tokens": 1000, "completion_tokens": 500, "error": ""})
        with mock.patch.object(claude_runner.subprocess, "run", return_value=_fake_proc(out)):
            a = claude_runner._panel_answer("gemini", PROV, "q", 300)
        self.assertTrue(a["ok"])
        self.assertEqual(a["text"], "hi")
        # (1000*0.30 + 500*1.50) / 1e6 = 0.00105
        self.assertAlmostEqual(a["cost"], 0.00105, places=9)
        self.assertEqual(a["prompt_tokens"], 1000)
        self.assertEqual(a["completion_tokens"], 500)

    def test_script_ok_false_surfaces_error_no_raise(self):
        out = json.dumps({"ok": False, "error": "GEMINI_API_KEY not set"})
        with mock.patch.object(claude_runner.subprocess, "run", return_value=_fake_proc(out)):
            a = claude_runner._panel_answer("gemini", PROV, "q", 300)
        self.assertFalse(a["ok"])
        self.assertIn("GEMINI_API_KEY", a["error"])

    def test_subprocess_exception_never_raises(self):
        boom = claude_runner.subprocess.TimeoutExpired(cmd="python3", timeout=5)
        with mock.patch.object(claude_runner.subprocess, "run", side_effect=boom):
            a = claude_runner._panel_answer("gemini", PROV, "q", 5)
        self.assertFalse(a["ok"])
        self.assertEqual(a["name"], "gemini")

    def test_garbage_stdout_never_raises(self):
        with mock.patch.object(claude_runner.subprocess, "run",
                               return_value=_fake_proc("not json")):
            a = claude_runner._panel_answer("gemini", PROV, "q", 300)
        self.assertFalse(a["ok"])


# ────────────────────────────── F1.4: _run_panel ───────────────────────────

class TestRunPanel(unittest.TestCase):
    def test_runs_each_seat_in_order(self):
        providers = {n: dict(PROV) for n in ("a", "b", "c")}
        with mock.patch.object(claude_runner, "_panel_answer",
                               side_effect=lambda n, p, q, t: {"name": n, "ok": True}):
            res = claude_runner._run_panel("q", ["a", "b", "c"], providers, 300)
        self.assertEqual([r["name"] for r in res], ["a", "b", "c"])

    def test_empty_panel_returns_empty(self):
        self.assertEqual(claude_runner._run_panel("q", [], {}, 300), [])


# ───────────────────────────── F1.5: _judge_prompt ─────────────────────────

class TestJudgePrompt(unittest.TestCase):
    def test_contains_original_answers_and_instruction(self):
        orig = "Answer in JSON: {\"x\": 1}"
        answers = [{"name": "gemini", "model": "g", "text": "AAA"},
                   {"name": "gemini2", "model": "g", "text": "BBB"}]
        jp = claude_runner._judge_prompt(orig, answers)
        self.assertIn(orig, jp)            # original prompt verbatim (schema travels)
        self.assertIn("AAA", jp)
        self.assertIn("BBB", jp)
        self.assertIn("synthesize the single best response", jp.lower())
        self.assertIn("exact same format", jp.lower())


# ─────────────────────────── F1.5: run_fusion_json ─────────────────────────

class TestRunFusionJson(unittest.TestCase):
    PROVIDERS = {"gemini": dict(PROV), "gemini2": dict(PROV)}
    PRESETS = {"budget": ["gemini", "gemini2"]}

    def _patch_cfg(self, active):
        """Patch config so run_fusion_json sees `active` as the active set."""
        cfg = {"preset": "budget", "timeout_s": 42,
               "providers": self.PROVIDERS, "presets": self.PRESETS}
        return [
            mock.patch.object(claude_runner.config, "fusion_config", return_value=cfg),
            mock.patch.object(claude_runner.config, "active_providers", return_value=active),
            mock.patch.object(claude_runner.spawn, "ensure_fusion_providers"),
        ]

    def test_happy_path_judges_and_sums_cost(self):
        active = {"gemini": dict(PROV), "gemini2": dict(PROV)}
        panel = [{"name": "gemini", "text": "A", "cost": 0.001, "ok": True},
                 {"name": "gemini2", "text": "B", "cost": 0.002, "ok": True}]
        fake_judge = ClaudeRun(ok=True, text="SYNTH", model="opus")
        patches = self._patch_cfg(active) + [
            mock.patch.object(claude_runner, "_run_panel", return_value=panel),
            mock.patch.object(claude_runner, "run_claude_json", return_value=fake_judge),
        ]
        with patches[0], patches[1], patches[2], patches[3], \
                mock.patch.object(claude_runner, "run_claude_json",
                                  return_value=fake_judge) as rcj:
            run = claude_runner.run_fusion_json("q", cwd="/tmp")
        self.assertTrue(run.ok)
        self.assertEqual(run.text, "SYNTH")
        self.assertAlmostEqual(run.cost_usd, 0.003, places=9)   # Σ panel cost
        self.assertEqual([a["name"] for a in run.raw["panel"]], ["gemini", "gemini2"])
        # Judge ran on Opus explicitly (run_claude_json defaults to sonnet).
        self.assertEqual(rcj.call_args.kwargs.get("model"), "opus")
        self.assertEqual(rcj.call_args.kwargs.get("effort"), "high")

    def test_under_two_active_returns_not_ok_without_judging(self):
        active = {"gemini": dict(PROV)}    # only 1 keyed
        with self._patch_cfg(active)[0], self._patch_cfg(active)[1], \
                self._patch_cfg(active)[2], \
                mock.patch.object(claude_runner, "_run_panel") as rp, \
                mock.patch.object(claude_runner, "run_claude_json") as rcj:
            run = claude_runner.run_fusion_json("q", cwd="/tmp")
        self.assertFalse(run.ok)
        self.assertIn(">=2", run.error)
        rp.assert_not_called()             # never spawned the panel
        rcj.assert_not_called()            # never ran the judge

    def test_panel_under_two_ok_returns_not_ok(self):
        active = {"gemini": dict(PROV), "gemini2": dict(PROV)}
        panel = [{"name": "gemini", "text": "A", "cost": 0.001, "ok": True},
                 {"name": "gemini2", "ok": False, "error": "boom"}]
        with self._patch_cfg(active)[0], self._patch_cfg(active)[1], \
                self._patch_cfg(active)[2], \
                mock.patch.object(claude_runner, "_run_panel", return_value=panel), \
                mock.patch.object(claude_runner, "run_claude_json") as rcj:
            run = claude_runner.run_fusion_json("q", cwd="/tmp")
        self.assertFalse(run.ok)
        self.assertIn("only 1 provider", run.error)
        self.assertIn("boom", run.error)
        rcj.assert_not_called()


# ───────────────────────────── F1.8: run_brain_json ────────────────────────

class TestRunBrainJson(unittest.TestCase):
    def test_fusion_off_calls_claude_directly(self):
        fake = ClaudeRun(ok=True, text="plain")
        with mock.patch.object(claude_runner, "run_fusion_json") as rfj, \
                mock.patch.object(claude_runner, "run_claude_json", return_value=fake) as rcj:
            run = claude_runner.run_brain_json("q", cwd="/tmp", fusion=False,
                                               model="opus", effort="high")
        self.assertEqual(run.text, "plain")
        rfj.assert_not_called()
        self.assertEqual(rcj.call_args.kwargs.get("model"), "opus")

    def test_fusion_on_and_ok_returns_fusion(self):
        fused = ClaudeRun(ok=True, text="fused", cost_usd=0.01)
        with mock.patch.object(claude_runner, "run_fusion_json", return_value=fused), \
                mock.patch.object(claude_runner, "run_claude_json") as rcj:
            run = claude_runner.run_brain_json("q", cwd="/tmp", fusion=True)
        self.assertEqual(run.text, "fused")
        rcj.assert_not_called()            # judge already happened inside fusion

    def test_fusion_on_but_failed_falls_back_to_claude(self):
        failed = ClaudeRun(ok=False, error="only 1 provider")
        fallback = ClaudeRun(ok=True, text="local")
        with mock.patch.object(claude_runner, "run_fusion_json", return_value=failed), \
                mock.patch.object(claude_runner, "run_claude_json",
                                  return_value=fallback) as rcj:
            run = claude_runner.run_brain_json("q", cwd="/tmp", fusion=True,
                                               model="opus", effort="high")
        self.assertTrue(run.ok)
        self.assertEqual(run.text, "local")    # degraded to the visible-tab claude call
        rcj.assert_called_once()


# ─────────────────── F1.7a subset: ensure_fusion_providers ──────────────────

class TestEnsureFusionProviders(unittest.TestCase):
    def test_materializes_repo_scripts_executable(self):
        repo = Path(tempfile.mkdtemp(prefix="repo_prov_"))
        dest = Path(tempfile.mkdtemp(prefix="data_prov_")) / "providers"
        try:
            (repo / "gemini.py").write_text("print('x')\n")
            with mock.patch.object(spawn, "_REPO_PROVIDERS_DIR", repo), \
                    mock.patch.object(spawn, "FUSION_PROVIDERS_DIR", dest):
                spawn.ensure_fusion_providers()
            copied = dest / "gemini.py"
            self.assertTrue(copied.is_file())
            self.assertEqual(copied.read_text(), "print('x')\n")
            self.assertTrue(copied.stat().st_mode & 0o100)   # owner-executable
        finally:
            shutil.rmtree(repo, ignore_errors=True)
            shutil.rmtree(dest.parent, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
