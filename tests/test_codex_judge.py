"""C3 tests — the SELECTABLE judge engine in run_fusion_json (sub-tasks C3.1–C3.2).

Fully OFFLINE — no real `claude`/`codex`, no iTerm2, no network. BOTH engine
entrypoints are mocked at the module level (run_claude_json AND run_codex_json),
so the judge / verifier / re-judge route through a MagicMock, never a real tab.

  C3.1  `judge_engine: str = "claude"` on run_fusion_json; the judge routes through
        the selected engine. Default "claude" keeps today's behavior byte-for-byte.
  C3.2  the verifier AND re-judge route through the SAME engine selection — with
        judge_engine="codex", NO run_claude_json call remains in the judge/verify/
        rejudge path (and the default path makes ZERO run_codex_json calls).

Two C3 guards beyond pure routing:
  - the codex path must NOT forward the 'opus' judge/verify default to `codex -m`
    (dispatch #3 'no silent downgrade') — it resolves DEFAULT_CODEX_MODEL.
  - the call passes ONLY the kwargs BOTH engines accept (prompt/cwd/model/effort/
    label); run_codex_json has NO max_turns param, so a stray one would TypeError
    in production even though a MagicMock would silently swallow it.

CRITICAL (mirrors test_codex_seat.py's hazard): run_fusion_json must resolve the
engine callable from the module namespace at CALL TIME (an in-function map), NOT a
module-level dict literal — else mock.patch.object here would not reach it and
these tests (and the existing ones) would fire REAL tabs. The codex JUDGE is
independent of codex SEAT availability, so these tests use a pure-PROVIDER panel
(mocked _panel_answers) and vary only the judge engine.

Usage:
    python -m unittest tests.test_codex_judge -v
"""

import contextlib
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orchestrator.lib import claude_runner
from orchestrator.lib.claude_runner import ClaudeRun

PROV = {"script": "providers/gemini.py", "model": "gemini-2.5-flash",
        "price_in": 0.30, "price_out": 1.50}


def _cr(text, parsed=None, ok=True):
    """A ClaudeRun stand-in for a mocked engine return (claude OR codex)."""
    return ClaudeRun(ok=ok, text=text, parsed_json=parsed, model="opus")


# A judge that returns SYNTH, then a verifier that flags a defect, then a re-judge
# that returns FIXED — drives ALL THREE judge-path sites so each is exercised.
def _judge_verify_rejudge_sequence():
    return [_cr("SYNTH"),
            _cr('{"defect": true, "issues": ["X"]}',
                parsed={"defect": True, "issues": ["X"]}),
            _cr("FIXED")]


