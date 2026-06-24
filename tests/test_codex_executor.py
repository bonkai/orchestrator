"""C6 tests — the $0 codex EXECUTOR (spawn_codex_dispatch + the §5 in-band poller).

Fully OFFLINE — no real `codex`, no iTerm2, no network. The poller is driven by a
SYNTHETIC codex sidecar JSONL fixture (the C6.0 schema) + mocked db / spawn / finalize,
and the unhappy paths (.done exit 0, nonzero, closed-tab, boot re-attach) are all
exercised. Covers:
  - claude_runner._codex_tool_event  — the tool-call fingerprint + timeline mapper
  - summarizer.distill_transcript     — the additive codex branch (note 4)
  - app._codex_timeline_step          — tool_use/tool_result + loop-watchdog feed
  - app._codex_dispatch_poller        — the lifetime finalizer + boot-reattach idempotency
  - watchdog._run(engine="codex")     — the cap HARD-KILL with a distinct reason (C6.3)
  - spawn.is_codex_dispatch / cleanup_dispatch_files — the executor file plumbing

Usage:  python -m unittest tests.test_codex_executor -v
"""

import asyncio
import contextlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orchestrator import app as app_module
from orchestrator.lib import claude_runner, loop_watchdog, spawn, summarizer, watchdog


# ── synthetic codex `exec --json` events (C6.0 schema, codex-cli 0.141.0) ────────
def _cmd_started(i, cmd):
    return json.dumps({"type": "item.started", "item": {
        "id": f"item_{i}", "type": "command_execution", "command": cmd,
        "aggregated_output": "", "exit_code": None, "status": "in_progress"}})


def _cmd_done(i, cmd, out="ok\n", code=0):
    return json.dumps({"type": "item.completed", "item": {
        "id": f"item_{i}", "type": "command_execution", "command": cmd,
        "aggregated_output": out, "exit_code": code, "status": "completed"}})


def _file_change(i, path, kind="add", phase="completed"):
    return json.dumps({"type": f"item.{phase}", "item": {
        "id": f"item_{i}", "type": "file_change",
        "changes": [{"path": path, "kind": kind}], "status": phase}})


def _agent(i, text):
    return json.dumps({"type": "item.completed",
                       "item": {"id": f"item_{i}", "type": "agent_message", "text": text}})


THREAD = json.dumps({"type": "thread.started", "thread_id": "019efabc-1234"})
TURN_DONE = json.dumps({"type": "turn.completed", "usage": {
    "input_tokens": 10, "cached_input_tokens": 5, "output_tokens": 3,
    "reasoning_output_tokens": 0}})


# ─────────────── claude_runner._codex_tool_event (fingerprint mapper) ────────────

class TestCodexToolEvent(unittest.TestCase):
    def test_command_execution_started(self):
        ev = claude_runner._codex_tool_event(json.loads(_cmd_started(1, "ls -la")))
        self.assertEqual(ev["tool_name"], "command_execution")
        self.assertEqual(ev["phase"], "start")
        self.assertEqual(ev["id"], "item_1")
        self.assertEqual(ev["detail"]["command"], "ls -la")

    def test_command_execution_completed_carries_exit_and_output(self):
        ev = claude_runner._codex_tool_event(json.loads(_cmd_done(1, "ls", "out\n", 0)))
        self.assertEqual(ev["phase"], "end")
        self.assertEqual(ev["detail"]["exit_code"], 0)
        self.assertIn("out", ev["detail"]["output_preview"])

    def test_file_change_is_a_tool_event(self):
        ev = claude_runner._codex_tool_event(json.loads(_file_change(2, "/p/a.py")))
        self.assertEqual(ev["tool_name"], "file_change")
        self.assertTrue(ev["input_hash"])
        self.assertIn("add:/p/a.py", ev["detail"]["changes"])

    def test_agent_message_is_not_a_tool_event(self):
        self.assertIsNone(claude_runner._codex_tool_event(json.loads(_agent(3, "hi"))))

    def test_thread_and_turn_ignored(self):
        self.assertIsNone(claude_runner._codex_tool_event(json.loads(THREAD)))
        self.assertIsNone(claude_runner._codex_tool_event(json.loads(TURN_DONE)))

    def test_malformed_returns_none(self):
        self.assertIsNone(claude_runner._codex_tool_event(None))
        self.assertIsNone(claude_runner._codex_tool_event({"type": "item.started"}))  # no item

    def test_identical_commands_collide_distinct_differ(self):
        # The loop-watchdog property: same command → same fingerprint (loop-detectable
        # across DISTINCT item ids); a different command → a different fingerprint.
        h1 = claude_runner._codex_tool_event(json.loads(_cmd_started(1, "X")))["input_hash"]
        h2 = claude_runner._codex_tool_event(json.loads(_cmd_started(2, "X")))["input_hash"]
        h3 = claude_runner._codex_tool_event(json.loads(_cmd_started(3, "Y")))["input_hash"]
        self.assertEqual(h1, h2)
        self.assertNotEqual(h1, h3)

    def test_started_and_completed_share_fingerprint(self):
        a = claude_runner._codex_tool_event(json.loads(_cmd_started(1, "Z")))
        b = claude_runner._codex_tool_event(json.loads(_cmd_done(1, "Z")))
        self.assertEqual((a["tool_name"], a["input_hash"]),
                         (b["tool_name"], b["input_hash"]))


