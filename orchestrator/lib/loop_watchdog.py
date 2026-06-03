"""Loop watchdog — kills a dispatch when Claude is stuck repeating the
same tool call N times in a row.

How it works:
  1. PreToolUse hook fires for every tool call.
  2. notify_tool_use.sh posts (run_id, tool_name, input_hash) to /api/tool_use.
  3. We keep a per-dispatch ring buffer of the last N fingerprints.
  4. When the buffer is full AND all N entries are identical → kill with
     reason="loop".

Why this matters: stuck-in-a-loop is the most common way claude wastes
tokens. Wall-clock cap eventually catches it but burns budget first.
This kills within ~N tool calls of detecting the loop.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque

log = logging.getLogger("orchestrator.loop_watchdog")

# Default: kill after 8 consecutive identical (tool_name, input_hash) calls.
# Tuned for "claude is genuinely stuck" — legitimate retries usually vary
# something (file path, line range, etc.) so they don't collide.
DEFAULT_LOOP_THRESHOLD = 8

# Per-dispatch ring buffers. Keys are dispatch_id, values are deques of
# (tool_name, input_hash) tuples capped at the threshold.
_buffers: dict[int, deque] = defaultdict(lambda: deque(maxlen=DEFAULT_LOOP_THRESHOLD))


def record(dispatch_id: int, tool_name: str, input_hash: str,
           threshold: int = DEFAULT_LOOP_THRESHOLD) -> bool:
    """Append a tool-call fingerprint. Returns True iff a loop was detected
    (all `threshold` recent entries are the same fingerprint)."""
    buf = _buffers[dispatch_id]
    if buf.maxlen != threshold:
        # Resize if the threshold changed — preserve recent entries
        new = deque(buf, maxlen=threshold)
        _buffers[dispatch_id] = new
        buf = new
    fp = (tool_name or "?", input_hash or "")
    buf.append(fp)
    if len(buf) < threshold:
        return False
    first = buf[0]
    return all(x == first for x in buf)


def clear(dispatch_id: int):
    """Drop tracking state for a dispatch (called on kill/completion)."""
    _buffers.pop(dispatch_id, None)


def buffer_size(dispatch_id: int) -> int:
    """How many fingerprints are currently tracked. Test helper."""
    return len(_buffers.get(dispatch_id, ()))


async def trigger_kill(dispatch_id: int, fingerprint: tuple[str, str]):
    """Kill a dispatch detected as looping. Importing watchdog lazily to
    avoid a circular import (watchdog also touches spawn/db)."""
    from orchestrator.lib import watchdog
    tool_name, _ = fingerprint
    log.warning("Loop detected on dispatch %s: %d consecutive identical %s calls — killing",
                dispatch_id, DEFAULT_LOOP_THRESHOLD, tool_name)
    clear(dispatch_id)
    await watchdog.manual_kill(dispatch_id, reason=f"loop:{tool_name}")
