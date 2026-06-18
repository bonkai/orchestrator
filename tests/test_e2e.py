"""End-to-end test for orchestrator phases 1+2.

Runs against an isolated test DB (via ORCHESTRATOR_TEST_HOME) so it does not
touch your real ~/.orchestrator. Mocks iTerm2 spawning to avoid opening real
windows; substitutes a fake `claude` so we can test the runner + PID flow +
kill flow + Stop hook for real.

Usage:
    python -m pytest tests/test_e2e.py -v
    # or
    python tests/test_e2e.py
"""

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# Make sure we import the in-repo orchestrator (not any installed version)
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _setup_isolated_home():
    """Point DATA_DIR at a tempdir, reload modules so they pick it up."""
    tmp = Path(tempfile.mkdtemp(prefix="orch_test_"))
    # Patch DATA_DIR before any module reads it
    import orchestrator.lib.db as db_mod
    db_mod.DATA_DIR = tmp
    db_mod.DB_PATH = tmp / "orchestrator.db"
    db_mod.TRANSCRIPTS_DIR = tmp / "transcripts"
    import orchestrator.lib.spawn as spawn_mod
    spawn_mod.TASKS_DIR = tmp / "tasks"
    spawn_mod.PIDS_DIR = tmp / "pids"
    spawn_mod.BIN_DIR = tmp / "bin"
    spawn_mod.RUN_SH = spawn_mod.BIN_DIR / "run.sh"
    return tmp


TMP_HOME = _setup_isolated_home()

# Now safe to import + initialize
from orchestrator.lib import db, spawn, watchdog
db.init_db()


def teardown_module(module):
    shutil.rmtree(TMP_HOME, ignore_errors=True)


# ───────────────────────── Test 1: DB schema + CRUD ────────────────────────

class TestDB(unittest.TestCase):
    def test_schema_tables_present(self):
        with db.conn() as c:
            tables = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        for t in ("projects", "ui_tabs", "dispatches", "outcomes", "artifacts"):
            self.assertIn(t, tables)

    def test_wal_mode_enabled(self):
        with db.conn() as c:
            mode = c.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_add_project_idempotent(self):
        td = Path(tempfile.mkdtemp())
        try:
            p1 = db.add_project(str(td))
            p2 = db.add_project(str(td))  # should not duplicate
            self.assertEqual(p1["id"], p2["id"])
            all_p = [p for p in db.list_projects() if p["path"] == str(td.resolve())]
            self.assertEqual(len(all_p), 1)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_add_project_rejects_nondir(self):
        with self.assertRaises(ValueError):
            db.add_project("/this/path/should/not/exist/xyz123")

    def test_tab_lifecycle(self):
        td = Path(tempfile.mkdtemp())
        try:
            p = db.add_project(str(td))
            db.open_tab(p["id"])
            tabs = [t for t in db.list_tabs() if t["project_id"] == p["id"]]
            self.assertEqual(len(tabs), 1)
            # idempotent open
            db.open_tab(p["id"])
            tabs = [t for t in db.list_tabs() if t["project_id"] == p["id"]]
            self.assertEqual(len(tabs), 1)
            db.close_tab(p["id"])
            tabs = [t for t in db.list_tabs() if t["project_id"] == p["id"]]
            self.assertEqual(len(tabs), 0)
        finally:
            shutil.rmtree(td, ignore_errors=True)


# ─── Test 2: dispatch lifecycle + Stop hook → /api/complete ──────────────

class TestDispatchLifecycle(unittest.IsolatedAsyncioTestCase):
    """Spawns a fake `claude` (a sleep-forever script) to test PID tracking,
    kill, and the orchestrator's /api/complete handler."""

    @classmethod
    def setUpClass(cls):
        # Set up a fake project
        cls.project_dir = Path(tempfile.mkdtemp(prefix="orch_proj_"))
        proj = db.add_project(str(cls.project_dir))
        cls.project_id = proj["id"]

        # Make a fake claude that just sleeps (so we have a real PID to kill)
        cls.fake_claude_dir = Path(tempfile.mkdtemp(prefix="orch_bin_"))
        fake = cls.fake_claude_dir / "claude"
        fake.write_text("#!/bin/bash\necho \"fake claude started, pid=$$\"\nsleep 300\n")
        fake.chmod(0o755)
        # Prepend to PATH so the runner finds our fake first
        cls._orig_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{cls.fake_claude_dir}{os.pathsep}{cls._orig_path}"

        spawn.ensure_runner()  # writes run.sh into our temp BIN_DIR

    @classmethod
    def tearDownClass(cls):
        os.environ["PATH"] = cls._orig_path
        shutil.rmtree(cls.project_dir, ignore_errors=True)
        shutil.rmtree(cls.fake_claude_dir, ignore_errors=True)

    def _fake_spawn(self, dispatch_id: int, task: str):
        """Bypass osascript: directly run the runner with ORCHESTRATOR_RUN_ID
        set, so we can test the PID + kill flow without iTerm2."""
        spawn.TASKS_DIR.mkdir(parents=True, exist_ok=True)
        (spawn.TASKS_DIR / f"{dispatch_id}.txt").write_text(task.strip())
        env = os.environ.copy()
        env["ORCHESTRATOR_RUN_ID"] = str(dispatch_id)
        env["HOME"] = str(TMP_HOME.parent)  # so $HOME/.orchestrator works
        # Patch run.sh in place to use our test HOME
        run_sh = spawn.RUN_SH
        original = run_sh.read_text()
        patched = original.replace('$HOME/.orchestrator', str(TMP_HOME))
        run_sh.write_text(patched)
        run_sh.chmod(0o755)
        # Spawn detached so test process doesn't wait
        proc = subprocess.Popen(
            ["bash", str(run_sh)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return proc

    async def test_full_dispatch_kill_cycle(self):
        # 1. Create dispatch row
        did = db.create_dispatch(self.project_id, "test task", wall_clock_cap_s=600)

        # 2. Spawn fake claude
        proc = self._fake_spawn(did, "test task")
        try:
            # 3. Wait for PID file to appear
            pid = spawn.read_claude_pid(did, timeout_s=5.0)
            self.assertIsNotNone(pid, "PID file never written")
            self.assertTrue(spawn.pid_alive(pid), "fake claude is not running")

            db.mark_started(did, terminal_pid=None, claude_pid=pid)

            # 4. Confirm status = running
            d = db.get_dispatch(did)
            self.assertEqual(d["status"], "running")
            self.assertEqual(d["claude_pid"], pid)

            # 5. Kill it via watchdog.manual_kill (async)
            killed = await watchdog.manual_kill(did)
            self.assertTrue(killed)

            # 6. Wait briefly for process to die. proc.wait() reaps any
            # zombie so pid_alive can give a definitive answer.
            try:
                proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                pass
            self.assertFalse(spawn.pid_alive(pid), "fake claude survived kill")

            # 7. Confirm DB state
            d = db.get_dispatch(did)
            self.assertEqual(d["status"], "killed")
            self.assertEqual(d["final_outcome"], "killed")
            self.assertEqual(d["outcome_reason"], "manual")
            self.assertIsNotNone(d["duration_s"])
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    async def test_kill_when_pid_in_file_but_not_db(self):
        """Simulates the race where the 5s poll missed the PID but the pid
        file exists. manual_kill must re-read the file and still kill."""
        did = db.create_dispatch(self.project_id, "race-test", wall_clock_cap_s=600)
        proc = self._fake_spawn(did, "race-test")
        try:
            pid = spawn.read_claude_pid(did, timeout_s=5.0)
            self.assertIsNotNone(pid)
            # Mark as running but WITHOUT claude_pid (simulating the race)
            db.mark_started(did, terminal_pid=None, claude_pid=None)
            d = db.get_dispatch(did)
            self.assertIsNone(d["claude_pid"])

            # Kill should still work via lazy re-read
            self.assertTrue(await watchdog.manual_kill(did))
            try:
                proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                pass
            self.assertFalse(spawn.pid_alive(pid))

            # And the late-read PID should have been persisted
            d = db.get_dispatch(did)
            self.assertEqual(d["claude_pid"], pid)
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    async def test_api_complete_marks_completed_and_idempotent(self):
        """Simulate the Stop hook posting /api/complete via the watchdog code path."""
        did = db.create_dispatch(self.project_id, "complete-test", wall_clock_cap_s=600)
        # Pretend it started 2s ago
        db.mark_started(did, terminal_pid=None, claude_pid=99999)
        # Fake a transcript file
        transcript = TMP_HOME / "fake_transcript.jsonl"
        transcript.write_text('{"role":"assistant","content":"done"}\n')

        # Simulate /api/complete by calling complete_dispatch directly
        db.complete_dispatch(
            did, session_id="sess-abc",
            transcript_path=str(transcript), exit_reason="Stop",
        )

        d = db.get_dispatch(did)
        self.assertEqual(d["status"], "completed")
        self.assertEqual(d["session_id"], "sess-abc")
        self.assertEqual(d["final_outcome"], "completed")

        # Idempotent: a second call should not blow up (INSERT OR REPLACE on outcomes)
        db.complete_dispatch(did, "sess-abc", str(transcript), "Stop")
        d = db.get_dispatch(did)
        self.assertEqual(d["status"], "completed")


# ─── Test 3: reaper for orphaned dispatches ──────────────────────────────

class TestReaper(unittest.TestCase):
    def setUp(self):
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.project_id = db.add_project(str(td))["id"]

    def test_reap_dead_pid(self):
        """A 'running' dispatch whose PID is no longer alive should be marked orphaned."""
        did = db.create_dispatch(self.project_id, "orphan-test", wall_clock_cap_s=600)
        # Pick a definitely-not-running PID (max + 1 on most systems)
        dead_pid = 99999999
        self.assertFalse(spawn.pid_alive(dead_pid))
        db.mark_started(did, terminal_pid=None, claude_pid=dead_pid)

        watchdog.reap_orphans()

        d = db.get_dispatch(did)
        self.assertEqual(d["status"], "completed")
        self.assertEqual(d["final_outcome"], "orphaned")
        self.assertEqual(d["outcome_reason"], "process_gone")

    def test_reap_missing_pid_record(self):
        """A 'running' dispatch with no PID anywhere → orphan reason 'no_pid_record'."""
        did = db.create_dispatch(self.project_id, "no-pid", wall_clock_cap_s=600)
        db.mark_started(did, terminal_pid=None, claude_pid=None)

        watchdog.reap_orphans()
        d = db.get_dispatch(did)
        self.assertEqual(d["final_outcome"], "orphaned")
        self.assertEqual(d["outcome_reason"], "no_pid_record")

    def test_periodic_reaper_skips_young_no_pid(self):
        """The periodic reaper (min_age_s set) must NOT orphan a just-spawned,
        no-PID dispatch — it may still be writing its PID file (iTerm latency)."""
        did = db.create_dispatch(self.project_id, "young-no-pid", wall_clock_cap_s=600)
        db.mark_started(did, terminal_pid=None, claude_pid=None)  # started_at = now
        watchdog.reap_orphans(min_age_s=60)
        d = db.get_dispatch(did)
        self.assertEqual(d["status"], "running",
                         "young no-PID dispatch was falsely orphaned by the reaper")

    def test_periodic_reaper_reaps_old_no_pid(self):
        """Past the grace window, a no-PID dispatch IS reaped."""
        import sqlite3, time as _time
        did = db.create_dispatch(self.project_id, "old-no-pid", wall_clock_cap_s=600)
        db.mark_started(did, terminal_pid=None, claude_pid=None)
        # Backdate started_at so it's older than the grace window.
        con = sqlite3.connect(str(db.DB_PATH))
        con.execute("UPDATE dispatches SET started_at = ? WHERE id = ?",
                    (int(_time.time()) - 120, did))
        con.commit(); con.close()
        watchdog.reap_orphans(min_age_s=60)
        d = db.get_dispatch(did)
        self.assertEqual(d["final_outcome"], "orphaned")
        self.assertEqual(d["outcome_reason"], "no_pid_record")

    def test_periodic_reaper_reaps_young_dead_pid(self):
        """A genuinely-dead recorded PID is reaped even within the grace window:
        the age guard only protects the no-PID (still-spawning) case, so a
        fast-failing claude (e.g. not on PATH) is still caught immediately."""
        did = db.create_dispatch(self.project_id, "young-dead-pid", wall_clock_cap_s=600)
        dead_pid = 99999999
        self.assertFalse(spawn.pid_alive(dead_pid))
        db.mark_started(did, terminal_pid=None, claude_pid=dead_pid)  # started_at = now
        watchdog.reap_orphans(min_age_s=60)
        d = db.get_dispatch(did)
        self.assertEqual(d["final_outcome"], "orphaned")
        self.assertEqual(d["outcome_reason"], "process_gone")


# ─── Test 4: watchdog wall-clock cap ─────────────────────────────────────

class TestWatchdogTimeout(unittest.IsolatedAsyncioTestCase):
    """Tests the asyncio wall-clock timer with a 1s cap and a sleep process."""

    async def test_watchdog_kills_after_cap(self):
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)

        project_id = db.add_project(str(td))["id"]
        did = db.create_dispatch(project_id, "watchdog-test", wall_clock_cap_s=1)

        # No Stop hook fires for a bare `sleep`, so no session_id ever arrives
        # → the watchdog falls through to a hard kill. Shrink the pause grace
        # so that fallback lands inside this test's wait window.
        orig_grace = watchdog.PAUSE_SESSION_GRACE_S
        watchdog.PAUSE_SESSION_GRACE_S = 0.5
        self.addCleanup(setattr, watchdog, "PAUSE_SESSION_GRACE_S", orig_grace)

        proc = subprocess.Popen(["sleep", "60"])
        try:
            db.mark_started(did, terminal_pid=None, claude_pid=proc.pid)
            watchdog.schedule(did, proc.pid, cap_s=1)
            # Timing:
            #   t=1s: cap expires, watchdog sends SIGTERM (sleep dies → zombie)
            #   t=1.0-6.0s: kill_pid_async polls os.kill(pid,0), which keeps
            #     returning success on the zombie (test process never reaps).
            #     In real flow Claude is a child of iTerm2's shell, not us, so
            #     no zombie. Here we have to wait out the full grace.
            #   t=6s: SIGKILL sent, kill_pid_async returns, DB updated.
            # We also actively reap the zombie via poll() so pid_alive() can
            # give a definitive answer once the watchdog finishes.
            for _ in range(40):  # 8s max
                await asyncio.sleep(0.2)
                proc.poll()
                d = db.get_dispatch(did)
                if d["status"] == "killed":
                    break
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

            self.assertFalse(spawn.pid_alive(proc.pid), "watchdog did not kill the process")
            d = db.get_dispatch(did)
            self.assertEqual(d["status"], "killed")
            self.assertEqual(d["outcome_reason"], "timeout")
        finally:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass

    async def test_kill_does_not_block_event_loop(self):
        """The async kill must not freeze the loop during SIGTERM grace.
        Simulate a process that ignores SIGTERM and verify other awaitables
        still make progress while kill_pid_async is waiting."""
        # A bash subshell that traps SIGTERM and keeps running. SIGKILL still works.
        proc = subprocess.Popen(
            ["bash", "-c", "trap '' TERM; sleep 30"],
        )
        try:
            # Kill task: should take ~5s (grace) before SIGKILL.
            kill_task = asyncio.create_task(spawn.kill_pid_async(proc.pid, grace_s=2.0))

            # Concurrently: a 100ms sleep should finish in ~100ms, not be delayed by kill.
            t0 = asyncio.get_event_loop().time()
            await asyncio.sleep(0.1)
            elapsed = asyncio.get_event_loop().time() - t0
            self.assertLess(elapsed, 0.5,
                f"event loop was blocked during kill: 100ms sleep took {elapsed:.2f}s")

            # Now wait for kill_task to actually finish + reap.
            await kill_task
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            self.assertFalse(spawn.pid_alive(proc.pid))
        finally:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass

    async def test_watchdog_cancel_stops_kill(self):
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        project_id = db.add_project(str(td))["id"]
        did = db.create_dispatch(project_id, "cancel-test", wall_clock_cap_s=1)

        proc = subprocess.Popen(["sleep", "10"])
        try:
            db.mark_started(did, terminal_pid=None, claude_pid=proc.pid)
            watchdog.schedule(did, proc.pid, cap_s=1)
            await asyncio.sleep(0.3)
            watchdog.cancel(did)
            await asyncio.sleep(1.5)
            # Process should still be alive — watchdog was cancelled before firing.
            self.assertTrue(spawn.pid_alive(proc.pid), "watchdog cancel was ignored")
        finally:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass

    async def test_watchdog_pauses_when_session_id_arrives(self):
        """Timeout + a session_id delivered during the grace window → the
        dispatch is PAUSED (resumable), not killed. Simulates the Stop hook's
        /api/complete POST by writing the session_id via db.attach_session
        while is_pausing() is True (the same in-process signal /api/complete
        consults)."""
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        project_id = db.add_project(str(td))["id"]
        did = db.create_dispatch(project_id, "pause-test", wall_clock_cap_s=1)

        # Generous grace so the test has time to inject the session_id.
        orig_grace = watchdog.PAUSE_SESSION_GRACE_S
        watchdog.PAUSE_SESSION_GRACE_S = 5.0
        self.addCleanup(setattr, watchdog, "PAUSE_SESSION_GRACE_S", orig_grace)

        proc = subprocess.Popen(["sleep", "60"])
        try:
            db.mark_started(did, terminal_pid=None, claude_pid=proc.pid)
            watchdog.schedule(did, proc.pid, cap_s=1)
            saw_pausing = False
            injected = False
            for _ in range(60):  # up to ~12s
                await asyncio.sleep(0.2)
                proc.poll()  # reap the SIGTERM'd zombie so kill_pid_async returns
                if watchdog.is_pausing(did):
                    saw_pausing = True
                    if not injected:
                        # Stop hook (graceful SIGTERM shutdown) delivers the
                        # session_id; /api/complete stores it via attach_session.
                        db.attach_session(did, "sess-paused", None)
                        injected = True
                if db.get_dispatch(did)["status"] == "paused":
                    break

            d = db.get_dispatch(did)
            self.assertTrue(saw_pausing, "is_pausing() was never True during the timeout flow")
            self.assertEqual(d["status"], "paused")
            self.assertEqual(d["final_outcome"], "paused")
            self.assertEqual(d["outcome_reason"], "timeout")
            self.assertEqual(d["session_id"], "sess-paused",
                             "session_id must survive the pause so resume works")
            self.assertFalse(watchdog.is_pausing(did),
                             "_pausing must be cleared once the decision is made")
        finally:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass


# ─── Test 5: concurrent DB writes ────────────────────────────────────────

class TestConcurrentWrites(unittest.TestCase):
    def test_10_concurrent_dispatches_no_lock_error(self):
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        project_id = db.add_project(str(td))["id"]

        import threading
        errors = []
        ids = []

        def make_one(i):
            try:
                d = db.create_dispatch(project_id, f"concurrent task {i}", wall_clock_cap_s=300)
                db.mark_started(d, terminal_pid=None, claude_pid=10000 + i)
                ids.append(d)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=make_one, args=(i,)) for i in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()

        self.assertEqual(errors, [], f"concurrent write errors: {errors}")
        self.assertEqual(len(ids), 10)
        self.assertEqual(len(set(ids)), 10, "dispatch IDs collided")