class TestRunFusionJsonJudgeEngine(unittest.TestCase):
    """The judge engine is selectable and independent of the panel seats: a
    pure-PROVIDER panel (mocked) is synthesized by either a claude or a codex
    judge. BOTH engine entrypoints are mocked, so all three judge-path calls hit a
    MagicMock — the assertions are about WHICH mock, with WHAT kwargs, how often."""

    PROVIDERS = {"gemini": dict(PROV), "gemini2": dict(PROV)}
    PRESETS = {"budget": ["gemini", "gemini2"]}
    ACTIVE = {"gemini": dict(PROV), "gemini2": dict(PROV)}
    PANEL = [{"name": "gemini", "text": "A", "cost": 0.001, "ok": True},
             {"name": "gemini2", "text": "B", "cost": 0.002, "ok": True}]

    @contextlib.contextmanager
    def _env(self):
        cfg = {"preset": "budget", "timeout_s": 42, "verify": False,
               "providers": self.PROVIDERS, "presets": self.PRESETS}
        with contextlib.ExitStack() as es:
            es.enter_context(mock.patch.object(
                claude_runner.config, "fusion_config", return_value=cfg))
            es.enter_context(mock.patch.object(
                claude_runner.config, "active_providers", return_value=self.ACTIVE))
            # Pure-provider panel, so codex_cli_available() is irrelevant to seat
            # selection; mock it False to keep the run fully offline (no real probe).
            es.enter_context(mock.patch.object(
                claude_runner.config, "codex_cli_available", return_value=False))
            es.enter_context(mock.patch.object(
                claude_runner.spawn, "ensure_fusion_providers"))
            rp = es.enter_context(mock.patch.object(claude_runner, "_panel_answers"))
            rp.return_value = [dict(a) for a in self.PANEL]   # fresh dicts per test
            # BOTH engines mocked — the judge/verify/rejudge must never spawn a tab.
            # If run_fusion_json built a MODULE-LEVEL engine map, these patches would
            # not reach it and a real tab would spawn — so this also guards the hazard.
            rcj = es.enter_context(mock.patch.object(claude_runner, "run_claude_json"))
            rcx = es.enter_context(mock.patch.object(claude_runner, "run_codex_json"))
            yield rp, rcj, rcx

    # ── C3.1 + C3.2: default engine == claude, byte-for-byte today ──────────

    def test_default_engine_routes_all_three_through_claude(self):
        # verify=True + a defect verdict so the verifier AND re-judge both fire —
        # otherwise two of the three sites would stay untested.
        with self._env() as (rp, rcj, rcx):
            rcj.side_effect = _judge_verify_rejudge_sequence()
            run = claude_runner.run_fusion_json("q", cwd="/tmp", verify=True)
        self.assertTrue(run.ok)
        self.assertEqual(run.text, "FIXED")               # re-judge applied
        self.assertEqual(rcj.call_count, 3)               # judge + verify + rejudge
        rcx.assert_not_called()                           # ZERO codex calls, default path
        labels = [c.kwargs.get("label") for c in rcj.call_args_list]
        self.assertEqual(labels, ["fusion-judge", "fusion-verify", "fusion-rejudge"])

    def test_default_engine_judge_keeps_opus_model(self):
        # Byte-for-byte: the claude judge still gets the opus default (no remap).
        with self._env() as (rp, rcj, rcx):
            rcj.return_value = _cr("SYNTH")
            run = claude_runner.run_fusion_json("q", cwd="/tmp")    # verify off
        self.assertTrue(run.ok)
        self.assertEqual(rcj.call_count, 1)
        self.assertEqual(rcj.call_args.kwargs.get("model"), "opus")
        self.assertEqual(rcj.call_args.kwargs.get("label"), "fusion-judge")
        rcx.assert_not_called()

    # ── C3.2: judge_engine="codex" routes all three through codex ───────────

    def test_codex_engine_routes_all_three_through_codex(self):
        with self._env() as (rp, rcj, rcx):
            rcx.side_effect = _judge_verify_rejudge_sequence()
            run = claude_runner.run_fusion_json("q", cwd="/tmp", verify=True,
                                                judge_engine="codex")
        self.assertTrue(run.ok)
        self.assertEqual(run.text, "FIXED")
        self.assertEqual(rcx.call_count, 3)               # judge + verify + rejudge
        rcj.assert_not_called()                           # ACCEPTANCE: zero claude in the path
        labels = [c.kwargs.get("label") for c in rcx.call_args_list]
        self.assertEqual(labels, ["fusion-judge", "fusion-verify", "fusion-rejudge"])

    def test_codex_engine_substitutes_codex_model_not_a_claude_id(self):
        # dispatch #3 'no silent downgrade': the codex judge/verify/rejudge must NOT
        # be handed the 'opus' default — all three get DEFAULT_CODEX_MODEL. (The
        # mock accepts any model, so without this assertion the trap passes CI yet
        # would feed a claude id to `codex -m` in production.)
        with self._env() as (rp, rcj, rcx):
            rcx.side_effect = _judge_verify_rejudge_sequence()
            claude_runner.run_fusion_json("q", cwd="/tmp", verify=True,
                                          judge_engine="codex")
        self.assertEqual(rcx.call_count, 3)
        for call in rcx.call_args_list:
            self.assertEqual(call.kwargs.get("model"), claude_runner.DEFAULT_CODEX_MODEL)
            self.assertNotEqual(call.kwargs.get("model"), "opus")
        rcj.assert_not_called()

    def test_codex_engine_passes_only_shared_kwargs_no_max_turns(self):
        # Signature divergence: run_codex_json has NO max_turns; the call must pass
        # exactly prompt/cwd/model/effort/label (a MagicMock would swallow a stray
        # max_turns, but the real run_codex_json would TypeError).
        with self._env() as (rp, rcj, rcx):
            rcx.return_value = _cr("SYNTH")
            claude_runner.run_fusion_json("q", cwd="/tmp", judge_engine="codex")
        self.assertEqual(rcx.call_count, 1)
        self.assertEqual(set(rcx.call_args.kwargs),
                         {"prompt", "cwd", "model", "effort", "label"})
        self.assertNotIn("max_turns", rcx.call_args.kwargs)
        rcj.assert_not_called()

    def test_unknown_engine_falls_back_to_claude(self):
        # Fail-safe (and the "never raises" contract): an unrecognized engine
        # resolves to claude — reversible, no surprise codex tab, no KeyError.
        with self._env() as (rp, rcj, rcx):
            rcj.return_value = _cr("SYNTH")
            run = claude_runner.run_fusion_json("q", cwd="/tmp", judge_engine="bogus")
        self.assertTrue(run.ok)
        self.assertEqual(rcj.call_count, 1)
        rcx.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
