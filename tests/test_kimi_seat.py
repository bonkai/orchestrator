"""Unit tests for the Kimi Code CLI Fusion SEAT (K1 + K2) — the kimi twin of
tests/test_codex_parser.py + tests/test_codex_seat.py:

  K1  _kimi_envelope_from_lines / _build_kimi_run / run_kimi_headless
  K2  _kimi_seat_answer + run_fusion_json's kind=="kimi_cli" fan-out

All subprocess / judge calls are mocked — never a real `kimi` call here (the live
end-to-end is exercised separately).
"""

import contextlib
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orchestrator.lib import claude_runner, config
from orchestrator.lib.claude_runner import ClaudeRun


def _ok_seat(seat, prompt, cwd):
    """Canned ok seat answer (stands in for a real _*_seat_answer in wiring tests)."""
    name = seat.get("name") or seat.get("model") or "seat"
    return {"name": name, "model": seat.get("model", ""), "text": f"ANS[{name}]",
            "cost": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
            "subscription": True, "lens": seat.get("lens", ""), "ok": True}


# ───────────────────── K1: _kimi_envelope_from_lines ────────────────────────

class TestKimiEnvelopeParser(unittest.TestCase):
    def test_last_assistant_wins_and_captures_session_id(self):
        lines = [
            '{"role":"assistant","content":"first"}',
            '{"role":"tool","tool_call_id":"t1","content":"ls output"}',
            '{"role":"assistant","content":"FINAL"}',
            '{"role":"meta","type":"session.resume_hint","session_id":"session_abc",'
            '"command":"kimi -r session_abc"}',
        ]
        env = claude_runner._kimi_envelope_from_lines(lines)
        self.assertEqual(env["result"], "FINAL")        # LAST assistant line
        self.assertEqual(env["session_id"], "session_abc")

    def test_tool_lines_never_become_the_answer(self):
        lines = ['{"role":"assistant","content":"ANS"}',
                 '{"role":"tool","tool_call_id":"t","content":"noise"}']
        self.assertEqual(claude_runner._kimi_envelope_from_lines(lines)["result"], "ANS")

    def test_no_assistant_line_returns_none(self):
        lines = ['{"role":"tool","content":"x"}',
                 '{"role":"meta","type":"session.resume_hint","session_id":"s"}']
        self.assertIsNone(claude_runner._kimi_envelope_from_lines(lines))

    def test_empty_and_garbage_lines_never_raise(self):
        lines = ["", "   ", "not json", "{bad}", "[]", "42",
                 '{"role":"assistant","content":"OK"}']
        env = claude_runner._kimi_envelope_from_lines(lines)
        self.assertEqual(env["result"], "OK")
        self.assertEqual(env["session_id"], "")          # no meta line → empty

    def test_empty_content_assistant_still_counts_as_answered(self):
        # An assistant turn with empty content is a (blank) answer, NOT "kimi died".
        env = claude_runner._kimi_envelope_from_lines(['{"role":"assistant","content":""}'])
        self.assertIsNotNone(env)
        self.assertEqual(env["result"], "")


# ───────────────────────── K1: _build_kimi_run ──────────────────────────────

class TestBuildKimiRun(unittest.TestCase):
    def test_envelope_becomes_subscription_run(self):
        run = claude_runner._build_kimi_run(
            {"result": "hello", "session_id": "session_z"}, "kimi-code/k3")
        self.assertTrue(run.ok)
        self.assertEqual(run.text, "hello")
        self.assertEqual(run.cost_usd, 0.0)              # subscription
        self.assertEqual(run.model, "kimi-code/k3")      # no model field in stream → requested
        self.assertEqual(run.raw["session_id"], "session_z")  # kept for K5 resume

    def test_json_content_is_parsed(self):
        run = claude_runner._build_kimi_run({"result": '{"a": 1}'}, "kimi-code/k3")
        self.assertEqual(run.parsed_json, {"a": 1})

    def test_empty_envelope_never_raises(self):
        run = claude_runner._build_kimi_run({}, "kimi-code/k3")
        self.assertTrue(run.ok)
        self.assertEqual(run.text, "")


# ───────────────────────── K1: run_kimi_headless ────────────────────────────

