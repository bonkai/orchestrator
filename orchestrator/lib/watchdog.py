"""Wall-clock cap watchdog. One asyncio task per running dispatch.

Loop watchdog (repeated identical tool calls → kill) is a planned addition
in a later phase; it requires tailing the session JSONL and is not in MVP.
"""

import asyncio
import logging
import time

from orchestrator.lib import db, idle_notifier, spawn

log = logging.getLogger("orchestrator.watchdog")

# dispatch_id → asyncio.Task
_watchers: dict[int, asyncio.Task] = {}

# Dispatches currently in the graceful timeout-pause flow: SIGTERM has been
# sent and the watchdog is waiting for the Stop hook to deliver a session_id.
# /api/complete consults this (via is_pausing) so it stores that session_id
# WITHOUT finalizing the dispatch as 'completed' — the watchdog owns the
# terminal 'paused' status. Same event loop, so a plain set is race-free.
_pausing: set[int] = set()

# How long, after SIGTERM, to wait for the Stop hook's session_id before
# giving up and hard-killing. Module-level so tests can shrink it.
PAUSE_SESSION_GRACE_S = 8.0
PAUSE_SESSION_POLL_S = 0.5

# Periodic orphan reaper: how often to scan, and how old a 'running' dispatch
# with no recorded PID must be before we treat it as failed-to-launch (rather
# than still-spawning — iTerm can take a moment to write the PID file).
ORPHAN_REAP_INTERVAL_S = 45
ORPHAN_REAP_GRACE_S = 60


def is_pausing(dispatch_id: int) -> bool:
    """True while a timeout pause is in flight for this dispatch (between
    SIGTERM and the watchdog's final paused/killed decision)."""
    return dispatch_id in _pausing


def schedule(dispatch_id: int, claude_pid: int | None, cap_s: int):
    """Start a wall-clock watchdog for this dispatch."""
    task = asyncio.create_task(_run(dispatch_id, claude_pid, cap_s))
    _watchers[dispatch_id] = task


def cancel(dispatch_id: int):
    """Cancel the watchdog (called on normal completion or manual kill)."""
    t = _watchers.pop(dispatch_id, None)
    if t and not t.done():
        t.cancel()


async def _run(dispatch_id: int, claude_pid: int | None, cap_s: int):
    from orchestrator.lib import loop_watchdog
    try:
        await asyncio.sleep(cap_s)
        log.warning("Dispatch %s exceeded %ss wall-clock cap — pausing", dispatch_id, cap_s)
        # Publish the pause intent BEFORE the SIGTERM. A graceful Claude runs
        # its Stop hook on SIGTERM, whose POST lands in /api/complete; seeing
        # this intent, that handler stores the session_id but leaves the
        # terminal status to us (rather than cancelling this task and marking
        # the dispatch 'completed', which would beat us to the row and strip
        # the resumable 'paused' state). Always cleared in `finally`.
        _pausing.add(dispatch_id)
        if not claude_pid:
            claude_pid = spawn.read_pid_now(dispatch_id)
            if claude_pid:
                db.update_claude_pid(dispatch_id, claude_pid)
        if claude_pid:
            await spawn.kill_pid_async(claude_pid)

        # Wait up to PAUSE_SESSION_GRACE_S for the Stop hook to deliver the
        # session_id (stored by /api/complete via db.attach_session). Each
        # poll re-reads the row; the read+branch below has no await between
        # them, so on the same event loop it sees a consistent value.
        iters = max(1, int(PAUSE_SESSION_GRACE_S / PAUSE_SESSION_POLL_S))
        session_id = None
        for _ in range(iters):
            await asyncio.sleep(PAUSE_SESSION_POLL_S)
            d = db.get_dispatch(dispatch_id)
            if d and d.get("session_id"):
                session_id = d["session_id"]
                break

        if session_id:
            # Resumable pause: keep session_id, and intentionally do NOT
            # cleanup_dispatch_files — the tab stays open and sidecars remain
            # so the user can see and `claude --resume` the paused session.
            db.mark_paused(dispatch_id, reason="timeout")
            db.record_event(dispatch_id, "stage", {"stage": "paused", "reason": "timeout"})
            log.info("Dispatch %s paused (timeout) — resumable via session %s",
                     dispatch_id, session_id)
        else:
            # No session_id arrived in time → hard kill as before (not resumable).
            db.kill_dispatch_record(dispatch_id, reason="timeout")
            spawn.cleanup_dispatch_files(dispatch_id)
            log.warning("Dispatch %s killed (timeout) — no session_id captured, "
                        "not resumable", dispatch_id)
        loop_watchdog.clear(dispatch_id)
        idle_notifier.clear(dispatch_id)
    except asyncio.CancelledError:
        pass
    finally:
        _pausing.discard(dispatch_id)
        _watchers.pop(dispatch_id, None)