# ─────────────── summarizer.distill_transcript codex branch (note 4) ─────────────

class TestDistillCodexTranscript(unittest.TestCase):
    def _write(self, lines):
        d = Path(tempfile.mkdtemp(prefix="orch_codex_tx_"))
        self.addCleanup(shutil.rmtree, d, True)
        p = d / "t.jsonl"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(p)

    def test_codex_sidecar_distills_to_nonempty_markdown(self):
        path = self._write([THREAD, _agent(0, "I will run the tests and edit a file."),
                            _cmd_started(1, "pytest -q"), _cmd_done(1, "pytest -q", "2 passed\n", 0),
                            _file_change(2, "/p/app.py"), _agent(3, "Done."), TURN_DONE])
        md = summarizer.distill_transcript(path)
        self.assertNotIn("no conversational content", md)   # never an empty summary
        self.assertIn("command_execution", md)
        self.assertIn("pytest -q", md)
        self.assertIn("file_change", md)
        self.assertIn("Done.", md)

    def test_claude_transcript_unaffected_by_codex_branch(self):
        # Regression: the additive codex branch must not perturb the claude path.
        claude = json.dumps({"type": "assistant", "message": {
            "content": [{"type": "text", "text": "hello from claude"}]}})
        md = summarizer.distill_transcript(self._write([claude]))
        self.assertIn("hello from claude", md)


# ─────────────── app._codex_timeline_step (timeline + loop-watchdog feed) ────────

class TestCodexTimelineStep(unittest.IsolatedAsyncioTestCase):
    async def test_tool_use_then_result_and_idle_reset(self):
        kinds = []
        with mock.patch.object(app_module.db, "record_event",
                               side_effect=lambda d, k, p=None: kinds.append(k)), \
             mock.patch.object(app_module.idle_notifier, "reset_idle") as ri, \
             mock.patch.object(app_module.loop_watchdog, "record", return_value=False):
            seen, comp = set(), set()
            app_module._codex_timeline_step(7, _cmd_started(1, "ls"), seen, comp)
            app_module._codex_timeline_step(7, _cmd_done(1, "ls"), seen, comp)
        self.assertEqual(kinds.count("tool_use"), 1)     # one per tool call (dedup by item.id)
        self.assertEqual(kinds.count("tool_result"), 1)
        ri.assert_called()                                # idle re-armed on activity

    async def test_fingerprint_recorded_once_per_item(self):
        recs = []
        with mock.patch.object(app_module.db, "record_event"), \
             mock.patch.object(app_module.idle_notifier, "reset_idle"), \
             mock.patch.object(app_module.loop_watchdog, "record",
                               side_effect=lambda *a: recs.append(a) or False):
            seen, comp = set(), set()
            app_module._codex_timeline_step(7, _cmd_started(1, "ls"), seen, comp)
            app_module._codex_timeline_step(7, _cmd_done(1, "ls"), seen, comp)
        self.assertEqual(len(recs), 1)                    # started+completed → ONE fingerprint

    async def test_garbage_line_is_ignored(self):
        with mock.patch.object(app_module.db, "record_event") as re_:
            app_module._codex_timeline_step(7, "not json {{{", set(), set())
            app_module._codex_timeline_step(7, _agent(1, "hi"), set(), set())  # not a tool event
        re_.assert_not_called()

    async def test_loop_trips_and_triggers_kill(self):
        # PROVE the kill, not just record(): feed THRESHOLD identical commands (distinct
        # item ids) through the REAL loop_watchdog.record → it trips → trigger_kill is
        # invoked with the codex fingerprint (the PreToolUse-driven kill path for codex).
        loop_watchdog.clear(99)
        with mock.patch.object(app_module.db, "record_event"), \
             mock.patch.object(app_module.idle_notifier, "reset_idle"), \
             mock.patch.object(app_module.loop_watchdog, "trigger_kill",
                               new_callable=mock.AsyncMock) as tk:
            seen, comp = set(), set()
            for i in range(loop_watchdog.DEFAULT_LOOP_THRESHOLD):
                app_module._codex_timeline_step(99, _cmd_started(i, "stuck --same-cmd"),
                                                seen, comp)
            await asyncio.sleep(0)        # let the create_task'd trigger_kill run
        tk.assert_awaited_once()
        did, fp = tk.await_args.args
        self.assertEqual(did, 99)
        self.assertEqual(fp[0], "command_execution")
        loop_watchdog.clear(99)


