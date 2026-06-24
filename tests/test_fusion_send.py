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

# starlette's TestClient needs httpx, which isn't in this no-extra-deps venv.
# Guard the import so the MODULE still loads (the async-forwarding tests below
# don't need TestClient) — only the endpoint class that uses it is skipped.
try:
    from starlette.testclient import TestClient
except Exception as _e:   # RuntimeError("...requires httpx...") or ImportError
    TestClient = None
    _TESTCLIENT_ERR = str(_e).splitlines()[0]
else:
    _TESTCLIENT_ERR = ""

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

@unittest.skipIf(TestClient is None,
                 "starlette TestClient requires httpx (absent in this no-deps venv)")
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


# ─────────────── "one knob": UI model/effort drives the judge ───────────────

class TestSendInBackgroundJudgeFromPicker(unittest.IsolatedAsyncioTestCase):
    """The dispatch's UI model/effort picker also steers the Fusion judge:
    _send_in_background passes judge_model/judge_effort positionally to
    rewriter.rewrite (args[4]/args[5]) and as kwargs to fusion_mod.enrich.

    Rules under test:
      - explicit model → judge uses it verbatim; effort flows through.
      - blank model ("default") → judge stays "opus" (no silent downgrade).
      - out-of-range effort → judge falls back to "high".
    Same mocking seams as TestSendInBackgroundForwarding."""

    def _patches(self, rewrite_result, dispatch=(1, "")):
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
        es.enter_context(mock.patch.object(app_module.db, "record_event"))
        en = es.enter_context(mock.patch.object(app_module.fusion_mod, "enrich"))
        return es, rw, en

    @staticmethod
    def _ok_result():
        return RewriteResult(ok=True, rewritten_prompt="REWRITTEN",
                             cost_usd=0.01, duration_s=2.0, model="opus",
                             bundle_chars=123)

    async def test_explicit_model_and_effort_reach_judge(self):
        es, rw, _ = self._patches(self._ok_result())
        with es:
            await app_module._send_in_background(
                1, "t", 600, True, "max", "sonnet",
                do_fusion=True, panel=["deepseek"])
        args = rw.call_args.args
        self.assertEqual(args[4], "sonnet")   # judge_model
        self.assertEqual(args[5], "max")      # judge_effort

    async def test_blank_model_keeps_judge_on_opus(self):
        es, rw, _ = self._patches(self._ok_result())
        with es:
            await app_module._send_in_background(
                1, "t", 600, True, "high", "",     # model="" → "default"
                do_fusion=True, panel=["deepseek"])
        args = rw.call_args.args
        self.assertEqual(args[4], "opus")     # no silent downgrade to sonnet
        self.assertEqual(args[5], "high")

    async def test_out_of_range_effort_falls_back_to_high(self):
        es, rw, _ = self._patches(self._ok_result())
        with es:
            await app_module._send_in_background(
                1, "t", 600, True, "ludicrous", "opus",
                do_fusion=True, panel=["deepseek"])
        self.assertEqual(rw.call_args.args[5], "high")

    async def test_enrich_judge_uses_same_picker_values(self):
        # do_enrich path forwards judge_model/judge_effort as kwargs to enrich.
        es, _, en = self._patches(self._ok_result())
        en.return_value = mock.Mock(ok=False, error="panel unavailable",
                                    cost_usd=0.0, enrichment_md="", panel_models=[])
        with es:
            await app_module._send_in_background(
                1, "t", 600, True, "xhigh", "haiku",
                do_fusion=True, panel=["deepseek"], do_enrich=True)
        en.assert_called_once()
        self.assertEqual(en.call_args.kwargs["judge_model"], "haiku")
        self.assertEqual(en.call_args.kwargs["judge_effort"], "xhigh")


# ─────────────── C5.1: codex seat parse + executor engine seam ──────────────

