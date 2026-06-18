"""F2 tests — the rewriter routing its ONE brain call through Fusion.

NO NETWORK: the brain call is mocked. Verifies (F2.1) that fusion/panel/model/
effort/label are forwarded to run_brain_json and the result parses unchanged,
and (F2.2) that the auto-retry stays single-model (run_claude_json) and never
re-fans-out the panel.

Usage:
    python -m unittest tests.test_fusion_rewriter -v
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orchestrator.lib import claude_runner, rewriter
from orchestrator.lib.claude_runner import ClaudeRun


class TestRewriterFusionRouting(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_rw_fusion_"))
        (self.tmp / "README.md").write_text("# demo\n")   # a minimal project

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fusion_true_forwards_panel_and_tier_to_run_brain_json(self):
        fake = ClaudeRun(ok=True, text='{"rewritten_prompt":"R"}',
                         parsed_json={"rewritten_prompt": "R"}, cost_usd=0.01, model="opus")
        with mock.patch.object(claude_runner, "run_brain_json", return_value=fake) as rbj:
            r = rewriter.rewrite("task", str(self.tmp),
                                 fusion=True, panel=["gemini", "gemini2"])
        self.assertTrue(r.ok)
        self.assertEqual(r.rewritten_prompt, "R")
        self.assertAlmostEqual(r.cost_usd, 0.01)        # panel cost flows through
        kw = rbj.call_args.kwargs
        self.assertTrue(kw["fusion"])
        self.assertEqual(kw["panel"], ["gemini", "gemini2"])
        self.assertEqual(kw["model"], "opus")           # tier preserved
        self.assertEqual(kw["effort"], "high")
        self.assertEqual(kw["label"], "rewriter")

    def test_fusion_false_is_the_default(self):
        fake = ClaudeRun(ok=True, text='{"rewritten_prompt":"R"}',
                         parsed_json={"rewritten_prompt": "R"})
        with mock.patch.object(claude_runner, "run_brain_json", return_value=fake) as rbj:
            r = rewriter.rewrite("task", str(self.tmp))
        self.assertTrue(r.ok)
        self.assertFalse(rbj.call_args.kwargs["fusion"])
        self.assertIsNone(rbj.call_args.kwargs["panel"])

    def test_retry_is_single_model_and_never_refans_panel(self):
        # First (fusion) call returns prose → triggers the retry. The retry MUST
        # be a single run_claude_json, never run_brain_json/run_fusion_json again.
        bad = ClaudeRun(ok=True, text="sorry, here is prose not json", parsed_json=None)
        good_retry = ClaudeRun(ok=True, text='{"rewritten_prompt":"R2"}',
                               parsed_json={"rewritten_prompt": "R2"})
        with mock.patch.object(claude_runner, "run_brain_json", return_value=bad) as rbj, \
                mock.patch.object(claude_runner, "run_claude_json", return_value=good_retry) as rcj, \
                mock.patch.object(claude_runner, "run_fusion_json") as rfj:
            r = rewriter.rewrite("task", str(self.tmp),
                                 fusion=True, panel=["gemini", "gemini2"])
        self.assertTrue(r.ok)
        self.assertEqual(r.rewritten_prompt, "R2")
        rbj.assert_called_once()        # the panel ran exactly once (the first call)
        rcj.assert_called_once()        # the retry was single-model
        rfj.assert_not_called()         # ...and never re-fanned the panel

    def test_fusion_failure_still_returns_via_run_brain_json_fallback(self):
        # run_brain_json already degrades internally; the rewriter just trusts its
        # result. Here it returns a good single-model answer (the fallback).
        fallback = ClaudeRun(ok=True, text='{"rewritten_prompt":"local"}',
                             parsed_json={"rewritten_prompt": "local"})
        with mock.patch.object(claude_runner, "run_brain_json", return_value=fallback):
            r = rewriter.rewrite("task", str(self.tmp), fusion=True)
        self.assertTrue(r.ok)
        self.assertEqual(r.rewritten_prompt, "local")


if __name__ == "__main__":
    unittest.main(verbosity=2)