# ─────────────── app._codex_dispatch_poller (the lifetime finalizer) ─────────────

class TestCodexPoller(unittest.IsolatedAsyncioTestCase):
    def _codex_dir(self):
        d = Path(tempfile.mkdtemp(prefix="orch_codex_poll_"))
        self.addCleanup(shutil.rmtree, d, True)
        return d

    @contextlib.contextmanager
    def _env(self, cd, *, pid=4321, pid_alive=True, record_sink=None):
        fin = mock.AsyncMock(return_value=True)
        rec = (mock.patch.object(app_module.db, "record_event",
                                 side_effect=lambda d, k, p=None: record_sink.append(k))
               if record_sink is not None
               else mock.patch.object(app_module.db, "record_event"))
        with mock.patch.object(spawn, "CODEX_DIR", cd), \
             mock.patch.object(app_module, "_finalize_dispatch", fin), \
             rec, \
             mock.patch.object(app_module.idle_notifier, "reset_idle"), \
             mock.patch.object(app_module.loop_watchdog, "record", return_value=False), \
             mock.patch.object(app_module.spawn, "read_pid_now", return_value=pid), \
             mock.patch.object(app_module.spawn, "pid_alive", return_value=pid_alive), \
             mock.patch.object(app_module, "_CODEX_POLL_INTERVAL_S", 0.01):
            yield fin

    async def test_done_exit0_finalizes_completed_with_transcript_and_timeline(self):
        cd, did = self._codex_dir(), 60
        (cd / f"{did}.jsonl").write_text("\n".join([
            THREAD, _cmd_started(1, "pytest"), _cmd_done(1, "pytest"),
            _agent(2, "done"), TURN_DONE]) + "\n", encoding="utf-8")
        (cd / f"{did}.done").write_text("0")
        recorded = []
        with self._env(cd, record_sink=recorded) as fin:
            await asyncio.wait_for(app_module._codex_dispatch_poller(did, False), timeout=5)
        fin.assert_awaited_once()
        self.assertEqual(fin.await_args.kwargs.get("outcome"), "completed")
        self.assertEqual(fin.await_args.kwargs.get("transcript_src"), str(cd / f"{did}.jsonl"))
        self.assertIn("tool_use", recorded)
        self.assertIn("tool_result", recorded)

    async def test_done_nonzero_finalizes_failed(self):
        cd, did = self._codex_dir(), 62
        (cd / f"{did}.jsonl").write_text("\n".join([THREAD, _agent(1, "boom")]) + "\n",
                                         encoding="utf-8")
        (cd / f"{did}.done").write_text("3")
        with self._env(cd) as fin:
            await asyncio.wait_for(app_module._codex_dispatch_poller(did, False), timeout=5)
        self.assertEqual(fin.await_args.kwargs.get("outcome"), "failed")
        self.assertIn("codex exit 3", fin.await_args.kwargs.get("exit_reason"))

    async def test_closed_tab_no_done_finalizes_failed(self):
        cd, did = self._codex_dir(), 63
        (cd / f"{did}.jsonl").write_text(THREAD + "\n", encoding="utf-8")  # no .done ever
        with self._env(cd, pid_alive=False) as fin:
            await asyncio.wait_for(app_module._codex_dispatch_poller(did, False), timeout=5)
        fin.assert_awaited_once()
        self.assertEqual(fin.await_args.kwargs.get("outcome"), "failed")
        self.assertIn("closed", fin.await_args.kwargs.get("exit_reason"))

    async def test_tail_only_does_not_reemit_prefix(self):
        # Boot re-attach: tail_only seeks past the already-consumed prefix, so a restart
        # must NOT re-emit timeline events (which would also re-trip the loop watchdog).
        cd, did = self._codex_dir(), 55
        (cd / f"{did}.jsonl").write_text("\n".join([
            THREAD, _cmd_started(1, "ls"), _cmd_done(1, "ls"), _agent(2, "hi")]) + "\n",
            encoding="utf-8")
        recorded = []
        with self._env(cd, record_sink=recorded):     # no .done, pid alive → it loops
            task = asyncio.create_task(app_module._codex_dispatch_poller(did, True))
            await asyncio.sleep(0.08)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.assertNotIn("tool_use", recorded)         # prefix NOT replayed


