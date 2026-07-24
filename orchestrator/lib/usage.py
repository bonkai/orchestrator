"""U1 (USAGE_PLAN.md): the historical usage BACKFILL + engine-state recompute.

Live metering is collected by the taps in claude_runner / the app pollers as
calls happen; this module makes the ledger useful RETROACTIVELY, on day one:

  - historical fusion panels → usage_events. Per-seat ok/tokens/error already
    persist as `panel_breakdown` JSON inside dispatch_events STAGE rows
    (stages rewrite_ok / rewrite_skipped — there is NO panel_breakdown table).
  - kimi-log 403s → limit events. The pinned cycle-quota signal
    (config.KIMI_LIMIT_SIGNAL, §3) appears in ~/.kimi-code/logs/kimi-code.log
    as an indented continuation line under a timestamped (UTC) log line, so
    the parser carries the last-seen timestamp forward.

Idempotency (two mechanisms, both required):
  - every backfilled row carries a deterministic `source` key
    ('pb:<event_id>:<seat_idx>' / 'kimilog:<iso_ts>:<hash>'); the partial
    UNIQUE index on usage_events.source makes re-runs INSERT OR IGNORE no-ops.
  - a history/live BOUNDARY: only events older than the FIRST live-collected
    row (source IS NULL) are ingested, so a call metered live can never be
    double-counted when its panel_breakdown event is backfilled later. Before
    any live row exists the cutoff is "now", i.e. everything historical.

After ingesting, engine_limit_state is RECOMPUTED per engine from the full
usage_events table (deterministic regardless of insertion order): last_ok_at =
newest ok event, last_error = newest failed event's raw_error. limited_since
is set ONLY by the one pinned kimi rule — newest kimi limit-signal event, iff
no kimi ok event is newer — with reset_hint left NULL. That is the plan-named
U1 slice, not a classifier; the per-engine error→class map and the live
LIMITED/clear transitions are U2.

Run manually (the deliberate invocation model — no startup hook, no surprise
writes at boot):  source .venv/bin/activate && python -m orchestrator.lib.usage
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Optional

from orchestrator.lib import config, db

# Matches the timestamp prefix of a kimi-code log line, e.g.
# '2026-07-20T21:49:11.832Z ERROR startup failed ...' (UTC, ms precision).
_KIMI_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z)\b")

# Claude seat names in panel_breakdown are '<model>-<effort>' (e.g.
# 'opus-xhigh'); a failed seat carries no model field, so attribution falls
# back to these name prefixes.
_CLAUDE_PREFIXES = ("opus", "sonnet", "haiku", "claude")


def attribute_seat_engine(seat: dict, provider_names: set) -> str:
    """Map ONE panel_breakdown seat to its engine. Precedence: the registry
    base name (F12 duplicate seats are 'glm#2' — the suffix is seat identity,
    not an engine), then CLI-seat prefixes on model-or-name (codex 'gpt-*',
    kimi 'kimi*', claude ids). Unattributable seats PASS THROUGH as their base
    name rather than being dropped — a custom provider registered only in an
    old config.json still gets counted under its own name."""
    name = str(seat.get("name") or "").strip()
    base = name.split("#")[0].strip()
    if base in provider_names:
        return base
    probe = str(seat.get("model") or "").strip() or base
    if probe.startswith("gpt-"):
        return "codex"
    if probe.startswith("kimi"):
        return "kimi"
    if any(probe.startswith(p) for p in _CLAUDE_PREFIXES):
        return "claude"
    return base or "unknown"


def parse_kimi_log_403s(log_path: str, signal: str) -> list[tuple[int, str, str]]:
    """Scan the kimi-code log for the pinned cycle-quota signal. Returns
    [(epoch_ts, iso_ts, line)] in file order. The 403 text is an INDENTED
    continuation line with no timestamp of its own, so each match is stamped
    with the last timestamped line seen above it; matches before any
    timestamp (rotation artifacts) are skipped. Never raises — an absent or
    unreadable log returns []."""
    out: list[tuple[int, str, str]] = []
    last_iso: Optional[str] = None
    last_epoch: Optional[int] = None
    try:
        with open(os.path.expanduser(log_path), encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _KIMI_TS_RE.match(line)
                if m:
                    last_iso = m.group(1)
                    try:
                        dt = datetime.strptime(last_iso, "%Y-%m-%dT%H:%M:%S.%fZ")
                        last_epoch = int(dt.replace(tzinfo=timezone.utc).timestamp())
                    except ValueError:
                        last_iso, last_epoch = None, None
                if signal in line and "403" in line and last_epoch is not None:
                    out.append((last_epoch, last_iso, line.strip()))
    except OSError:
        return []
    return out


def _live_cutoff(now_ts: int) -> int:
    """The history/live boundary: the ts of the FIRST live-collected row
    (source IS NULL), else `now_ts`. Only events strictly older are
    backfilled — anything newer is (or will be) live-covered."""
    with db.conn() as c:
        row = c.execute(
            "SELECT MIN(ts) FROM usage_events WHERE source IS NULL"
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else int(now_ts)


def _iter_panel_events():
    """Yield (event_id, dispatch_id, ts, breakdown_list) for every stage event
    carrying a REAL panel_breakdown key. The LIKE is only a prefilter — a
    fusion_ok event whose analysis TEXT mentions the word must not match, so
    the parsed payload's key is authoritative. Malformed payloads are skipped."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT id, dispatch_id, ts, payload_json FROM dispatch_events "
            "WHERE kind = 'stage' AND payload_json LIKE '%\"panel_breakdown\"%' "
            "ORDER BY ts ASC, id ASC"
        ).fetchall()
    for r in rows:
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except (ValueError, TypeError):
            continue
        breakdown = payload.get("panel_breakdown") if isinstance(payload, dict) else None
        if isinstance(breakdown, list):
            yield r["id"], r["dispatch_id"], int(r["ts"]), breakdown