class TestParseFusionPanelCodexSeat(unittest.TestCase):
    """C5.1 + codex-seat-picker producer side: _parse_fusion_panel turns a
    {type:"codex",model[,effort][,lens]} seat into a {kind:"codex_cli","model"
    [,"effort"][,"lens"]} panel entry (run_fusion_json consumes that third kind +
    its effort — C2.3). The codex model is validated against a whitelist sourced
    from CODEX_ENGINE_SEED (a codex id, NEVER a Claude id); a blank/unknown MODEL is
    DROPPED. The codex EFFORT (thinking level) is OPTIONAL — carried only when
    whitelisted, otherwise OMITTED (codex uses the model's own default), never
    seat-fatal. The claude/provider branches are unchanged. Pure/offline — no
    TestClient, so skipped stays 4."""

    CODEX = {"gpt-5.5"}
    CODEX_EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}
    ACTIVE = {"deepseek": {}, "minimax": {}}

    def test_codex_seat_becomes_codex_cli_kind(self):
        panel = app_module._parse_fusion_panel(
            '[{"type":"codex","model":"gpt-5.5"}]', "", self.ACTIVE, self.CODEX,
            self.CODEX_EFFORTS)
        self.assertEqual(panel, [{"kind": "codex_cli", "model": "gpt-5.5"}])

    def test_codex_seat_carries_lens_and_whitelisted_effort(self):
        # The codex-seat picker sends model + thinking-level (effort) + lens; a
        # whitelisted effort is CARRIED through to run_fusion_json (which reads it).
        panel = app_module._parse_fusion_panel(
            '[{"type":"codex","model":"gpt-5.5","lens":"risks","effort":"high"}]',
            "", self.ACTIVE, self.CODEX, self.CODEX_EFFORTS)
        self.assertEqual(panel,
                         [{"kind": "codex_cli", "model": "gpt-5.5",
                           "effort": "high", "lens": "risks"}])

    def test_codex_seat_unknown_effort_omitted_seat_kept(self):
        # An out-of-whitelist effort (stale/crafted, or a claude-only value like
        # "max") is DROPPED, not seat-fatal — codex falls back to the model default
        # (so an invalid -c value codex would 400 on never ships). Seat survives.
        panel = app_module._parse_fusion_panel(
            '[{"type":"codex","model":"gpt-5.5","effort":"max"}]',
            "", self.ACTIVE, self.CODEX, self.CODEX_EFFORTS)
        self.assertEqual(panel, [{"kind": "codex_cli", "model": "gpt-5.5"}])
        self.assertNotIn("effort", panel[0])

    def test_codex_seat_blank_effort_omitted(self):
        # The picker's "default" option sends effort "" (or omits it) → no effort key,
        # so codex uses its own model default (the documented codex divergence).
        for raw in ('[{"type":"codex","model":"gpt-5.5","effort":""}]',
                    '[{"type":"codex","model":"gpt-5.5"}]'):
            panel = app_module._parse_fusion_panel(raw, "", self.ACTIVE, self.CODEX,
                                                   self.CODEX_EFFORTS)
            self.assertEqual(panel, [{"kind": "codex_cli", "model": "gpt-5.5"}])

    def test_codex_efforts_defaults_to_seed_when_omitted(self):
        # Mirrors _derive_executor: when codex_efforts isn't passed, it falls back to
        # _codex_seat_efforts() (the seed). Patched so it doesn't read real config.json.
        with mock.patch.object(app_module.config, "codex_engine",
                               return_value={"efforts": ["minimal", "high"]}):
            panel = app_module._parse_fusion_panel(
                '[{"type":"codex","model":"gpt-5.5","effort":"high"}]', "", self.ACTIVE,
                self.CODEX)
        self.assertEqual(panel, [{"kind": "codex_cli", "model": "gpt-5.5", "effort": "high"}])

    def test_model_less_codex_seat_is_dropped(self):
        self.assertEqual(
            app_module._parse_fusion_panel('[{"type":"codex"}]', "", self.ACTIVE, self.CODEX), [])

    def test_claude_id_in_codex_seat_is_dropped(self):
        # A Claude id must NEVER pass codex validation (it would reach `codex -m`).
        self.assertEqual(
            app_module._parse_fusion_panel(
                '[{"type":"codex","model":"opus"}]', "", self.ACTIVE, self.CODEX), [])

    def test_mixed_panel_keeps_claude_codex_provider(self):
        raw = ('[{"type":"claude","model":"opus","effort":"high"},'
               '{"type":"codex","model":"gpt-5.5"},'
               '{"type":"provider","name":"deepseek"}]')
        self.assertEqual(
            app_module._parse_fusion_panel(raw, "", self.ACTIVE, self.CODEX),
            [{"kind": "claude_cli", "model": "opus", "effort": "high"},
             {"kind": "codex_cli", "model": "gpt-5.5"},
             "deepseek"])

    def test_legacy_comma_panel_unaffected(self):
        # No fusion_seats → the legacy comma fallback still filters by active.
        self.assertEqual(
            app_module._parse_fusion_panel("", "deepseek,glm,minimax", self.ACTIVE, self.CODEX),
            ["deepseek", "minimax"])


class TestValidateExecutorEngine(unittest.TestCase):
    """C5.1 executor gate: _validate_executor_engine. claude (default) ignores the
    codex model; codex REQUIRES an explicit, whitelisted codex id (the two
    rejections — blank vs unknown — are distinct, both reject). It never downgrades
    a codex pick to a Claude id or the claude engine (dispatch #3). Pure/offline."""

    CODEX = {"gpt-5.5"}

    def test_default_is_claude_and_ignores_model(self):
        self.assertEqual(app_module._validate_executor_engine("", "", self.CODEX), ("claude", ""))
        # A codex model riding along with claude is ignored (not an error).
        self.assertEqual(
            app_module._validate_executor_engine("claude", "gpt-5.5", self.CODEX),
            ("claude", ""))

    def test_codex_with_whitelisted_model_ok(self):
        self.assertEqual(
            app_module._validate_executor_engine("codex", "gpt-5.5", self.CODEX),
            ("codex", "gpt-5.5"))

    def test_codex_blank_model_rejected(self):
        with self.assertRaises(ValueError):
            app_module._validate_executor_engine("codex", "", self.CODEX)

    def test_codex_claude_id_rejected(self):
        # opus is a Claude id → never valid for `codex -m`.
        with self.assertRaises(ValueError):
            app_module._validate_executor_engine("codex", "opus", self.CODEX)

    def test_unknown_engine_rejected(self):
        with self.assertRaises(ValueError):
            app_module._validate_executor_engine("gpt", "", self.CODEX)