# ─────────────── watchdog cap HARD-KILL for codex (C6.3, note 5) ─────────────────

class TestCodexCapWatchdog(unittest.IsolatedAsyncioTestCase):
    async def test_codex_cap_hardkills_distinct_reason_and_cancels_poller(self):
        killed = {}
        with mock.patch.object(watchdog.spawn, "read_pid_now", return_value=1234), \
             mock.patch.object(watchdog.spawn, "kill_pid_async", new_callable=mock.AsyncMock) as kpa, \
             mock.patch.object(watchdog.spawn, "cleanup_dispatch_files"), \
             mock.patch.object(watchdog.db, "update_claude_pid"), \
             mock.patch.object(watchdog.db, "kill_dispatch_record",
                               side_effect=lambda did, reason: killed.update(did=did, reason=reason)), \
             mock.patch.object(watchdog, "cancel_codex_poller") as ccp, \
             mock.patch.object(watchdog.idle_notifier, "clear"):
            await watchdog._run(77, 1234, 0.01, engine="codex")
        self.assertEqual(killed["did"], 77)
        self.assertIn("codex", killed["reason"].lower())     # distinct from a claude timeout
        self.assertIn("not resumable", killed["reason"].lower())
        kpa.assert_awaited()                                  # codex was actually killed
        ccp.assert_called_once_with(77)                       # the poller was cancelled


# ─────────────── spawn executor file plumbing ───────────────────────────────────

class TestCodexExecutorFiles(unittest.TestCase):
    def test_is_codex_dispatch_detects_prompt_sidecar(self):
        cd = Path(tempfile.mkdtemp(prefix="orch_cx_"))
        self.addCleanup(shutil.rmtree, cd, True)
        with mock.patch.object(spawn, "CODEX_DIR", cd):
            self.assertFalse(spawn.is_codex_dispatch(5))
            (cd / "5.prompt").write_text("hi")
            self.assertTrue(spawn.is_codex_dispatch(5))

    def test_cleanup_clears_codex_executor_sidecars(self):
        cd = Path(tempfile.mkdtemp(prefix="orch_cx_"))
        pids = Path(tempfile.mkdtemp(prefix="orch_pid_"))
        tasks = Path(tempfile.mkdtemp(prefix="orch_task_"))
        for d in (cd, pids, tasks):
            self.addCleanup(shutil.rmtree, d, True)
        with mock.patch.object(spawn, "CODEX_DIR", cd), \
             mock.patch.object(spawn, "PIDS_DIR", pids), \
             mock.patch.object(spawn, "TASKS_DIR", tasks), \
             mock.patch.object(spawn, "auto_close_enabled", return_value=False):
            for suf in ("prompt", "model", "effort", "jsonl", "done", "fifo"):
                (cd / f"9.{suf}").write_text("x")
            (pids / "9.pid").write_text("123")
            spawn.cleanup_dispatch_files(9)
            for suf in ("prompt", "model", "effort", "jsonl", "done", "fifo"):
                self.assertFalse((cd / f"9.{suf}").exists(), f"codex {suf} not cleared")
            self.assertFalse((pids / "9.pid").exists())