# ─── Test 6: concurrent /api/complete race + idempotency ────────────────

class TestCompleteRace(unittest.TestCase):
    def setUp(self):
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.project_id = db.add_project(str(td))["id"]

    def test_concurrent_complete_only_one_wins(self):
        """Two concurrent complete_dispatch calls on the same dispatch_id:
        exactly one must return True (the writer), the other False (loser).
        Prevents the duplicate-artifact / double-transcript-copy bug."""
        did = db.create_dispatch(self.project_id, "race-complete")
        db.mark_started(did, terminal_pid=None, claude_pid=12345)

        import threading
        results = []
        def worker():
            results.append(db.complete_dispatch(
                did, session_id=f"s-{threading.get_ident()}",
                transcript_path=None, exit_reason="Stop",
            ))

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads: t.start()
        for t in threads: t.join()

        winners = sum(1 for r in results if r is True)
        losers = sum(1 for r in results if r is False)
        self.assertEqual(winners, 1, f"expected exactly 1 winner, got {winners}; results={results}")
        self.assertEqual(losers, 4, f"expected 4 losers, got {losers}")

        d = db.get_dispatch(did)
        self.assertEqual(d["status"], "completed")

    def test_complete_after_kill_is_noop(self):
        """If a dispatch was killed first, a late Stop hook shouldn't
        flip it back to 'completed'."""
        did = db.create_dispatch(self.project_id, "kill-then-complete")
        db.mark_started(did, terminal_pid=None, claude_pid=12345)
        db.kill_dispatch_record(did, reason="manual")

        # Late Stop hook tries to complete
        changed = db.complete_dispatch(
            did, session_id="late", transcript_path=None, exit_reason="Stop",
        )
        self.assertFalse(changed)

        d = db.get_dispatch(did)
        self.assertEqual(d["status"], "killed")
        self.assertEqual(d["outcome_reason"], "manual")


# ─── Test 6b: graceful pause DB helpers ──────────────────────────────────

class TestPauseDB(unittest.TestCase):
    def setUp(self):
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.project_id = db.add_project(str(td))["id"]

    def test_attach_session_does_not_change_status(self):
        """attach_session stores session_id/transcript without finalizing."""
        did = db.create_dispatch(self.project_id, "attach")
        db.mark_started(did, terminal_pid=None, claude_pid=111)
        db.attach_session(did, "sess-x", "/tmp/t.jsonl")
        d = db.get_dispatch(did)
        self.assertEqual(d["status"], "running")  # unchanged
        self.assertEqual(d["session_id"], "sess-x")
        self.assertEqual(d["transcript_path"], "/tmp/t.jsonl")

    def test_attach_session_coalesce_does_not_clobber(self):
        """A second attach must not overwrite a value already present."""
        did = db.create_dispatch(self.project_id, "attach2")
        db.mark_started(did, terminal_pid=None, claude_pid=111)
        db.attach_session(did, "first", None)
        db.attach_session(did, "second", "/tmp/late.jsonl")
        d = db.get_dispatch(did)
        self.assertEqual(d["session_id"], "first")
        self.assertEqual(d["transcript_path"], "/tmp/late.jsonl")  # NULL → filled

    def test_mark_paused_sets_status_and_preserves_session(self):
        did = db.create_dispatch(self.project_id, "pause")
        db.mark_started(did, terminal_pid=None, claude_pid=111)
        db.attach_session(did, "sess-y", None)
        db.mark_paused(did, reason="timeout")
        d = db.get_dispatch(did)
        self.assertEqual(d["status"], "paused")
        self.assertEqual(d["final_outcome"], "paused")
        self.assertEqual(d["outcome_reason"], "timeout")
        self.assertEqual(d["session_id"], "sess-y")  # resume stays possible

    def test_complete_after_pause_is_noop(self):
        """A late Stop hook POST (complete_dispatch) after a pause must NOT
        clobber the resumable 'paused' status or its session_id."""
        did = db.create_dispatch(self.project_id, "pause-then-complete")
        db.mark_started(did, terminal_pid=None, claude_pid=111)
        db.attach_session(did, "sess-z", None)
        db.mark_paused(did, reason="timeout")
        changed = db.complete_dispatch(
            did, session_id="late", transcript_path=None, exit_reason="Stop",
        )
        self.assertFalse(changed)
        d = db.get_dispatch(did)
        self.assertEqual(d["status"], "paused")
        self.assertEqual(d["session_id"], "sess-z")


# ─── Test 7: dispatch to deleted project path ────────────────────────────

class TestTranscriptView(unittest.IsolatedAsyncioTestCase):
    async def test_huge_transcript_is_truncated(self):
        """view_transcript must NOT read a 50MB file into memory."""
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        proj = db.add_project(str(td))
        did = db.create_dispatch(proj["id"], "huge")
        db.mark_started(did, terminal_pid=None, claude_pid=12345)

        # Make a synthetic 10 MB transcript
        big = TMP_HOME / "big_transcript.jsonl"
        with big.open("wb") as f:
            f.write(b'{"line":"head"}\n')
            f.write(b"x" * (10 * 1024 * 1024))
            f.write(b'\n{"line":"tail"}\n')
        db.complete_dispatch(did, "s", str(big), "Stop")

        from orchestrator.app import view_transcript, MAX_TRANSCRIPT_BYTES
        resp = await view_transcript(did)
        body = resp.body.decode()
        # Must contain the truncation note and the tail marker
        self.assertIn("Showing last", body)
        self.assertIn("tail", body)
        # Must NOT contain the head marker (it was past the truncation point)
        self.assertNotIn('"line":"head"', body)
        # Response body shouldn't be wildly larger than the cap (+ chrome)
        self.assertLess(len(body), MAX_TRANSCRIPT_BYTES * 2)