class TestRunDispatchCodexSeam(unittest.IsolatedAsyncioTestCase):
    """C6 executor (was the C5 inert seam): an engine='codex' dispatch now SPAWNS the
    codex executor (spawn_codex_dispatch) and NEVER spawn_iterm2 — a codex pick must
    never become a silent claude executor (dispatch #3), on SUCCESS or on spawn FAILURE.
    The cap watcher gets engine='codex' and the in-band poller is attached. db / spawn /
    watchdog are mocked, so no real DB / iTerm2 / poller is touched."""

    def _patches(self):
        import contextlib
        es = contextlib.ExitStack()
        es.enter_context(mock.patch.object(
            app_module.db, "get_project", return_value={"id": 1, "path": str(REPO)}))
        es.enter_context(mock.patch.object(
            app_module.attachments_mod, "list_files", return_value=[]))
        es.enter_context(mock.patch.object(
            app_module.db, "create_dispatch", return_value=42))
        es.enter_context(mock.patch.object(app_module.db, "record_event"))
        es.enter_context(mock.patch.object(app_module.db, "mark_started"))
        es.enter_context(mock.patch.object(app_module.db, "touch_project"))
        es.enter_context(mock.patch.object(app_module.spawn, "cleanup_dispatch_files"))
        es.enter_context(mock.patch.object(
            app_module.spawn, "read_claude_pid", return_value=4321))
        es.enter_context(mock.patch.object(app_module.watchdog, "schedule"))
        es.enter_context(mock.patch.object(app_module.watchdog, "schedule_codex_poller"))
        es.enter_context(mock.patch.object(app_module.config, "codex_cli_available", return_value=True))
        es.enter_context(mock.patch.object(
            app_module.config, "codex_engine",
            return_value={"model": "gpt-5.5", "max_concurrent_dispatches": 0}))
        es.enter_context(mock.patch.object(app_module.db, "running_dispatches", return_value=[]))
        mfs = es.enter_context(mock.patch.object(app_module.db, "mark_failed_to_spawn"))
        spawn_it = es.enter_context(mock.patch.object(app_module.spawn, "spawn_iterm2"))
        spawn_cx = es.enter_context(mock.patch.object(app_module.spawn, "spawn_codex_dispatch"))
        return es, mfs, spawn_it, spawn_cx

    async def test_codex_engine_spawns_codex_not_claude(self):
        es, mfs, spawn_it, spawn_cx = self._patches()
        with es:
            did, err = await app_module._run_dispatch(
                1, "do the thing", 600, "max", "gpt-5.5")
            # cap watcher took the codex branch; the in-band poller was attached.
            self.assertEqual(
                app_module.watchdog.schedule.call_args.kwargs.get("engine"), "codex")
            app_module.watchdog.schedule_codex_poller.assert_called_once()
        self.assertEqual(did, 42)
        self.assertEqual(err, "")
        spawn_cx.assert_called_once()        # the codex executor was spawned
        # spawn_codex_dispatch(project_path, dispatch_id, task, executor_model)
        self.assertEqual(spawn_cx.call_args.args[3], "gpt-5.5")  # explicit codex model (no downgrade)
        spawn_it.assert_not_called()         # NO silent claude executor
        mfs.assert_not_called()

    async def test_codex_spawn_failure_is_visible_never_claude_fallback(self):
        # Even when the codex spawn FAILS, we mark a visible failed row and return an
        # error — we do NOT fall through to the claude spawn (the dispatch #3 downgrade).
        es, mfs, spawn_it, spawn_cx = self._patches()
        spawn_cx.side_effect = RuntimeError("boom")
        with es:
            did, err = await app_module._run_dispatch(
                1, "do the thing", 600, "max", "gpt-5.5")
        self.assertIsNone(did)
        self.assertIn("codex", err.lower())
        spawn_it.assert_not_called()         # STILL no claude fallback on failure
        mfs.assert_called_once()             # visible failed row

    async def test_claude_engine_reaches_spawn(self):
        # The default (claude) path is unchanged: it reaches spawn_iterm2 and NOT the
        # codex spawn. (We make the spawn raise so the success-tail mocks aren't needed;
        # the point is only that the claude branch is taken.)
        es, mfs, spawn_it, spawn_cx = self._patches()
        spawn_it.side_effect = RuntimeError("boom")
        with es:
            did, err = await app_module._run_dispatch(1, "do the thing", 600, "max", "")
        spawn_it.assert_called_once()        # claude path reached the spawn
        spawn_cx.assert_not_called()         # codex path NOT taken for a claude dispatch
        self.assertIsNone(did)
        self.assertIn("spawn failed", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