class TestCodexConcurrencyCap(unittest.IsolatedAsyncioTestCase):
    """§2 Q7 Plus guard: a codex dispatch is rejected (VISIBLE failed row, NEVER a
    claude fallback) once `max_concurrent_dispatches` codex dispatches are already
    running; under the cap it proceeds; cap<=0 ⇒ unlimited; a claude dispatch is
    unaffected (the cap lives only in the codex branch). All seams mocked — offline."""

    def _patches(self, *, cap, running_codex_count):
        import contextlib
        es = contextlib.ExitStack()
        es.enter_context(mock.patch.object(
            app_module.db, "get_project", return_value={"id": 1, "path": str(REPO)}))
        es.enter_context(mock.patch.object(
            app_module.attachments_mod, "list_files", return_value=[]))
        es.enter_context(mock.patch.object(app_module.db, "create_dispatch", return_value=42))
        es.enter_context(mock.patch.object(app_module.db, "record_event"))
        es.enter_context(mock.patch.object(app_module.db, "mark_started"))
        es.enter_context(mock.patch.object(app_module.db, "touch_project"))
        es.enter_context(mock.patch.object(app_module.spawn, "cleanup_dispatch_files"))
        es.enter_context(mock.patch.object(
            app_module.spawn, "read_claude_pid", return_value=4321))
        es.enter_context(mock.patch.object(app_module.watchdog, "schedule"))
        es.enter_context(mock.patch.object(app_module.watchdog, "schedule_codex_poller"))
        es.enter_context(mock.patch.object(
            app_module.config, "codex_engine",
            return_value={"model": "gpt-5.5", "max_concurrent_dispatches": cap}))
        es.enter_context(mock.patch.object(app_module.config, "codex_cli_available", return_value=True))
        es.enter_context(mock.patch.object(
            app_module.db, "running_dispatches",
            return_value=[{"id": i} for i in range(running_codex_count)]))
        es.enter_context(mock.patch.object(
            app_module.spawn, "is_codex_dispatch", return_value=True))
        mfs = es.enter_context(mock.patch.object(app_module.db, "mark_failed_to_spawn"))
        spawn_cx = es.enter_context(mock.patch.object(app_module.spawn, "spawn_codex_dispatch"))
        spawn_it = es.enter_context(mock.patch.object(app_module.spawn, "spawn_iterm2"))
        return es, mfs, spawn_cx, spawn_it

    async def test_at_cap_rejected_no_spawn_no_claude_fallback(self):
        es, mfs, spawn_cx, spawn_it = self._patches(cap=2, running_codex_count=2)
        with es:
            did, err = await app_module._run_dispatch(
                1, "t", 600, "max", "gpt-5.5")
        self.assertIsNone(did)
        self.assertIn("concurrency cap", err.lower())
        spawn_cx.assert_not_called()
        spawn_it.assert_not_called()      # never a claude fallback
        mfs.assert_called_once()          # visible failed row

    async def test_under_cap_proceeds(self):
        es, mfs, spawn_cx, spawn_it = self._patches(cap=2, running_codex_count=1)
        with es:
            did, err = await app_module._run_dispatch(
                1, "t", 600, "max", "gpt-5.5")
        self.assertEqual(did, 42)
        spawn_cx.assert_called_once()
        mfs.assert_not_called()

    async def test_cap_zero_is_unlimited(self):
        es, mfs, spawn_cx, spawn_it = self._patches(cap=0, running_codex_count=99)
        with es:
            did, err = await app_module._run_dispatch(
                1, "t", 600, "max", "gpt-5.5")
        self.assertEqual(did, 42)
        spawn_cx.assert_called_once()

    async def test_claude_dispatch_ignores_codex_cap(self):
        # codex dispatches at cap must NOT block a claude dispatch (cap is codex-branch only).
        es, mfs, spawn_cx, spawn_it = self._patches(cap=2, running_codex_count=5)
        spawn_it.side_effect = RuntimeError("boom")   # short-circuit the claude success tail
        with es:
            did, err = await app_module._run_dispatch(1, "t", 600, "max", "")  # claude
        spawn_it.assert_called_once()     # claude path reached its spawn despite codex at cap
        spawn_cx.assert_not_called()


# ─────────────── executor routing: a non-Claude id NEVER reaches `claude --model` ──