async def manual_kill(dispatch_id: int, reason: str = "manual") -> bool:
    # Lazy import to avoid circular dep — loop_watchdog imports watchdog
    from orchestrator.lib import loop_watchdog
    """Kill a single dispatch (used by /api/dispatch/{id}/kill).

    Async so the SIGTERM grace period doesn't block the event loop for other
    dispatches. If the DB doesn't have a claude_pid yet (the initial 5s poll
    missed it), re-read the pid file before giving up.
    """
    d = db.get_dispatch(dispatch_id)
    if not d or d["status"] != "running":
        return False
    cancel(dispatch_id)
    pid = d.get("claude_pid")
    if not pid:
        pid = spawn.read_pid_now(dispatch_id)
        if pid:
            db.update_claude_pid(dispatch_id, pid)
    if pid:
        await spawn.kill_pid_async(int(pid))
    db.kill_dispatch_record(dispatch_id, reason=reason)
    spawn.cleanup_dispatch_files(dispatch_id)
    loop_watchdog.clear(dispatch_id)
    idle_notifier.clear(dispatch_id)
    return True


async def kill_all() -> int:
    """Kill every running dispatch concurrently. Returns count killed."""
    running = db.running_dispatches()
    if not running:
        return 0
    results = await asyncio.gather(
        *(manual_kill(d["id"], reason="killall") for d in running),
        return_exceptions=True,
    )
    return sum(1 for r in results if r is True)


def reap_orphans(min_age_s: int = 0):
    """For each 'running' dispatch, check if its claude PID is still alive.
    If not, mark it as orphaned (process died without Stop hook firing —
    happens when user Ctrl-D's manually or kills via Activity Monitor, when
    `claude` isn't on the tab's PATH so `exec` fails, or when orchestrator was
    down when the Stop hook tried to post).

    `min_age_s` guards the no-PID branch only: a just-spawned dispatch may not
    have written its PID file yet (iTerm startup latency), so when reaping
    periodically we skip no-PID dispatches younger than this to avoid
    false-orphaning a still-spawning one. Boot reaping passes 0 (nothing is
    mid-spawn during boot). A *dead* recorded PID is always reaped regardless
    of age — that's genuinely gone, even seconds after spawn."""
    now_ts = int(time.time())
    for d in db.running_dispatches():
        pid = d.get("claude_pid") or spawn.read_pid_now(d["id"])
        # No pid recorded AND no pid file → either dispatch never actually
        # started or its files were cleaned up. Mark orphaned, unless it's
        # young enough to still be writing its PID file.
        if not pid:
            if min_age_s:
                started = d.get("started_at") or d.get("created_at") or now_ts
                if (now_ts - started) < min_age_s:
                    continue
            db.mark_orphaned(d["id"], reason="no_pid_record")
            spawn.cleanup_dispatch_files(d["id"])
            continue
        if not spawn.pid_alive(pid):
            db.mark_orphaned(d["id"], reason="process_gone")
            spawn.cleanup_dispatch_files(d["id"])


async def run_orphan_reaper(interval_s: int = ORPHAN_REAP_INTERVAL_S):
    """Background loop: periodically reap dispatches whose claude process died
    without firing a Stop hook. Without this, such rows sit 'running' until the
    wall-clock cap (~30 min) or the next restart. reap_orphans() hits the DB and
    runs `ps`, so offload it to a thread to keep the event loop free. The first
    scan waits one interval — boot already reaped via resume_watchers_on_boot."""
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(interval_s)
        try:
            await loop.run_in_executor(None, reap_orphans, ORPHAN_REAP_GRACE_S)
        except Exception as e:
            log.warning("orphan reaper iteration failed: %s", e)


def resume_watchers_on_boot():
    """On orchestrator restart:
      1. Reap any dispatches whose claude is already dead (orphan cleanup).
      2. For dispatches still genuinely running, re-attach a wall-clock
         watchdog with the time they have left on their original cap.
    """
    reap_orphans()
    now_ts = int(time.time())
    for d in db.running_dispatches():
        started = d.get("started_at") or d.get("created_at") or now_ts
        cap = d.get("wall_clock_cap_s", 1800)
        remaining = max(60, cap - (now_ts - started))
        schedule(d["id"], d.get("claude_pid"), remaining)
