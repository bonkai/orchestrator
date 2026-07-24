"""SQLite schema + helpers. One DB file at ~/.orchestrator/orchestrator.db."""

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path.home() / ".orchestrator"
DB_PATH = DATA_DIR / "orchestrator.db"
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    path         TEXT NOT NULL UNIQUE,
    slug         TEXT NOT NULL,
    layout_json  TEXT,
    added_at     INTEGER NOT NULL,
    last_used_at INTEGER
);

CREATE TABLE IF NOT EXISTS ui_tabs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL UNIQUE,
    opened_at   INTEGER NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS dispatches (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id        INTEGER NOT NULL,
    created_at        INTEGER NOT NULL,
    started_at        INTEGER,
    ended_at          INTEGER,
    status            TEXT NOT NULL DEFAULT 'pending',
    user_task         TEXT NOT NULL,
    rewritten_prompt  TEXT,
    bundle_hash       TEXT,
    session_id        TEXT,
    terminal_pid      INTEGER,
    claude_pid        INTEGER,
    transcript_path   TEXT,
    wall_clock_cap_s  INTEGER NOT NULL DEFAULT 1800,
    -- F5: out-of-pocket spend for this dispatch's brain call (the fused-rewrite
    -- panel sum; $0 for a plain claude rewrite). `fused` flags that a real
    -- multi-model panel (>=2 seats) actually authored the rewrite — used for
    -- the ⚡ badge and copied into the outcome row at completion.
    cost_usd          REAL NOT NULL DEFAULT 0,
    fused             INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_dispatches_status ON dispatches(status);
CREATE INDEX IF NOT EXISTS idx_dispatches_project ON dispatches(project_id);