class TestExecutorRoutingNonClaude(unittest.TestCase):
    """The app.py:~1010 convention MIRROR — "a codex executor NEVER falls back to a
    Claude id" implies its inverse: a non-Claude / unknown id must NEVER silently reach
    `claude --model`. _derive_executor is TOTAL: a codex id → codex, "" or a known Claude
    id → claude, ANYTHING ELSE → 'invalid' (rejected by _validate_executor_engine).
    Regression for the gpt-5.5 + "skip rewrite & send" bug (dispatch #241), where a model
    absent from the runtime codex set fell through the old catch-all `return "claude",
    model, ""` to `claude --model gpt-5.5`. Pure/offline — codex_models passed
    explicitly, so no config.json read."""

    def test_unknown_gpt_id_never_derives_claude_carrying_it(self):
        # THE bug: the runtime codex set lacked gpt-5.5 (stale process / empty set), so
        # the old catch-all returned ("claude","gpt-5.5","") → `claude --model gpt-5.5`.
        engine, claude_model, executor_model = app_module._derive_executor(
            "gpt-5.5", codex_models=set())
        self.assertNotEqual(engine, "claude")            # never the claude engine...
        self.assertNotEqual(claude_model, "gpt-5.5")     # ...carrying the non-Claude id
        self.assertEqual(claude_model, "")               # nothing rides the `claude --model` slot
        self.assertEqual(engine, "invalid")              # it is explicitly rejected

    def test_unknown_ids_are_the_whole_class_not_just_gpt55(self):
        # Typos, future ids, retired/ChatGPT-rejected codex ids — every unknown id, not
        # just gpt-5.5, must be invalid (codex set has only gpt-5.5 here).
        for bad in ("gpt-6", "o3", "claude-opus-4-8", "opusx", "gpt-5.5-mini", "gpt-5-codex"):
            engine, claude_model, _ = app_module._derive_executor(bad, codex_models={"gpt-5.5"})
            self.assertEqual(engine, "invalid", f"{bad!r} should be invalid")
            self.assertEqual(claude_model, "", f"{bad!r} must not ride `claude --model`")

    def test_empty_model_stays_claude_default(self):
        # "" (the picker's "default") and whitespace-only both → claude, no --model flag.
        self.assertEqual(app_module._derive_executor("", codex_models={"gpt-5.5"}),
                         ("claude", "", ""))
        self.assertEqual(app_module._derive_executor("   ", codex_models={"gpt-5.5"}),
                         ("claude", "", ""))

    def test_known_claude_executor_models_stay_claude(self):
        # Every Anthropic option the executor picker offers — INCLUDING fable, which is
        # absent from CLAUDE_SEAT_MODELS; gating on the seat list would falsely reject a
        # legitimate fable dispatch.
        for m in app_module.CLAUDE_EXECUTOR_MODELS:
            self.assertEqual(app_module._derive_executor(m, codex_models={"gpt-5.5"}),
                             ("claude", m, ""))
        self.assertIn("fable", app_module.CLAUDE_EXECUTOR_MODELS)  # the fable guard

    def test_codex_model_routes_to_codex(self):
        # When the set DOES carry it, gpt-5.5 routes to codex (the codex-tab arm of the
        # acceptance OR) — and the Claude model slot is cleared.
        self.assertEqual(app_module._derive_executor("gpt-5.5", codex_models={"gpt-5.5"}),
                         ("codex", "", "gpt-5.5"))

    def test_validate_rejects_invalid_engine_naming_the_model(self):
        # The symmetric half: derive marks it 'invalid'; validate REJECTS it (never a
        # silent claude), and the message names the offending id so the failure is clear.
        with self.assertRaises(ValueError) as cm:
            app_module._validate_executor_engine("invalid", "gpt-5.5", {"gpt-5.5"},
                                                 codex_available=True)
        self.assertIn("gpt-5.5", str(cm.exception))

    def test_shared_seam_rejects_unknown_model_end_to_end(self):
        # The EXACT shared derive→validate seam both /send and _run_dispatch use, with the
        # bug's input (unknown id + empty codex set): it must raise, never yield a claude
        # engine carrying the id. codex_available=True proves the rejection is independent
        # of codex login state (it's the id that's unknown, not codex that's down).
        engine, _claude, executor_model = app_module._derive_executor("gpt-5.5", codex_models=set())
        with self.assertRaises(ValueError):
            app_module._validate_executor_engine(engine, executor_model, set(),
                                                 codex_available=True)