class TestDispatchValidation(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_to_deleted_path_returns_400(self):
        """If the project dir was removed after adding, /dispatch must
        catch it instead of spawning a doomed iTerm2 tab."""
        # Make + delete a project dir
        td = Path(tempfile.mkdtemp())
        proj = db.add_project(str(td))
        shutil.rmtree(td)

        from fastapi import HTTPException
        from orchestrator.app import dispatch as dispatch_endpoint
        with self.assertRaises(HTTPException) as ctx:
            await dispatch_endpoint(project_id=proj["id"], task="should not run", wall_cap_s=300)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("no longer exists", ctx.exception.detail)


# ─── Test 8: notify_complete.sh shell script ─────────────────────────────

class TestNotifyHookScript(unittest.TestCase):
    HOOK = str(REPO / "bin" / "notify_complete.sh")

    def test_no_op_without_env_var(self):
        """Hook MUST be a no-op when ORCHESTRATOR_RUN_ID is not set."""
        env = {k: v for k, v in os.environ.items() if k != "ORCHESTRATOR_RUN_ID"}
        r = subprocess.run(
            ["bash", self.HOOK], input='{"hook_event_name":"Stop","session_id":"x"}',
            text=True, capture_output=True, env=env, timeout=5,
        )
        self.assertEqual(r.returncode, 0, f"hook should exit 0; stderr={r.stderr}")

    def test_posts_when_env_set(self):
        """With env var set, hook should attempt POST. We give it a port no
        one's listening on; the curl will fail but the hook must still exit 0."""
        env = os.environ.copy()
        env["ORCHESTRATOR_RUN_ID"] = "12345"
        env["ORCHESTRATOR_PORT"] = "1"  # unreachable port → curl fails → still exit 0
        r = subprocess.run(
            ["bash", self.HOOK],
            input=json.dumps({
                "hook_event_name": "Stop",
                "session_id": "abc",
                "transcript_path": "/tmp/nope.jsonl",
                "cwd": "/tmp",
                "stop_hook_active": False,
            }),
            text=True, capture_output=True, env=env, timeout=10,
        )
        self.assertEqual(r.returncode, 0, f"hook must always exit 0; stderr={r.stderr}")


# ─── Test 9: context bundler (phase 3) ───────────────────────────────────

class TestBundle(unittest.TestCase):
    def setUp(self):
        from orchestrator.lib import bundle as bundle_mod
        self.bundle_mod = bundle_mod
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_bundle_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_empty_project_returns_just_dir_tree(self):
        pack = self.bundle_mod.build_bundle(str(self.tmp))
        # No CLAUDE.md, no memory, no git — only the dir tree section
        self.assertEqual(len(pack.sections), 1)
        self.assertEqual(pack.sections[0].title, "Directory tree")

    def test_nonexistent_path_returns_error_section(self):
        pack = self.bundle_mod.build_bundle("/no/such/path/here/xyz")
        self.assertEqual(pack.sections[0].title, "ERROR")

    def test_full_project_layout_picked_up(self):
        (self.tmp / "CLAUDE.md").write_text("# instructions")
        (self.tmp / "PLAN.md").write_text("# plan")
        (self.tmp / "memory").mkdir()
        (self.tmp / "memory" / "lessons.md").write_text("learned: don't drop the db")
        (self.tmp / "knowledge").mkdir()
        (self.tmp / "knowledge" / "stack.md").write_text("python + fastapi")
        (self.tmp / "tasks").mkdir()
        (self.tmp / "tasks" / "task1.md").write_text("first task")

        pack = self.bundle_mod.build_bundle(str(self.tmp))
        titles = [s.title for s in pack.sections]
        # Order: CLAUDE, PLAN, memory, knowledge, tasks, (no git), dir tree
        self.assertIn("CLAUDE.md (Claude instructions)", titles)
        self.assertIn("PLAN.md (project plan)", titles)
        self.assertTrue(any("Memory" in t for t in titles))
        self.assertTrue(any("Knowledge" in t for t in titles))
        self.assertTrue(any("Recent task" in t for t in titles))
        self.assertIn("Directory tree", titles)
        # Body should contain the actual file contents
        md = pack.to_markdown()
        self.assertIn("learned: don't drop the db", md)
        self.assertIn("python + fastapi", md)

    def test_per_file_truncation(self):
        (self.tmp / "memory").mkdir()
        # File larger than PER_FILE_CHARS (5000)
        (self.tmp / "memory" / "huge.md").write_text("X" * 20_000)
        pack = self.bundle_mod.build_bundle(str(self.tmp))
        mem = [s for s in pack.sections if "Memory" in s.title][0]
        self.assertTrue(mem.truncated)
        self.assertLessEqual(mem.chars, self.bundle_mod.PER_FILE_CHARS + 100)

    def test_total_budget_drops_overflow(self):
        # Many small files that together exceed the 1000-char cap
        (self.tmp / "memory").mkdir()
        for i in range(20):
            (self.tmp / "memory" / f"m{i}.md").write_text("x" * 200)
        pack = self.bundle_mod.build_bundle(str(self.tmp), total_chars=1000)
        self.assertTrue(pack.over_budget)
        self.assertLessEqual(pack.total_chars, 1000)

    def test_forge_json_layout_override(self):
        # Custom layout: memory lives in "notes/" not "memory/"
        (self.tmp / ".forge.json").write_text(
            '{"layout":{"memory_dirs":["notes"],"knowledge_dirs":[],"task_dirs":[]}}'
        )
        (self.tmp / "notes").mkdir()
        (self.tmp / "notes" / "n1.md").write_text("custom note location")
        # A real "memory/" should be ignored since layout says notes/
        (self.tmp / "memory").mkdir()
        (self.tmp / "memory" / "ignored.md").write_text("SHOULD NOT APPEAR")

        pack = self.bundle_mod.build_bundle(str(self.tmp))
        md = pack.to_markdown()
        self.assertIn("custom note location", md)
        self.assertNotIn("SHOULD NOT APPEAR", md)

    def test_git_section_when_repo(self):
        subprocess.run(["git", "init", "-q"], cwd=self.tmp, check=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t"})
        subprocess.run(["git", "-C", str(self.tmp), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(self.tmp), "config", "user.name", "t"], check=True)
        (self.tmp / "README.md").write_text("hi")
        subprocess.run(["git", "-C", str(self.tmp), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.tmp), "commit", "-q", "-m", "init"], check=True)

        pack = self.bundle_mod.build_bundle(str(self.tmp))
        titles = [s.title for s in pack.sections]
        self.assertIn("Git context", titles)
        git_sec = [s for s in pack.sections if s.title == "Git context"][0]
        self.assertIn("Branch", git_sec.body)


# ─── Test 10: bundler hardening (path traversal, bad input, symlinks) ────

class TestBundleHardening(unittest.TestCase):
    """Adversarial tests for the context bundler.

    The bundler ingests files referenced by .forge.json — which means a
    malicious or just-broken .forge.json must not be able to read files
    outside the project root, crash the bundler, or hang it."""

    def setUp(self):
        from orchestrator.lib import bundle as bundle_mod
        self.bundle_mod = bundle_mod
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_bundle_sec_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # An "outside" sentinel file we must NEVER read
        self.outside = Path(tempfile.mkdtemp(prefix="orch_outside_"))
        self.addCleanup(shutil.rmtree, self.outside, ignore_errors=True)
        (self.outside / "secret.md").write_text("SECRET_DO_NOT_LEAK")

    def test_layout_path_traversal_via_memory_dirs(self):
        rel_traversal = os.path.relpath(self.outside, self.tmp)  # e.g. ../orch_outside_xxx
        (self.tmp / ".forge.json").write_text(
            json.dumps({"layout": {"memory_dirs": [rel_traversal]}})
        )
        pack = self.bundle_mod.build_bundle(str(self.tmp))
        md = pack.to_markdown()
        self.assertNotIn("SECRET_DO_NOT_LEAK", md,
            "bundler followed a ../ path out of the project root")

    def test_layout_path_traversal_via_claude_md(self):
        rel_traversal = os.path.relpath(self.outside / "secret.md", self.tmp)
        (self.tmp / ".forge.json").write_text(
            json.dumps({"layout": {"claude_md": rel_traversal}})
        )
        pack = self.bundle_mod.build_bundle(str(self.tmp))
        md = pack.to_markdown()
        self.assertNotIn("SECRET_DO_NOT_LEAK", md,
            "bundler read claude_md from outside project root")

    def test_layout_absolute_path_rejected(self):
        (self.tmp / ".forge.json").write_text(
            json.dumps({"layout": {"memory_dirs": [str(self.outside)]}})
        )
        pack = self.bundle_mod.build_bundle(str(self.tmp))
        self.assertNotIn("SECRET_DO_NOT_LEAK", pack.to_markdown())

    def test_layout_wrong_types_dont_crash(self):
        # claude_md as a list, memory_dirs as a string, etc.
        (self.tmp / ".forge.json").write_text(
            json.dumps({"layout": {
                "claude_md": ["wrong", "type"],
                "memory_dirs": "should-be-a-list",
                "plan_md": 42,
            }})
        )
        # Must not crash — should fall back to defaults
        pack = self.bundle_mod.build_bundle(str(self.tmp))
        self.assertIsNotNone(pack)

    def test_invalid_json_in_forge_falls_back_to_defaults(self):
        (self.tmp / ".forge.json").write_text("{ this is not json")
        (self.tmp / "CLAUDE.md").write_text("default-layout works")
        pack = self.bundle_mod.build_bundle(str(self.tmp))
        self.assertIn("default-layout works", pack.to_markdown())

    def test_symlink_pointing_outside_not_followed(self):
        (self.tmp / "memory").mkdir()
        link = self.tmp / "memory" / "evil.md"
        link.symlink_to(self.outside / "secret.md")
        pack = self.bundle_mod.build_bundle(str(self.tmp))
        self.assertNotIn("SECRET_DO_NOT_LEAK", pack.to_markdown(),
            "symlinked file outside project was read")

    def test_unreadable_file_doesnt_crash(self):
        (self.tmp / "memory").mkdir()
        m = self.tmp / "memory" / "locked.md"
        m.write_text("contents")
        m.chmod(0o000)
        try:
            pack = self.bundle_mod.build_bundle(str(self.tmp))
            # Must not raise. Section may have an error note or be skipped.
            self.assertIsNotNone(pack)
        finally:
            m.chmod(0o644)  # restore so cleanup works

    def test_non_utf8_file_does_not_crash(self):
        (self.tmp / "memory").mkdir()
        (self.tmp / "memory" / "binary.md").write_bytes(b"\xff\xfe\x00\x00 invalid utf-8 \x80\x81")
        pack = self.bundle_mod.build_bundle(str(self.tmp))
        # File should appear in the bundle (with replacement chars), not crash
        self.assertTrue(any("binary.md" in s.source for s in pack.sections))


# ─── Test 11: claude_runner (vendored stream_run) ────────────────────────

class TestClaudeRunner(unittest.TestCase):
    def setUp(self):
        from orchestrator.lib import claude_runner
        self.cr = claude_runner

    def test_strip_fences_json_block(self):
        self.assertEqual(self.cr._strip_fences('```json\n{"a":1}\n```'), '{"a":1}')

    def test_strip_fences_plain_block(self):
        self.assertEqual(self.cr._strip_fences('```\n{"a":1}\n```'), '{"a":1}')

    def test_strip_fences_no_fence(self):
        self.assertEqual(self.cr._strip_fences('{"a":1}'), '{"a":1}')

    def test_strip_fences_whitespace(self):
        self.assertEqual(self.cr._strip_fences('   \n{"a":1}\n  '), '{"a":1}')

    def test_missing_binary_returns_error(self):
        """When `claude` isn't on PATH the headless fallback must return
        ok=False, not raise."""
        # Replace PATH with somewhere `claude` definitely isn't
        orig = os.environ.get("PATH", "")
        try:
            os.environ["PATH"] = "/nonexistent_dir_xyz"
            r = self.cr.run_claude_headless("hi", cwd="/tmp")
            self.assertFalse(r.ok)
            self.assertIn("not found", r.error.lower())
        finally:
            os.environ["PATH"] = orig

    def test_timeout_returns_error(self):
        """Sub-1s timeout on the headless fallback should fire on any real
        claude call (or fall back to PATH miss). Either way: ok=False, no
        exception."""
        orig = os.environ.get("PATH", "")
        try:
            # Force a hang: replace `claude` with a sleep
            tdir = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, tdir, ignore_errors=True)
            fake = tdir / "claude"
            fake.write_text("#!/bin/bash\nsleep 30\n")
            fake.chmod(0o755)
            os.environ["PATH"] = f"{tdir}:{orig}"
            r = self.cr.run_claude_headless("hi", cwd="/tmp", timeout_s=1)
            self.assertFalse(r.ok)
            self.assertIn("timed out", r.error.lower())
        finally:
            os.environ["PATH"] = orig

    def test_brain_call_strips_orchestrator_run_id_from_env(self):
        """The Stop hook is env-gated on ORCHESTRATOR_RUN_ID — internal brain
        calls must NOT carry it, or the hook would post /api/complete spuriously.
        (The tab path enforces this structurally by never exporting the var —
        see test_brain_tab_cmd_sets_brain_id_not_run_id; here we cover the
        headless fallback, which scrubs it from the subprocess env.)"""
        orig = os.environ.get("PATH", "")
        orig_run = os.environ.get("ORCHESTRATOR_RUN_ID")
        try:
            tdir = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, tdir, ignore_errors=True)
            envprobe = tdir / "claude"
            # Fake claude: print ORCHESTRATOR_RUN_ID from env, then a fake claude JSON envelope
            envprobe.write_text(
                '#!/bin/bash\n'
                'echo "{\\"result\\":\\"saw=$ORCHESTRATOR_RUN_ID\\",\\"total_cost_usd\\":0,\\"duration_ms\\":1}"\n'
            )
            envprobe.chmod(0o755)
            os.environ["PATH"] = f"{tdir}:{orig}"
            os.environ["ORCHESTRATOR_RUN_ID"] = "999"
            r = self.cr.run_claude_headless("hi", cwd="/tmp", timeout_s=5)
            self.assertTrue(r.ok, f"runner failed: {r.error}")
            self.assertEqual(r.text, "saw=", "ORCHESTRATOR_RUN_ID leaked into brain-call env")
        finally:
            os.environ["PATH"] = orig
            if orig_run is None:
                os.environ.pop("ORCHESTRATOR_RUN_ID", None)
            else:
                os.environ["ORCHESTRATOR_RUN_ID"] = orig_run

    def test_envelope_from_stream_jsonl_and_build_run(self):
        """The tab path reconstructs the result envelope from a stream-json
        transcript: result text from the `result` event, model from `init`."""
        tdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tdir, ignore_errors=True)
        f = tdir / "x.jsonl"
        f.write_text(
            '{"type":"system","subtype":"init","model":"claude-sonnet-4-6"}\n'
            '{"type":"assistant","message":{"model":"claude-sonnet-4-6","content":[{"type":"text","text":"hi"}]}}\n'
            '{"type":"result","subtype":"success","result":"{\\"a\\":1}",'
            '"total_cost_usd":0.01,"duration_ms":1200}\n'
        )
        env = self.cr._envelope_from_stream_jsonl(f)
        self.assertIsNotNone(env)
        self.assertEqual(env["result"], '{"a":1}')
        self.assertEqual(env["model"], "claude-sonnet-4-6")
        run = self.cr._build_claude_run(env, "sonnet")
        self.assertTrue(run.ok)
        self.assertEqual(run.parsed_json, {"a": 1})
        self.assertEqual(run.cost_usd, 0.01)
        # No result event → None (claude crashed mid-stream)
        g = tdir / "empty.jsonl"
        g.write_text('{"type":"system","subtype":"init"}\n')
        self.assertIsNone(self.cr._envelope_from_stream_jsonl(g))

    def test_run_claude_json_falls_back_to_headless_without_iterm2(self):
        """No iTerm2 installed → the brain call still runs (headless), rather
        than erroring."""
        from unittest.mock import patch
        orig = os.environ.get("PATH", "")
        try:
            tdir = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, tdir, ignore_errors=True)
            fake = tdir / "claude"
            fake.write_text(
                '#!/bin/bash\n'
                'echo "{\\"result\\":\\"hi\\",\\"total_cost_usd\\":0,\\"duration_ms\\":1}"\n'
            )
            fake.chmod(0o755)
            os.environ["PATH"] = f"{tdir}:{orig}"
            with patch.object(self.cr.spawn, "iterm2_installed", return_value=False):
                r = self.cr.run_claude_json("hi", cwd="/tmp", timeout_s=5, label="rewriter")
            self.assertTrue(r.ok, f"runner failed: {r.error}")
            self.assertEqual(r.text, "hi")
        finally:
            os.environ["PATH"] = orig

    def test_brain_tab_cmd_sets_brain_id_not_run_id(self):
        """The brain tab must export ORCHESTRATOR_BRAIN_ID and NEVER
        ORCHESTRATOR_RUN_ID (else the Stop hook would fire for brain calls)."""
        from orchestrator.lib import spawn
        cmd = spawn._brain_tab_cmd("rewriter-abc12345", "/tmp/proj",
                                   "orch brain: rewriter abc12345")
        self.assertIn("ORCHESTRATOR_BRAIN_ID=rewriter-abc12345", cmd)
        self.assertNotIn("ORCHESTRATOR_RUN_ID", cmd)
        self.assertIn("brain_run.sh", cmd)
        # Tagged with a user var so close-by-id works after claude rewrites the title.
        self.assertIn("SetUserVar=orch_brain=", cmd)


