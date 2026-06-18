"""F6 tests — summarizer + onboarding routing their ONE brain call through
run_brain_json so they can (optionally) use a Fusion panel.

NO NETWORK: run_brain_json is mocked. Verifies the drop-in is faithful:
  - fusion=False forwards fusion=False at the DELIBERATE low tier (sonnet/medium)
    — byte-for-byte the original single-claude behavior;
  - fusion=True forwards the flag + panel + a sonnet/medium judge (these calls
    must NOT silently escalate to the Opus judge the rewriter uses).

Usage:
    python -m unittest tests.test_fusion_brain_calls -v
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orchestrator.lib import claude_runner, summarizer, onboarding
from orchestrator.lib.claude_runner import ClaudeRun


class TestSummarizerFusionRouting(unittest.TestCase):
    SUMMARY_JSON = {"summary_md": "did the thing", "what_worked": "w",
                    "what_broke": "b", "lessons": "l", "tags": ["t"]}

    def _run(self, **kw):
        fake = ClaudeRun(ok=True, text="{}", parsed_json=self.SUMMARY_JSON,
                         cost_usd=0.0, model="sonnet")
        with mock.patch.object(summarizer, "distill_transcript", return_value="TRANSCRIPT"), \
                mock.patch.object(claude_runner, "run_brain_json", return_value=fake) as rbj:
            res = summarizer.summarize("/tmp/t.jsonl", "the task", "/tmp", **kw)
        return res, rbj

    def test_fusion_off_is_low_tier_single_claude(self):
        res, rbj = self._run()
        self.assertTrue(res.ok)
        self.assertEqual(res.summary_md, "did the thing")
        kw = rbj.call_args.kwargs
        self.assertFalse(kw["fusion"])
        self.assertEqual(kw["model"], "sonnet")        # deliberate low tier preserved
        self.assertEqual(kw["effort"], "medium")
        self.assertEqual(kw["label"], "summarizer")

    def test_fusion_on_forwards_panel_and_sonnet_judge(self):
        res, rbj = self._run(fusion=True, panel=["gemini-lite", "glm"])
        self.assertTrue(res.ok)
        kw = rbj.call_args.kwargs
        self.assertTrue(kw["fusion"])
        self.assertEqual(kw["panel"], ["gemini-lite", "glm"])
        # The judge stays sonnet/medium — a summary must not escalate to Opus.
        self.assertEqual(kw["judge_model"], "sonnet")
        self.assertEqual(kw["judge_effort"], "medium")


class TestOnboardingFusionRouting(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_onb_fusion_"))
        (self.tmp / "README.md").write_text("# demo project\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, **kw):
        fake = ClaudeRun(ok=True, text="{}",
                         parsed_json={"project_summary": "s", "recommendations": [], "edits": []},
                         cost_usd=0.0, model="sonnet")
        with mock.patch.object(claude_runner, "run_brain_json", return_value=fake) as rbj:
            res = onboarding.analyze(str(self.tmp), **kw)
        return res, rbj

    def test_fusion_off_is_low_tier_single_claude(self):
        res, rbj = self._run()
        self.assertTrue(res.ok)
        kw = rbj.call_args.kwargs
        self.assertFalse(kw["fusion"])
        self.assertEqual(kw["model"], "sonnet")
        self.assertEqual(kw["effort"], "medium")
        self.assertEqual(kw["label"], "onboarding")

    def test_fusion_on_forwards_panel_and_sonnet_judge(self):
        res, rbj = self._run(fusion=True, panel=["gemini-lite", "glm"])
        self.assertTrue(res.ok)
        kw = rbj.call_args.kwargs
        self.assertTrue(kw["fusion"])
        self.assertEqual(kw["panel"], ["gemini-lite", "glm"])
        self.assertEqual(kw["judge_model"], "sonnet")
        self.assertEqual(kw["judge_effort"], "medium")


if __name__ == "__main__":
    unittest.main(verbosity=2)
