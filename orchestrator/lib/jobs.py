"""Tiny in-memory job registry for async work that outlives the HTTP request.

Used by routes whose work (a `claude` brain call) takes long enough that
a browser tab disconnect during the wait would lose the result. The route
creates a job, kicks off the work as a background asyncio task held in a
strong-ref set in app.py, then returns immediately with a job_id. The
client polls `/api/job/{job_id}` until status is done/error.

In-memory is fine: jobs are short-lived (minutes), the server is a single
local process, and there's no value in persisting a transient analysis
result across restarts — the user just re-runs.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

_LOCK = threading.Lock()
_JOBS: dict[str, dict] = {}
_TTL_S = 3600  # drop finished jobs after an hour so the dict can't grow forever


def _gc_locked() -> None:
    """Called under _LOCK. Drop jobs older than TTL."""
    now = time.time()
    stale = [k for k, v in _JOBS.items() if now - v["created_at"] > _TTL_S]
    for k in stale:
        _JOBS.pop(k, None)


def create(kind: str) -> str:
    """Create a new pending job and return its id."""
    job_id = uuid.uuid4().hex[:12]
    with _LOCK:
        _gc_locked()
        _JOBS[job_id] = {
            "id": job_id,
            "kind": kind,
            "status": "pending",
            "result": None,
            "error": None,
            "created_at": time.time(),
        }
    return job_id


def set_done(job_id: str, result: Any) -> None:
    with _LOCK:
        j = _JOBS.get(job_id)
        if j is not None:
            j["status"] = "done"
            j["result"] = result


def set_error(job_id: str, error: str) -> None:
    with _LOCK:
        j = _JOBS.get(job_id)
        if j is not None:
            j["status"] = "error"
            j["error"] = error


def get(job_id: str) -> dict | None:
    with _LOCK:
        j = _JOBS.get(job_id)
        # Return a shallow copy so callers can't mutate registry state.
        return dict(j) if j is not None else None