CREATE TABLE IF NOT EXISTS outcomes (
    dispatch_id     INTEGER PRIMARY KEY,
    outcome         TEXT NOT NULL,
    reason          TEXT,
    duration_s      INTEGER,
    summary_md      TEXT,
    what_worked     TEXT,
    what_broke      TEXT,
    lessons         TEXT,
    tags_json       TEXT,
    -- F5: copied from dispatches.cost_usd when the outcome row is created, so
    -- the learning loop reads per-dispatch out-of-pocket spend from one place.
    cost_usd        REAL NOT NULL DEFAULT 0,
    FOREIGN KEY (dispatch_id) REFERENCES dispatches(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS artifacts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id  INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    path         TEXT NOT NULL,
    UNIQUE(dispatch_id, kind),
    FOREIGN KEY (dispatch_id) REFERENCES dispatches(id) ON DELETE CASCADE
);

-- Phase 6: dense vectors for semantic cross-project retrieval.
-- `vector` is float32 little-endian packed bytes; cosine is computed in Python.
CREATE TABLE IF NOT EXISTS dispatch_embeddings (
    dispatch_id  INTEGER PRIMARY KEY,
    project_id   INTEGER NOT NULL,
    model        TEXT NOT NULL,
    dim          INTEGER NOT NULL,
    vector       BLOB NOT NULL,
    created_at   INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    FOREIGN KEY (dispatch_id) REFERENCES dispatches(id) ON DELETE CASCADE,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_embeddings_project ON dispatch_embeddings(project_id);

-- QoL: stream of live events for the dispatch detail page timeline.
-- Kinds: 'stage' (orchestrator lifecycle), 'tool_use', 'tool_result'.
-- payload_json is a small dict with kind-specific fields.
CREATE TABLE IF NOT EXISTS dispatch_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id   INTEGER NOT NULL,
    ts            INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    payload_json  TEXT,
    FOREIGN KEY (dispatch_id) REFERENCES dispatches(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_events_dispatch ON dispatch_events(dispatch_id, id);

-- Phase 9: persistent log of project onboarding runs. Each "analyze project"
-- click writes one row so the user can rerun analysis over time and see
-- exactly what changed each round. `result_json` is the full serialized
-- OnboardingResult — applied/skipped/failed lists, edits, recommendations,
-- scan — enough to fully re-render the detail page.
CREATE TABLE IF NOT EXISTS onboarding_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id       INTEGER NOT NULL,
    created_at       INTEGER NOT NULL,
    ok               INTEGER NOT NULL DEFAULT 0,
    model            TEXT,
    duration_s       REAL,
    cost_usd         REAL,
    project_summary  TEXT,
    error            TEXT,
    applied_count    INTEGER NOT NULL DEFAULT 0,
    skipped_count    INTEGER NOT NULL DEFAULT 0,
    failed_count     INTEGER NOT NULL DEFAULT 0,
    result_json      TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_onboarding_runs_project ON onboarding_runs(project_id, id);

-- U1 (USAGE_PLAN.md): local metering ledger. One row per engine CALL (live
-- collector taps in claude_runner / the executor pollers, plus the historical
-- backfill). No FK on dispatch_id: this is an accounting ledger and must
-- survive its dispatch row; dispatch_id is NULL for calls not tied to one
-- (kimi-log backfill rows, brain calls recorded outside a dispatch).
-- error_class stays NULL until U2's classifier; raw_error is the verbatim
-- (bounded) error string — including today's degraded 'kimi exit 1'.
-- `source` is the backfill's idempotency key (e.g. 'pb:<event_id>:<seat_idx>',
-- 'kimilog:<iso_ts>:<hash>'); live rows leave it NULL. The partial UNIQUE
-- index makes every backfill INSERT OR IGNORE re-runnable.
CREATE TABLE IF NOT EXISTS usage_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                INTEGER NOT NULL,
    engine            TEXT NOT NULL,
    model             TEXT,
    role              TEXT NOT NULL,          -- seat | executor | judge | brain
    dispatch_id       INTEGER,
    calls             INTEGER NOT NULL DEFAULT 1,
    prompt_tokens     INTEGER,                -- NULL = engine doesn't report (kimi, CLI seats)
    completion_tokens INTEGER,
    ok                INTEGER NOT NULL DEFAULT 1,
    error_class       TEXT,                   -- U2's classifier fills this
    raw_error         TEXT,
    source            TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_engine_ts ON usage_events(engine, ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_source
    ON usage_events(source) WHERE source IS NOT NULL;

-- U1: per-engine limit state (Layer B). U1 only maintains last_ok_at /
-- last_error from the collector and lets the BACKFILL set limited_since from
-- the one pinned kimi cycle-quota signal; the live limit-hit ⇒ LIMITED /
-- next-ok ⇒ clear transitions (and reset_hint parsing) are U2.
CREATE TABLE IF NOT EXISTS engine_limit_state (
    engine        TEXT PRIMARY KEY,
    limited_since INTEGER,
    reset_hint    TEXT,
    last_ok_at    INTEGER,
    last_error    TEXT
);
"""


def now() -> int:
    return int(time.time())


@contextmanager
def conn():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    # timeout=10 → wait up to 10s if another writer holds the lock instead of
    # immediately raising OperationalError. WAL mode (set in init_db) lets
    # readers + one writer proceed concurrently, which matters with 10+
    # parallel dispatches all writing to the same db.
    c = sqlite3.connect(DB_PATH, timeout=10.0)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA busy_timeout = 10000")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _column_names(c, table: str) -> set[str]:
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(c):
    """Idempotent additive migrations for columns introduced after a DB already
    exists (CREATE TABLE IF NOT EXISTS never adds columns to an existing table).
    Each ALTER is guarded by a column-presence check, so this is a safe no-op on
    an already-migrated DB and on a freshly-created one. ADD COLUMN with a
    NOT NULL needs a DEFAULT (we use 0) — SQLite backfills existing rows."""
    disp_cols = _column_names(c, "dispatches")
    if "cost_usd" not in disp_cols:
        c.execute("ALTER TABLE dispatches ADD COLUMN cost_usd REAL NOT NULL DEFAULT 0")
    if "fused" not in disp_cols:
        c.execute("ALTER TABLE dispatches ADD COLUMN fused INTEGER NOT NULL DEFAULT 0")
    if "cost_usd" not in _column_names(c, "outcomes"):
        c.execute("ALTER TABLE outcomes ADD COLUMN cost_usd REAL NOT NULL DEFAULT 0")


def init_db():
    with conn() as c:
        # WAL allows concurrent reads with one writer. NORMAL synchronous is
        # safe with WAL and noticeably faster than FULL for our workload.
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("PRAGMA synchronous = NORMAL")
        c.executescript(SCHEMA)
        _migrate(c)


# ─── projects ─────────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "project"


def add_project(path: str) -> dict:
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise ValueError(f"Not a directory: {p}")
    slug = slugify(p.name)
    layout = None
    forge_json = p / ".forge.json"
    if forge_json.is_file():
        try:
            layout = forge_json.read_text()
        except Exception:
            pass
    with conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO projects(path, slug, layout_json, added_at) "
            "VALUES (?, ?, ?, ?)",
            (str(p), slug, layout, now()),
        )
        row = c.execute("SELECT * FROM projects WHERE path = ?", (str(p),)).fetchone()
        return dict(row)


def list_projects() -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM projects ORDER BY last_used_at DESC, added_at DESC").fetchall()
        return [dict(r) for r in rows]


def get_project(pid: int) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        return dict(row) if row else None


def touch_project(pid: int):
    with conn() as c:
        c.execute("UPDATE projects SET last_used_at = ? WHERE id = ?", (now(), pid))


# ─── tabs ─────────────────────────────────────────────────────────────────

def list_tabs() -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT t.*, p.path, p.slug FROM ui_tabs t "
            "JOIN projects p ON p.id = t.project_id "
            "ORDER BY t.sort_order, t.opened_at"
        ).fetchall()
        return [dict(r) for r in rows]


def open_tab(project_id: int):
    with conn() as c:
        max_order = c.execute("SELECT COALESCE(MAX(sort_order), -1) FROM ui_tabs").fetchone()[0]
        c.execute(
            "INSERT OR IGNORE INTO ui_tabs(project_id, opened_at, sort_order) VALUES (?, ?, ?)",
            (project_id, now(), max_order + 1),
        )


def close_tab(project_id: int):
    with conn() as c:
        c.execute("DELETE FROM ui_tabs WHERE project_id = ?", (project_id,))


# ─── dispatches ───────────────────────────────────────────────────────────

def create_dispatch(project_id: int, user_task: str, wall_clock_cap_s: int = 1800) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO dispatches(project_id, created_at, status, user_task, wall_clock_cap_s) "
            "VALUES (?, ?, 'pending', ?, ?)",
            (project_id, now(), user_task, wall_clock_cap_s),
        )
        return cur.lastrowid


def mark_started(dispatch_id: int, terminal_pid: int | None, claude_pid: int | None = None):
    with conn() as c:
        c.execute(
            "UPDATE dispatches SET status = 'running', started_at = ?, "
            "terminal_pid = ?, claude_pid = ? WHERE id = ?",
            (now(), terminal_pid, claude_pid, dispatch_id),
        )


def set_dispatch_cost(dispatch_id: int, cost_usd: float, fused: bool = False):
    """F5: record the brain-call out-of-pocket spend (and whether a real fused
    panel authored the rewrite) on the dispatch row. Set at /send time, after the
    rewrite; the value is copied into the outcome row when that row is created.
    Never raises — cost accounting must not break a dispatch."""
    try:
        with conn() as c:
            c.execute(
                "UPDATE dispatches SET cost_usd = ?, fused = ? WHERE id = ?",
                (float(cost_usd or 0), 1 if fused else 0, dispatch_id),
            )
    except Exception as e:
        import logging
        logging.getLogger("orchestrator.db").warning("set_dispatch_cost failed: %s", e)


# F5: every outcome INSERT carries cost_usd forward from the dispatch row via
# this subquery, so the per-dispatch spend lands on the outcome the learning loop
# reads — regardless of how the dispatch terminated (complete/kill/pause/orphan).
_COST_SUBQ = "COALESCE((SELECT cost_usd FROM dispatches WHERE id = ?), 0)"


def mark_failed_to_spawn(dispatch_id: int, reason: str):
    with conn() as c:
        c.execute(
            "UPDATE dispatches SET status = 'failed', ended_at = ? WHERE id = ?",
            (now(), dispatch_id),
        )
        c.execute(
            "INSERT OR REPLACE INTO outcomes(dispatch_id, outcome, reason, duration_s, cost_usd) "
            f"VALUES (?, 'failed_to_spawn', ?, 0, {_COST_SUBQ})",
            (dispatch_id, reason, dispatch_id),
        )


def complete_dispatch(
    dispatch_id: int,
    session_id: str | None,
    transcript_path: str | None,
    exit_reason: str | None,
    outcome: str = "completed",
) -> bool:
    """Atomically mark a dispatch completed. Returns True only if this call
    actually changed the row (i.e., it was still 'running' or 'pending').

    The UPDATE guards on current status, so concurrent Stop-hook POSTs for
    the same dispatch race to set the row exactly once — the loser sees
    rowcount=0 and skips outcome/transcript work.
    """
    with conn() as c:
        row = c.execute(
            "SELECT started_at, created_at FROM dispatches WHERE id = ?",
            (dispatch_id,),
        ).fetchone()
        if not row:
            return False
        started = row["started_at"] or row["created_at"]
        duration = now() - started
        cur = c.execute(
            "UPDATE dispatches SET status = ?, ended_at = ?, session_id = ?, transcript_path = ? "
            "WHERE id = ? AND status NOT IN ('completed','killed','failed','paused')",
            (outcome, now(), session_id, transcript_path, dispatch_id),
        )
        if cur.rowcount == 0:
            return False
        c.execute(
            "INSERT OR REPLACE INTO outcomes(dispatch_id, outcome, reason, duration_s, cost_usd) "
            f"VALUES (?, ?, ?, ?, {_COST_SUBQ})",
            (dispatch_id, outcome, exit_reason, duration, dispatch_id),
        )
        return True


def kill_dispatch_record(dispatch_id: int, reason: str):
    """Mark as killed in DB. The actual SIGTERM happens in spawn.py."""
    with conn() as c:
        row = c.execute(
            "SELECT started_at, created_at, status FROM dispatches WHERE id = ?",
            (dispatch_id,),
        ).fetchone()
        if not row or row["status"] in ("completed", "killed", "failed"):
            return
        started = row["started_at"] or row["created_at"]
        duration = now() - started
        c.execute(
            "UPDATE dispatches SET status = 'killed', ended_at = ? WHERE id = ?",
            (now(), dispatch_id),
        )
        c.execute(
            "INSERT OR REPLACE INTO outcomes(dispatch_id, outcome, reason, duration_s, cost_usd) "
            f"VALUES (?, 'killed', ?, ?, {_COST_SUBQ})",
            (dispatch_id, reason, duration, dispatch_id),
        )


def mark_paused(dispatch_id: int, reason: str):
    """Mark a dispatch as gracefully paused (e.g. on wall-clock timeout).

    Same shape as `kill_dispatch_record` but the terminal status is 'paused'
    and the outcome is 'paused'. Crucially this does NOT touch session_id, so
    a session_id captured by the Stop hook (via `attach_session`) survives and
    the dispatch stays resumable. The actual SIGTERM happens in spawn.py.
    """
    with conn() as c:
        row = c.execute(
            "SELECT started_at, created_at, status FROM dispatches WHERE id = ?",
            (dispatch_id,),
        ).fetchone()
        if not row or row["status"] in ("completed", "killed", "failed", "paused"):
            return
        started = row["started_at"] or row["created_at"]
        duration = now() - started
        c.execute(
            "UPDATE dispatches SET status = 'paused', ended_at = ? WHERE id = ?",
            (now(), dispatch_id),
        )
        c.execute(
            "INSERT OR REPLACE INTO outcomes(dispatch_id, outcome, reason, duration_s, cost_usd) "
            f"VALUES (?, 'paused', ?, ?, {_COST_SUBQ})",
            (dispatch_id, reason, duration, dispatch_id),
        )


def attach_session(dispatch_id: int, session_id: str | None, transcript_path: str | None):
    """Store the session_id / transcript_path on a dispatch WITHOUT changing
    its status or writing an outcome row.

    Used during a graceful pause: the wall-clock watchdog has SIGTERM'd the
    dispatch and is waiting for the Stop hook's POST to deliver the session_id
    it needs to stay resumable, but the watchdog — not /api/complete — owns the
    terminal 'paused' status. Columns are filled with COALESCE so we never
    clobber a value a real completion already wrote.
    """
    with conn() as c:
        c.execute(
            "UPDATE dispatches SET "
            "session_id = COALESCE(session_id, ?), "
            "transcript_path = COALESCE(transcript_path, ?) "
            "WHERE id = ?",
            (session_id, transcript_path, dispatch_id),
        )


def get_dispatch(dispatch_id: int) -> dict | None:
    with conn() as c:
        row = c.execute(
            "SELECT d.*, o.outcome as final_outcome, o.reason as outcome_reason, o.duration_s "
            "FROM dispatches d LEFT JOIN outcomes o ON o.dispatch_id = d.id "
            "WHERE d.id = ?",
            (dispatch_id,),
        ).fetchone()
        return dict(row) if row else None


def recent_dispatches(limit: int = 50, project_id: int | None = None) -> list[dict]:
    q = (
        "SELECT d.*, p.slug as project_slug, p.path as project_path, "
        "o.outcome as final_outcome, o.reason as outcome_reason, o.duration_s, "
        "(SELECT COUNT(*) FROM dispatch_events WHERE dispatch_id=d.id AND kind='tool_use') as tool_calls "
        "FROM dispatches d "
        "JOIN projects p ON p.id = d.project_id "
        "LEFT JOIN outcomes o ON o.dispatch_id = d.id "
    )
    args: tuple = ()
    if project_id is not None:
        q += "WHERE d.project_id = ? "
        args = (project_id,)
    q += "ORDER BY d.id DESC LIMIT ?"
    args = args + (limit,)
    with conn() as c:
        rows = c.execute(q, args).fetchall()
        return [dict(r) for r in rows]


def running_dispatches() -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT d.*, p.slug as project_slug, p.path as project_path "
            "FROM dispatches d JOIN projects p ON p.id = d.project_id "
            "WHERE d.status = 'running' ORDER BY d.id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_running_dispatch_last_tool_use(dispatch_id: int) -> int | None:
    with conn() as c:
        row = c.execute(
            "SELECT MAX(ts) FROM dispatch_events "
            "WHERE dispatch_id = ? AND kind = 'tool_use'",
            (dispatch_id,),
        ).fetchone()
        return row[0] if row and row[0] is not None else None


def running_dispatches_with_last_activity() -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT d.*, p.slug as project_slug, p.path as project_path, "
            "MAX(CASE WHEN e.kind = 'tool_use' THEN e.ts END) as last_tool_use_ts "
            "FROM dispatches d "
            "JOIN projects p ON p.id = d.project_id "
            "LEFT JOIN dispatch_events e ON e.dispatch_id = d.id "
            "WHERE d.status = 'running' "
            "GROUP BY d.id ORDER BY d.id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def record_event(dispatch_id: int, kind: str, payload: dict | None = None):
    """Append an event to the dispatch's timeline. Used by:
      - /api/tool_use     (kind='tool_use')
      - /api/tool_result  (kind='tool_result')
      - orchestrator-internal lifecycle stages (kind='stage')

    Never raises — events are best-effort UX, not correctness-critical."""
    try:
        with conn() as c:
            c.execute(
                "INSERT INTO dispatch_events(dispatch_id, ts, kind, payload_json) "
                "VALUES (?, ?, ?, ?)",
                (dispatch_id, now(), kind, json.dumps(payload or {}, default=str)),
            )
    except Exception as e:
        # Silently swallow — better to lose a UX event than break a dispatch
        import logging
        logging.getLogger("orchestrator.db").warning("record_event failed: %s", e)


# ─── usage metering (U1, USAGE_PLAN.md) ───────────────────────────────────

# The collector is ARMED explicitly — by the server process (app lifespan) and
# by the backfill CLI — never as an import side effect. Two reasons: the test
# suite exercises the claude_runner funnels with heavy mocking and must not
# write usage rows into the real ~/.orchestrator DB; and it matches reality —
# with reload=False the collector is inert until `python -m orchestrator` is
# restarted anyway.
_usage_collection_enabled = False

# Bound raw_error so a runaway stderr dump can't bloat the ledger. The string
# is stored VERBATIM (truncated) — classification is U2's job, not U1's.
_RAW_ERROR_MAX = 2000


def enable_usage_collection(enabled: bool = True):
    """Arm (or disarm) the usage collector for this process."""
    global _usage_collection_enabled
    _usage_collection_enabled = enabled


def touch_engine_state(engine: str, last_ok_at: int | None = None,
                       last_error: str | None = None):
    """Upsert engine_limit_state bookkeeping fields. last_ok_at is monotonic
    (only ever moves forward); last_error overwrites when provided (callers
    apply events in ts order, so the newest error wins). limited_since /
    reset_hint are NOT touched here — transitions are U2 (the backfill sets
    them via set_engine_limited). Never raises."""
    try:
        with conn() as c:
            c.execute(
                "INSERT INTO engine_limit_state(engine, last_ok_at, last_error) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(engine) DO UPDATE SET "
                "last_ok_at = CASE WHEN excluded.last_ok_at IS NOT NULL "
                "  AND COALESCE(engine_limit_state.last_ok_at, 0) < excluded.last_ok_at "
                "  THEN excluded.last_ok_at ELSE engine_limit_state.last_ok_at END, "
                "last_error = COALESCE(excluded.last_error, engine_limit_state.last_error)",
                (engine, last_ok_at, last_error),
            )
    except Exception as e:
        import logging
        logging.getLogger("orchestrator.db").warning("touch_engine_state failed: %s", e)


def record_usage(engine: str, *, model: str | None = None, role: str = "brain",
                 dispatch_id: int | None = None, ok: bool = True,
                 prompt_tokens: int | None = None,
                 completion_tokens: int | None = None,
                 raw_error: str | None = None, calls: int = 1,
                 ts: int | None = None, source: str | None = None,
                 error_class: str | None = None, limit_hit: bool = False,
                 reset_hint: str | None = None) -> bool:
    """Append ONE usage event and apply the engine's limit-state bookkeeping.
    Returns True only when a row was actually inserted — a `source`-keyed
    re-insert (backfill re-run) is ignored by the partial UNIQUE index and
    returns False without touching state.

    U2 transitions (the callers classify — db stays config-free): the caller
    passes `error_class` (stored on the row) and `limit_hit` (whether that
    class means "limited right now", per config.USAGE_LIMIT_CLASSES):
      - ok call        ⇒ last_ok_at forward + LIMITED CLEARED (limited_since /
                         reset_hint → NULL) — the plan's "next ok call clears".
      - limit-hit call ⇒ limited_since = first hit's ts (kept on repeat hits,
                         so "LIMITED since T" stays the ONSET), reset_hint
                         filled when the classifier parsed one.
      - other failure  ⇒ last_error only; LIMITED state untouched.
    No-op (False) until enable_usage_collection() arms the process.
    Never raises — metering must not break a call, like record_event."""
    if not _usage_collection_enabled:
        return False
    try:
        ts = int(ts if ts is not None else now())
        if raw_error is not None:
            raw_error = str(raw_error)[:_RAW_ERROR_MAX]
        with conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO usage_events("
                "ts, engine, model, role, dispatch_id, calls, "
                "prompt_tokens, completion_tokens, ok, error_class, raw_error, source"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, engine, model or None, role, dispatch_id, int(calls),
                 prompt_tokens, completion_tokens, 1 if ok else 0,
                 error_class, raw_error, source),
            )
            if cur.rowcount == 0:
                return False
            c.execute(
                "INSERT INTO engine_limit_state(engine, last_ok_at, last_error, "
                "limited_since, reset_hint) VALUES (:e, :ok_ts, :err, :lim_ts, :hint) "
                "ON CONFLICT(engine) DO UPDATE SET "
                "last_ok_at = CASE WHEN :ok_ts IS NOT NULL "
                "  AND COALESCE(engine_limit_state.last_ok_at, 0) < :ok_ts "
                "  THEN :ok_ts ELSE engine_limit_state.last_ok_at END, "
                "last_error = COALESCE(:err, engine_limit_state.last_error), "
                "limited_since = CASE WHEN :is_ok = 1 THEN NULL "
                "  WHEN :is_hit = 1 THEN COALESCE(engine_limit_state.limited_since, :lim_ts) "
                "  ELSE engine_limit_state.limited_since END, "
                "reset_hint = CASE WHEN :is_ok = 1 THEN NULL "
                "  WHEN :is_hit = 1 THEN COALESCE(:hint, engine_limit_state.reset_hint) "
                "  ELSE engine_limit_state.reset_hint END",
                {"e": engine, "ok_ts": ts if ok else None,
                 "err": raw_error if not ok else None,
                 "lim_ts": ts if (not ok and limit_hit) else None,
                 "hint": reset_hint if (not ok and limit_hit) else None,
                 "is_ok": 1 if ok else 0,
                 "is_hit": 1 if (not ok and limit_hit) else 0},
            )
        return True
    except Exception as e:
        import logging
        logging.getLogger("orchestrator.db").warning("record_usage failed: %s", e)
        return False


def ensure_engine_limit_rows(engines: list[str]):
    """Seed one engine_limit_state row per engine (all-NULL state), so the
    usage page always has a row to render per engine. Idempotent — existing
    rows (and their state) are untouched. The engine list comes from
    config.usage_engines(); it is passed in because db.py cannot import
    config (config imports db). Never raises."""
    try:
        with conn() as c:
            c.executemany(
                "INSERT OR IGNORE INTO engine_limit_state(engine) VALUES (?)",
                [(e,) for e in engines],
            )
    except Exception as e:
        import logging
        logging.getLogger("orchestrator.db").warning("ensure_engine_limit_rows failed: %s", e)


def set_engine_limited(engine: str, limited_since: int | None,
                       reset_hint: str | None = None):
    """Set (or, with limited_since=None, clear) an engine's LIMITED state.
    U1's only caller is the backfill's pinned kimi-403 rule; U2's live
    classifier becomes the real owner of these transitions. Never raises."""
    try:
        with conn() as c:
            c.execute(
                "INSERT INTO engine_limit_state(engine, limited_since, reset_hint) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(engine) DO UPDATE SET "
                "limited_since = excluded.limited_since, "
                "reset_hint = excluded.reset_hint",
                (engine, limited_since, reset_hint),
            )
    except Exception as e:
        import logging
        logging.getLogger("orchestrator.db").warning("set_engine_limited failed: %s", e)


def get_events(dispatch_id: int, since_id: int = 0, limit: int = 200) -> list[dict]:
    """Return events for a dispatch with id > since_id, oldest first.
    Used by the UI's polling timeline."""
    with conn() as c:
        rows = c.execute(
            "SELECT id, ts, kind, payload_json FROM dispatch_events "
            "WHERE dispatch_id = ? AND id > ? "
            "ORDER BY id ASC LIMIT ?",
            (dispatch_id, since_id, limit),
        ).fetchall()
        out = []
        for r in rows:
            payload = {}
            try:
                payload = json.loads(r["payload_json"]) if r["payload_json"] else {}
            except (ValueError, TypeError):
                pass
            out.append({"id": r["id"], "ts": r["ts"], "kind": r["kind"], "payload": payload})
        return out


def count_events(dispatch_id: int, kind: str | None = None) -> int:
    """How many events of `kind` (or any kind) for this dispatch. Used by
    the runs panel to show 'N tool calls' as a quick progress indicator."""
    with conn() as c:
        if kind:
            row = c.execute(
                "SELECT COUNT(*) FROM dispatch_events WHERE dispatch_id = ? AND kind = ?",
                (dispatch_id, kind),
            ).fetchone()
        else:
            row = c.execute(
                "SELECT COUNT(*) FROM dispatch_events WHERE dispatch_id = ?",
                (dispatch_id,),
            ).fetchone()
        return row[0] if row else 0


def update_claude_pid(dispatch_id: int, pid: int):
    """Late update: if the PID arrived after dispatch returned."""
    with conn() as c:
        c.execute(
            "UPDATE dispatches SET claude_pid = ? WHERE id = ? AND claude_pid IS NULL",
            (pid, dispatch_id),
        )


def set_summary(dispatch_id: int, summary_md: str, what_worked: str,
                 what_broke: str, lessons: str, tags: list[str]):
    """Fill the outcomes-row summary fields. Used by the summarizer
    background task after /api/complete fires."""
    tags_json = json.dumps(tags) if tags else None
    with conn() as c:
        c.execute(
            "UPDATE outcomes SET summary_md = ?, what_worked = ?, what_broke = ?, "
            "lessons = ?, tags_json = ? WHERE dispatch_id = ?",
            (summary_md, what_worked, what_broke, lessons, tags_json, dispatch_id),
        )


def get_dispatch_with_project(dispatch_id: int) -> dict | None:
    """get_dispatch + project_path joined in. Used by summarizer to know
    which cwd to run from."""
    with conn() as c:
        row = c.execute(
            "SELECT d.*, p.path as project_path, p.slug as project_slug, "
            "o.outcome as final_outcome, o.reason as outcome_reason, o.duration_s, "
            "o.summary_md, o.what_worked, o.what_broke, o.lessons, o.tags_json "
            "FROM dispatches d "
            "JOIN projects p ON p.id = d.project_id "
            "LEFT JOIN outcomes o ON o.dispatch_id = d.id "
            "WHERE d.id = ?",
            (dispatch_id,),
        ).fetchone()
        return dict(row) if row else None


# ─── onboarding runs ──────────────────────────────────────────────────────

def save_onboarding_run(
    project_id: int,
    *,
    ok: bool,
    model: str | None,
    duration_s: float | None,
    cost_usd: float | None,
    project_summary: str | None,
    error: str | None,
    applied_count: int,
    skipped_count: int,
    failed_count: int,
    result_json: str,
) -> int:
    """Persist one onboarding analysis round. Returns the new run id."""
    with conn() as c:
        cur = c.execute(
            "INSERT INTO onboarding_runs("
            "project_id, created_at, ok, model, duration_s, cost_usd, "
            "project_summary, error, applied_count, skipped_count, failed_count, result_json"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, now(), 1 if ok else 0, model, duration_s, cost_usd,
             project_summary, error, applied_count, skipped_count, failed_count,
             result_json),
        )
        return cur.lastrowid


def list_onboarding_runs(project_id: int, limit: int = 50) -> list[dict]:
    """Newest first. Excludes `result_json` to keep the list view light."""
    with conn() as c:
        rows = c.execute(
            "SELECT id, project_id, created_at, ok, model, duration_s, cost_usd, "
            "project_summary, error, applied_count, skipped_count, failed_count "
            "FROM onboarding_runs WHERE project_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_onboarding_run(run_id: int) -> dict | None:
    """Full row including `result_json` for the detail view."""
    with conn() as c:
        row = c.execute(
            "SELECT * FROM onboarding_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        return dict(row) if row else None


def latest_onboarding_run(project_id: int) -> dict | None:
    """Most recent run for a project (header row only, no result_json)."""
    with conn() as c:
        row = c.execute(
            "SELECT id, project_id, created_at, ok, applied_count, skipped_count, "
            "failed_count, project_summary "
            "FROM onboarding_runs WHERE project_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        return dict(row) if row else None


def mark_orphaned(dispatch_id: int, reason: str = "process_gone"):
    """Used by the stale reaper: dispatch's claude PID no longer exists."""
    with conn() as c:
        row = c.execute(
            "SELECT started_at, created_at FROM dispatches WHERE id = ?",
            (dispatch_id,),
        ).fetchone()
        if not row:
            return
        started = row["started_at"] or row["created_at"]
        duration = now() - started
        c.execute(
            "UPDATE dispatches SET status = 'completed', ended_at = ? WHERE id = ?",
            (now(), dispatch_id),
        )
        c.execute(
            "INSERT OR REPLACE INTO outcomes(dispatch_id, outcome, reason, duration_s, cost_usd) "
            f"VALUES (?, 'orphaned', ?, ?, {_COST_SUBQ})",
            (dispatch_id, reason, duration, dispatch_id),
        )