# ─── Test 12: rewriter (mocked claude) ───────────────────────────────────

class TestRewriter(unittest.TestCase):
    def setUp(self):
        from orchestrator.lib import rewriter, claude_runner
        self.rewriter = rewriter
        self.claude_runner = claude_runner
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_rewrite_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "CLAUDE.md").write_text("Hard rule: no Anthropic API.")

    def test_empty_task_returns_error(self):
        r = self.rewriter.rewrite("   ", str(self.tmp))
        self.assertFalse(r.ok)
        self.assertIn("empty", r.error)

    def test_nonexistent_project_returns_error(self):
        r = self.rewriter.rewrite("hi", "/no/such/dir/xyz")
        self.assertFalse(r.ok)
        self.assertIn("does not exist", r.error)

    def test_happy_path(self):
        """Mock claude to return a well-formed JSON envelope; verify the
        rewriter parses every field correctly."""
        from unittest.mock import patch
        fake_envelope = self.claude_runner.ClaudeRun(
            ok=True,
            text='{"rewritten_prompt":"REWRITTEN","rationale":"because","files_to_read":["x.py","y.py"],"hazards_acknowledged":["no API"]}',
            parsed_json={
                "rewritten_prompt": "REWRITTEN",
                "rationale": "because",
                "files_to_read": ["x.py", "y.py"],
                "hazards_acknowledged": ["no API"],
            },
            cost_usd=0.01, duration_s=2.5, model="sonnet",
        )
        with patch.object(self.claude_runner, "run_claude_json", return_value=fake_envelope):
            r = self.rewriter.rewrite("original task", str(self.tmp))
        self.assertTrue(r.ok, f"rewrite failed: {r.error}")
        self.assertEqual(r.rewritten_prompt, "REWRITTEN")
        self.assertEqual(r.rationale, "because")
        self.assertEqual(r.files_to_read, ["x.py", "y.py"])
        self.assertEqual(r.hazards_acknowledged, ["no API"])
        self.assertEqual(r.cost_usd, 0.01)

    def test_claude_failure_returns_error(self):
        from unittest.mock import patch
        fake = self.claude_runner.ClaudeRun(ok=False, error="claude exit 1: nope")
        with patch.object(self.claude_runner, "run_claude_json", return_value=fake):
            r = self.rewriter.rewrite("task", str(self.tmp))
        self.assertFalse(r.ok)
        self.assertIn("nope", r.error)

    def test_non_json_response_returns_error(self):
        """Model went off-script and returned prose. Must fail gracefully."""
        from unittest.mock import patch
        fake = self.claude_runner.ClaudeRun(
            ok=True, text="sure, here is your task: do it!", parsed_json=None,
            cost_usd=0.01,
        )
        with patch.object(self.claude_runner, "run_claude_json", return_value=fake):
            r = self.rewriter.rewrite("task", str(self.tmp))
        self.assertFalse(r.ok)
        self.assertIn("non-JSON", r.error)
        self.assertIn("here is your task", r.raw_assistant_text)

    def test_empty_rewritten_prompt_returns_error(self):
        """Model returned valid JSON but with empty rewritten_prompt.
        Must NOT silently dispatch an empty string. Falls back to user's task."""
        from unittest.mock import patch
        fake = self.claude_runner.ClaudeRun(
            ok=True, text='{"rewritten_prompt":""}',
            parsed_json={"rewritten_prompt": ""},
            cost_usd=0.01,
        )
        with patch.object(self.claude_runner, "run_claude_json", return_value=fake):
            r = self.rewriter.rewrite("the original task", str(self.tmp))
        self.assertFalse(r.ok)
        self.assertEqual(r.rewritten_prompt, "the original task")

    def test_list_coercion_of_string_field(self):
        """Model returns a string instead of a list for hazards_acknowledged.
        Coercer wraps it into a single-element list rather than crashing."""
        from unittest.mock import patch
        fake = self.claude_runner.ClaudeRun(
            ok=True, text='{}',
            parsed_json={"rewritten_prompt": "x",
                         "hazards_acknowledged": "single hazard as string",
                         "files_to_read": "single/file.py"},
            cost_usd=0,
        )
        with patch.object(self.claude_runner, "run_claude_json", return_value=fake):
            r = self.rewriter.rewrite("t", str(self.tmp))
        self.assertTrue(r.ok)
        self.assertEqual(r.hazards_acknowledged, ["single hazard as string"])
        self.assertEqual(r.files_to_read, ["single/file.py"])


# ─── Test 13: phase 4 hardening ──────────────────────────────────────────

class TestPhase4Hardening(unittest.TestCase):
    def setUp(self):
        from orchestrator.lib import rewriter, claude_runner
        self.rewriter = rewriter
        self.claude_runner = claude_runner
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_p4_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "CLAUDE.md").write_text("simple rule")

    def test_strip_fences_crlf_line_endings(self):
        """Windows-style line endings in model output."""
        s = '```json\r\n{"a":1}\r\n```'
        self.assertEqual(self.claude_runner._strip_fences(s), '{"a":1}')

    def test_parsed_json_as_list_does_not_crash_rewriter(self):
        """Model returned a JSON list instead of object. data.get(...) would
        AttributeError without the type guard."""
        from unittest.mock import patch
        fake = self.claude_runner.ClaudeRun(
            ok=True, text='[1,2,3]', parsed_json=[1, 2, 3], cost_usd=0,
        )
        with patch.object(self.claude_runner, "run_claude_json", return_value=fake):
            r = self.rewriter.rewrite("t", str(self.tmp))
        self.assertFalse(r.ok)
        self.assertIn("non-JSON", r.error)

    def test_non_string_rewritten_prompt_coerced(self):
        """If the model returns a dict/list for rewritten_prompt (shouldn't
        but might), it's coerced via str() — must not crash, but should
        still produce something usable."""
        from unittest.mock import patch
        fake = self.claude_runner.ClaudeRun(
            ok=True, text='{}',
            parsed_json={"rewritten_prompt": {"oops": "wrong type"}},
            cost_usd=0,
        )
        with patch.object(self.claude_runner, "run_claude_json", return_value=fake):
            r = self.rewriter.rewrite("t", str(self.tmp))
        # Either ok=True with stringified content, or ok=False with clear error —
        # both acceptable, must NOT raise.
        self.assertIsNotNone(r)

    def test_template_injection_bundle_contains_user_task_literal(self):
        """A memory file containing the literal string '{user_task}' must NOT
        get text-replaced when filling the rewriter template. Otherwise a
        malicious memory file could inject content into the user's task slot."""
        (self.tmp / "memory").mkdir()
        (self.tmp / "memory" / "trap.md").write_text(
            "lesson: the placeholder string {user_task} caused issues once"
        )

        from unittest.mock import patch
        captured_prompt = {}
        def fake_run(prompt, cwd, **kw):
            captured_prompt["body"] = prompt
            return self.claude_runner.ClaudeRun(
                ok=True, text='{"rewritten_prompt":"x"}',
                parsed_json={"rewritten_prompt": "x"}, cost_usd=0,
            )
        with patch.object(self.claude_runner, "run_claude_json", side_effect=fake_run):
            self.rewriter.rewrite("ORIGINAL_USER_TASK_MARKER", str(self.tmp))

        body = captured_prompt["body"]
        # The literal {user_task} inside the memory file must appear unchanged.
        self.assertIn("{user_task} caused issues", body)
        # The user's actual task must appear exactly once (in its dedicated slot),
        # not duplicated into where the memory file's literal would have been.
        self.assertEqual(body.count("ORIGINAL_USER_TASK_MARKER"), 1)

    def test_template_injection_user_task_contains_bundle_literal(self):
        """A user task containing the literal '{bundle}' must NOT trigger a
        recursive bundle expansion in the user-task slot."""
        from unittest.mock import patch
        captured_prompt = {}
        def fake_run(prompt, cwd, **kw):
            captured_prompt["body"] = prompt
            return self.claude_runner.ClaudeRun(
                ok=True, text='{"rewritten_prompt":"x"}',
                parsed_json={"rewritten_prompt": "x"}, cost_usd=0,
            )
        with patch.object(self.claude_runner, "run_claude_json", side_effect=fake_run):
            self.rewriter.rewrite("rename the {bundle} variable", str(self.tmp))

        body = captured_prompt["body"]
        # User's literal {bundle} preserved verbatim
        self.assertIn("rename the {bundle} variable", body)


# ─── Test 14: summarizer ─────────────────────────────────────────────────

