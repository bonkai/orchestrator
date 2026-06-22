"""Offline unit tests for the F11.c.1 VERIFIER seat in run_fusion_json — the
opt-in critic that checks the fusion judge's synthesis and, on a found defect,
triggers ONE re-judge. NO NETWORK: the panel (_panel_answers) and every claude
call (run_claude_json) are mocked, so this exercises the verify→re-judge wiring,
the fail-safes, and the cost invariant without an API call.

Usage:
    python -m unittest tests.test_fusion_verify -v
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
    """A ClaudeRun stand-in for a mocked run_claude_json return."""
    return ClaudeRun(ok=ok, text=text, parsed_json=parsed, model="opus")


class TestVerifyPrompts(unittest.TestCase):
    def test_verify_prompt_has_task_synthesis_panel_and_json(self):
        answers = [{"name": "gemini", "model": "g", "text": "PANELTEXT"}]
        vp = claude_runner._verify_prompt("ORIGTASK", "SYNTHTEXT", answers)
        self.assertIn("ORIGTASK", vp)
        self.assertIn("SYNTHTEXT", vp)
        self.assertIn("PANELTEXT", vp)
        self.assertIn('"defect"', vp)

    def test_rejudge_prompt_has_orig_prior_issues_and_format(self):
        answers = [{"name": "gemini", "model": "g", "text": "PANELTEXT"}]
        rp = claude_runner._rejudge_prompt("ORIGTASK", answers, "PRIORSYNTH",
                                           ["ISSUE-ALPHA"])
        self.assertIn("ORIGTASK", rp)          # original prompt verbatim
        self.assertIn("PRIORSYNTH", rp)
        self.assertIn("ISSUE-ALPHA", rp)
        self.assertIn("exact same format", rp.lower())


class TestRunFusionVerify(unittest.TestCase):
    PROVIDERS = {"gemini": dict(PROV), "gemini2": dict(PROV)}
    PRESETS = {"budget": ["gemini", "gemini2"]}
    ACTIVE = {"gemini": dict(PROV), "gemini2": dict(PROV)}
    PANEL = [{"name": "gemini", "text": "A", "cost": 0.001, "ok": True},
             {"name": "gemini2", "text": "B", "cost": 0.002, "ok": True}]

    @contextlib.contextmanager
    def _env(self, verify_cfg=False):
        cfg = {"preset": "budget", "timeout_s": 42, "verify": verify_cfg,
               "providers": self.PROVIDERS, "presets": self.PRESETS}
        with contextlib.ExitStack() as es:
            es.enter_context(mock.patch.object(
                claude_runner.config, "fusion_config", return_value=cfg))
            es.enter_context(mock.patch.object(
                claude_runner.config, "active_providers", return_value=self.ACTIVE))
            es.enter_context(mock.patch.object(
                claude_runner.spawn, "ensure_fusion_providers"))
            rp = es.enter_context(mock.patch.object(claude_runner, "_panel_answers"))
            rp.return_value = [dict(a) for a in self.PANEL]   # fresh dicts per test
            rcj = es.enter_context(mock.patch.object(claude_runner, "run_claude_json"))
            yield rp, rcj

    def test_verify_off_runs_judge_only(self):
        # verify defaults off (cfg verify False, no param): one claude call (judge),
        # no verifier block, synthesis untouched, cost = Σ panel.
        with self._env(verify_cfg=False) as (rp, rcj):
            rcj.return_value = _cr("SYNTH")
            run = claude_runner.run_fusion_json("q", cwd="/tmp")
        self.assertTrue(run.ok)
        self.assertEqual(run.text, "SYNTH")
        self.assertEqual(rcj.call_count, 1)
        self.assertNotIn("verifier", run.raw)
        self.assertAlmostEqual(run.cost_usd, 0.003, places=9)

    def test_verify_on_clean_keeps_synthesis(self):
        # verdict defect=false → verifier ran, no re-judge (2 calls), synthesis kept.
        with self._env() as (rp, rcj):
            rcj.side_effect = [_cr("SYNTH"),
                               _cr('{"defect": false, "issues": []}',
                                   parsed={"defect": False, "issues": []})]
            run = claude_runner.run_fusion_json("q", cwd="/tmp", verify=True)
        self.assertEqual(run.text, "SYNTH")
        self.assertEqual(rcj.call_count, 2)
        self.assertTrue(run.raw["verifier"]["ran"])
        self.assertFalse(run.raw["verifier"]["defect"])
        self.assertFalse(run.raw["verifier"]["rejudged"])
        self.assertAlmostEqual(run.cost_usd, 0.003, places=9)

    def test_verify_on_defect_triggers_one_rejudge(self):
        with self._env() as (rp, rcj):
            rcj.side_effect = [_cr("SYNTH"),
                               _cr('{"defect": true, "issues": ["X"]}',
                                   parsed={"defect": True, "issues": ["X"]}),
                               _cr("FIXED")]
            run = claude_runner.run_fusion_json("q", cwd="/tmp", verify=True)
        self.assertEqual(run.text, "FIXED")            # corrected synthesis returned
        self.assertEqual(rcj.call_count, 3)
        self.assertTrue(run.raw["verifier"]["defect"])
        self.assertTrue(run.raw["verifier"]["rejudged"])
        self.assertEqual(run.raw["verifier"]["issues"], ["X"])
        self.assertAlmostEqual(run.cost_usd, 0.003, places=9)   # re-judge is $0

    def test_verify_via_config_default(self):
        # No verify param, but cfg verify True → verifier runs.
        with self._env(verify_cfg=True) as (rp, rcj):
            rcj.side_effect = [_cr("SYNTH"),
                               _cr('{"defect": false}', parsed={"defect": False})]
            run = claude_runner.run_fusion_json("q", cwd="/tmp")
        self.assertEqual(rcj.call_count, 2)
        self.assertTrue(run.raw["verifier"]["ran"])

    def test_verify_param_false_overrides_config_on(self):
        # cfg verify True but explicit verify=False → off (Optional[bool] override).
        with self._env(verify_cfg=True) as (rp, rcj):
            rcj.return_value = _cr("SYNTH")
            run = claude_runner.run_fusion_json("q", cwd="/tmp", verify=False)
        self.assertEqual(rcj.call_count, 1)
        self.assertNotIn("verifier", run.raw)

    def test_verifier_call_failure_is_failsafe(self):
        # verifier claude call fails → keep synthesis, no re-judge (2 calls).
        with self._env() as (rp, rcj):
            rcj.side_effect = [_cr("SYNTH"), _cr("", ok=False)]
            run = claude_runner.run_fusion_json("q", cwd="/tmp", verify=True)
        self.assertEqual(run.text, "SYNTH")
        self.assertEqual(rcj.call_count, 2)
        self.assertFalse(run.raw["verifier"]["defect"])
        self.assertFalse(run.raw["verifier"]["rejudged"])

    def test_unparseable_verdict_treated_as_no_defect(self):
        with self._env() as (rp, rcj):
            rcj.side_effect = [_cr("SYNTH"), _cr("not json at all", parsed=None)]
            run = claude_runner.run_fusion_json("q", cwd="/tmp", verify=True)
        self.assertEqual(run.text, "SYNTH")
        self.assertEqual(rcj.call_count, 2)
        self.assertFalse(run.raw["verifier"]["rejudged"])

    def test_rejudge_failure_keeps_original_synthesis(self):
        # defect found, but the re-judge call fails → keep the original synthesis.
        with self._env() as (rp, rcj):
            rcj.side_effect = [_cr("SYNTH"),
                               _cr('{"defect": true, "issues": ["X"]}',
                                   parsed={"defect": True, "issues": ["X"]}),
                               _cr("", ok=False)]
            run = claude_runner.run_fusion_json("q", cwd="/tmp", verify=True)
        self.assertEqual(run.text, "SYNTH")
        self.assertEqual(rcj.call_count, 3)
        self.assertTrue(run.raw["verifier"]["defect"])
        self.assertFalse(run.raw["verifier"]["rejudged"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
