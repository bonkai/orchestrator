"""Offline tests for the visible-tab Fusion path (F1.6 / F1.7):
  - the standalone fusion_call.py runner (run as a subprocess, fake providers)
  - claude_runner._price_tab_answers / _panel_answers dispatch + fallback
  - spawn fusion-tab helpers (cmd string, cleanup, ensure_fusion_runner, guard)

NO NETWORK and NO iTerm2: providers are faked with a tiny local script, the tab
spawn is mocked. The live end-to-end (real Gemini fusion tab + Opus judge) is
verified separately.

Usage:
    python -m unittest tests.test_fusion_tab -v
"""

import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orchestrator.lib import claude_runner, spawn

FUSION_CALL = REPO / "orchestrator" / "fusion_call.py"

# A fake provider script: reads the request on stdin, echoes a normalized result.
# `ok` and token counts are derived from the model id so we can assert per-seat.
FAKE_PROVIDER = (
    "import sys, json\n"
    "req = json.load(sys.stdin)\n"
    "sys.stderr.write('fake provider ' + req['model'] + '\\n')\n"
    "print(json.dumps({'ok': True, 'text': 'ans-' + req['model'], 'model': req['model'],\n"
    "                  'prompt_tokens': 5, 'completion_tokens': 7, 'error': ''}))\n"
)
FAKE_PROVIDER_ERR = (
    "import sys, json\n"
    "json.load(sys.stdin)\n"
    "print(json.dumps({'ok': False, 'error': 'no key'}))\n"
)


# ───────────────────── F1.6: the standalone fusion_call.py ──────────────────

class TestFusionCallStandalone(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fusioncall_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, content):
        p = self.tmp / name
        p.write_text(content)
        return str(p)            # absolute path → fusion_call.py runs it as-is

    def _run(self, body):
        req = self.tmp / "req.json"
        req.write_text(json.dumps(body))
        # Run the REAL repo fusion_call.py as a subprocess (as the tab would).
        p = subprocess.run([sys.executable, str(FUSION_CALL), str(req)],
                           capture_output=True, text=True, timeout=30)
        return p

    def test_collects_named_answers_in_order(self):
        a = self._write("a.py", FAKE_PROVIDER)
        b = self._write("b.py", FAKE_PROVIDER)
        body = {"prompt": "q", "timeout_s": 10, "panel": ["a", "b"],
                "providers": {"a": {"script": a, "model": "m1"},
                              "b": {"script": b, "model": "m2"}}}
        p = self._run(body)
        out = json.loads(p.stdout)
        self.assertEqual([x["name"] for x in out], ["a", "b"])
        self.assertTrue(all(x["ok"] for x in out))
        self.assertEqual(out[0]["text"], "ans-m1")
        self.assertEqual(out[1]["completion_tokens"], 7)
        # Provider stderr streamed (watchable), NOT captured into the JSON stdout.
        self.assertIn("fake provider m1", p.stderr)

    def test_failing_provider_passed_through(self):
        ok = self._write("ok.py", FAKE_PROVIDER)
        bad = self._write("bad.py", FAKE_PROVIDER_ERR)
        body = {"prompt": "q", "timeout_s": 10, "panel": ["ok", "bad"],
                "providers": {"ok": {"script": ok, "model": "m1"},
                              "bad": {"script": bad, "model": "m2"}}}
        out = json.loads(self._run(body).stdout)
        by = {x["name"]: x for x in out}
        self.assertTrue(by["ok"]["ok"])
        self.assertFalse(by["bad"]["ok"])
        self.assertIn("no key", by["bad"]["error"])

    def test_bad_request_path_emits_valid_json_no_traceback(self):
        p = subprocess.run([sys.executable, str(FUSION_CALL), "/nope/missing.json"],
                           capture_output=True, text=True, timeout=30)
        out = json.loads(p.stdout)              # still valid JSON
        self.assertFalse(out[0]["ok"])
        self.assertNotIn("Traceback", p.stdout)


# ───────────────────── F1.7: _price_tab_answers ────────────────────────────

class TestPriceTabAnswers(unittest.TestCase):
    PROVIDERS = {"gemini": {"model": "gemini-2.5-flash", "price_in": 0.30, "price_out": 1.50}}

    def test_prices_ok_and_passes_errors(self):
        raw = [{"name": "gemini", "ok": True, "text": "hi", "model": "gemini-2.5-flash",
                "prompt_tokens": 1000, "completion_tokens": 500},
               {"name": "gemini", "ok": False, "error": "boom"},
               "garbage-not-a-dict"]
        out = claude_runner._price_tab_answers(raw, self.PROVIDERS)
        self.assertEqual(len(out), 2)                 # the non-dict is skipped
        self.assertAlmostEqual(out[0]["cost"], 0.00105, places=9)
        self.assertEqual(out[0]["text"], "hi")
        self.assertFalse(out[1]["ok"])
        self.assertEqual(out[1]["error"], "boom")