class TestKimiHeadless(unittest.TestCase):
    STREAM = ('{"role":"assistant","content":"ANSWER"}\n'
              '{"role":"meta","type":"session.resume_hint","session_id":"session_q"}\n')

    def _run(self, **kw):
        return mock.patch.object(claude_runner.subprocess, "run", **kw)

    def setUp(self):
        p = mock.patch.object(claude_runner, "_kimi_bin", return_value="/fake/bin/kimi")
        p.start(); self.addCleanup(p.stop)

    def test_happy_path_parses_answer(self):
        with self._run(return_value=mock.Mock(returncode=0, stdout=self.STREAM, stderr="")):
            run = claude_runner.run_kimi_headless("Q", "/tmp", model="kimi-code/k3")
        self.assertTrue(run.ok)
        self.assertEqual(run.text, "ANSWER")

    def test_command_uses_pinned_flags(self):
        captured = {}

        def fake(cmd, **kw):
            captured["cmd"] = cmd
            captured["env"] = kw.get("env")
            captured["stdin"] = kw.get("stdin")
            return mock.Mock(returncode=0, stdout=self.STREAM, stderr="")

        with self._run(side_effect=fake):
            claude_runner.run_kimi_headless("Q", "/tmp", model="kimi-code/k3")
        self.assertEqual(captured["cmd"],
                         ["/fake/bin/kimi", "-p", "Q", "--output-format", "stream-json",
                          "-m", "kimi-code/k3"])       # NO --print, NO -s, NO effort
        self.assertEqual(captured["stdin"], subprocess.DEVNULL)

    def test_scrubs_billed_keys_and_stop_hook(self):
        captured = {}

        def fake(cmd, **kw):
            captured["env"] = kw.get("env")
            return mock.Mock(returncode=0, stdout=self.STREAM, stderr="")

        with mock.patch.dict(os.environ, {"MOONSHOT_API_KEY": "sk-m", "OPENAI_API_KEY": "sk-o",
                                           "ORCHESTRATOR_RUN_ID": "42"}), self._run(side_effect=fake):
            claude_runner.run_kimi_headless("Q", "/tmp")
        self.assertNotIn("MOONSHOT_API_KEY", captured["env"])   # subscription, not billed
        self.assertNotIn("OPENAI_API_KEY", captured["env"])
        self.assertNotIn("ORCHESTRATOR_RUN_ID", captured["env"])  # no Stop hook

    def test_nonzero_exit_is_ok_false(self):
        with self._run(return_value=mock.Mock(returncode=1, stdout="", stderr="boom")):
            run = claude_runner.run_kimi_headless("Q", "/tmp")
        self.assertFalse(run.ok)
        self.assertIn("kimi exit 1", run.error)

    def test_missing_binary_is_ok_false(self):
        with self._run(side_effect=FileNotFoundError()):
            run = claude_runner.run_kimi_headless("Q", "/tmp")
        self.assertFalse(run.ok)
        self.assertIn("not found", run.error)

    def test_timeout_is_ok_false(self):
        with self._run(side_effect=subprocess.TimeoutExpired(cmd="kimi", timeout=5)):
            run = claude_runner.run_kimi_headless("Q", "/tmp", timeout_s=5)
        self.assertFalse(run.ok)
        self.assertIn("timed out", run.error)

    def test_no_assistant_message_is_ok_false(self):
        with self._run(return_value=mock.Mock(returncode=0,
                                              stdout='{"role":"tool","content":"x"}\n', stderr="")):
            run = claude_runner.run_kimi_headless("Q", "/tmp")
        self.assertFalse(run.ok)
        self.assertIn("no assistant message", run.error)


# ───────────────────────── K2: _kimi_seat_answer ────────────────────────────