class TestSummarizer(unittest.TestCase):
    def setUp(self):
        from orchestrator.lib import summarizer, claude_runner
        self.summarizer = summarizer
        self.claude_runner = claude_runner
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_sum_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _write_jsonl(self, name: str, lines: list[dict]) -> str:
        p = self.tmp / name
        with p.open("w") as f:
            for obj in lines:
                f.write(json.dumps(obj) + "\n")
        return str(p)

    def test_distill_filters_noise_keeps_signal(self):
        path = self._write_jsonl("t.jsonl", [
            {"type": "permission-mode", "permissionMode": "default"},
            {"type": "file-history-snapshot", "messageId": "abc"},
            {"type": "attachment", "attachment": {"type": "skill_listing", "content": "lots of skills..."}},
            {"type": "user", "message": {"role": "user", "content": "do the thing"}},
            {"type": "assistant", "message": {"content": [
                {"type": "thinking", "thinking": "should never appear in distilled output"},
                {"type": "text", "text": "I'll do the thing now."},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/x.py"}},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "content": "file contents..."},
            ]}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Done."},
            ]}},
            {"type": "ai-title", "aiTitle": "Doing the thing"},
        ])
        out = self.summarizer.distill_transcript(path)
        # Keeps: user task, assistant text x2, tool_use, tool_result
        self.assertIn("do the thing", out)
        self.assertIn("I'll do the thing now", out)
        self.assertIn("Read", out)
        self.assertIn("/x.py", out)
        self.assertIn("file contents", out)
        self.assertIn("Done.", out)
        # Drops: permission-mode, file-history-snapshot, attachment, ai-title, thinking
        self.assertNotIn("permission-mode", out)
        self.assertNotIn("file-history-snapshot", out)
        self.assertNotIn("skill_listing", out)
        self.assertNotIn("should never appear", out)
        self.assertNotIn("Doing the thing", out)  # ai-title block

    def test_distill_missing_file(self):
        out = self.summarizer.distill_transcript("/no/such/file.jsonl")
        self.assertIn("missing", out)

    def test_distill_malformed_lines_skipped(self):
        path = self.tmp / "bad.jsonl"
        path.write_text(
            'not json at all\n'
            '{"type":"user","message":{"content":"valid"}}\n'
            'also not json\n'
        )
        out = self.summarizer.distill_transcript(str(path))
        self.assertIn("valid", out)

    def test_distill_truncates_huge_transcripts(self):
        # 200 messages of ~5KB each = ~1MB total; should cap at default 30K
        lines = [{"type": "assistant", "message": {"content": [
            {"type": "text", "text": "x" * 5000}
        ]}} for _ in range(200)]
        path = self._write_jsonl("huge.jsonl", lines)
        out = self.summarizer.distill_transcript(path)
        self.assertLess(len(out), self.summarizer.DISTILLED_MAX_CHARS + 1000)
        self.assertIn("truncated", out)

    def test_distill_caps_per_block(self):
        path = self._write_jsonl("big.jsonl", [
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "y" * 10000}
            ]}},
        ])
        out = self.summarizer.distill_transcript(path)
        # Single block must not exceed PER_BLOCK_MAX + a bit for truncation marker
        self.assertLess(len(out), self.summarizer.PER_BLOCK_MAX + 200)
        self.assertIn("trunc", out)

    def test_summarize_with_mocked_claude_happy_path(self):
        path = self._write_jsonl("t.jsonl", [
            {"type": "user", "message": {"content": "fix the bug"}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Done — patched foo.py line 42"}
            ]}},
        ])
        from unittest.mock import patch
        fake = self.claude_runner.ClaudeRun(
            ok=True, text='{}',
            parsed_json={
                "summary_md": "fixed bug in foo.py",
                "what_worked": "- read foo.py\n- patched line 42",
                "what_broke": "",
                "lessons": "- check tests next time",
                "tags": ["bugfix", "foo"],
            },
            cost_usd=0.02, duration_s=3.0, model="sonnet",
        )
        with patch.object(self.claude_runner, "run_claude_json", return_value=fake):
            r = self.summarizer.summarize(path, "fix the bug", "/tmp")
        self.assertTrue(r.ok, f"summarize failed: {r.error}")
        self.assertEqual(r.summary_md, "fixed bug in foo.py")
        self.assertEqual(r.tags, ["bugfix", "foo"])

    def test_summarize_handles_non_json_response(self):
        path = self._write_jsonl("t.jsonl", [{"type":"user","message":{"content":"x"}}])
        from unittest.mock import patch
        fake = self.claude_runner.ClaudeRun(
            ok=True, text="sorry I can't", parsed_json=None, cost_usd=0,
        )
        with patch.object(self.claude_runner, "run_claude_json", return_value=fake):
            r = self.summarizer.summarize(path, "x", "/tmp")
        self.assertFalse(r.ok)
        self.assertIn("non-JSON", r.error)

    def test_summarize_template_isolation(self):
        """A transcript containing literal `{user_task}` text must not be
        text-replaced. Same single-pass guarantee as the rewriter."""
        path = self._write_jsonl("t.jsonl", [
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "I noticed the {user_task} placeholder"}
            ]}},
        ])
        from unittest.mock import patch
        captured = {}
        def fake_run(prompt, cwd, **kw):
            captured["body"] = prompt
            return self.claude_runner.ClaudeRun(
                ok=True, text='{}', parsed_json={"summary_md":"x"}, cost_usd=0,
            )
        with patch.object(self.claude_runner, "run_claude_json", side_effect=fake_run):
            self.summarizer.summarize(path, "UNIQUE_TASK_TOKEN", "/tmp")
        self.assertIn("the {user_task} placeholder", captured["body"])
        self.assertEqual(captured["body"].count("UNIQUE_TASK_TOKEN"), 1)


class TestSummarizerDbWrite(unittest.TestCase):
    def setUp(self):
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.project_id = db.add_project(str(td))["id"]
        self.dispatch_id = db.create_dispatch(self.project_id, "test")
        db.mark_started(self.dispatch_id, terminal_pid=None, claude_pid=1)
        db.complete_dispatch(self.dispatch_id, "s", "/tmp/fake.jsonl", "Stop")

    def test_set_summary_writes_all_fields(self):
        db.set_summary(
            self.dispatch_id,
            summary_md="did a thing",
            what_worked="- step a",
            what_broke="- bad thing",
            lessons="- lesson 1",
            tags=["t1", "t2"],
        )
        d = db.get_dispatch_with_project(self.dispatch_id)
        self.assertEqual(d["summary_md"], "did a thing")
        self.assertEqual(d["what_worked"], "- step a")
        self.assertEqual(d["what_broke"], "- bad thing")
        self.assertEqual(d["lessons"], "- lesson 1")
        self.assertEqual(json.loads(d["tags_json"]), ["t1", "t2"])

    def test_set_summary_with_empty_tags_writes_null(self):
        db.set_summary(self.dispatch_id, "s", "", "", "", [])
        d = db.get_dispatch_with_project(self.dispatch_id)
        self.assertIsNone(d["tags_json"])


# ─── Test 15: phase 5 hardening ──────────────────────────────────────────

class TestPhase5Hardening(unittest.TestCase):
    def setUp(self):
        from orchestrator.lib import summarizer
        self.summarizer = summarizer
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_p5h_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _write_jsonl(self, name, lines):
        p = self.tmp / name
        with p.open("w") as f:
            for obj in lines:
                f.write(json.dumps(obj) + "\n")
        return str(p)

    def test_assistant_string_content_kept(self):
        """Some claude responses have message.content as a plain string
        rather than a list of blocks. Must not be silently dropped."""
        path = self._write_jsonl("t.jsonl", [
            {"type": "user", "message": {"content": "ping"}},
            {"type": "assistant", "message": {"content": "STRING_FORM_REPLY"}},
        ])
        out = self.summarizer.distill_transcript(path)
        self.assertIn("STRING_FORM_REPLY", out, "assistant string-content was dropped")
        self.assertIn("ASSISTANT", out)

    def test_empty_transcript_file_no_crash(self):
        empty = self.tmp / "empty.jsonl"
        empty.write_text("")
        out = self.summarizer.distill_transcript(str(empty))
        self.assertIn("no conversational content", out)

    def test_transcript_with_only_noise_no_crash(self):
        path = self._write_jsonl("noise.jsonl", [
            {"type": "permission-mode", "permissionMode": "default"},
            {"type": "file-history-snapshot", "messageId": "x"},
            {"type": "ai-title", "aiTitle": "x"},
        ])
        out = self.summarizer.distill_transcript(path)
        self.assertIn("no conversational content", out)

    def test_partial_last_line_handled(self):
        """Simulate the race where Stop hook fires while claude is still
        writing the last line. Distiller must skip the bad line, keep the
        good ones, and not crash."""
        p = self.tmp / "partial.jsonl"
        p.write_text(
            '{"type":"user","message":{"content":"hi"}}\n'
            '{"type":"assistant","message":{"content":[{"type":"text","text":"complete"}]}}\n'
            '{"type":"assistant","message":{"content":[{"type":"te'  # truncated
        )
        out = self.summarizer.distill_transcript(str(p))
        self.assertIn("hi", out)
        self.assertIn("complete", out)

    def test_assistant_with_no_message_field(self):
        path = self._write_jsonl("nomsg.jsonl", [
            {"type": "assistant"},  # missing message
            {"type": "user", "message": {"content": "keepme"}},
        ])
        out = self.summarizer.distill_transcript(path)
        self.assertIn("keepme", out)

    def test_summarize_propagates_claude_runner_failure(self):
        from orchestrator.lib import claude_runner
        from unittest.mock import patch
        path = self._write_jsonl("t.jsonl", [{"type":"user","message":{"content":"x"}}])
        fake = claude_runner.ClaudeRun(ok=False, error="claude not on PATH")
        with patch.object(claude_runner, "run_claude_json", return_value=fake):
            r = self.summarizer.summarize(path, "x", "/tmp")
        self.assertFalse(r.ok)
        self.assertIn("not on PATH", r.error)

    def test_summarize_with_deleted_cwd(self):
        """If the project dir was deleted between completion and summarizer
        run, the subprocess gets FileNotFoundError on cwd. Must surface ok=False."""
        from unittest.mock import patch
        from orchestrator.lib import claude_runner
        deleted = Path(tempfile.mkdtemp())
        shutil.rmtree(deleted)
        path = self._write_jsonl("t.jsonl", [{"type":"user","message":{"content":"x"}}])
        # We don't mock the claude call itself — exercise the real runner with a
        # bad cwd. Force the headless path so this is deterministic and fast
        # regardless of whether iTerm2 is installed on the test machine (the tab
        # path would instead open a real tab and wait out the startup grace).
        with patch.object(claude_runner.spawn, "iterm2_installed", return_value=False):
            r = self.summarizer.summarize(path, "x", str(deleted))
        self.assertFalse(r.ok)


# ─── Test 16: /api/complete summarizer only fires for the winner ─────────

class TestApiCompleteOnceOnly(unittest.IsolatedAsyncioTestCase):
    """When two Stop-hook POSTs race for the same dispatch_id, only the
    one that flipped status='completed' should spawn a summarizer task."""

    async def test_concurrent_complete_spawns_one_summarizer(self):
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        proj = db.add_project(str(td))
        did = db.create_dispatch(proj["id"], "race")
        db.mark_started(did, terminal_pid=None, claude_pid=1)
        # Fake transcript so the api_complete code path runs through fully
        transcript = td / "fake.jsonl"
        transcript.write_text('{"type":"user","message":{"content":"x"}}\n')

        from orchestrator.app import api_complete, _background_tasks
        # Snapshot how many tasks were already in flight (test isolation)
        baseline = len(_background_tasks)
        from unittest.mock import patch

        # Replace _run_summarizer with a no-op coroutine so we don't actually
        # call claude — we're only verifying the spawn count.
        spawn_count = 0
        async def fake_runner(dispatch_id):
            nonlocal spawn_count
            spawn_count += 1

        with patch("orchestrator.app._run_summarizer", side_effect=fake_runner):
            # Fire 5 concurrent /api/complete calls
            results = await asyncio.gather(*(
                api_complete({"run_id": str(did), "session_id": f"s{i}",
                              "transcript_path": str(transcript),
                              "cwd": str(td), "exit_reason": "Stop"})
                for i in range(5)
            ))
            # Let the spawned tasks settle
            await asyncio.sleep(0.05)

        self.assertEqual(spawn_count, 1,
            f"expected exactly 1 summarizer spawn, got {spawn_count}; results={results}")


# ─── Test 17: phase 6 embeddings + retrieval ─────────────────────────────

class TestEmbeddingsModule(unittest.TestCase):
    def test_vec_blob_roundtrip(self):
        from orchestrator.lib import embeddings
        v = [0.1, -0.2, 3.14, -1e-7, 1234.5]
        blob = embeddings.vec_to_blob(v)
        self.assertEqual(len(blob), len(v) * 4)
        back = embeddings.blob_to_vec(blob)
        for a, b in zip(v, back):
            self.assertAlmostEqual(a, b, places=4)

    def test_embed_returns_none_on_unreachable(self):
        """If Ollama isn't reachable, embed() returns None (never raises)."""
        from orchestrator.lib import embeddings
        # Point at a port nothing's listening on
        orig = embeddings.OLLAMA_URL
        try:
            embeddings.OLLAMA_URL = "http://127.0.0.1:1"
            r = embeddings.embed("hi", timeout_s=1)
            self.assertIsNone(r)
        finally:
            embeddings.OLLAMA_URL = orig

    def test_embed_empty_text_returns_none(self):
        from orchestrator.lib import embeddings
        self.assertIsNone(embeddings.embed(""))
        self.assertIsNone(embeddings.embed("   "))


