"""F7 tests — multi-model ENRICHMENT mode.

Covers:
  F7.1  fusion.enrich(): happy path (panel→judge→analysis→rendered block), input
        cap, and EVERY failure mode degrading to ok=False without raising;
        render_block() dropping empty sections.
  F7.2  _send_in_background's enrich path: the block is APPENDED to the executor
        prompt; an enrich FAILURE never aborts the dispatch (records
        fusion_skipped + dispatches the un-enriched prompt).

NO NETWORK: run_fusion_json (F7.1) and enrich + db + _run_dispatch (F7.2) are
mocked.

Usage:
    python -m unittest tests.test_fusion_enrich -v
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orchestrator.lib import claude_runner, fusion
from orchestrator.lib.claude_runner import ClaudeRun
from orchestrator.lib.fusion import FusionResult
from orchestrator import app as app_module


ANALYSIS = {"consensus": ["use WAL"], "contradictions": ["x vs y"],
            "partial_coverage": [], "unique_insights": ["z"], "blind_spots": ["watch fsync"]}


def _judge(text, panel=None):
    return ClaudeRun(ok=True, text=text, cost_usd=0.0012, model="opus",
                     raw={"panel": panel if panel is not None else
                          [{"name": "gemini-lite", "ok": True},
                           {"name": "glm", "ok": True}],
                          "preset": "hybrid", "seats": ["gemini-lite", "glm"]})


# ───────────────────────────── F7.1: enrich() ──────────────────────────────

class TestEnrich(unittest.TestCase):
    def test_happy_path_builds_block_and_models(self):
        with mock.patch.object(claude_runner, "run_fusion_json",
                               return_value=_judge(json.dumps(ANALYSIS))) as rfj:
            res = fusion.enrich("do a thing", "/proj", panel=["gemini-lite", "glm"])
        self.assertTrue(res.ok)
        self.assertEqual(res.analysis["consensus"], ["use WAL"])
        self.assertIn("## Multi-model analysis", res.enrichment_md)
        self.assertIn("- use WAL", res.enrichment_md)
        self.assertEqual(res.panel_models, ["gemini-lite", "glm"])
        self.assertAlmostEqual(res.cost_usd, 0.0012, places=6)
        # panel/preset forwarded to run_fusion_json
        self.assertEqual(rfj.call_args.kwargs["panel"], ["gemini-lite", "glm"])

    def test_strips_fences_around_judge_json(self):
        fenced = "Here is the analysis:\n```json\n" + json.dumps(ANALYSIS) + "\n```"
        with mock.patch.object(claude_runner, "run_fusion_json", return_value=_judge(fenced)):
            res = fusion.enrich("t", "/proj")
        self.assertTrue(res.ok)
        self.assertEqual(res.analysis["blind_spots"], ["watch fsync"])

    def test_input_is_capped(self):
        big = "x" * 50_000
        with mock.patch.object(claude_runner, "run_fusion_json",
                               return_value=_judge(json.dumps(ANALYSIS))) as rfj:
            fusion.enrich(big, "/proj")
        sent = rfj.call_args.kwargs["prompt"]
        # The contiguous task slice fed to the panel is bounded by MAX_INPUT_CHARS
        # (the template adds a couple of stray 'x's in prose, so count the run).
        self.assertIn("x" * fusion.MAX_INPUT_CHARS, sent)
        self.assertNotIn("x" * (fusion.MAX_INPUT_CHARS + 1), sent)

    def test_panel_unavailable_returns_not_ok(self):
        with mock.patch.object(claude_runner, "run_fusion_json",
                               return_value=ClaudeRun(ok=False, error="need >=2 seats")):
            res = fusion.enrich("t", "/proj")
        self.assertFalse(res.ok)
        self.assertIn(">=2", res.error)

    def test_unparseable_judge_returns_not_ok(self):
        with mock.patch.object(claude_runner, "run_fusion_json",
                               return_value=_judge("not json at all")):
            res = fusion.enrich("t", "/proj")
        self.assertFalse(res.ok)
        self.assertIn("unparseable", res.error)

    def test_empty_analysis_returns_not_ok(self):
        empty = {k: [] for k in fusion.ANALYSIS_KEYS}
        with mock.patch.object(claude_runner, "run_fusion_json",
                               return_value=_judge(json.dumps(empty))):
            res = fusion.enrich("t", "/proj")
        self.assertFalse(res.ok)

    def test_empty_prompt_returns_not_ok_without_calling_panel(self):
        with mock.patch.object(claude_runner, "run_fusion_json") as rfj:
            res = fusion.enrich("   ", "/proj")
        self.assertFalse(res.ok)
        rfj.assert_not_called()

    def test_never_raises_on_crash(self):
        with mock.patch.object(claude_runner, "run_fusion_json",
                               side_effect=RuntimeError("boom")):
            res = fusion.enrich("t", "/proj")
        self.assertFalse(res.ok)
        self.assertIn("crashed", res.error)


class TestRenderBlock(unittest.TestCase):
    def test_drops_empty_sections(self):
        md = fusion.render_block({"consensus": ["a"], "contradictions": [],
                                  "partial_coverage": [], "unique_insights": [],
                                  "blind_spots": ["b"]})
        self.assertIn("Consensus", md)
        self.assertIn("Blind spots", md)
        self.assertNotIn("Contradictions", md)     # empty → dropped

    def test_coerce_list_normalizes(self):
        self.assertEqual(fusion._coerce_list("solo"), ["solo"])
        self.assertEqual(fusion._coerce_list(["a", "", "b", None, 3]), ["a", "b", "3"])
        self.assertEqual(fusion._coerce_list(None), [])


# ──────────────── F7.2: _send_in_background enrich wiring ───────────────────

class TestSendBackgroundEnrich(unittest.IsolatedAsyncioTestCase):
    def _patches(self, enrich_result):
        """Patch db + _run_dispatch + fusion.enrich; capture the task that
        reaches _run_dispatch and the recorded events."""
        recorded = []
        run_dispatch = mock.AsyncMock(return_value=(123, ""))
        es = [
            mock.patch.object(app_module.db, "get_project",
                              return_value={"id": 1, "path": "/proj"}),
            mock.patch.object(app_module, "_run_dispatch", run_dispatch),
            mock.patch.object(app_module.db, "set_dispatch_cost"),
            mock.patch.object(app_module.db, "record_event",
                              side_effect=lambda did, kind, payload: recorded.append(payload)),
            mock.patch.object(app_module.attachments_mod, "list_files", return_value=[]),
            mock.patch.object(app_module.fusion_mod, "enrich", return_value=enrich_result),
        ]
        return es, run_dispatch, recorded

    async def test_block_appended_and_event_recorded(self):
        good = FusionResult(ok=True, analysis=ANALYSIS,
                            enrichment_md="## Multi-model analysis\n\n- use WAL",
                            panel_models=["gemini-lite", "glm"], cost_usd=0.0012)
        es, run_dispatch, recorded = self._patches(good)
        with es[0], es[1], es[2], es[3], es[4], es[5]:
            await app_module._send_in_background(
                1, "do a thing", 600, do_rewrite=False, do_enrich=True,
                panel=["gemini-lite", "glm"])
        sent_task = run_dispatch.call_args.args[1]
        self.assertIn("## Multi-model analysis", sent_task)
        self.assertTrue(sent_task.startswith("do a thing"))
        stages = [p.get("stage") for p in recorded]
        self.assertIn("fusion_ok", stages)

    async def test_enrich_failure_does_not_abort_dispatch(self):
        bad = FusionResult(ok=False, error="panel unavailable")
        es, run_dispatch, recorded = self._patches(bad)
        with es[0], es[1], es[2], es[3], es[4], es[5]:
            await app_module._send_in_background(
                1, "do a thing", 600, do_rewrite=False, do_enrich=True, panel=[])
        # dispatched anyway, with the ORIGINAL (un-enriched) prompt
        run_dispatch.assert_awaited_once()
        sent_task = run_dispatch.call_args.args[1]
        self.assertEqual(sent_task, "do a thing")
        self.assertIn("fusion_skipped", [p.get("stage") for p in recorded])


if __name__ == "__main__":
    unittest.main(verbosity=2)