def _seat_tokens(seat: dict, ok: bool) -> tuple[Optional[int], Optional[int]]:
    """Token columns for one historical seat. Failed seats → NULL (nothing
    reported). Subscription (CLI) seats report 0/0 on this path because the
    CLI surfaces no per-seat counts — that is UNKNOWN, not zero, so it is
    stored as NULL; provider seats keep their real numbers."""
    if not ok:
        return None, None
    pt = seat.get("prompt_tokens")
    ct = seat.get("completion_tokens")
    pt = int(pt) if isinstance(pt, int) else None
    ct = int(ct) if isinstance(ct, int) else None
    if seat.get("subscription") and not pt and not ct:
        return None, None
    return pt, ct


def reclassify() -> int:
    """U2: (re)fill usage_events.error_class on every failed row from the
    pinned config map. Idempotent — classification is a pure function of
    (engine, raw_error), so re-running converges; rows written before the
    classifier existed (U1 backfill) get their class here. Returns the number
    of rows whose class changed."""
    changed = 0
    with db.conn() as c:
        rows = c.execute(
            "SELECT id, engine, raw_error, error_class FROM usage_events WHERE ok = 0"
        ).fetchall()
        for r in rows:
            cls, _hint = config.classify_error(r["engine"], r["raw_error"])
            if cls != r["error_class"]:
                c.execute("UPDATE usage_events SET error_class = ? WHERE id = ?",
                          (cls, r["id"]))
                changed += 1
    return changed