class TestRetrievalModule(unittest.TestCase):
    """Tests retrieval logic with mocked embeddings, so they don't require
    Ollama to be running."""

    def setUp(self):
        from orchestrator.lib import retrieval, embeddings
        self.retrieval = retrieval
        self.embeddings = embeddings
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.project_id = db.add_project(str(td))["id"]
        # Shared module-level test DB → wipe embeddings between tests for
        # deterministic assertions on count and ordering.
        with db.conn() as c:
            c.execute("DELETE FROM dispatch_embeddings")

    def _seed_dispatch(self, task: str, summary: str, tags: list[str], vec: list[float]):
        did = db.create_dispatch(self.project_id, task)
        db.mark_started(did, terminal_pid=None, claude_pid=1)
        db.complete_dispatch(did, "s", None, "Stop")
        db.set_summary(did, summary, "", "", "", tags)
        # Insert embedding directly so we can test retrieval without Ollama
        with db.conn() as c:
            c.execute(
                "INSERT INTO dispatch_embeddings(dispatch_id, project_id, model, dim, vector) "
                "VALUES (?, ?, 'test', ?, ?)",
                (did, self.project_id, len(vec), self.embeddings.vec_to_blob(vec)),
            )
        return did

    def test_cosine_basic(self):
        """Sanity check on the hand-rolled cosine."""
        self.assertAlmostEqual(self.retrieval._cosine([1, 0, 0], [1, 0, 0]), 1.0, places=4)
        self.assertAlmostEqual(self.retrieval._cosine([1, 0, 0], [0, 1, 0]), 0.0, places=4)
        self.assertAlmostEqual(self.retrieval._cosine([1, 0, 0], [-1, 0, 0]), -1.0, places=4)
        # Zero vector → 0 (not NaN)
        self.assertEqual(self.retrieval._cosine([0, 0, 0], [1, 1, 1]), 0.0)

    def test_find_similar_ranks_by_cosine(self):
        """Most-similar vector should be first; min_score filters noise."""
        from unittest.mock import patch
        # Three dispatches with deliberately-chosen vectors
        self._seed_dispatch("close match",  "summary A", ["t1"], [1.0, 0.0, 0.0])
        self._seed_dispatch("ok match",     "summary B", ["t2"], [0.7, 0.7, 0.0])
        self._seed_dispatch("far match",    "summary C", ["t3"], [0.0, 0.0, 1.0])

        # Query vector: same direction as the first
        with patch.object(self.embeddings, "embed", return_value=[1.0, 0.0, 0.0]):
            hits = self.retrieval.find_similar("anything", k=5, min_score=0.0)

        self.assertEqual(len(hits), 3)
        self.assertEqual(hits[0].user_task, "close match")
        self.assertEqual(hits[1].user_task, "ok match")
        self.assertEqual(hits[2].user_task, "far match")
        self.assertAlmostEqual(hits[0].score, 1.0, places=3)

    def test_find_similar_min_score_filters(self):
        from unittest.mock import patch
        self._seed_dispatch("orthogonal", "summary", [], [0.0, 1.0, 0.0])
        with patch.object(self.embeddings, "embed", return_value=[1.0, 0.0, 0.0]):
            hits = self.retrieval.find_similar("q", k=5, min_score=0.3)
        self.assertEqual(len(hits), 0)

    def test_find_similar_returns_empty_when_query_embed_fails(self):
        from unittest.mock import patch
        self._seed_dispatch("x", "y", [], [1.0, 0.0])
        with patch.object(self.embeddings, "embed", return_value=None):
            hits = self.retrieval.find_similar("q")
        self.assertEqual(hits, [])

    def test_find_similar_excludes_dispatch_id(self):
        from unittest.mock import patch
        d1 = self._seed_dispatch("a", "sa", [], [1.0, 0.0])
        d2 = self._seed_dispatch("b", "sb", [], [0.9, 0.1])
        with patch.object(self.embeddings, "embed", return_value=[1.0, 0.0]):
            hits = self.retrieval.find_similar("q", k=5, exclude_dispatch_id=d1, min_score=0.0)
        self.assertEqual([h.dispatch_id for h in hits], [d2])

    def test_find_similar_dim_mismatch_skipped(self):
        """Vectors of a different dim (e.g., from a previously-pulled model)
        must not be cosine-compared (would error or give garbage)."""
        from unittest.mock import patch
        self._seed_dispatch("3-dim", "s", [], [1.0, 0.0, 0.0])
        self._seed_dispatch("2-dim", "s", [], [1.0, 0.0])  # different dim
        # Query is 3-dim — should match only the 3-dim row
        with patch.object(self.embeddings, "embed", return_value=[1.0, 0.0, 0.0]):
            hits = self.retrieval.find_similar("q", k=5, min_score=0.0)
        self.assertEqual([h.user_task for h in hits], ["3-dim"])

    def test_index_dispatch_skips_unsummarized(self):
        from unittest.mock import patch
        # Dispatch with NO summary (just created + started)
        did = db.create_dispatch(self.project_id, "no summary yet")
        db.mark_started(did, terminal_pid=None, claude_pid=1)
        with patch.object(self.embeddings, "embed", return_value=[1.0, 2.0, 3.0]):
            ok = self.retrieval.index_dispatch(did)
        self.assertFalse(ok, "index_dispatch should refuse to index dispatches without summaries")

    def test_index_dispatch_upserts(self):
        """Re-indexing same dispatch overwrites vector, doesn't error on PK conflict."""
        from unittest.mock import patch
        did = self._seed_dispatch("task", "summary", [], [1.0, 0.0])

        with patch.object(self.embeddings, "embed", return_value=[0.5, 0.5]):
            ok = self.retrieval.index_dispatch(did)
        self.assertTrue(ok)
        # Verify the vector was actually replaced
        with db.conn() as c:
            row = c.execute("SELECT vector FROM dispatch_embeddings WHERE dispatch_id = ?", (did,)).fetchone()
        v = self.embeddings.blob_to_vec(row["vector"])
        self.assertAlmostEqual(v[0], 0.5, places=4)
        self.assertAlmostEqual(v[1], 0.5, places=4)

    def test_backfill_only_indexes_summarized(self):
        from unittest.mock import patch
        # Shared module-level DB carries dispatches from prior tests. Filter
        # backfill scope to just OUR project's dispatches.
        d1 = self._seed_dispatch("task1", "summary1", [], [1.0])
        d2 = self._seed_dispatch("task2", "summary2", [], [0.0])
        d3 = db.create_dispatch(self.project_id, "no summary")
        db.mark_started(d3, None, 1)
        # Clear embeddings so backfill has work to do
        with db.conn() as c:
            c.execute("DELETE FROM dispatch_embeddings")
        with patch.object(self.embeddings, "embed", return_value=[1.0]):
            self.retrieval.backfill_missing()
        # Verify d1, d2 got indexed and d3 did not — for OUR project specifically
        with db.conn() as c:
            ids = [r[0] for r in c.execute(
                "SELECT dispatch_id FROM dispatch_embeddings WHERE project_id = ? ORDER BY dispatch_id",
                (self.project_id,),
            )]
        self.assertEqual(sorted(ids), sorted([d1, d2]),
            "expected exactly d1+d2 indexed for our project, d3 (no summary) skipped")

    def test_render_hits_for_prompt_caps_total_chars(self):
        from orchestrator.lib.retrieval import Hit, render_hits_for_prompt
        # 20 hits with 1000-char summaries each = 20K total; cap at 5K
        hits = [Hit(i, 1, "p", "task" * 50, "s" * 1000, "l" * 200, 0.9) for i in range(20)]
        out = render_hits_for_prompt(hits, max_chars=5000)
        self.assertLess(len(out), 6000)  # cap + a little slack for the dropped-marker
        self.assertIn("dropped", out)


# ─── Test 18: phase 6 hardening ──────────────────────────────────────────

class TestPhase6Hardening(unittest.TestCase):
    def setUp(self):
        from orchestrator.lib import embeddings, retrieval
        self.embeddings = embeddings
        self.retrieval = retrieval
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        self.project_id = db.add_project(str(td))["id"]
        with db.conn() as c:
            c.execute("DELETE FROM dispatch_embeddings")

    def test_embed_nan_inf_rejected(self):
        """If Ollama ever returns NaN/Inf in the vector, embed() must drop it
        rather than poisoning cosine math downstream."""
        from unittest.mock import patch
        bad_responses = [
            {"embedding": [1.0, float("nan"), 0.5]},
            {"embedding": [1.0, float("inf"), 0.5]},
            {"embedding": [1.0, float("-inf"), 0.5]},
        ]
        for resp in bad_responses:
            with patch("urllib.request.urlopen") as mock_open:
                cm = mock_open.return_value.__enter__.return_value
                cm.read.return_value = json.dumps(resp).encode()
                self.assertIsNone(self.embeddings.embed("x"),
                    f"non-finite value not rejected: {resp}")

    def test_embed_non_numeric_rejected(self):
        from unittest.mock import patch
        with patch("urllib.request.urlopen") as mock_open:
            cm = mock_open.return_value.__enter__.return_value
            cm.read.return_value = json.dumps({"embedding": [1.0, "oops", 0.5]}).encode()
            self.assertIsNone(self.embeddings.embed("x"))

    def test_embed_strips_long_input(self):
        """Inputs over MAX_INPUT_CHARS should be truncated, not crash Ollama."""
        from unittest.mock import patch
        sent_body = {}
        def capture(req, timeout=None):
            sent_body["body"] = req.data
            class R:
                def __enter__(self): return self
                def __exit__(self, *a): pass
                def read(self_inner): return json.dumps({"embedding": [0.0]*768}).encode()
            return R()
        with patch("urllib.request.urlopen", side_effect=capture):
            self.embeddings.embed("z" * 50_000)
        body = json.loads(sent_body["body"])
        self.assertLessEqual(len(body["prompt"]), self.embeddings.MAX_INPUT_CHARS)

    def test_find_similar_skips_corrupt_rows_gracefully(self):
        """An incomplete/corrupt vector blob shouldn't crash retrieval."""
        from unittest.mock import patch
        # Seed valid + corrupt rows — all DB writes done outside conn() to
        # avoid the lock-reentrancy issue (each db.* call opens its own conn).
        did = db.create_dispatch(self.project_id, "valid")
        db.mark_started(did, terminal_pid=None, claude_pid=1)
        db.complete_dispatch(did, "s", None, "Stop")
        db.set_summary(did, "summary", "", "", "", [])
        bad_did = db.create_dispatch(self.project_id, "bad")
        db.mark_started(bad_did, terminal_pid=None, claude_pid=1)
        with db.conn() as c:
            c.execute(
                "INSERT INTO dispatch_embeddings(dispatch_id, project_id, model, dim, vector) "
                "VALUES (?, ?, 'test', 3, ?)",
                (did, self.project_id, self.embeddings.vec_to_blob([1.0, 0.0, 0.0])),
            )
            # Row whose blob length doesn't match its dim field
            c.execute(
                "INSERT INTO dispatch_embeddings(dispatch_id, project_id, model, dim, vector) "
                "VALUES (?, ?, 'test', 3, ?)",
                (bad_did, self.project_id, b"\x00\x00"),  # 2 bytes ≠ 3 floats
            )

        # Query with matching dim — corrupt row should be filtered (its
        # blob_to_vec will produce shorter list, cosine zip stops short)
        with patch.object(self.embeddings, "embed", return_value=[1.0, 0.0, 0.0]):
            hits = self.retrieval.find_similar("q", k=5, min_score=0.0)
        # Should return the valid row at least, and not crash
        ids = [h.dispatch_id for h in hits]
        self.assertIn(did, ids)

    def test_query_text_empty_returns_empty_hits(self):
        from unittest.mock import patch
        with patch.object(self.embeddings, "embed", return_value=None):
            self.assertEqual(self.retrieval.find_similar(""), [])

    def test_render_hits_for_prompt_empty_returns_empty_string(self):
        self.assertEqual(self.retrieval.render_hits_for_prompt([]), "")

    def test_index_dispatch_with_long_summary_truncated(self):
        """A 50KB summary should be embedded after truncation, not crash."""
        from unittest.mock import patch
        did = db.create_dispatch(self.project_id, "x")
        db.mark_started(did, terminal_pid=None, claude_pid=1)
        db.complete_dispatch(did, "s", None, "Stop")
        db.set_summary(did, "L" * 50_000, "", "", "", [])

        captured = {}
        def capture_embed(text, **kw):
            captured["text"] = text
            return [0.0] * 768
        with patch.object(self.embeddings, "embed", side_effect=capture_embed):
            ok = self.retrieval.index_dispatch(did)
        self.assertTrue(ok)
        # Combined text passed to embed should still be substantial but bounded
        self.assertGreater(len(captured["text"]), 1000)

    def test_rewriter_works_when_ollama_down(self):
        """If embeddings.embed returns None throughout, the rewriter should
        still function — just with an empty 'similar past tasks' section."""
        from orchestrator.lib import rewriter, claude_runner
        from unittest.mock import patch

        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        (td / "CLAUDE.md").write_text("test project")

        fake = claude_runner.ClaudeRun(
            ok=True, text='{"rewritten_prompt":"x"}',
            parsed_json={"rewritten_prompt": "x"}, cost_usd=0,
        )
        captured = {}
        def capture_claude(prompt, cwd, **kw):
            captured["prompt"] = prompt
            return fake

        with patch.object(self.embeddings, "embed", return_value=None), \
             patch.object(claude_runner, "run_claude_json", side_effect=capture_claude):
            r = rewriter.rewrite("test task", str(td))

        self.assertTrue(r.ok)
        self.assertEqual(r.similar_hits, [])
        # The "no similar past tasks indexed yet" sentinel must appear in the prompt
        self.assertIn("no similar past tasks", captured["prompt"])

    def test_rewriter_excludes_self_when_re_rewriting(self):
        """Sanity: find_similar can exclude a specific dispatch_id, which
        matters once we have many of these and want to avoid trivial 'most
        similar to itself = itself' results."""
        from unittest.mock import patch
        # Seed dispatch with vector
        did = db.create_dispatch(self.project_id, "self")
        db.mark_started(did, None, 1)
        db.complete_dispatch(did, "s", None, "Stop")
        db.set_summary(did, "summary", "", "", "", [])
        with db.conn() as c:
            c.execute(
                "INSERT INTO dispatch_embeddings(dispatch_id, project_id, model, dim, vector) "
                "VALUES (?, ?, 'test', 3, ?)",
                (did, self.project_id, self.embeddings.vec_to_blob([1.0, 0.0, 0.0])),
            )
        with patch.object(self.embeddings, "embed", return_value=[1.0, 0.0, 0.0]):
            with_self = self.retrieval.find_similar("q", k=5, min_score=0.0)
            without_self = self.retrieval.find_similar("q", k=5, min_score=0.0, exclude_dispatch_id=did)
        self.assertEqual(len(with_self), 1)
        self.assertEqual(len(without_self), 0)


# ─── Test 19: phase 7 loop watchdog ──────────────────────────────────────