class TestKimiSeatAnswer(unittest.TestCase):
    def _capture(self, run_result):
        captured = {}

        def fake(**kw):
            captured.update(kw)
            return run_result

        return captured, mock.patch.object(claude_runner, "run_kimi_json", side_effect=fake)

    def test_ok_returns_normalized_subscription_dict(self):
        cap, p = self._capture(ClaudeRun(ok=True, text="ANS", model="kimi-code/k3"))
        with p:
            ans = claude_runner._kimi_seat_answer({"model": "kimi-code/k3"}, "TASK", "/tmp")
        self.assertTrue(ans["ok"])
        self.assertEqual(ans["text"], "ANS")
        self.assertEqual(ans["model"], "kimi-code/k3")
        self.assertEqual(ans["cost"], 0.0)
        self.assertTrue(ans["subscription"])
        self.assertEqual(ans["effort"], "")            # kimi has no effort — empty for shape-parity
        self.assertEqual(ans["lens"], "")

    def test_model_defaults_when_absent(self):
        cap, p = self._capture(ClaudeRun(ok=True, text="A"))
        with p:
            claude_runner._kimi_seat_answer({}, "T", "/tmp")
        self.assertEqual(cap["model"], claude_runner.DEFAULT_KIMI_MODEL)

    def test_lens_applied_and_surfaced(self):
        cap, p = self._capture(ClaudeRun(ok=True, text="A", model="kimi-code/k3"))
        with p:
            ans = claude_runner._kimi_seat_answer(
                {"model": "kimi-code/k3", "lens": "risks", "lens_text": "FIND RISKS"},
                "TASK", "/tmp")
        self.assertEqual(cap["prompt"], claude_runner._apply_lens("TASK", "FIND RISKS"))
        self.assertIn("risks", cap["label"])
        self.assertEqual(ans["lens"], "risks")

    def test_fails_soft_carries_lens(self):
        cap, p = self._capture(ClaudeRun(ok=False, error="auth expired"))
        with p:
            ans = claude_runner._kimi_seat_answer(
                {"model": "kimi-code/k3", "lens": "risks", "lens_text": "X"}, "T", "/tmp")
        self.assertFalse(ans["ok"])
        self.assertIn("auth expired", ans["error"])
        self.assertEqual(ans["lens"], "risks")


# ─────────────── K2: run_fusion_json fans out kimi seats ────────────────────

class TestRunFusionJsonKimiSeat(unittest.TestCase):
    @contextlib.contextmanager
    def _env(self, *, claude_ok=False, codex_ok=False, kimi_ok=True, judge=None):
        cfg = {"preset": "budget", "timeout_s": 42, "providers": {}, "presets": {},
               "lenses": {"risks": "RISK-TEXT", "simplest": "SIMPLE-TEXT"}}
        with contextlib.ExitStack() as es:
            es.enter_context(mock.patch.object(claude_runner.config, "fusion_config", return_value=cfg))
            es.enter_context(mock.patch.object(claude_runner.config, "active_providers", return_value={}))
            es.enter_context(mock.patch.object(claude_runner.config, "claude_cli_available", return_value=claude_ok))
            es.enter_context(mock.patch.object(claude_runner.config, "codex_cli_available", return_value=codex_ok))
            es.enter_context(mock.patch.object(claude_runner.config, "kimi_cli_available", return_value=kimi_ok))
            ksa = es.enter_context(mock.patch.object(claude_runner, "_kimi_seat_answer", side_effect=_ok_seat))
            asa = es.enter_context(mock.patch.object(claude_runner, "_anthropic_seat_answer", side_effect=_ok_seat))
            # The judge is still claude — mock it or it spawns a real tab.
            rcj = es.enter_context(mock.patch.object(
                claude_runner, "run_claude_json",
                return_value=(judge or ClaudeRun(ok=True, text="SYNTH"))))
            yield ksa, rcj

    def test_pure_kimi_pair_clears_the_two_seat_gate(self):
        panel = [{"kind": "kimi_cli", "model": "kimi-code/k3", "lens": "risks"},
                 {"kind": "kimi_cli", "model": "kimi-code/k3", "lens": "simplest"}]
        with self._env() as (ksa, rcj):
            run = claude_runner.run_fusion_json("TASK", panel=panel)
        self.assertTrue(run.ok)
        self.assertEqual(ksa.call_count, 2)             # both kimi seats fanned out
        rcj.assert_called()                             # judge ran

    def test_kimi_seats_skipped_when_unavailable(self):
        panel = [{"kind": "kimi_cli", "model": "kimi-code/k3"},
                 {"kind": "kimi_cli", "model": "kimi-code/k3"}]
        with self._env(kimi_ok=False) as (ksa, rcj):
            run = claude_runner.run_fusion_json("TASK", panel=panel)
        self.assertFalse(run.ok)                         # <2 usable seats → fallback
        self.assertIn("need >=2 usable panel seats", run.error)
        ksa.assert_not_called()

    def test_mixed_claude_and_kimi_panel(self):
        panel = [{"kind": "claude_cli", "model": "opus", "effort": "high"},
                 {"kind": "kimi_cli", "model": "kimi-code/k3", "lens": "risks"}]
        with self._env(claude_ok=True) as (ksa, rcj):
            run = claude_runner.run_fusion_json("TASK", panel=panel)
        self.assertTrue(run.ok)
        self.assertEqual(ksa.call_count, 1)             # the one kimi seat ran


if __name__ == "__main__":
    unittest.main(verbosity=2)