# ───────────────────── F1.7: _panel_answers dispatch + fallback ─────────────

class TestPanelAnswersDispatch(unittest.TestCase):
    def test_prefers_tab_when_iterm2_present(self):
        tab_ans = [{"name": "gemini", "ok": True, "cost": 0.0}]
        with mock.patch.object(claude_runner.spawn, "iterm2_installed", return_value=True), \
                mock.patch.object(claude_runner, "_run_fusion_in_tab", return_value=tab_ans) as tab, \
                mock.patch.object(claude_runner, "_run_panel") as inproc:
            out = claude_runner._panel_answers("q", ["gemini", "gemini2"], {}, 60)
        self.assertEqual(out, tab_ans)
        tab.assert_called_once()
        inproc.assert_not_called()

    def test_falls_back_in_process_when_no_iterm2(self):
        inproc_ans = [{"name": "gemini", "ok": True}]
        with mock.patch.object(claude_runner.spawn, "iterm2_installed", return_value=False), \
                mock.patch.object(claude_runner, "_run_fusion_in_tab") as tab, \
                mock.patch.object(claude_runner, "_run_panel", return_value=inproc_ans) as inproc:
            out = claude_runner._panel_answers("q", ["gemini", "gemini2"], {}, 60)
        self.assertEqual(out, inproc_ans)
        tab.assert_not_called()                  # never tried the tab
        inproc.assert_called_once()

    def test_falls_back_when_tab_returns_none(self):
        inproc_ans = [{"name": "gemini", "ok": True}]
        with mock.patch.object(claude_runner.spawn, "iterm2_installed", return_value=True), \
                mock.patch.object(claude_runner, "_run_fusion_in_tab", return_value=None), \
                mock.patch.object(claude_runner, "_run_panel", return_value=inproc_ans) as inproc:
            out = claude_runner._panel_answers("q", ["gemini", "gemini2"], {}, 60)
        self.assertEqual(out, inproc_ans)        # tab failed → in-process
        inproc.assert_called_once()


# ───────────────────── F1.7a: spawn fusion-tab helpers ──────────────────────

class TestSpawnFusionHelpers(unittest.TestCase):
    def test_tab_cmd_sets_fusion_id_not_run_id(self):
        cmd = spawn._fusion_tab_cmd("fusion-abc123", "/proj", "t")
        self.assertIn("ORCHESTRATOR_FUSION_ID=fusion-abc123", cmd)
        self.assertNotIn("ORCHESTRATOR_RUN_ID", cmd)   # Stop hook stays a no-op
        self.assertIn("fusion_run.sh", cmd)

    def test_spawn_fusion_tab_raises_without_iterm2(self):
        with mock.patch.object(spawn, "iterm2_installed", return_value=False):
            with self.assertRaises(RuntimeError):
                spawn.spawn_fusion_tab("fusion-x", {"prompt": "q"}, "/tmp")

    def test_cleanup_removes_sidecars(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            with mock.patch.object(spawn, "FUSION_DIR", tmp):
                for suf in ("request.json", "json", "done", "pid"):
                    (tmp / f"fusion-z.{suf}").write_text("x")
                spawn.cleanup_fusion_files("fusion-z")
                self.assertEqual(list(tmp.glob("fusion-z.*")), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ensure_fusion_runner_materializes(self):
        # Redirect the destination paths to a temp dir; keep the repo source real
        # so the actual fusion_call.py + provider scripts get copied.
        tmp = Path(tempfile.mkdtemp())
        bind = tmp / "bin"
        with contextlib.ExitStack() as es:
            es.enter_context(mock.patch.object(spawn, "FUSION_DIR", tmp / "fusion"))
            es.enter_context(mock.patch.object(spawn, "BIN_DIR", bind))
            es.enter_context(mock.patch.object(spawn, "FUSION_RUN_SH", bind / "fusion_run.sh"))
            es.enter_context(mock.patch.object(spawn, "FUSION_CALL_PY", bind / "fusion_call.py"))
            es.enter_context(mock.patch.object(spawn, "FUSION_PROVIDERS_DIR", bind / "providers"))
            try:
                spawn.ensure_fusion_runner()
                self.assertTrue((bind / "fusion_run.sh").is_file())
                self.assertIn("ORCHESTRATOR_FUSION_ID", (bind / "fusion_run.sh").read_text())
                self.assertTrue((bind / "fusion_call.py").is_file())
                self.assertTrue((bind / "providers" / "gemini.py").is_file())
            finally:
                shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