class TestClaudeSpawnGuard(unittest.IsolatedAsyncioTestCase):
    """Last-line backstop in _run_dispatch (the literal mirror of the codex branch's
    no-fallthrough guard): even if _derive_executor REGRESSES to the old catch-all and
    hands the claude branch a non-Claude id, the dispatch must refuse `claude --model
    <id>` — a VISIBLE failed row (mark_failed_to_spawn + stage event), NEVER spawn_iterm2
    (the dispatch #241 bug) and NEVER the codex spawn. All seams mocked — offline."""

    def _patches(self):
        es = contextlib.ExitStack()
        es.enter_context(mock.patch.object(
            app_module.db, "get_project", return_value={"id": 1, "path": str(REPO)}))
        es.enter_context(mock.patch.object(
            app_module.attachments_mod, "list_files", return_value=[]))
        es.enter_context(mock.patch.object(app_module.db, "create_dispatch", return_value=42))
        es.enter_context(mock.patch.object(app_module.db, "record_event"))
        es.enter_context(mock.patch.object(app_module.db, "mark_started"))
        es.enter_context(mock.patch.object(app_module.db, "touch_project"))
        es.enter_context(mock.patch.object(app_module.spawn, "cleanup_dispatch_files"))
        es.enter_context(mock.patch.object(app_module.spawn, "read_claude_pid", return_value=4321))
        es.enter_context(mock.patch.object(app_module.watchdog, "schedule"))
        es.enter_context(mock.patch.object(
            app_module.config, "codex_cli_available", return_value=True))
        es.enter_context(mock.patch.object(
            app_module.config, "codex_engine",
            return_value={"model": "gpt-5.5", "max_concurrent_dispatches": 0}))
        es.enter_context(mock.patch.object(app_module.db, "running_dispatches", return_value=[]))
        mfs = es.enter_context(mock.patch.object(app_module.db, "mark_failed_to_spawn"))
        spawn_it = es.enter_context(mock.patch.object(app_module.spawn, "spawn_iterm2"))
        spawn_cx = es.enter_context(mock.patch.object(app_module.spawn, "spawn_codex_dispatch"))
        return es, mfs, spawn_it, spawn_cx

    async def test_regressed_derivation_cannot_spawn_claude_model(self):
        es, mfs, spawn_it, spawn_cx = self._patches()
        # Simulate the OLD buggy catch-all: claude engine carrying a non-Claude id.
        es.enter_context(mock.patch.object(
            app_module, "_derive_executor", return_value=("claude", "gpt-5.5", "")))
        with es:
            did, err = await app_module._run_dispatch(1, "t", 600, "max", "gpt-5.5")
        self.assertIsNone(did)
        self.assertIn("gpt-5.5", err)
        spawn_it.assert_not_called()      # NEVER `claude --model gpt-5.5`
        spawn_cx.assert_not_called()      # and never the codex spawn either
        mfs.assert_called_once()          # visible failed row

    async def test_claude_default_still_reaches_spawn(self):
        # The guard must NOT fire for the normal claude default (model=""): it short-
        # circuits on `model and ...`, so the real claude path is byte-for-byte unchanged.
        es, mfs, spawn_it, spawn_cx = self._patches()
        spawn_it.side_effect = RuntimeError("boom")   # short-circuit the success tail
        with es:
            did, err = await app_module._run_dispatch(1, "t", 600, "max", "")
        spawn_it.assert_called_once()     # claude path reached its spawn
        spawn_cx.assert_not_called()
        self.assertIn("spawn failed", err)


# ─────────────── executor reasoning-effort routing (engine-aware, safe fallback) ──