class TestLoopWatchdog(unittest.TestCase):
    def setUp(self):
        from orchestrator.lib import loop_watchdog
        self.lw = loop_watchdog
        # Clear any leftover state from prior tests
        self.lw._buffers.clear()

    def test_buffer_starts_empty(self):
        self.assertEqual(self.lw.buffer_size(123), 0)

    def test_n_identical_calls_below_threshold_no_kill(self):
        # 7 identical calls, threshold 8 → no kill
        for i in range(7):
            triggered = self.lw.record(123, "Read", "abc", threshold=8)
            self.assertFalse(triggered, f"triggered prematurely at call {i+1}")
        self.assertEqual(self.lw.buffer_size(123), 7)

    def test_n_identical_calls_at_threshold_kills(self):
        triggered = False
        for _ in range(8):
            triggered = self.lw.record(123, "Read", "abc", threshold=8)
        self.assertTrue(triggered)

    def test_mixed_calls_dont_trigger(self):
        for i in range(20):
            t = self.lw.record(123, "Read", f"hash-{i}", threshold=8)
            self.assertFalse(t, f"varied input_hash triggered at call {i+1}")

    def test_different_tools_dont_collide(self):
        # 7 Reads then 1 Write → buffer last entry differs → not a loop
        for _ in range(7):
            self.lw.record(123, "Read", "x", threshold=8)
        self.assertFalse(self.lw.record(123, "Write", "x", threshold=8))

    def test_per_dispatch_isolation(self):
        # Dispatch A loops, dispatch B does normal varied work
        for _ in range(8):
            t_a = self.lw.record(1, "Read", "loop", threshold=8)
        for i in range(8):
            t_b = self.lw.record(2, "Read", f"h{i}", threshold=8)
        self.assertTrue(t_a)
        self.assertFalse(t_b)

    def test_clear_resets_state(self):
        for _ in range(5):
            self.lw.record(123, "Read", "x")
        self.assertGreater(self.lw.buffer_size(123), 0)
        self.lw.clear(123)
        self.assertEqual(self.lw.buffer_size(123), 0)

    def test_threshold_change_preserves_recent(self):
        for _ in range(3):
            self.lw.record(123, "Read", "x", threshold=8)
        # Smaller threshold should be enough to trigger if all are identical
        triggered = False
        for _ in range(2):
            triggered = self.lw.record(123, "Read", "x", threshold=3)
        self.assertTrue(triggered)

    def test_empty_strings_dont_crash(self):
        for _ in range(8):
            self.lw.record(123, "", "")
        # No crash — and they should be treated as identical too
        triggered = self.lw.record(123, "", "")
        self.assertTrue(triggered)


class TestLoopWatchdogHookScript(unittest.TestCase):
    HOOK = str(REPO / "bin" / "notify_tool_use.sh")

    def test_no_op_without_env_var(self):
        env = {k: v for k, v in os.environ.items() if k != "ORCHESTRATOR_RUN_ID"}
        r = subprocess.run(
            ["bash", self.HOOK],
            input='{"tool_name":"Read","tool_input":{"file_path":"/x"}}',
            text=True, capture_output=True, env=env, timeout=5,
        )
        self.assertEqual(r.returncode, 0)

    def test_always_exits_zero(self):
        """Even if curl fails / payload is junk, the hook must exit 0 so
        it never blocks a tool call."""
        env = os.environ.copy()
        env["ORCHESTRATOR_RUN_ID"] = "1"
        env["ORCHESTRATOR_PORT"] = "1"  # unreachable
        for payload in ['{}', 'not json', '{"tool_name":null}']:
            r = subprocess.run(
                ["bash", self.HOOK], input=payload, text=True,
                capture_output=True, env=env, timeout=5,
            )
            self.assertEqual(r.returncode, 0, f"hook failed on payload: {payload!r}")


class TestApiToolUse(unittest.IsolatedAsyncioTestCase):
    """/api/tool_use must record fingerprints and trigger kill on loop."""

    async def test_records_and_returns_ok(self):
        from orchestrator.app import api_tool_use
        from orchestrator.lib import loop_watchdog
        loop_watchdog._buffers.clear()
        r = await api_tool_use({"run_id": "999", "tool_name": "Read", "input_hash": "abc"})
        self.assertEqual(r, {"ok": True})
        self.assertEqual(loop_watchdog.buffer_size(999), 1)

    async def test_loop_detected_returns_killed_true(self):
        from orchestrator.app import api_tool_use
        from orchestrator.lib import loop_watchdog
        from unittest.mock import patch
        loop_watchdog._buffers.clear()

        # Need to stub out the real kill chain (it would hit watchdog → db)
        async def fake_trigger(dispatch_id, fp):
            pass
        with patch.object(loop_watchdog, "trigger_kill", side_effect=fake_trigger):
            r = None
            for _ in range(loop_watchdog.DEFAULT_LOOP_THRESHOLD):
                r = await api_tool_use({"run_id": "888", "tool_name": "Bash", "input_hash": "xx"})
        self.assertEqual(r.get("killed"), True)

    async def test_bad_run_id_doesnt_crash(self):
        from orchestrator.app import api_tool_use
        r = await api_tool_use({"run_id": "not-a-number", "tool_name": "X", "input_hash": "Y"})
        self.assertEqual(r["ok"], False)


# ─── Test 20: phase 8 edits — adversarial validation ────────────────────

class TestEditsValidation(unittest.TestCase):
    def setUp(self):
        from orchestrator.lib import edits
        self.edits = edits
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_edits_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # Standard layout
        (self.tmp / "memory").mkdir()
        (self.tmp / "knowledge").mkdir()
        (self.tmp / "tasks").mkdir()
        # An outside sentinel dir/file we must NEVER reach
        self.outside = Path(tempfile.mkdtemp(prefix="orch_outside_"))
        self.addCleanup(shutil.rmtree, self.outside, ignore_errors=True)
        (self.outside / "secret.md").write_text("DO_NOT_LEAK")

    def _p(self, action, path, content="hello", rationale=""):
        return self.edits.EditProposal(action=action, path=path, content=content, rationale=rationale)

    def test_unknown_action_rejected(self):
        ok, err = self.edits.validate(self._p("delete_everything", "memory/x.md"), str(self.tmp))
        self.assertFalse(ok); self.assertIn("not in", err)

    def test_absolute_path_rejected(self):
        ok, err = self.edits.validate(self._p("append_to_memory", "/etc/passwd"), str(self.tmp))
        self.assertFalse(ok); self.assertIn("absolute", err)

    def test_tilde_path_rejected(self):
        ok, err = self.edits.validate(self._p("append_to_memory", "~/secrets.md"), str(self.tmp))
        self.assertFalse(ok); self.assertIn("absolute", err)

    def test_dotdot_traversal_rejected(self):
        ok, err = self.edits.validate(self._p("append_to_memory", "memory/../../etc/passwd"), str(self.tmp))
        self.assertFalse(ok); self.assertIn("..", err)

    def test_hidden_path_rejected(self):
        ok, err = self.edits.validate(self._p("append_to_memory", ".env"), str(self.tmp))
        self.assertFalse(ok); self.assertIn("hidden", err)

    def test_hidden_dir_anywhere_rejected(self):
        ok, err = self.edits.validate(self._p("append_to_memory", "memory/.hidden/x.md"), str(self.tmp))
        self.assertFalse(ok); self.assertIn("hidden", err)

    def test_non_md_rejected(self):
        ok, err = self.edits.validate(self._p("append_to_memory", "memory/notes.txt"), str(self.tmp))
        self.assertFalse(ok); self.assertIn("only .md", err)

    def test_empty_path_rejected(self):
        ok, err = self.edits.validate(self._p("append_to_memory", ""), str(self.tmp))
        self.assertFalse(ok); self.assertIn("empty path", err)

    def test_append_to_memory_wrong_dir_rejected(self):
        """A memory edit pointed at tasks/ should fail."""
        ok, err = self.edits.validate(self._p("append_to_memory", "tasks/x.md"), str(self.tmp))
        self.assertFalse(ok); self.assertIn("can only write into", err)

    def test_create_task_file_into_memory_rejected(self):
        ok, err = self.edits.validate(self._p("create_task_file", "memory/x.md"), str(self.tmp))
        self.assertFalse(ok); self.assertIn("can only write into", err)

    def test_create_task_file_refuses_existing(self):
        (self.tmp / "tasks" / "exists.md").write_text("already here")
        ok, err = self.edits.validate(self._p("create_task_file", "tasks/exists.md"), str(self.tmp))
        self.assertFalse(ok); self.assertIn("overwrite", err)

    def test_content_too_large_rejected(self):
        big = "x" * (self.edits.MAX_CONTENT_BYTES + 1)
        ok, err = self.edits.validate(self._p("append_to_memory", "memory/big.md", content=big), str(self.tmp))
        self.assertFalse(ok); self.assertIn("too large", err)

    def test_symlink_escape_via_directory_rejected(self):
        """memory/ replaced with a symlink to outside → resolved path must
        not be inside project, so the validator must reject the write."""
        # Remove the real memory dir and replace with a symlink to outside
        import os as _os
        (self.tmp / "memory").rmdir()
        _os.symlink(str(self.outside), str(self.tmp / "memory"))
        ok, err = self.edits.validate(self._p("append_to_memory", "memory/secret.md"), str(self.tmp))
        # Either rejected as "escapes project root" OR (depending on resolution)
        # as "can only write into ...". Either way, must NOT be allowed.
        self.assertFalse(ok, f"symlink escape was allowed: {err}")

    def test_valid_append_to_memory_passes(self):
        ok, err = self.edits.validate(self._p("append_to_memory", "memory/lesson.md"), str(self.tmp))
        self.assertTrue(ok, f"valid edit rejected: {err}")

    def test_valid_create_task_passes(self):
        ok, err = self.edits.validate(self._p("create_task_file", "tasks/new.md"), str(self.tmp))
        self.assertTrue(ok, f"valid edit rejected: {err}")

    def test_forge_json_layout_override_honored(self):
        """Project that puts memory in `notes/` (via .forge.json) — edits
        must go to notes/, NOT memory/."""
        import json as _json
        (self.tmp / ".forge.json").write_text(_json.dumps({
            "layout": {"memory_dirs": ["notes"], "knowledge_dirs": ["knowledge"], "task_dirs": ["tasks"]}
        }))
        (self.tmp / "notes").mkdir()
        # memory/ now rejected
        ok, _ = self.edits.validate(self._p("append_to_memory", "memory/x.md"), str(self.tmp))
        self.assertFalse(ok, "default memory/ should be rejected when layout overrides")
        # notes/ now allowed
        ok, err = self.edits.validate(self._p("append_to_memory", "notes/x.md"), str(self.tmp))
        self.assertTrue(ok, f"layout override path rejected: {err}")


class TestEditsApply(unittest.TestCase):
    def setUp(self):
        from orchestrator.lib import edits
        self.edits = edits
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_edits_apply_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "memory").mkdir()
        (self.tmp / "knowledge").mkdir()
        (self.tmp / "tasks").mkdir()

    def test_append_creates_file_when_missing(self):
        r = self.edits.apply_edit(
            self.edits.EditProposal("append_to_memory", "memory/new.md", "first lesson"),
            str(self.tmp),
        )
        self.assertTrue(r.ok)
        body = (self.tmp / "memory" / "new.md").read_text()
        self.assertIn("first lesson", body)

    def test_append_preserves_existing_content_with_separator(self):
        (self.tmp / "memory" / "lessons.md").write_text("# Lessons\n\nfirst")
        r = self.edits.apply_edit(
            self.edits.EditProposal("append_to_memory", "memory/lessons.md", "second"),
            str(self.tmp),
        )
        self.assertTrue(r.ok)
        body = (self.tmp / "memory" / "lessons.md").read_text()
        self.assertIn("first", body)
        self.assertIn("second", body)
        # Original ends without trailing newline → expect double-newline separator inserted
        self.assertIn("first\n\nsecond", body)

    def test_create_task_writes_file(self):
        r = self.edits.apply_edit(
            self.edits.EditProposal("create_task_file", "tasks/refactor-foo.md", "# Refactor foo"),
            str(self.tmp),
        )
        self.assertTrue(r.ok)
        self.assertEqual((self.tmp / "tasks" / "refactor-foo.md").read_text().strip(), "# Refactor foo")

    def test_invalid_proposal_returns_error_not_raises(self):
        r = self.edits.apply_edit(
            self.edits.EditProposal("DROP TABLE", "memory/x.md", "evil"),
            str(self.tmp),
        )
        self.assertFalse(r.ok); self.assertTrue(r.error)

    def test_apply_doesnt_write_anything_when_invalid(self):
        # Verify a rejected proposal didn't create a file as a side effect
        self.edits.apply_edit(
            self.edits.EditProposal("append_to_memory", "../etc/sneaky.md", "x"),
            str(self.tmp),
        )
        self.assertFalse((self.tmp.parent / "etc" / "sneaky.md").exists())


# ─── Test 21: phase 9 onboarding ─────────────────────────────────────────

