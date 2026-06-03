"""Idle-dispatch notifier. When a running Claude session hasn't made a tool
call in IDLE_THRESHOLD_S seconds, fire a macOS notification so the user
knows Claude may be waiting for input.
"""

import asyncio
import logging
import subprocess
import time

from orchestrator.lib import db

log = logging.getLogger("orchestrator.idle_notifier")

IDLE_THRESHOLD_S = 300  # 5 minutes

# dispatch_ids that have already had a notification fired this idle period.
# Cleared on new tool_use (reset_idle) or dispatch completion/kill (clear).
_notified: set[int] = set()


async def run_idle_checker(interval_s: int = 60):
    """Loop forever, checking every interval_s seconds for idle dispatches."""
    while True:
        try:
            now_ts = int(time.time())
            for d in db.running_dispatches_with_last_activity():
                last = d.get("last_tool_use_ts")
                did = d["id"]
                if last is None:
                    continue
                if (now_ts - last) > IDLE_THRESHOLD_S and did not in _notified:
                    _send_notification(did, d.get("project_slug") or "")
                    _notified.add(did)
        except Exception as e:
            log.warning("idle checker iteration failed: %s", e)
        await asyncio.sleep(interval_s)


def _send_notification(dispatch_id: int, project_slug: str):
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "Dispatch #{dispatch_id} may be waiting for your input" '
             f'with title "Orchestrator" subtitle "{project_slug}"'],
            capture_output=True, timeout=5,
        )
    except Exception as e:
        log.warning("idle notification failed for #%s: %s", dispatch_id, e)


def reset_idle(dispatch_id: int):
    """New tool_use arrived — re-arm the notifier for this dispatch."""
    _notified.discard(dispatch_id)


def clear(dispatch_id: int):
    """Dispatch is done (completed/killed) — drop its notified state."""
    _notified.discard(dispatch_id)