class TestCodexExecutorEffort(unittest.IsolatedAsyncioTestCase):
    """Effort now flows to the codex EXECUTOR (it used to be dropped). A codex-valid
    effort is forwarded to spawn_codex_dispatch (→ `-c model_reasoning_effort`); a
    claude-only value ("max") or the picker's "default" safely falls back to "" (the
    model's own default), so a cross-engine pick never errors. All seams mocked — offline."""

    def _patches(self):
        es = contextlib.ExitStack()
        es.enter_context(mock.patch.object(
            app_module.db, "get_project", return_value={"id": 1, "path": str(REPO)}))
        es.enter_context(mock.patch.object(
            app_module.attachments_mod, "list_files", return_value=[]))
        es.enter_context(mock.patch.object(app_module.db, "create_dispatch", return_value=42))
        es.enter_context(mock.patch.object(app_module.db, "record_event"))
        es.enter_context(mock.patch.object(app_module.db, "mark_started"))
        es.enter_context(mock.patch.object(app_module.db, "touch_project"))
        es.enter_context(mock.patch.object(app_module.spawn, "cleanup_dispatch_files"))
        es.enter_context(mock.patch.object(app_module.spawn, "read_claude_pid", return_value=4321))
        es.enter_context(mock.patch.object(app_module.watchdog, "schedule"))
        es.enter_context(mock.patch.object(app_module.watchdog, "schedule_codex_poller"))
        es.enter_context(mock.patch.object(
            app_module.config, "codex_cli_available", return_value=True))
        es.enter_context(mock.patch.object(
            app_module.config, "codex_engine",
            return_value={"model": "gpt-5.5",
                          "efforts": ["minimal", "low", "medium", "high", "xhigh"],
                          "max_concurrent_dispatches": 0}))
        es.enter_context(mock.patch.object(app_module.db, "running_dispatches", return_value=[]))
        spawn_cx = es.enter_context(mock.patch.object(app_module.spawn, "spawn_codex_dispatch"))
        return es, spawn_cx

    async def _effort_forwarded(self, picked):
        # spawn_codex_dispatch(project_path, dispatch_id, task, model, effort) -> args[4]
        es, spawn_cx = self._patches()
        with es:
            did, err = await app_module._run_dispatch(1, "t", 600, picked, "gpt-5.5")
        self.assertEqual(did, 42, err)
        spawn_cx.assert_called_once()
        return spawn_cx.call_args.args[4]

    async def test_valid_codex_effort_forwarded(self):
        self.assertEqual(await self._effort_forwarded("high"), "high")

    async def test_codex_only_minimal_effort_forwarded(self):
        # minimal is NOT a claude effort — proves codex's OWN ladder is honored, not claude's.
        self.assertEqual(await self._effort_forwarded("minimal"), "minimal")

    async def test_claude_only_max_falls_back_to_model_default(self):
        # "max" is claude-only; codex 400s on it, so it must become "" (no -c override).
        self.assertEqual(await self._effort_forwarded("max"), "")

    async def test_picker_default_value_falls_back_to_model_default(self):
        # The dropdown's "default (model's own)" option sends "default" → not a codex
        # effort → "" (the model's own reasoning default).
        self.assertEqual(await self._effort_forwarded("default"), "")


class TestClaudeExecutorEffort(unittest.IsolatedAsyncioTestCase):
    """The Claude executor now accepts the FULL ladder including "low" (it previously
    silently coerced anything outside medium..max to "max"); a codex-only value like
    "minimal" still falls back to the default "max". spawn_iterm2 is made to raise so we
    only need the call args, not the success tail."""

    def _patches(self):
        es = contextlib.ExitStack()
        es.enter_context(mock.patch.object(
            app_module.db, "get_project", return_value={"id": 1, "path": str(REPO)}))
        es.enter_context(mock.patch.object(
            app_module.attachments_mod, "list_files", return_value=[]))
        es.enter_context(mock.patch.object(app_module.db, "create_dispatch", return_value=42))
        es.enter_context(mock.patch.object(app_module.db, "record_event"))
        es.enter_context(mock.patch.object(app_module.spawn, "cleanup_dispatch_files"))
        es.enter_context(mock.patch.object(
            app_module.config, "codex_cli_available", return_value=True))
        es.enter_context(mock.patch.object(
            app_module.config, "codex_engine",
            return_value={"model": "gpt-5.5",
                          "efforts": ["minimal", "low", "medium", "high", "xhigh"]}))
        spawn_it = es.enter_context(mock.patch.object(app_module.spawn, "spawn_iterm2"))
        spawn_it.side_effect = RuntimeError("boom")   # short-circuit the success tail
        return es, spawn_it

    async def _effort_used(self, picked):
        # spawn_iterm2(project_path, dispatch_id, task, tab_title, effort, model) -> args[4]
        es, spawn_it = self._patches()
        with es:
            await app_module._run_dispatch(1, "t", 600, picked, "")   # "" => claude
        spawn_it.assert_called_once()
        return spawn_it.call_args.args[4]

    async def test_low_is_now_allowed(self):
        self.assertEqual(await self._effort_used("low"), "low")

    async def test_codex_only_effort_falls_back_to_max(self):
        self.assertEqual(await self._effort_used("minimal"), "max")

    async def test_garbage_effort_falls_back_to_max(self):
        self.assertEqual(await self._effort_used("bogus"), "max")


if __name__ == "__main__":
    unittest.main(verbosity=2)