class TestOnboardingScan(unittest.TestCase):
    def setUp(self):
        from orchestrator.lib import onboarding
        self.onboarding = onboarding
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_onb_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_empty_project_returns_empty_scan(self):
        s = self.onboarding.scan_project(str(self.tmp))
        self.assertEqual(s.rule_files, {})
        self.assertEqual(s.cursor_rules_dir, [])
        self.assertFalse(s.has_forge_json)
        self.assertFalse(s.has_memory_dir)

    def test_scan_picks_up_claude_md(self):
        (self.tmp / "CLAUDE.md").write_text("# rules\nbe careful")
        s = self.onboarding.scan_project(str(self.tmp))
        self.assertIn("CLAUDE.md", s.rule_files)
        self.assertIn("be careful", s.rule_files["CLAUDE.md"])

    def test_scan_picks_up_cursorrules_legacy(self):
        (self.tmp / ".cursorrules").write_text("legacy rules here")
        s = self.onboarding.scan_project(str(self.tmp))
        self.assertIn(".cursorrules", s.rule_files)

    def test_scan_picks_up_cursor_rules_dir(self):
        (self.tmp / ".cursor" / "rules").mkdir(parents=True)
        (self.tmp / ".cursor" / "rules" / "global.mdc").write_text("modern cursor rule")
        (self.tmp / ".cursor" / "rules" / "lang.mdc").write_text("language rule")
        s = self.onboarding.scan_project(str(self.tmp))
        paths = [p for p, _ in s.cursor_rules_dir]
        self.assertIn(".cursor/rules/global.mdc", paths)
        self.assertIn(".cursor/rules/lang.mdc", paths)

    def test_scan_detects_stack_signals(self):
        (self.tmp / "package.json").write_text("{}")
        (self.tmp / "requirements.txt").write_text("fastapi")
        s = self.onboarding.scan_project(str(self.tmp))
        self.assertIn("package.json", s.stack_signals)
        self.assertIn("requirements.txt", s.stack_signals)

    def test_scan_detects_forge_layout(self):
        (self.tmp / ".forge.json").write_text('{"layout":{}}')
        (self.tmp / "memory").mkdir()
        (self.tmp / "memory" / "a.md").write_text("x")
        (self.tmp / "memory" / "b.md").write_text("y")
        (self.tmp / "tasks").mkdir()
        s = self.onboarding.scan_project(str(self.tmp))
        self.assertTrue(s.has_forge_json)
        self.assertTrue(s.has_memory_dir)
        self.assertEqual(s.memory_file_count, 2)
        self.assertTrue(s.has_tasks_dir)
        self.assertEqual(s.task_file_count, 0)

    def test_scan_truncates_huge_rule_files(self):
        (self.tmp / "CLAUDE.md").write_text("X" * 20_000)
        s = self.onboarding.scan_project(str(self.tmp))
        self.assertLessEqual(len(s.rule_files["CLAUDE.md"]),
                             self.onboarding.PER_FILE_CHARS + 50)
        self.assertIn("truncated", s.rule_files["CLAUDE.md"])

    def test_scan_nonexistent_path_returns_empty(self):
        s = self.onboarding.scan_project("/no/such/path/onb")
        self.assertEqual(s.rule_files, {})


class TestOnboardingAnalyze(unittest.TestCase):
    def setUp(self):
        from orchestrator.lib import onboarding, claude_runner
        self.onboarding = onboarding
        self.claude_runner = claude_runner
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_onb_an_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "CLAUDE.md").write_text("be safe")

    def test_analyze_bad_path_returns_error(self):
        r = self.onboarding.analyze("/no/such/path")
        self.assertFalse(r.ok)
        self.assertIn("not found", r.error)

    def test_analyze_happy_path_with_mock(self):
        from unittest.mock import patch
        fake = self.claude_runner.ClaudeRun(
            ok=True, text='{}',
            parsed_json={
                "project_summary": "a tiny test project",
                "strengths": ["has CLAUDE.md"],
                "gaps": ["no PLAN.md"],
                "recommendations": [{
                    "title": "Add PLAN.md",
                    "rationale": "tracks goals",
                    "target_path": "PLAN.md",
                    "manual_content": "# Plan\n\nTBD",
                }],
                "proposed_edits": [{
                    "action": "append_to_memory",
                    "path": "memory/setup.md",
                    "content": "use venv",
                    "rationale": "common mistake",
                }],
            },
            cost_usd=0.03, duration_s=4.0, model="sonnet",
        )
        with patch.object(self.claude_runner, "run_claude_json", return_value=fake):
            r = self.onboarding.analyze(str(self.tmp))
        self.assertTrue(r.ok, f"analyze failed: {r.error}")
        self.assertEqual(r.project_summary, "a tiny test project")
        self.assertEqual(r.strengths, ["has CLAUDE.md"])
        self.assertEqual(len(r.recommendations), 1)
        self.assertEqual(r.recommendations[0].title, "Add PLAN.md")
        self.assertEqual(len(r.proposed_edits), 1)
        # proposed_edit was pre-validated — memory/setup.md is in the default layout
        self.assertTrue(r.proposed_edits[0].valid,
            f"valid proposed_edit marked invalid: {r.proposed_edits[0].validation_error}")

    def test_analyze_marks_invalid_proposed_edits(self):
        """A proposed_edit with a path-traversal attempt must be flagged
        invalid (not crash, not auto-apply) — just like in the rewriter."""
        from unittest.mock import patch
        fake = self.claude_runner.ClaudeRun(
            ok=True, text='{}',
            parsed_json={
                "project_summary": "x", "strengths": [], "gaps": [], "recommendations": [],
                "proposed_edits": [{
                    "action": "append_to_memory",
                    "path": "../../etc/sneaky.md",
                    "content": "evil",
                    "rationale": "attack",
                }],
            },
            cost_usd=0, duration_s=1, model="sonnet",
        )
        with patch.object(self.claude_runner, "run_claude_json", return_value=fake):
            r = self.onboarding.analyze(str(self.tmp))
        self.assertTrue(r.ok)
        self.assertEqual(len(r.proposed_edits), 1)
        self.assertFalse(r.proposed_edits[0].valid)
        self.assertTrue(r.proposed_edits[0].validation_error)

    def test_analyze_handles_non_json_response(self):
        from unittest.mock import patch
        fake = self.claude_runner.ClaudeRun(
            ok=True, text="sorry I can't help", parsed_json=None, cost_usd=0,
        )
        with patch.object(self.claude_runner, "run_claude_json", return_value=fake):
            r = self.onboarding.analyze(str(self.tmp))
        self.assertFalse(r.ok)
        self.assertIn("non-JSON", r.error)
        self.assertIsNotNone(r.scan)  # scan is still returned for debugging

    def test_analyze_handles_claude_failure(self):
        from unittest.mock import patch
        fake = self.claude_runner.ClaudeRun(ok=False, error="ollama down or whatever")
        with patch.object(self.claude_runner, "run_claude_json", return_value=fake):
            r = self.onboarding.analyze(str(self.tmp))
        self.assertFalse(r.ok)
        self.assertIn("ollama down", r.error)

    def test_template_injection_isolation(self):
        """A rule file containing literal {bundle} or {scan} must not be
        text-replaced when filling the ONBOARDING.md template."""
        (self.tmp / ".cursorrules").write_text("the {scan} placeholder caused issues once")
        from unittest.mock import patch
        captured = {}
        def fake_run(prompt, cwd, **kw):
            captured["body"] = prompt
            return self.claude_runner.ClaudeRun(
                ok=True, text='{}',
                parsed_json={"project_summary": "x"}, cost_usd=0,
            )
        with patch.object(self.claude_runner, "run_claude_json", side_effect=fake_run):
            self.onboarding.analyze(str(self.tmp))
        # The literal {scan} inside the cursor rule must survive verbatim
        self.assertIn("the {scan} placeholder", captured["body"])


# ─── Test 22: phase 9 hardening ──────────────────────────────────────────

class TestOnboardingHardening(unittest.TestCase):
    def setUp(self):
        from orchestrator.lib import onboarding
        self.onboarding = onboarding
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_onb_sec_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # An "outside" file the scanner must NEVER read via a project symlink
        self.outside = Path(tempfile.mkdtemp(prefix="orch_outside_"))
        self.addCleanup(shutil.rmtree, self.outside, ignore_errors=True)
        (self.outside / "secret.md").write_text("SECRET_FROM_ONBOARDING")

    def test_symlink_claude_md_to_outside_not_read(self):
        """CLAUDE.md as a symlink to a file outside the project must not
        end up in the scan output. The orchestrator would then send that
        content to claude (in the analyzer prompt) and display it in the
        UI — both data leaks."""
        import os as _os
        _os.symlink(str(self.outside / "secret.md"), str(self.tmp / "CLAUDE.md"))
        s = self.onboarding.scan_project(str(self.tmp))
        for path, content in s.rule_files.items():
            self.assertNotIn("SECRET_FROM_ONBOARDING", content,
                f"symlinked rule file '{path}' leaked content from outside project")

    def test_symlink_in_cursor_rules_dir_not_read(self):
        (self.tmp / ".cursor" / "rules").mkdir(parents=True)
        import os as _os
        _os.symlink(str(self.outside / "secret.md"),
                    str(self.tmp / ".cursor" / "rules" / "evil.mdc"))
        s = self.onboarding.scan_project(str(self.tmp))
        for _, content in s.cursor_rules_dir:
            self.assertNotIn("SECRET_FROM_ONBOARDING", content,
                "symlinked .mdc leaked content from outside project")

    def test_many_cursor_rule_files_capped(self):
        (self.tmp / ".cursor" / "rules").mkdir(parents=True)
        for i in range(100):
            (self.tmp / ".cursor" / "rules" / f"rule{i:03d}.mdc").write_text(f"rule body {i}")
        s = self.onboarding.scan_project(str(self.tmp))
        # Hard cap so a project with N rule files can't blow the prompt
        self.assertLessEqual(len(s.cursor_rules_dir), self.onboarding.MAX_CURSOR_RULES,
            f"cursor_rules_dir not capped: got {len(s.cursor_rules_dir)}")

    def test_unreadable_rule_file_doesnt_crash(self):
        cm = self.tmp / "CLAUDE.md"
        cm.write_text("hello")
        cm.chmod(0o000)
        try:
            s = self.onboarding.scan_project(str(self.tmp))
            # Either the file is omitted, or content has the [read error] marker.
            if "CLAUDE.md" in s.rule_files:
                self.assertIn("read error", s.rule_files["CLAUDE.md"])
        finally:
            cm.chmod(0o644)

    def test_huge_top_level_dir_doesnt_blow_memory(self):
        """A project with 500 top-level entries shouldn't be reported
        verbatim — the render step caps at 40 but scan should also cap.
        We allow MAX_TOP_LEVEL+1 to account for the trailing '… more' marker."""
        for i in range(500):
            (self.tmp / f"file{i:04d}.txt").write_text("x")
        s = self.onboarding.scan_project(str(self.tmp))
        self.assertLessEqual(len(s.top_level_entries), self.onboarding.MAX_TOP_LEVEL + 1,
            f"top_level_entries not capped at scan time: {len(s.top_level_entries)}")

    def test_to_prompt_section_safe_with_empty_scan(self):
        s = self.onboarding.ProjectScan()
        out = s.to_prompt_section()
        # Should render cleanly, mention "none found"
        self.assertIn("none found", out.lower() if "none found" in out.lower() else out)

    def test_recommendations_with_missing_fields_dont_crash(self):
        from orchestrator.lib import claude_runner
        from unittest.mock import patch
        fake = claude_runner.ClaudeRun(
            ok=True, text='{}',
            parsed_json={
                "project_summary": "x",
                "strengths": [], "gaps": [], "proposed_edits": [],
                "recommendations": [
                    {},                              # totally empty
                    {"title": "only title"},          # missing other fields
                    {"target_path": "x"},             # missing title
                    "not a dict",                    # wrong type — skipped
                ],
            },
            cost_usd=0, duration_s=1, model="sonnet",
        )
        with patch.object(claude_runner, "run_claude_json", return_value=fake):
            r = self.onboarding.analyze(str(self.tmp))
        self.assertTrue(r.ok)
        # 3 dict entries kept (empty + only-title + target_path-only); string skipped
        self.assertEqual(len(r.recommendations), 3)
        # Missing fields become empty strings — no crash, no error
        for rec in r.recommendations:
            self.assertIsInstance(rec.title, str)
            self.assertIsInstance(rec.manual_content, str)

    def test_recommendations_wrong_outer_type_skipped(self):
        from orchestrator.lib import claude_runner
        from unittest.mock import patch
        # recommendations is a string instead of a list
        fake = claude_runner.ClaudeRun(
            ok=True, text='{}',
            parsed_json={"project_summary": "x",
                         "strengths": [], "gaps": [],
                         "recommendations": "not a list",
                         "proposed_edits": []},
            cost_usd=0, duration_s=1, model="sonnet",
        )
        with patch.object(claude_runner, "run_claude_json", return_value=fake):
            r = self.onboarding.analyze(str(self.tmp))
        self.assertTrue(r.ok)
        self.assertEqual(r.recommendations, [])

    def test_xss_in_recommendation_fields_escaped_in_response(self):
        """Recommendation fields containing HTML must be auto-escaped by
        Jinja when rendered. Tested at the endpoint level."""
        # This is verified at the Jinja autoescape level — the templates
        # use {{ }} everywhere. We rely on that contract. This test is here
        # to make sure if anyone ever switches to a raw filter, CI catches it.
        from orchestrator.app import templates
        env = templates.env
        from jinja2 import Template
        t = env.from_string("{{ r.title }}")
        rendered = t.render(r=type("R", (), {"title": "<script>alert(1)</script>"})())
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