def recompute_engine_state(engines: Optional[list[str]] = None) -> dict:
    """Deterministically rebuild engine_limit_state from the FULL usage_events
    table (order-independent, hence idempotent): last_ok_at = newest ok event,
    last_error = newest failed event's raw_error, and — U2, classifier-driven
    for EVERY engine — limited_since = the newest limit-hit event's ts
    (error_class in config.USAGE_LIMIT_CLASSES) iff no ok call is newer, with
    reset_hint re-derived from that event's raw error. Live traffic maintains
    the same state incrementally in db.record_usage; this recompute is the
    backfill's authoritative pass over history. Covers the configured engine
    list PLUS any engine present in the ledger (e.g. a provider since removed
    from config.json). Call reclassify() first so error_class is filled."""
    engines = list(engines if engines is not None else config.usage_engines())
    limit_marks = ",".join("?" for _ in config.USAGE_LIMIT_CLASSES)
    with db.conn() as c:
        for (e,) in c.execute("SELECT DISTINCT engine FROM usage_events").fetchall():
            if e not in engines:
                engines.append(e)
        stats: dict = {}
        for engine in engines:
            ok_row = c.execute(
                "SELECT MAX(ts) FROM usage_events WHERE engine = ? AND ok = 1",
                (engine,),
            ).fetchone()
            err_row = c.execute(
                "SELECT ts, raw_error FROM usage_events "
                "WHERE engine = ? AND ok = 0 ORDER BY ts DESC, id DESC LIMIT 1",
                (engine,),
            ).fetchone()
            hit_row = c.execute(
                "SELECT ts, raw_error FROM usage_events "
                f"WHERE engine = ? AND ok = 0 AND error_class IN ({limit_marks}) "
                "ORDER BY ts DESC, id DESC LIMIT 1",
                (engine, *config.USAGE_LIMIT_CLASSES),
            ).fetchone()
            stats[engine] = {
                "last_ok_at": ok_row[0] if ok_row else None,
                "last_error": err_row["raw_error"] if err_row else None,
                "hit_ts": hit_row["ts"] if hit_row else None,
                "hit_raw": hit_row["raw_error"] if hit_row else None,
            }

    limited: dict = {}
    for engine, s in stats.items():
        if s["last_ok_at"] is not None or s["last_error"] is not None:
            db.touch_engine_state(engine, last_ok_at=s["last_ok_at"],
                                  last_error=s["last_error"])
        since = (s["hit_ts"]
                 if s["hit_ts"] is not None and s["hit_ts"] > (s["last_ok_at"] or 0)
                 else None)
        hint = config.classify_error(engine, s["hit_raw"])[1] if since else None
        db.set_engine_limited(engine, since, hint)
        if since:
            limited[engine] = since
    return {"engines": sorted(stats.keys()), "limited": limited,
            "kimi_limited_since": limited.get("kimi")}


def backfill(kimi_log_path: Optional[str] = None,
             now_ts: Optional[int] = None) -> dict:
    """Run the full idempotent backfill; returns a summary dict. Arms the
    usage collector for this process (record_usage is the single insert path,
    and a backfill's whole job is writing). Safe to re-run any time: source
    keys dedupe rows, the live cutoff excludes live-covered history, and the
    state recompute is a deterministic function of the table."""
    db.init_db()
    db.enable_usage_collection()
    db.ensure_engine_limit_rows(config.usage_engines())
    now_ts = int(now_ts if now_ts is not None else db.now())
    cutoff = _live_cutoff(now_ts)
    providers = set(config.fusion_config()["providers"].keys())

    summary = {"cutoff": cutoff, "pb_events": 0, "pb_seats_inserted": 0,
               "pb_seats_dup": 0, "pb_after_cutoff": 0, "pb_seats_malformed": 0,
               "kimi_403s_found": 0, "kimi_403s_inserted": 0}

    for event_id, dispatch_id, ts, breakdown in _iter_panel_events():
        if ts >= cutoff:
            summary["pb_after_cutoff"] += 1
            continue
        summary["pb_events"] += 1
        for idx, seat in enumerate(breakdown):
            if not isinstance(seat, dict) or not seat.get("name"):
                summary["pb_seats_malformed"] += 1
                continue
            ok = bool(seat.get("ok"))
            pt, ct = _seat_tokens(seat, ok)
            inserted = db.record_usage(
                attribute_seat_engine(seat, providers),
                model=(str(seat.get("model") or "").strip() or None),
                role="seat", dispatch_id=dispatch_id, ok=ok,
                prompt_tokens=pt, completion_tokens=ct,
                raw_error=None if ok else (seat.get("error") or "seat failed"),
                ts=ts, source=f"pb:{event_id}:{idx}",
            )
            summary["pb_seats_inserted" if inserted else "pb_seats_dup"] += 1

    log_path = kimi_log_path or config.kimi_engine().get(
        "log_path", config.KIMI_ENGINE_SEED["log_path"])
    for epoch, iso, line in parse_kimi_log_403s(log_path, config.KIMI_LIMIT_SIGNAL):
        if epoch >= cutoff:
            continue
        summary["kimi_403s_found"] += 1
        digest = hashlib.sha1(line.encode("utf-8", "replace")).hexdigest()[:12]
        if db.record_usage("kimi", role="seat", ok=False, raw_error=line,
                           ts=epoch, source=f"kimilog:{iso}:{digest}"):
            summary["kimi_403s_inserted"] += 1

    summary.update(recompute_engine_state())
    return summary


def main():
    s = backfill()
    print("[usage backfill] " + json.dumps(s, indent=2, default=str))


if __name__ == "__main__":
    main()
