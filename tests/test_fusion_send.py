"""F3 tests — the /send → _send_in_background pipeline threading the Fusion
flag + validated panel through to the rewriter.

NO NETWORK, NO API KEYS: db.get_project, config.active_providers, the rewriter,
and _run_dispatch are all mocked, so this exercises ONLY the server-side
plumbing (F3.1 parse/validate in /send, F3.2 forward into rewriter.rewrite +
record on the rewrite_ok event). The /send endpoint is driven through a real
Starlette TestClient (offline — no lifespan, no iTerm2, no real DB).

Verifies:
  F3.1  POST fusion=true&fusion_panel=deepseek,minimax,glm → _send_in_background
        gets do_fusion=True and panel=["deepseek","minimax"] (unkeyed names
        silently dropped against config.active_providers()).
  F3.1  the fusion=false / no-fusion POST is byte-for-byte as today
        (do_fusion=False, panel=[], response body unchanged).
  F3.2  _send_in_background forwards fusion/panel positionally to
        rewriter.rewrite and stamps them onto the rewrite_ok stage event;
        fusion=false forwards fusion=False (the original single-claude path).

Usage:
    python3 -m unittest tests.test_fusion_send -v
    python3 tests/test_fusion_send.py
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from starlette.testclient import TestClient

from orchestrator import app as app_module
from orchestrator.lib.rewriter import RewriteResult

# Two keyed/enabled providers — the membership set /send validates the panel
# against. Values are the merged-registry shape active_providers() returns;
# only the keys (names) matter to the F3.1 drop logic.
ACTIVE = {
    "deepseek": {"model": "deepseek-chat", "price_in": 0.44, "price_out": 0.87},
    "minimax": {"model": "MiniMax-Text-01", "price_in": 0.30, "price_out": 1.20},
}


# ───────────────────────── F3.1: /send parse + thread ──────────────────────

class TestSendEndpointFusionParsing(unittest.TestCase):
    """Drive the real /send endpoint; assert what reaches _send_in_background.

    No `with TestClient(...)` context manager → the app lifespan never runs, so
    this touches no real DB / iTerm2 / embeddings. _send_in_background is an
    AsyncMock: asyncio.create_task gets a real coroutine, and the call args are
    recorded synchronously as the handler builds that coroutine."""

    def _post(self, data):
        """POST /send with db.get_project + active_providers + the background
        worker mocked. Returns (response, send_in_background_mock)."""
        with mock.patch.object(app_module.db, "get_project",
                               return_value={"id": 1, "path": str(REPO)}), \
                mock.patch.object(app_module.config, "active_providers",
                                  return_value=ACTIVE), \
                mock.patch.object(app_module, "_send_in_background",
                                  new_callable=mock.AsyncMock) as sib:
            client = TestClient(app_module.app)
            resp = client.post("/send", data=data)
        return resp, sib

    def test_fusion_true_threads_flag_and_validated_panel(self):
        resp, sib = self._post({
            "project_id": 1, "task": "design the thing",
            "fusion": "true", "fusion_panel": "deepseek,minimax,glm",
        })
        self.assertEqual(resp.status_code, 200)
        sib.assert_called_once()
        kw = sib.call_args.kwargs
        self.assertTrue(kw["do_fusion"])
        # "glm" has no active key → dropped; order preserved from the request.
        self.assertEqual(kw["panel"], ["deepseek", "minimax"])

    def test_unkeyed_only_panel_collapses_to_empty(self):
        # Every requested name is unkeyed → panel empties out (run_fusion_json
        # then falls back to the configured preset; nothing is forced).
        _, sib = self._post({
            "project_id": 1, "task": "t",
            "fusion": "on", "fusion_panel": "glm,qwen,nonsense",
        })
        kw = sib.call_args.kwargs
        self.assertTrue(kw["do_fusion"])
        self.assertEqual(kw["panel"], [])

    def test_fusion_false_is_byte_for_byte_unchanged(self):
        # No fusion fields at all: defaults must give do_fusion=False, panel=[],
        # and the response body must be exactly what it was before F3.
        resp, sib = self._post({"project_id": 1, "task": "t"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True, "rewrite": False})
        kw = sib.call_args.kwargs
        self.assertFalse(kw["do_fusion"])
        self.assertEqual(kw["panel"], [])

    def test_fusion_panel_without_flag_does_not_enable_fusion(self):
        # A panel can ride along, but do_fusion is governed solely by `fusion`.
        _, sib = self._post({
            "project_id": 1, "task": "t",
            "fusion": "false", "fusion_panel": "deepseek,minimax",
        })
        kw = sib.call_args.kwargs
        self.assertFalse(kw["do_fusion"])
        # Names still validated/echoed (harmless — inert while fusion is off).
        self.assertEqual(kw["panel"], ["deepseek", "minimax"])


# ─────────────────── F3.2: _send_in_background forwarding ───────────────────

class TestSendInBackgroundForwarding(unittest.IsolatedAsyncioTestCase):
    """Call _send_in_background directly; assert fusion/panel reach
    rewriter.rewrite (positionally, through run_in_executor) and land on the
    rewrite_ok event. The rewriter, _run_dispatch, attachments, and db are
    mocked, so no brain call / iTerm2 / DB is touched."""

    def _patches(self, rewrite_result, dispatch=(1, "")):
        """ExitStack of the seams _send_in_background touches. Yields
        (rewrite_mock, run_dispatch_mock, record_event_mock)."""
        import contextlib
        es = contextlib.ExitStack()
        es.enter_context(mock.patch.object(
            app_module.db, "get_project", return_value={"id": 1, "path": "/tmp/proj"}))
        es.enter_context(mock.patch.object(
            app_module.attachments_mod, "list_files", return_value=[]))
        rw = es.enter_context(mock.patch.object(
            app_module.rewriter, "rewrite", return_value=rewrite_result))
        rd = es.enter_context(mock.patch.object(
            app_module, "_run_dispatch", new_callable=mock.AsyncMock))
        rd.return_value = dispatch
        rec = es.enter_context(mock.patch.object(app_module.db, "record_event"))
        return es, rw, rd, rec

    @staticmethod
    def _ok_result():
        return RewriteResult(ok=True, rewritten_prompt="REWRITTEN",
                             cost_usd=0.01, duration_s=2.0, model="opus",
                             bundle_chars=123)

    @staticmethod
    def _rewrite_ok_event(rec_mock):
        """Pull the rewrite_ok stage event out of record_event's calls."""
        for c in rec_mock.call_args_list:
            if len(c.args) >= 3 and c.args[1] == "stage" \
                    and isinstance(c.args[2], dict) \
                    and c.args[2].get("stage") == "rewrite_ok":
                return c.args[2]
        return None

    async def test_fusion_on_forwards_panel_and_stamps_event(self):
        es, rw, rd, rec = self._patches(self._ok_result())
        with es:
            await app_module._send_in_background(
                1, "do the thing", 600, True, "max", "",
                do_fusion=True, panel=["deepseek", "minimax"])
        # rewriter.rewrite got (task, path, fusion, panel) positionally.
        rw.assert_called_once()
        args = rw.call_args.args
        self.assertIs(args[2], True)                       # fusion
        self.assertEqual(args[3], ["deepseek", "minimax"])  # panel
        # The rewritten prompt was dispatched.
        rd.assert_awaited_once()
        self.assertEqual(rd.call_args.args[1], "REWRITTEN")
        # rewrite_ok event carries fusion + panel.
        ev = self._rewrite_ok_event(rec)
        self.assertIsNotNone(ev)
        self.assertIs(ev["fusion"], True)
        self.assertEqual(ev["panel"], ["deepseek", "minimax"])

    async def test_fusion_off_forwards_false_byte_for_byte(self):
        es, rw, rd, rec = self._patches(self._ok_result())
        with es:
            await app_module._send_in_background(
                1, "do the thing", 600, True, "max", "",
                do_fusion=False, panel=[])
        args = rw.call_args.args
        self.assertIs(args[2], False)     # fusion off → original single-claude path
        self.assertEqual(args[3], [])
        rd.assert_awaited_once()          # still dispatches normally
        ev = self._rewrite_ok_event(rec)
        self.assertIs(ev["fusion"], False)

    async def test_defaults_are_backward_compatible(self):
        # Pre-F3 callers passed neither do_fusion nor panel — the new defaults
        # (False / None) must reproduce the old rewriter.rewrite(task, path) call.
        es, rw, rd, _ = self._patches(self._ok_result())
        with es:
            await app_module._send_in_background(1, "t", 600, True)
        args = rw.call_args.args
        self.assertIs(args[2], False)
        self.assertIsNone(args[3])
        rd.assert_awaited_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
