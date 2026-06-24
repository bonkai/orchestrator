"""FastAPI app — orchestrator UI + dispatch endpoints + Stop hook receiver."""

import asyncio
import html
import json
import re
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from orchestrator.lib import attachments as attachments_mod
from orchestrator.lib import bundle as bundle_mod
from orchestrator.lib import claude_runner, config, db, edits as edits_mod, embeddings, fusion as fusion_mod, idle_notifier, jobs, loop_watchdog, onboarding, retrieval, rewriter, spawn, summarizer, watchdog

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Fusion Claude Code seats: the models / efforts the dispatch picker offers and
# that /send validates a submitted panel against. Claude seats run via the local
# `claude` CLI (NO Anthropic API), so any combination here is free and duplicates
# are allowed. Efforts are the `claude --effort` choices (low…max).
CLAUDE_SEAT_MODELS = ["opus", "sonnet", "haiku"]
CLAUDE_SEAT_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
# F8.4: ceiling on a per-seat lens string accepted from /send (a configured lens
# name is short; a literal lens is a sentence or two). Bounds a crafted request.
_MAX_LENS_CHARS = 2000


def _codex_seat_models() -> set[str]:
    """Valid codex model ids for the dispatch picker + /send validation (C5.1),
    SOURCED from the codex ENGINE config — CODEX_ENGINE_SEED merged with
    config.json's `fusion.codex` (C4: IMPORT the codex model, never redefine it; no
    codex-id literal lives in app.py). Returns the engine's default model plus every
    model in its default seat panel, so a `fusion.codex` override is honored and the
    set grows as codex adds models (today this is the degenerate {"gpt-5.5"}).

    Deliberately SEPARATE from CLAUDE_SEAT_MODELS: a codex model is a codex id,
    NEVER a Claude id. A shared/merged list would let a Claude id (e.g. "opus") pass
    codex validation and reach `codex -m` — the dispatch #3 silent-downgrade hazard.
    Kept apart, codex validation rejects every Claude id."""
    eng = config.codex_engine()
    models = {str(eng.get("model", "")).strip()}
    # The full valid-model list (C6: gpt-5.5/gpt-5.4/gpt-5.4-mini) — the picker's options
    # + the validation whitelist. Unioned with the default `model` + the seat-panel models
    # so a `fusion.codex` override of ANY of them is honored.
    for m in eng.get("models", []) or []:
        if isinstance(m, str) and m.strip():
            models.add(m.strip())
    for seat in eng.get("seats", []) or []:
        if isinstance(seat, dict):
            m = str(seat.get("model", "")).strip()
            if m:
                models.add(m)
    models.discard("")
    return models


def _codex_seat_efforts() -> set[str]:
    """Valid codex reasoning-effort values for the dispatch picker + /send
    validation, SOURCED from the codex ENGINE config (CODEX_ENGINE_SEED's `efforts`
    merged with config.json's `fusion.codex.efforts`) — like _codex_seat_models, no
    effort literal lives in app.py (C4 import-don't-redefine).

    Codex's reasoning ladder is its OWN (minimal/low/medium/high/xhigh — verified
    against the live API), NOT claude's (low/medium/high/xhigh/max — CLAUDE_SEAT_EFFORTS),
    so the two are deliberately separate. The empty string is NOT in this set on
    purpose: a codex seat with effort "" omits the `-c model_reasoning_effort` override
    and uses the model's own default — that's the picker's "default" option, always
    valid, never validated against this whitelist."""
    eng = config.codex_engine()
    return {str(e).strip() for e in (eng.get("efforts") or [])
            if isinstance(e, str) and e.strip()}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    spawn.ensure_runner()
    # C6: inject the codex poller factory BEFORE resume so boot re-attach can recreate
    # the in-band finalizer for codex dispatches that were running at restart.
    watchdog.set_codex_poller_factory(_codex_dispatch_poller)
    watchdog.resume_watchers_on_boot()
    idle_task = asyncio.create_task(idle_notifier.run_idle_checker())
    _background_tasks.add(idle_task)
    idle_task.add_done_callback(_background_tasks.discard)
    reaper_task = asyncio.create_task(watchdog.run_orphan_reaper())
    _background_tasks.add(reaper_task)
    reaper_task.add_done_callback(_background_tasks.discard)
    # Probe embeddings; log clearly if missing so user knows phase 6 is degraded
    if not embeddings.is_available():
        print(f"[orchestrator] WARNING: embedding backend not reachable "
              f"(Ollama + {embeddings.DEFAULT_MODEL}). Cross-project retrieval disabled. "
              f"Start Ollama and `ollama pull {embeddings.DEFAULT_MODEL}` to enable.")
    yield
    idle_task.cancel()
    reaper_task.cancel()
    for did in list(watchdog._watchers.keys()):
        watchdog.cancel(did)
    for did in list(watchdog._codex_pollers.keys()):   # C6: stop in-band codex pollers
        watchdog.cancel_codex_poller(did)
    for t in list(_background_tasks):
        t.cancel()


app = FastAPI(title="orchestrator", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ─── helpers ──────────────────────────────────────────────────────────────

def _fmt_duration(seconds: int | None) -> str:
    if seconds is None:
        return ""
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _fmt_rel(ts: int | None) -> str:
    if not ts:
        return ""
    delta = int(time.time()) - ts
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _view_ctx() -> dict:
    tabs = db.list_tabs()
    open_ids = {t["project_id"] for t in tabs}
    all_projects = db.list_projects()
    saved = [p for p in all_projects if p["id"] not in open_ids]
    # F4.2/F4.4: Fusion dispatch-form picker data. active_providers() are the
    # keyed+enabled seats (checkable in the UI); every other registry entry is
    # still listed but greyed-out ("no API key set"). fusion_available mirrors
    # config.is_fusion_available() but is derived from the SAME `active` snapshot
    # so the checkbox-enabled state and the row list can never disagree.
    # fusion_default_panel seeds the picker's checked set from the configured
    # preset's active members (the JS uses it only when nothing is saved yet).
    fcfg = config.fusion_config()
    active = config.active_providers()
    fusion_providers = [
        {"name": name, "model": prov.get("model", ""), "active": name in active}
        for name, prov in fcfg["providers"].items()
    ]
    fusion_default_panel = [n for n in fcfg["presets"].get(fcfg.get("preset"), [])
                            if n in active]
    # F8.4: named lenses offered as an (optional) per-seat dropdown in the picker.
    # Names + text travel; the form sends only the chosen NAME per seat (or none).
    fusion_lenses = [{"name": n, "text": t} for n, t in fcfg.get("lenses", {}).items()]
    # Saved profiles (name → {claude_seats, provider_seats}); the picker offers
    # them as a quick-switch dropdown that re-populates the seats in one click.
    fusion_profiles = fcfg.get("profiles", {})
    # Codex availability + model list for the dispatch-form's merged model picker.
    # codex_cli_available mirrors claude_cli_available, but it runs an auth-probe
    # SUBPROCESS (`codex login status`; near-instant, finite-timeout) rather than a
    # bare `which`. is_fusion_available() above short-circuits PAST that probe when
    # claude or >=2 providers exist, so this is the one deliberate codex probe per
    # render — the template greys Codex model options when it's False
    # (logged-out/absent). codex_seat_models seeds that picker (sourced from the
    # codex SEED via config — no codex-id literal in the template).
    codex_available = config.codex_cli_available()
    # Picker options: the DEFAULT model first, then
    # the rest sorted — a bare sort would surface gpt-5.4 ahead of the gpt-5.5 default.
    codex_models = _codex_seat_models()
    codex_default = str(config.codex_engine().get("model", "")).strip()
    codex_models_ordered = (([codex_default] if codex_default in codex_models else [])
                            + sorted(codex_models - {codex_default}))
    return {
        "tabs": tabs,
        "saved_projects": saved,
        "fmt_duration": _fmt_duration,
        "fmt_rel": _fmt_rel,
        "fusion_providers": fusion_providers,
        "fusion_available": config.is_fusion_available(),
        "claude_cli_available": config.claude_cli_available(),
        "codex_cli_available": codex_available,
        "codex_seat_models": codex_models_ordered,
        "claude_seat_models": CLAUDE_SEAT_MODELS,
        "claude_seat_efforts": CLAUDE_SEAT_EFFORTS,
        "fusion_default_panel": fusion_default_panel,
        "fusion_lenses": fusion_lenses,
        "fusion_profiles": fusion_profiles,
        "verify_default": fcfg.get("verify", False),   # F11.c.1: pre-check the checkbox
    }


# ─── pages ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, active: int | None = None):
    ctx = _view_ctx()
    tabs = ctx["tabs"]
    active_id = active
    if active_id is None and tabs:
        active_id = tabs[0]["project_id"]
    active_project = db.get_project(active_id) if active_id else None
    runs = db.recent_dispatches(limit=30, project_id=active_id) if active_id else []
    all_runs = db.recent_dispatches(limit=20)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            **ctx,
            "active_id": active_id,
            "active_project": active_project,
            "runs": runs,
            "all_runs": all_runs,
        },
    )


@app.get("/partials/runs", response_class=HTMLResponse)
async def partial_runs(request: Request, active: int | None = None):
    """Polled partial: project-specific runs + global recent runs."""
    runs = db.recent_dispatches(limit=30, project_id=active) if active else []
    all_runs = db.recent_dispatches(limit=20)
    return templates.TemplateResponse(
        request,
        "_runs.html",
        {
            "runs": runs,
            "all_runs": all_runs,
            "active_id": active,
            "fmt_duration": _fmt_duration,
            "fmt_rel": _fmt_rel,
        },
    )


# ─── project + tab CRUD ───────────────────────────────────────────────────

@app.post("/projects/add")
async def projects_add(path: str = Form(...)):
    try:
        p = db.add_project(path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.open_tab(p["id"])
    return RedirectResponse(f"/?active={p['id']}", status_code=303)


@app.post("/tabs/open/{project_id}")
async def tab_open(project_id: int):
    if not db.get_project(project_id):
        raise HTTPException(404, "unknown project")
    db.open_tab(project_id)
    return RedirectResponse(f"/?active={project_id}", status_code=303)


@app.post("/tabs/close/{project_id}")
async def tab_close(project_id: int):
    db.close_tab(project_id)
    return RedirectResponse("/", status_code=303)


@app.post("/projects/remove/{project_id}")
async def project_remove(project_id: int):
    """Forget a project entirely (keeps dispatch history? No — cascade deletes it)."""
    with db.conn() as c:
        c.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    return RedirectResponse("/", status_code=303)


# ─── dispatch ─────────────────────────────────────────────────────────────

async def _run_dispatch(project_id: int, task: str, wall_cap_s: int, effort: str = "max", model: str = "") -> tuple[int | None, str]:
    """Core dispatch flow shared by `/dispatch` (sync redirect) and `/send`
    (background task). Returns (dispatch_id, error). On any failure, returns
    (None, reason) — never raises HTTPException, since the background-task
    caller has no HTTP response to fail.

    The submitted model selects both executor and CLI model: codex ids route to
    the codex executor, while every other value stays on Claude. This derivation
    is repeated here (after /send validates it) so direct /dispatch callers can
    never send a codex id down Claude's `--model` path."""
    proj = db.get_project(project_id)
    if not proj:
        return None, "unknown project"
    if not Path(proj["path"]).is_dir():
        return None, f"project path no longer exists: {proj['path']}"
    task = task.strip()
    if not task:
        return None, "empty task"
    wall_cap_s = max(60, min(21600, int(wall_cap_s)))  # ceiling: 6h
    executor_engine, model, executor_model = _derive_executor(model)
    try:
        executor_engine, executor_model = _validate_executor_engine(
            executor_engine, executor_model, _codex_seat_models(),
            codex_available=config.codex_cli_available())
    except ValueError as e:
        return None, str(e)
    # Effort is a Claude-only flag. Codex uses its own model default, so never
    # forward xhigh/max (or any other Claude effort) into `-c
    # model_reasoning_effort`.
    effort = (effort or "").strip()
    if executor_engine == "codex":
        effort = ""
    elif effort not in ("medium", "high", "xhigh", "max"):
        effort = "max"

    # Pick up any staged attachments (drag-drop), move them to a dispatch-
    # owned dir so they survive after the stash is cleared, prepend their
    # paths to the task so the dispatched claude session knows about them.
    stash_atts = attachments_mod.list_files(str(project_id))
    moved_atts = []
    if stash_atts:
        moved_atts = attachments_mod.move_to_dispatch(str(project_id), 0)  # tmp dispatch id

    dispatch_id = db.create_dispatch(project_id, task, wall_clock_cap_s=wall_cap_s)
    if moved_atts:
        old_dir = attachments_mod.ATTACHMENTS_DIR / "dispatch_0"
        new_dir = attachments_mod.ATTACHMENTS_DIR / f"dispatch_{dispatch_id}"
        if old_dir.exists():
            try:
                old_dir.rename(new_dir)
                moved_atts = [attachments_mod.Attachment(
                    name=a.name, path=str(new_dir / a.name), size=a.size,
                ) for a in moved_atts]
            except OSError:
                pass
        attach_header = attachments_mod.render_for_prompt(moved_atts)
        task = attach_header + "\n\n" + task

    db.record_event(dispatch_id, "stage", {
        "stage": "dispatch_created",
        "task_chars": len(task),
        "attachments": len(moved_atts),
    })

    # C6: the $0 codex EXECUTOR. engine+model were validated in /send. This branch
    # spawns a watchable codex tab (writes confined to the project via -s
    # workspace-write) and NEVER falls through to the `claude` spawn below — a codex
    # pick must never become a silent claude executor (the dispatch #3 downgrade). A
    # spawn failure is a VISIBLE failed row, still no claude fallback. The §5 hook-gap
    # convergence: the cap watcher uses the codex branch (hard-kill, no pause-resume)
    # and the in-band poller is the SOLE finalizer (codex has no Stop hook).
    if executor_engine == "codex":
        # §2 Q7 / Plus cap GUARD: bound concurrent codex EXECUTOR dispatches so a burst
        # can't silently exhaust the shared 5-hour subscription window. Counts the OTHER
        # codex dispatches currently running (the just-created row is still 'pending', so
        # it isn't counted); at/over the cap we reject as a VISIBLE failed row — never a
        # claude fallback (dispatch #3) — and the user waits or kills one. Soft cap (a
        # near-simultaneous pair may overshoot by 1); 0/None ⇒ unlimited.
        cap = int(config.codex_engine().get("max_concurrent_dispatches", 0) or 0)
        if cap > 0:
            running_codex = sum(1 for d in db.running_dispatches()
                                if spawn.is_codex_dispatch(d["id"]))
            if running_codex >= cap:
                reason = (f"codex concurrency cap reached: {running_codex}/{cap} codex "
                          f"dispatches already running — wait or kill one (raise "
                          f"fusion.codex.max_concurrent_dispatches to change)")
                db.mark_failed_to_spawn(dispatch_id, reason)
                db.record_event(dispatch_id, "stage", {
                    "stage": "spawn_failed", "error": reason, "engine": "codex",
                    "model": executor_model, "running_codex": running_codex, "cap": cap})
                spawn.cleanup_dispatch_files(dispatch_id)
                return None, reason
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, spawn.spawn_codex_dispatch, proj["path"], dispatch_id, task,
                executor_model,
            )
        except Exception as e:
            db.mark_failed_to_spawn(dispatch_id, str(e))
            db.record_event(dispatch_id, "stage", {
                "stage": "spawn_failed", "error": str(e),
                "engine": "codex", "model": executor_model,
            })
            spawn.cleanup_dispatch_files(dispatch_id)
            return None, f"codex spawn failed: {e}"
        db.record_event(dispatch_id, "stage", {
            "stage": "iterm2_spawned", "engine": "codex", "model": executor_model})
        pid = await loop.run_in_executor(None, spawn.read_claude_pid, dispatch_id, 5.0)
        db.mark_started(dispatch_id, terminal_pid=None, claude_pid=pid)
        db.touch_project(project_id)
        watchdog.schedule(dispatch_id, pid, wall_cap_s, engine="codex")
        watchdog.schedule_codex_poller(dispatch_id)   # §5 fix iii: the SOLE finalizer
        db.record_event(dispatch_id, "stage", {
            "stage": "running", "pid": pid, "engine": "codex"})
        return dispatch_id, ""

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None, spawn.spawn_iterm2, proj["path"], dispatch_id, task, None, effort, model
        )
    except Exception as e:
        db.mark_failed_to_spawn(dispatch_id, str(e))
        db.record_event(dispatch_id, "stage", {"stage": "spawn_failed", "error": str(e)})
        spawn.cleanup_dispatch_files(dispatch_id)
        return None, f"spawn failed: {e}"

    db.record_event(dispatch_id, "stage", {"stage": "iterm2_spawned"})
    pid = await loop.run_in_executor(None, spawn.read_claude_pid, dispatch_id, 5.0)
    db.mark_started(dispatch_id, terminal_pid=None, claude_pid=pid)
    db.touch_project(project_id)
    watchdog.schedule(dispatch_id, pid, wall_cap_s)
    db.record_event(dispatch_id, "stage", {"stage": "running", "pid": pid})
    return dispatch_id, ""


@app.post("/dispatch")
async def dispatch(project_id: int = Form(...), task: str = Form(...), wall_cap_s: int = Form(14400), effort: str = Form("max"), model: str = Form("")):
    """Sync dispatch — used by the onboarding /apply_edits flow which wants
    a redirect back to the main page. The main pane uses /send instead."""
    dispatch_id, err = await _run_dispatch(project_id, task, wall_cap_s, effort, model)
    if err:
        # Surface common validation errors with appropriate codes
        if "unknown project" in err:
            raise HTTPException(404, err)
        if "empty task" in err or "project path" in err:
            raise HTTPException(400, err)
        raise HTTPException(500, err)
    return RedirectResponse(f"/?active={project_id}", status_code=303)


@app.post("/dispatch/{dispatch_id}/kill")
async def dispatch_kill(dispatch_id: int):
    ok = await watchdog.manual_kill(dispatch_id)
    if not ok:
        raise HTTPException(404, "dispatch not running")
    d = db.get_dispatch(dispatch_id)
    return RedirectResponse(f"/?active={d['project_id']}" if d else "/", status_code=303)


@app.post("/dispatch/{dispatch_id}/open")
async def dispatch_open(dispatch_id: int):
    """Jump to a dispatch's iTerm tab.
      - If it's still running, select the existing tab by its "orch #<id>" name.
      - If it's finished and we have a session_id, create a NEW dispatch row
        for the resume (so the Stop hook can complete it and the summarizer
        runs), then open a new tab and `claude --resume <session_id>`.
      - If finished without a session_id (e.g., failed_to_spawn), return 400.
    """
    d = db.get_dispatch_with_project(dispatch_id)
    if not d:
        raise HTTPException(404, "unknown dispatch")
    loop = asyncio.get_running_loop()
    if d["status"] == "running":
        try:
            found = await loop.run_in_executor(None, spawn.select_iterm2_tab, dispatch_id)
        except Exception as e:
            raise HTTPException(500, f"could not focus iTerm tab: {e}")
        if not found:
            raise HTTPException(404, "iTerm tab not found (closed?)")
        return JSONResponse({"ok": True, "mode": "selected"})
    sid = d.get("session_id")
    if not sid:
        raise HTTPException(400, "no session_id stored for this dispatch — nothing to resume")
    if not Path(d["project_path"]).is_dir():
        raise HTTPException(400, f"project path no longer exists: {d['project_path']}")

    # Create a tracked dispatch row for the resume so the Stop hook fires
    # and the summarizer updates project memory. Use the new 4h default — a
    # dispatch resumed from a 6h timeout-pause needs a real runway, not 30m.
    project_id = d["project_id"]
    wall_cap_s = 14400
    new_dispatch_id = db.create_dispatch(
        project_id, f"resume of dispatch #{dispatch_id}", wall_clock_cap_s=wall_cap_s
    )
    db.record_event(new_dispatch_id, "stage", {
        "stage": "dispatch_created", "resume_of": dispatch_id, "session_id": sid,
    })
    try:
        await loop.run_in_executor(
            None, spawn.spawn_iterm2_resume, d["project_path"], sid, new_dispatch_id
        )
    except Exception as e:
        db.mark_failed_to_spawn(new_dispatch_id, str(e))
        db.record_event(new_dispatch_id, "stage", {"stage": "spawn_failed", "error": str(e)})
        spawn.cleanup_dispatch_files(new_dispatch_id)
        raise HTTPException(500, f"spawn failed: {e}")
    db.record_event(new_dispatch_id, "stage", {"stage": "iterm2_spawned"})
    pid = await loop.run_in_executor(None, spawn.read_claude_pid, new_dispatch_id, 5.0)
    db.mark_started(new_dispatch_id, terminal_pid=None, claude_pid=pid)
    db.touch_project(project_id)
    watchdog.schedule(new_dispatch_id, pid, wall_cap_s)
    db.record_event(new_dispatch_id, "stage", {"stage": "running", "pid": pid})
    return JSONResponse({
        "ok": True, "mode": "resumed", "session_id": sid,
        "dispatch_id": new_dispatch_id, "resume_of": dispatch_id,
    })


@app.post("/killall")
async def killall():
    n = await watchdog.kill_all()
    return JSONResponse({"killed": n})


@app.post("/tabs/close_completed")
async def tabs_close_completed():
    """Close iTerm2 tabs for every dispatch that has already finished.
    Useful for clearing stale tabs that accumulated before auto-close was
    wired in. Running dispatches are untouched."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT id FROM dispatches "
            "WHERE status IN ('completed', 'killed', 'failed')"
        ).fetchall()
    ids = [r["id"] for r in rows]
    if not ids:
        return JSONResponse({"closed": 0, "candidates": 0})
    loop = asyncio.get_running_loop()
    closed = await loop.run_in_executor(None, spawn.close_iterm2_tabs, ids)
    return JSONResponse({"closed": closed, "candidates": len(ids)})


# ─── Stop hook receiver ───────────────────────────────────────────────────

@app.post("/api/tool_use")
async def api_tool_use(payload: dict):
    """PreToolUse hook receiver. Two jobs:
      1. Loop-watchdog: kill on N consecutive identical tool calls.
      2. Live activity: record a `tool_use` event for the UI timeline.
    """
    try:
        dispatch_id = int(payload.get("run_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad run_id"}
    tool_name = str(payload.get("tool_name") or "")
    input_hash = str(payload.get("input_hash") or "")

    # Live-activity event (best-effort, doesn't block hook)
    db.record_event(dispatch_id, "tool_use", {
        "tool_name": tool_name,
        "input_hash": input_hash,
    })
    idle_notifier.reset_idle(dispatch_id)

    if loop_watchdog.record(dispatch_id, tool_name, input_hash):
        # Loop detected — fire kill asynchronously so this hook returns fast
        db.record_event(dispatch_id, "stage", {"stage": "loop_detected", "tool": tool_name})
        task = asyncio.create_task(loop_watchdog.trigger_kill(dispatch_id, (tool_name, input_hash)))
        _background_tasks.add(task)  # reuse the GC-safety set
        task.add_done_callback(_background_tasks.discard)
        return {"ok": True, "killed": True}
    return {"ok": True}


@app.post("/api/tool_result")
async def api_tool_result(payload: dict):
    """PostToolUse hook receiver. Records a `tool_result` event with a
    preview of the response so the UI timeline can show 'Read returned 1.2KB'
    style updates."""
    try:
        dispatch_id = int(payload.get("run_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad run_id"}
    db.record_event(dispatch_id, "tool_result", {
        "tool_name": str(payload.get("tool_name") or ""),
        "response_preview": str(payload.get("response_preview") or "")[:400],
        "response_bytes": int(payload.get("response_bytes") or 0),
    })
    return {"ok": True}


@app.get("/api/events/{dispatch_id}")
async def api_events(dispatch_id: int, since: int = 0):
    """Polled by the live-activity timeline. Returns new events since
    `since` (cursor by event id) plus the dispatch's current status so
    the UI knows when to stop polling."""
    d = db.get_dispatch(dispatch_id)
    if not d:
        raise HTTPException(404)
    events = db.get_events(dispatch_id, since_id=since, limit=200)
    return JSONResponse({
        "status": d["status"],
        "events": events,
        "next_since": events[-1]["id"] if events else since,
    })


@app.post("/api/complete")
async def api_complete(payload: dict):
    """Called by ~/.orchestrator/bin/notify_complete.sh after each dispatch.

    Expected JSON:
      {
        "run_id":          "<dispatch_id>",
        "session_id":      "<claude session id>",
        "transcript_path": "<path to jsonl>",
        "cwd":             "<project dir>",
        "exit_reason":     "<stop_hook_active|...>"
      }
    """
    try:
        dispatch_id = int(payload.get("run_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "missing or bad run_id")

    d = db.get_dispatch(dispatch_id)
    if not d:
        raise HTTPException(404, "unknown dispatch")
    if d["status"] in ("killed", "completed", "failed", "paused"):
        return {"ok": True, "note": f"already {d['status']}"}

    session_id = payload.get("session_id")
    src_transcript = payload.get("transcript_path")
    exit_reason = payload.get("exit_reason")

    # Graceful timeout-pause in flight: the wall-clock watchdog has SIGTERM'd
    # this dispatch and is polling for exactly this session_id so the dispatch
    # stays resumable. Store it WITHOUT finalizing — the watchdog owns the
    # terminal 'paused' status. We must NOT fall through to the normal path
    # below: that cancels the watchdog (aborting the pause) and marks the row
    # 'completed' (stripping the 'paused' state and triggering the summarizer
    # on an interrupted session).
    if watchdog.is_pausing(dispatch_id):
        # `or None` so an empty-string id (degraded jq output) stays NULL under
        # attach_session's COALESCE rather than sticking and blocking a real one.
        db.attach_session(dispatch_id, session_id or None, src_transcript or None)
        db.record_event(dispatch_id, "stage", {
            "stage": "pause_session_captured",
            "has_session_id": bool(session_id),
        })
        return {"ok": True, "note": "pausing", "dispatch_id": dispatch_id}

    # Mark completed FIRST (atomic; loser of a race sees changed=False), then copy
    # the transcript + fire the summarizer — all via the shared completion CORE, which
    # C6 extracted so the codex in-band poller finalizes identically without the Claude
    # hooks (§5 fix iii). For the claude Stop-hook path the transcript source is the
    # hook's transcript_path; the behavior here is byte-for-byte the pre-C6 inline code.
    won = await _finalize_dispatch(
        dispatch_id, session_id=session_id, transcript_src=src_transcript,
        exit_reason=exit_reason, outcome="completed")
    if not won:
        return {"ok": True, "note": "already finalized"}
    return {"ok": True, "dispatch_id": dispatch_id}


# Strong refs for in-flight background tasks (summarizer, onboarding,
# /send dispatches, loop-watchdog kills). asyncio.create_task only keeps a
# weak ref to the task, so without this Python could GC it mid-run.
_background_tasks: set[asyncio.Task] = set()


async def _run_summarizer(dispatch_id: int):
    """Background task: distill the transcript, ask `claude` (visible tab) for a
    structured summary, write it into outcomes. Failures are logged only —
    never propagated, since this runs detached from any request."""
    try:
        d = db.get_dispatch_with_project(dispatch_id)
        if not d or not d.get("transcript_path"):
            return
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, summarizer.summarize,
            d["transcript_path"], d["user_task"], d["project_path"],
        )
        if result.ok:
            db.set_summary(
                dispatch_id,
                summary_md=result.summary_md,
                what_worked=result.what_worked,
                what_broke=result.what_broke,
                lessons=result.lessons,
                tags=result.tags,
            )
            db.record_event(dispatch_id, "stage", {
                "stage": "summarized",
                "cost_usd": round(result.cost_usd, 4),
                "duration_s": round(result.duration_s, 1),
                "tags": result.tags,
            })
            print(f"[orchestrator] summarized dispatch #{dispatch_id} "
                  f"({result.duration_s:.1f}s, ${result.cost_usd:.4f})")
            try:
                ok = await loop.run_in_executor(None, retrieval.index_dispatch, dispatch_id)
                if ok:
                    db.record_event(dispatch_id, "stage", {"stage": "indexed"})
                    print(f"[orchestrator] indexed dispatch #{dispatch_id} for retrieval")
            except Exception as e:
                print(f"[orchestrator] embedding failed for #{dispatch_id}: {e}")
        else:
            db.record_event(dispatch_id, "stage", {"stage": "summarizer_failed",
                                                   "error": result.error})
            print(f"[orchestrator] summarizer failed for #{dispatch_id}: {result.error}")
    except Exception as e:
        print(f"[orchestrator] summarizer crashed for #{dispatch_id}: {e}")


async def _finalize_dispatch(dispatch_id: int, *, session_id: str | None,
                             transcript_src: str | None, exit_reason: str | None,
                             outcome: str = "completed") -> bool:
    """The /api/complete completion CORE, extracted (C6) so BOTH the claude Stop-hook
    path (/api/complete) and the codex in-band poller (§5 fix iii) finalize a dispatch
    identically — codex gets the SAME atomic-complete → transcript/artifact → summarizer
    WITHOUT the Claude hooks. Deliberately OMITS the claude-only is_pausing branch (the
    /api/complete caller still owns that, before calling here).

    Returns True iff this call WON the finalize race. The race is FOUR-way — this, the
    cap watchdog, manual_kill/kill_all, the orphan reaper — and is settled by
    db.complete_dispatch's atomic compare-and-set: the losers get changed=False and skip
    the transcript/summarizer work, so there is exactly one outcome row + one summarizer.

    `transcript_src` is the file copied into TRANSCRIPTS_DIR + registered as the artifact
    + handed to the summarizer: the claude Stop-hook transcript for claude, the codex
    `exec --json` sidecar for codex (distill_transcript learned the codex schema in C6).
    The copy runs BEFORE spawn.cleanup_dispatch_files (which deletes the codex sidecar),
    so the summary/artifact survive. Never raises into the caller's flow."""
    watchdog.cancel(dispatch_id)
    changed = db.complete_dispatch(
        dispatch_id,
        session_id=session_id,
        transcript_path=transcript_src,
        exit_reason=exit_reason,
        outcome=outcome,
    )
    if not changed:
        return False

    # Now safe to copy transcript and insert artifact — we're the winner.
    if transcript_src:
        src = Path(transcript_src).expanduser()
        if src.is_file():
            dest = db.TRANSCRIPTS_DIR / f"{dispatch_id}.jsonl"
            try:
                shutil.copyfile(src, dest)
                with db.conn() as c:
                    c.execute(
                        "INSERT OR IGNORE INTO artifacts(dispatch_id, kind, path) "
                        "VALUES (?, 'transcript', ?)",
                        (dispatch_id, str(dest)),
                    )
                    c.execute(
                        "UPDATE dispatches SET transcript_path = ? WHERE id = ?",
                        (str(dest), dispatch_id),
                    )
            except Exception as e:
                print(f"[orchestrator] transcript copy failed: {e}")

    spawn.cleanup_dispatch_files(dispatch_id)
    # Drop loop-watchdog state — dispatch is done
    loop_watchdog.clear(dispatch_id)
    idle_notifier.clear(dispatch_id)
    db.record_event(dispatch_id, "stage", {"stage": "completed",
                                           "exit_reason": exit_reason or "stop"})

    # Fire-and-forget summarizer (offloads its blocking claude call to a thread).
    # Backgrounded + strong-ref'd so a tab disconnect / poller exit can't GC it mid-run.
    task = asyncio.create_task(_run_summarizer(dispatch_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return True


# ─── codex EXECUTOR in-band poller (C6 §5 fix iii) ───────────────────────────
# codex has NO Stop/PreToolUse/PostToolUse hooks, so a dispatched codex executor loses
# completion logging, the loop watchdog, AND the live timeline. This poller is the
# convergence (fix iii): the SOLE finalizer + activity feed for a codex dispatch, modeled
# on watchdog (one async task for the dispatch's whole life), NOT on run_codex_json (a sync
# one-shot). It tails the sidecar JSONL the executor run.sh tees, records tool_use/
# tool_result timeline events + feeds loop_watchdog from the codex tool-call fingerprint
# (C6.0 schema), and on .done finalizes IN-PROCESS via _finalize_dispatch (never self-POSTs
# /api/complete). watchdog tracks/cancels the task; resume_watchers_on_boot re-attaches it.

_CODEX_POLL_INTERVAL_S = 0.5
# codex_dispatch_run.sh writes the PID right after backgrounding codex; if none appears
# within this window the tab never started its runner, so finalize as failed (the codex
# analogue of run_claude_json's _STARTUP_GRACE_S, a touch longer for codex's startup).
_CODEX_POLLER_STARTUP_GRACE_S = 90


def _codex_timeline_step(dispatch_id: int, line: str, seen_items: set, completed_items: set):
    """Translate ONE codex sidecar JSONL line into the SAME signals the claude hooks
    produce: a `tool_use` timeline event + a loop_watchdog fingerprint on first sight of
    a tool call (the PreToolUse analogue — mirrors /api/tool_use), and a `tool_result`
    event on its completion (the PostToolUse analogue — mirrors /api/tool_result). Dedups
    by codex `item.id` so a tool call counts ONCE (it emits item.started THEN
    item.completed). A detected loop fires the SAME async kill path claude uses
    (loop_watchdog.trigger_kill → watchdog.manual_kill, reason='loop:<tool>')."""
    line = line.strip()
    if not line:
        return
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return
    ev = claude_runner._codex_tool_event(obj)
    if ev is None:
        return
    item_id, tool, ihash = ev["id"], ev["tool_name"], ev["input_hash"]
    # First sighting of this tool item → tool_use timeline + loop-watchdog fingerprint
    # + idle reset (exactly /api/tool_use, but fed from the stream instead of a hook).
    if item_id not in seen_items:
        seen_items.add(item_id)
        db.record_event(dispatch_id, "tool_use",
                        {"tool_name": tool, "input_hash": ihash, **ev["detail"]})
        idle_notifier.reset_idle(dispatch_id)
        if loop_watchdog.record(dispatch_id, tool, ihash):
            db.record_event(dispatch_id, "stage", {"stage": "loop_detected", "tool": tool})
            t = asyncio.create_task(loop_watchdog.trigger_kill(dispatch_id, (tool, ihash)))
            _background_tasks.add(t)
            t.add_done_callback(_background_tasks.discard)
    # Completion of the tool call → tool_result timeline (exactly /api/tool_result).
    if ev["phase"] == "end" and item_id not in completed_items:
        completed_items.add(item_id)
        db.record_event(dispatch_id, "tool_result", {"tool_name": tool, **ev["detail"]})


async def _codex_dispatch_poller(dispatch_id: int, tail_only: bool = False):
    """Lifetime poller for one codex EXECUTOR dispatch — the §5 in-band finalizer. Tails
    the sidecar JSONL live (→ timeline + loop watchdog), and on .done (or a closed/killed
    tab) finalizes via _finalize_dispatch. Runs for the dispatch's whole life as a
    watchdog-tracked task. `tail_only=True` (boot re-attach) seeks past the already-
    consumed prefix so a restart can't re-emit timeline events or re-trip the loop
    watchdog on old tool calls. Never raises (a crash here would leave the dispatch with
    no finalizer); on cancellation (kill/cap) it just exits — the killer wrote the row."""
    jsonl = spawn.CODEX_DIR / f"{dispatch_id}.jsonl"
    done = spawn.CODEX_DIR / f"{dispatch_id}.done"
    offset = 0
    buf = ""
    seen_items: set = set()        # item.ids already fingerprinted (tool_use + loop feed)
    completed_items: set = set()   # item.ids already tool_result'd
    pid = None
    started_at = time.time()
    # Boot re-attach: skip the prefix the pre-restart poller already consumed.
    if tail_only and jsonl.is_file():
        try:
            offset = jsonl.stat().st_size
        except OSError:
            offset = 0
    try:
        while True:
            # 1. consume newly-appended JSONL (incremental; keep a partial trailing
            #    line in `buf` until its newline arrives so we never parse half a line).
            if jsonl.is_file():
                try:
                    with jsonl.open(encoding="utf-8", errors="replace") as f:
                        f.seek(offset)
                        chunk = f.read()
                        offset = f.tell()
                except OSError:
                    chunk = ""
                if chunk:
                    buf += chunk
                    while "\n" in buf:
                        ln, buf = buf.split("\n", 1)
                        _codex_timeline_step(dispatch_id, ln, seen_items, completed_items)
            # 2. .done → finalize (exit code written AFTER the sidecar flushed).
            if done.is_file():
                try:
                    exit_code = int((done.read_text().strip() or "1"))
                except (ValueError, OSError):
                    exit_code = 1
                outcome = "completed" if exit_code == 0 else "failed"
                exit_reason = "codex" if exit_code == 0 else f"codex exit {exit_code}"
                await _finalize_dispatch(
                    dispatch_id, session_id=None,
                    transcript_src=str(jsonl) if jsonl.is_file() else None,
                    exit_reason=exit_reason, outcome=outcome)
                return
            # 3. liveness: a closed/killed tab → no .done will ever arrive.
            if pid is None:
                pid = spawn.read_pid_now(dispatch_id)
                if pid is None and (time.time() - started_at) > _CODEX_POLLER_STARTUP_GRACE_S:
                    await _finalize_dispatch(
                        dispatch_id, session_id=None, transcript_src=None,
                        exit_reason="codex tab failed to start (no PID)", outcome="failed")
                    return
            elif not spawn.pid_alive(pid):
                if done.is_file():
                    continue  # race: .done just landed — handle on the next loop top
                await _finalize_dispatch(
                    dispatch_id, session_id=None,
                    transcript_src=str(jsonl) if jsonl.is_file() else None,
                    exit_reason="codex tab closed before completion", outcome="failed")
                return
            await asyncio.sleep(_CODEX_POLL_INTERVAL_S)
    except asyncio.CancelledError:
        # Killed (manual/kill-all/cap) cancelled us — the killer already wrote the
        # outcome row + cleaned up. Nothing to finalize; exit quietly.
        raise
    except Exception as e:
        print(f"[orchestrator] codex poller for #{dispatch_id} crashed: {e}")


# ─── transcript view ──────────────────────────────────────────────────────

# ─── fire-and-forget send (rewrite optional) ─────────────────────────────

# F5: how much of each panel seat's answer text to persist on the rewrite event.
# The full per-seat answers can be several KB each; a preview keeps the
# dispatch_events row small while still letting you see what each model said.
_SEAT_PREVIEW_CHARS = 600


def _fusion_panel_breakdown(result) -> list[dict]:
    """Trim rewriter's raw fusion_panel (run.raw['panel']) into a small,
    persistable per-seat breakdown for the rewrite stage event: identity +
    cost + tokens + a bounded text/error preview. Claude Code seats report
    cost 0.0 and carry a `subscription` marker so the UI can label them
    '$0 (subscription)'. Returns [] for a non-fused rewrite."""
    out = []
    for a in result.fusion_panel or []:
        if not isinstance(a, dict):
            continue
        seat = {
            "name": a.get("name", "?"),
            "model": a.get("model", ""),
            "ok": bool(a.get("ok")),
            "cost": round(float(a.get("cost", 0.0) or 0.0), 6),
            "prompt_tokens": a.get("prompt_tokens", 0) or 0,
            "completion_tokens": a.get("completion_tokens", 0) or 0,
            "subscription": bool(a.get("subscription")),
            "lens": a.get("lens", "") or "",        # F8.4: which lens this seat used
        }
        if a.get("ok"):
            seat["preview"] = (a.get("text", "") or "")[:_SEAT_PREVIEW_CHARS]
        else:
            seat["error"] = (a.get("error", "") or "")[:_SEAT_PREVIEW_CHARS]
        out.append(seat)
    return out


def _parse_fusion_panel(fusion_seats: str, fusion_panel: str, active: dict,
                        codex_models: set, codex_efforts: set | None = None) -> list:
    """Normalize the dispatch form's panel selection into the seat list
    run_fusion_json consumes. `fusion_seats` is the F9 JSON shape — a list of
    {type:"claude"|"codex"|"provider", ...}; `fusion_panel` is the legacy comma
    fallback (provider names). Each seat is validated and unhonorable ones are
    DROPPED, so a stale UI selection can never force an unusable seat:
      - claude   → {"kind":"claude_cli","model","effort"[,"lens"]}; model/effort
                   against the CLAUDE_SEAT_* whitelists.
      - codex    → {"kind":"codex_cli","model"[,"effort"][,"lens"]}; model against
                   the codex whitelist (a codex id, NEVER a Claude id) — a blank/unknown
                   model DROPS the seat (no silent downgrade). The optional effort
                   (thinking level) is carried only when it's a whitelisted codex effort
                   (codex_efforts); a blank or unknown effort is OMITTED so codex uses
                   the model's own reasoning default (effort is OPTIONAL for codex,
                   unlike a required Claude effort). run_fusion_json already consumes
                   this third kind + its effort (C2.3), so the transform is additive
                   and never touches claude_cli.
      - provider → the bare name (or {"name","lens"} when lensed) if key-active.
    Pure when `codex_efforts` is passed (no config reads on the hot path — /send passes
    `active`, `codex_models`, and `codex_efforts`); omitting `codex_efforts` falls back
    to the seed via _codex_seat_efforts(), mirroring _derive_executor. An empty result
    lets run_fusion_json fall back to the configured preset.

    The dispatch panel's "+ add codex seat" button emits {type:"codex",model,effort,
    lens}; codex seats also persist in saved profiles. The claude/provider branches are
    byte-for-byte the pre-C5 /send loop."""
    codex_efforts = _codex_seat_efforts() if codex_efforts is None else codex_efforts
    panel: list = []
    raw_seats = (fusion_seats or "").strip()
    if raw_seats:
        try:
            decoded = json.loads(raw_seats)
        except (ValueError, TypeError):
            decoded = []
        for s in decoded if isinstance(decoded, list) else []:
            if not isinstance(s, dict):
                continue
            # F8.4: an optional per-seat lens (a configured lens NAME or literal
            # text) decorrelates the panel; capped + stripped, resolved seat-side by
            # config.resolve_lens. Empty ⇒ the seat gets the prompt verbatim.
            seat_lens = str(s.get("lens", "")).strip()[:_MAX_LENS_CHARS]
            if s.get("type") == "claude":
                # NB: seat_model/seat_effort — NOT the `model`/`effort` Form params,
                # which govern the EXECUTOR. Shadowing them here silently rewrote the
                # dispatched session's model/effort to the last Claude seat's.
                seat_model = str(s.get("model", "")).strip()
                seat_effort = str(s.get("effort", "")).strip()
                if seat_model in CLAUDE_SEAT_MODELS and seat_effort in CLAUDE_SEAT_EFFORTS:
                    seat = {"kind": "claude_cli", "model": seat_model, "effort": seat_effort}
                    if seat_lens:
                        seat["lens"] = seat_lens
                    panel.append(seat)
            elif s.get("type") == "codex":
                seat_model = str(s.get("model", "")).strip()
                if seat_model in codex_models:
                    seat = {"kind": "codex_cli", "model": seat_model}
                    if seat_lens:
                        seat["lens"] = seat_lens
                    panel.append(seat)
            elif s.get("type") == "provider":
                name = str(s.get("name", "")).strip()
                if name in active:
                    # A lens turns the bare-name seat into the dict form
                    # run_fusion_json also accepts; no lens ⇒ stays a plain name.
                    panel.append({"name": name, "lens": seat_lens} if seat_lens else name)
    else:
        panel = [p for p in fusion_panel.split(",") if p in active]
    return panel


def _derive_executor(model: str, codex_models: set | None = None) -> tuple[str, str, str]:
    """Return (engine, claude_model, codex_model) derived from one picker value.

    The codex whitelist comes only from config.codex_engine() through
    _codex_seat_models(); no model ids are defined here. A codex selection clears
    the Claude model so it cannot reach `claude --model`.
    """
    model = (model or "").strip()
    codex_models = _codex_seat_models() if codex_models is None else codex_models
    if model in codex_models:
        return "codex", "", model
    return "claude", model, ""


def _validate_executor_engine(engine: str, model: str, codex_models: set,
                              codex_available: bool = True) -> tuple[str, str]:
    """Validate the dispatch EXECUTOR engine + (for codex) its model — the C5.1
    server gate behind the UI's engine picker. The disabled codex <option> (C5.2)
    is cosmetic; a crafted POST is rejected HERE. Returns (engine, model).

    Raises ValueError on an unknown engine, unavailable codex CLI, or a codex engine
    with a blank or unknown model. The two codex rejections are DISTINCT (per C5.1 "a default that
    omits the model is rejected" vs an unknown id): a missing model is the
    no-downgrade guard, an out-of-whitelist model is a bad id — both reject. A codex
    executor NEVER falls back to a Claude id, nor silently to the claude engine
    (dispatch #3). engine="claude" (the default) ignores `model` and returns
    ("claude", ""), so the no-codex path is byte-for-byte unchanged."""
    engine = (engine or "claude").strip() or "claude"
    if engine not in ("claude", "codex"):
        raise ValueError(f"unknown executor engine: {engine!r}")
    if engine == "claude":
        return "claude", ""
    model = (model or "").strip()
    if not model:
        raise ValueError("codex executor requires an explicit codex model (no silent downgrade)")
    if model not in codex_models:
        raise ValueError(f"unknown codex executor model: {model!r}")
    if not codex_available:
        raise ValueError("codex executor is unavailable: install and log in to the Codex CLI")
    return "codex", model


async def _send_in_background(project_id: int, task: str, wall_cap_s: int, do_rewrite: bool, effort: str = "max", model: str = "", do_fusion: bool = False, panel: list | None = None, do_enrich: bool = False, do_verify: bool = False):
    """Background task spawned by /send. If do_rewrite, run the rewriter
    first and use its output. If the rewrite FAILS we no longer silently
    fall back to dispatching the original task — that hid the failure behind
    a normal-looking run, so the user couldn't tell their rewrite never
    happened. Instead we create a dispatch row, mark it failed (with the
    rewrite reason), record the stage event, and stop: no iTerm tab opens
    and the UI shows a clear failed row. The user can re-submit (e.g. via
    "skip rewrite & send") to run the task as-is. On rewrite success the
    flow is unchanged."""
    proj = db.get_project(project_id)
    if not proj:
        return
    final_task = task
    # Capture rewrite outcome before the dispatch row exists; recorded on the
    # event timeline once we have a dispatch_id.
    rewrite_event: dict | None = None
    rewrite_failed = False
    rewrite_error = ""
    rewrite_cost = 0.0          # F5: out-of-pocket spend stored on the dispatch row
    rewrite_fused = False       # F5: a real (>=2 seat) panel authored the rewrite
    # "One knob": the dispatch's UI model/effort picker also drives the Fusion
    # judge (rewrite synthesis) and the enrich judge — same model that runs the
    # executor. Blank model ("default") keeps the judge on opus so an untouched
    # picker never silently downgrades the synthesis seat; effort flows straight
    # through (claude --effort accepts medium/high/xhigh/max), falling back to
    # "high" only for an out-of-range value.
    _, claude_model, _ = _derive_executor(model)
    judge_model = claude_model or "opus"
    judge_effort = (effort or "").strip()
    if judge_effort not in ("medium", "high", "xhigh", "max"):
        judge_effort = "high"
    if do_rewrite:
        # Include staged attachments in the rewriter input so it can plan
        # around them. They get moved to the dispatch dir inside _run_dispatch.
        stash_atts = attachments_mod.list_files(str(project_id))
        task_for_rewriter = (
            attachments_mod.render_for_prompt(stash_atts) + "\n\n" + task
            if stash_atts else task
        )
        loop = asyncio.get_running_loop()
        try:
            # F3.2: route the rewrite's ONE brain call through Fusion when the
            # send asked for it. fusion/panel/judge_* are passed positionally
            # (run_in_executor takes *args, not kwargs) — rewriter.rewrite's
            # signature is (user_task, project_path, fusion, panel, judge_model,
            # judge_effort, verify). With do_fusion=False this is byte-for-byte the
            # original single-claude path (panel + judge_*/verify are inert
            # downstream when fusion is off).
            result = await loop.run_in_executor(
                None, rewriter.rewrite, task_for_rewriter, proj["path"],
                do_fusion, panel, judge_model, judge_effort, do_verify,
            )
            if result.ok and result.rewritten_prompt:
                final_task = result.rewritten_prompt
                rewrite_cost = result.cost_usd
                rewrite_fused = bool(result.fusion_panel)
                rewrite_event = {
                    "stage": "rewrite_ok",
                    "cost_usd": round(result.cost_usd, 4),
                    "duration_s": round(result.duration_s, 1),
                    "model": result.model,
                    "bundle_chars": result.bundle_chars,
                    # F5: `fusion` = the toggle was on; `fused` = a real panel
                    # actually authored it (vs. silently falling back to plain
                    # claude). panel_breakdown carries the per-seat cost/tokens.
                    "fusion": do_fusion,
                    "fused": rewrite_fused,
                    "panel": panel,
                    "fusion_preset": result.fusion_preset,
                    "fusion_seats": result.fusion_seats,
                    "panel_breakdown": _fusion_panel_breakdown(result),
                    "verifier": result.verifier,   # F11.c.1: verdict (empty if off)
                }
            else:
                reason = result.error or "rewrite returned empty prompt"
                # Include a preview of the assistant's raw text so non-JSON /
                # empty-prompt failures are diagnosable from the timeline.
                # Without this we only see "model returned non-JSON" with no
                # clue what it actually said.
                raw_preview = (result.raw_assistant_text or "")[:600]
                rewrite_cost = result.cost_usd
                rewrite_fused = bool(result.fusion_panel)
                rewrite_event = {
                    "stage": "rewrite_skipped",
                    "reason": reason,
                    "cost_usd": round(result.cost_usd, 4),
                    "duration_s": round(result.duration_s, 1),
                    "model": result.model,
                    "bundle_chars": result.bundle_chars,
                    "raw_preview": raw_preview,
                    # F5: a fused rewrite that produced bad JSON still SPENT on the
                    # panel — surface that breakdown so the cost isn't a mystery.
                    "fusion": do_fusion,
                    "fused": rewrite_fused,
                    "fusion_preset": result.fusion_preset,
                    "fusion_seats": result.fusion_seats,
                    "panel_breakdown": _fusion_panel_breakdown(result),
                    "verifier": result.verifier,   # F11.c.1: verdict (empty if off)
                }
                rewrite_failed = True
                rewrite_error = reason
                print(f"[orchestrator] /send rewrite failed, not dispatching: {reason}")
                if raw_preview:
                    print(f"[orchestrator] rewriter raw response preview:\n{raw_preview}")
        except Exception as e:
            rewrite_event = {"stage": "rewrite_skipped", "reason": f"exception: {e}"}
            rewrite_failed = True
            rewrite_error = f"exception: {e}"
            print(f"[orchestrator] /send rewrite failed, not dispatching: {e}")

    # Rewrite was requested but failed: surface it as a failed dispatch row
    # instead of quietly running the original task. Create the row, mark it
    # failed with the rewrite reason (shown as the row's outcome reason), and
    # record the rewrite_skipped stage event for the timeline. Then stop —
    # _run_dispatch is never called, so no iTerm tab opens. The user's prompt
    # is preserved client-side (localStorage) for a re-submit.
    if do_rewrite and rewrite_failed:
        dispatch_id = db.create_dispatch(project_id, task, wall_clock_cap_s=wall_cap_s)
        # F5: stamp cost BEFORE mark_failed_to_spawn — it copies cost_usd onto the
        # outcome row, so a fused rewrite that failed still records what it spent.
        db.set_dispatch_cost(dispatch_id, rewrite_cost, fused=rewrite_fused)
        db.mark_failed_to_spawn(dispatch_id, f"rewrite failed: {rewrite_error}")
        if rewrite_event:
            db.record_event(dispatch_id, "stage", rewrite_event)
        print(f"[orchestrator] /send created failed dispatch #{dispatch_id} (rewrite failed)")
        return

    # F7: optional multi-model ENRICHMENT — analyze the (possibly rewritten) task
    # with a panel and APPEND a "## Multi-model analysis" block to the executor's
    # prompt. This is SEPARATE from the rewrite (which AUTHORS the prompt); here
    # the panel only informs. A failure must NEVER abort the dispatch — on any
    # shortfall we dispatch the un-enriched prompt and record fusion_skipped.
    fusion_event: dict | None = None
    if do_enrich:
        try:
            loop = asyncio.get_running_loop()
            fres = await loop.run_in_executor(
                None, lambda: fusion_mod.enrich(
                    final_task, proj["path"], panel,
                    judge_model=judge_model, judge_effort=judge_effort,
                    verify=do_verify))
            if fres.ok and fres.enrichment_md:
                final_task = final_task + "\n\n" + fres.enrichment_md
                rewrite_cost += fres.cost_usd                    # add to dispatch spend
                rewrite_fused = rewrite_fused or bool(fres.panel_models)
                fusion_event = {
                    "stage": "fusion_ok",
                    "cost_usd": round(fres.cost_usd, 4),
                    "panel_models": fres.panel_models,
                    "analysis": fres.analysis,
                }
            else:
                fusion_event = {
                    "stage": "fusion_skipped",
                    "reason": fres.error or "no analysis produced",
                    "cost_usd": round(fres.cost_usd, 4),
                }
                print(f"[orchestrator] /send enrich skipped: {fres.error}")
        except Exception as e:
            fusion_event = {"stage": "fusion_skipped", "reason": f"exception: {e}"}
            print(f"[orchestrator] /send enrich crashed (dispatching un-enriched): {e}")

    dispatch_id, err = await _run_dispatch(
        project_id, final_task, wall_cap_s, effort, model)
    if err:
        print(f"[orchestrator] /send dispatch failed: {err}")
        return
    # F5: record the rewrite (+ enrich) spend on the dispatch row; complete_dispatch
    # (and the kill/pause/orphan writers) copy it onto the outcome row at completion.
    if do_rewrite or do_enrich:
        db.set_dispatch_cost(dispatch_id, rewrite_cost, fused=rewrite_fused)
    if rewrite_event:
        db.record_event(dispatch_id, "stage", rewrite_event)
    if fusion_event:
        db.record_event(dispatch_id, "stage", fusion_event)


@app.post("/send")
async def send(
    project_id: int = Form(...),
    task: str = Form(...),
    wall_cap_s: int = Form(14400),
    rewrite: str = Form("false"),
    effort: str = Form("max"),
    model: str = Form(""),
    fusion: str = Form("false"),
    fusion_panel: str = Form(""),
    fusion_seats: str = Form(""),
    fusion_enrich: str = Form("false"),
    fusion_verify: str = Form("false"),
):
    """Fire-and-forget send. Validates synchronously, then schedules the
    rewrite+dispatch as a background task and returns immediately so the
    browser tab stays interactive. Status shows up in the polled runs panel
    once the dispatch row is created."""
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(404, "unknown project")
    if not Path(proj["path"]).is_dir():
        raise HTTPException(400, f"project path no longer exists: {proj['path']}")
    task = task.strip()
    if not task:
        raise HTTPException(400, "empty task")
    wall_cap_s = max(60, min(21600, int(wall_cap_s)))  # ceiling: 6h (see _run_dispatch)
    do_rewrite = rewrite.lower() in ("1", "true", "yes", "on")

    # Optional multi-model Fusion for the rewrite brain call. The panel is a mixed
    # list of seats: Claude Code seats (local `claude` CLI, model+effort, $0, NO
    # Anthropic API), codex seats (C5.1: local `codex exec`, $0, NO OpenAI API), and
    # external cross-lab providers (key-gated). The UI sends it as JSON in
    # `fusion_seats`; _parse_fusion_panel validates each seat and drops anything we
    # can't honor (Claude/codex models against their whitelists, providers against
    # active keys) so a stale UI selection can never force an unusable seat. (Legacy
    # comma `fusion_panel` is still accepted as a fallback.) An empty panel lets
    # run_fusion_json fall back to the configured preset.
    do_fusion = fusion.lower() in ("1", "true", "yes", "on")
    active = config.active_providers()
    codex_models = _codex_seat_models()
    panel = _parse_fusion_panel(fusion_seats, fusion_panel, active, codex_models)

    # The single model picker determines the executor. The disabled Codex options
    # are cosmetic; derive and validate server-side so a crafted codex request is
    # rejected when unavailable instead of falling through to Claude.
    executor_engine, _, executor_model = _derive_executor(model, codex_models)
    try:
        executor_engine, executor_model = _validate_executor_engine(
            executor_engine, executor_model, codex_models,
            codex_available=config.codex_cli_available())
    except ValueError as e:
        raise HTTPException(400, str(e))
    # F7: enrichment mode — analyze the task with a panel and APPEND the analysis
    # to the executor prompt (separate from the rewrite). Reuses the same panel
    # selection; needs the panel to be usable (>=2 seats) or it self-skips.
    do_enrich = fusion_enrich.lower() in ("1", "true", "yes", "on")
    do_verify = fusion_verify.lower() in ("1", "true", "yes", "on")
    if do_fusion or do_enrich:
        print(f"[orchestrator] /send fusion={'on' if do_fusion else 'off'} "
              f"enrich={'on' if do_enrich else 'off'} "
              f"verify={'on' if do_verify else 'off'}, panel={panel or '(preset)'}")

    # Strong-ref the task so Python's GC can't drop the rewrite+dispatch
    # mid-run when the browser tab disconnects after the immediate response.
    task = asyncio.create_task(_send_in_background(
        project_id, task, wall_cap_s, do_rewrite, effort, model,
        do_fusion=do_fusion, panel=panel, do_enrich=do_enrich, do_verify=do_verify,
    ))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return JSONResponse({"ok": True, "rewrite": do_rewrite})


# ─── attachments (drag-drop on dispatch form) ────────────────────────────

@app.post("/attachments/{project_id}/upload")
async def attachment_upload(project_id: int, file: UploadFile = File(...)):
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(404, "unknown project")
    data = await file.read()
    ok, err, att = attachments_mod.save(str(project_id), file.filename or "unnamed", data)
    if not ok:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    files = attachments_mod.list_files(str(project_id))
    return JSONResponse({"ok": True, "added": att.name,
                         "files": [{"name": a.name, "size": a.size} for a in files]})


@app.post("/attachments/{project_id}/remove")
async def attachment_remove(project_id: int, name: str = Form(...)):
    if not db.get_project(project_id):
        raise HTTPException(404)
    attachments_mod.remove(str(project_id), name)
    files = attachments_mod.list_files(str(project_id))
    return JSONResponse({"ok": True,
                         "files": [{"name": a.name, "size": a.size} for a in files]})


@app.get("/attachments/{project_id}")
async def attachment_list(project_id: int):
    files = attachments_mod.list_files(str(project_id))
    return JSONResponse({"files": [{"name": a.name, "size": a.size} for a in files]})


# ─── project onboarding (phase 9) ────────────────────────────────────────

def _auto_apply_onboarding(result: onboarding.OnboardingResult, project_path: str) -> dict:
    """Apply everything we safely can from a successful onboarding analysis.

    Two passes:
      1. proposed_edits: route through edits_mod.apply_edit, which already
         enforces phase-8 rules (allowed actions, layout-scoped parents,
         no overwrite for create_task_file). Invalid edits get logged as
         skipped — we never bypass validation.
      2. recommendations: root-level file creates. We ONLY create if the
         target doesn't already exist — never overwrite, never delete.
         Restricted to .md files and .forge.json; dotfiles otherwise are
         too risky to auto-create. Path must stay inside project root.

    Per-item exceptions are caught and recorded; one failure never aborts
    the batch. Returns an apply_log dict with applied/skipped/failed lists.
    """
    log = {"applied": [], "skipped": [], "failed": []}
    project_root = Path(project_path).resolve()

    for view in result.proposed_edits:
        label = f"{view.action} → {view.path}"
        if not view.valid:
            log["skipped"].append({
                "kind": "edit", "label": label, "path": view.path,
                "reason": view.validation_error or "validation failed",
            })
            continue
        proposal = edits_mod.EditProposal(
            action=view.action, path=view.path,
            content=view.content, rationale=view.rationale,
        )
        try:
            res = edits_mod.apply_edit(proposal, project_path)
        except (OSError, ValueError) as e:
            log["failed"].append({
                "kind": "edit", "label": label, "path": view.path, "reason": str(e),
            })
            continue
        if res.ok:
            log["applied"].append({
                "kind": "edit", "label": label, "path": res.path,
                "bytes": res.written_bytes,
            })
        else:
            log["failed"].append({
                "kind": "edit", "label": label, "path": view.path,
                "reason": res.error or "apply_edit returned not-ok",
            })

    for rec in result.recommendations:
        rel = (rec.target_path or "").strip()
        label = rec.title or rel or "(unnamed recommendation)"
        if not rel:
            log["skipped"].append({"kind": "recommendation", "label": label,
                                   "path": "", "reason": "no target_path"})
            continue
        if not rec.manual_content:
            log["skipped"].append({"kind": "recommendation", "label": label,
                                   "path": rel, "reason": "no manual_content to write"})
            continue
        # Path hygiene: no traversal, no absolutes, no symlink escape.
        if rel.startswith("/") or rel.startswith("~"):
            log["skipped"].append({"kind": "recommendation", "label": label,
                                   "path": rel, "reason": "absolute paths not allowed"})
            continue
        parts = Path(rel).parts
        if ".." in parts:
            log["skipped"].append({"kind": "recommendation", "label": label,
                                   "path": rel, "reason": "'..' not allowed in path"})
            continue
        # Allow .md anywhere; allow .forge.json at root; reject other dotfiles.
        basename = Path(rel).name
        is_forge = (rel == ".forge.json")
        if not is_forge and any(p.startswith(".") for p in parts):
            log["skipped"].append({"kind": "recommendation", "label": label, "path": rel,
                                   "reason": "dotfile creates not auto-applied (except .forge.json)"})
            continue
        if not (rel.endswith(".md") or is_forge):
            log["skipped"].append({"kind": "recommendation", "label": label, "path": rel,
                                   "reason": "only .md or .forge.json may be auto-created"})
            continue
        target = project_root / rel
        if not onboarding._within_project(project_root, target):
            log["skipped"].append({"kind": "recommendation", "label": label, "path": rel,
                                   "reason": "path escapes project root"})
            continue
        if target.exists():
            log["skipped"].append({"kind": "recommendation", "label": label, "path": rel,
                                   "reason": "file already exists — won't overwrite"})
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            content = rec.manual_content
            if not content.endswith("\n"):
                content += "\n"
            target.write_text(content)
        except (OSError, ValueError) as e:
            log["failed"].append({"kind": "recommendation", "label": label,
                                  "path": rel, "reason": str(e)})
            continue
        log["applied"].append({
            "kind": "recommendation", "label": label, "path": rel,
            "bytes": len(content.encode("utf-8", errors="replace")),
        })

    return log


def _render_prior_runs(project_id: int, limit: int = 5) -> str:
    """Render the last `limit` onboarding rounds as a markdown context the
    analyzer can use to avoid duplicating prior work. Each round shows when
    it ran, counts, and the specific labels we applied / skipped / failed
    so the model can see "we already added memory/X.md" and skip it.

    Bounded by `limit` rounds and ~3 KB total — long enough to be useful,
    short enough to not blow up the analyzer prompt. Returns "" if there
    are no prior rounds; analyze() turns that into a friendly default.
    """
    import json as _json
    rows = db.list_onboarding_runs(project_id, limit=limit)
    if not rows:
        return ""
    lines = [f"This project has been analyzed {len(rows)} time(s) before "
             f"(showing newest first; full history is in the orchestrator UI)."]
    total_chars = 0
    char_cap = 3000
    for i, row in enumerate(rows, 1):
        if total_chars > char_cap:
            lines.append(f"\n*(…{len(rows) - i + 1} older round(s) omitted to keep this prompt small)*")
            break
        when = _fmt_rel(row["created_at"])
        header = (f"\n## Round #{row['id']} ({when})  "
                  f"applied {row['applied_count']} · skipped {row['skipped_count']} · failed {row['failed_count']}")
        lines.append(header)
        total_chars += len(header)
        if row.get("project_summary"):
            summary_line = f"_summary_: {row['project_summary']}"
            lines.append(summary_line)
            total_chars += len(summary_line)
        # Pull the apply_log out of result_json for per-item detail.
        full = db.get_onboarding_run(row["id"])
        if not full:
            continue
        try:
            data = _json.loads(full["result_json"])
        except (_json.JSONDecodeError, TypeError):
            continue
        log = data.get("apply_log") or {}
        for bucket in ("applied", "skipped", "failed"):
            entries = log.get(bucket) or []
            if not entries:
                continue
            lines.append(f"**{bucket}:**")
            for e in entries:
                label = e.get("label") or e.get("path") or "?"
                reason = e.get("reason")
                line = f"- {label}" + (f" — {reason}" if reason else "")
                lines.append(line)
                total_chars += len(line)
                if total_chars > char_cap:
                    lines.append("- *(more items omitted)*")
                    break
    return "\n".join(lines)


async def _run_onboard_job(job_id: str, project_id: int, project_path: str) -> None:
    """Background task: gather prior-run context → run `onboarding.analyze`
    → auto-apply what we safely can → persist a row in `onboarding_runs`
    → set `result.run_id` so the UI can redirect to the permanent URL.
    Detached from any HTTP request so a tab disconnect mid-analysis doesn't
    lose the result."""
    import json as _json
    loop = asyncio.get_running_loop()
    try:
        prior_ctx = await loop.run_in_executor(None, _render_prior_runs, project_id)
        latest_run = await loop.run_in_executor(None, db.latest_onboarding_run, project_id)
        last_run_ts = latest_run["created_at"] if latest_run else None
        result = await loop.run_in_executor(
            None, onboarding.analyze, project_path, prior_ctx, last_run_ts
        )
        if result.ok:
            try:
                result.apply_log = await loop.run_in_executor(
                    None, _auto_apply_onboarding, result, project_path
                )
            except Exception as e:
                # Auto-apply itself crashed — keep the analysis but surface the failure.
                result.apply_log = {"applied": [], "skipped": [], "failed": [
                    {"kind": "batch", "label": "auto-apply crashed",
                     "path": "", "reason": str(e)}
                ]}
        # Persist this round so the user can see history across re-runs.
        # We persist failed analyses too so the user can see them in history.
        # Persistence failures are logged but don't break the in-memory result.
        try:
            log = result.apply_log or {}
            run_id = await loop.run_in_executor(
                None,
                lambda: db.save_onboarding_run(
                    project_id,
                    ok=result.ok,
                    model=result.model or None,
                    duration_s=result.duration_s or None,
                    cost_usd=result.cost_usd or None,
                    project_summary=result.project_summary or None,
                    error=result.error or None,
                    applied_count=len(log.get("applied", [])),
                    skipped_count=len(log.get("skipped", [])),
                    failed_count=len(log.get("failed", [])),
                    result_json=_json.dumps(onboarding.result_to_dict(result), default=str),
                ),
            )
            result.run_id = run_id
            print(f"[orchestrator] persisted onboarding run #{run_id} for project {project_id}")
        except Exception as e:
            print(f"[orchestrator] failed to persist onboarding run for project {project_id}: {e}")
        jobs.set_done(job_id, result)
    except Exception as e:
        jobs.set_error(job_id, str(e))


@app.post("/project/{project_id}/onboard", response_class=HTMLResponse)
async def onboard_project(request: Request, project_id: int):
    """Kick off the onboarding analysis and return a pending page. The
    analysis (~10-60s `claude` brain call, visible tab) runs as a background task held
    in the strong-ref set; the page polls /api/job/<id> and reloads itself
    when done. This way the user can switch tabs / navigate away and come
    back to a finished result."""
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(404, "unknown project")
    if not Path(proj["path"]).is_dir():
        raise HTTPException(400, f"project path no longer exists: {proj['path']}")

    job_id = jobs.create("onboard")
    task = asyncio.create_task(_run_onboard_job(job_id, project_id, proj["path"]))
    # Strong-ref so Python doesn't GC the task mid-run (see _background_tasks).
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return templates.TemplateResponse(
        request, "onboarding.html",
        {"project": proj, "result": None, "job_id": job_id, "job_status": "pending"},
    )


@app.get("/project/{project_id}/onboard", response_class=HTMLResponse)
async def onboard_project_status(request: Request, project_id: int, job_id: str | None = None):
    """Render the onboarding page in the current job state. Once a job
    finishes successfully we redirect to the permanent /run/<id> URL so
    the result survives navigation. No-job-id hits go to history — that
    page lists every past round and is the canonical entry point."""
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(404, "unknown project")
    if not job_id:
        return RedirectResponse(f"/project/{project_id}/onboard/history", status_code=303)

    job = jobs.get(job_id)
    if job is None:
        # Job expired or never existed — history is the persistent record.
        return RedirectResponse(f"/project/{project_id}/onboard/history", status_code=303)

    if job["status"] == "done":
        result = job["result"]
        # If we persisted a row for this run, that's the canonical URL —
        # redirect there so the user lands on a page they can come back to.
        rid = getattr(result, "run_id", None) if result is not None else None
        if rid:
            return RedirectResponse(
                f"/project/{project_id}/onboard/run/{rid}", status_code=303
            )
        # Fall back to inline render only if persistence failed.
        return templates.TemplateResponse(
            request, "onboarding.html",
            {"project": proj, "result": result, "job_id": job_id, "job_status": "done"},
        )
    if job["status"] == "error":
        result = onboarding.OnboardingResult(ok=False, error=job["error"] or "unknown error")
        return templates.TemplateResponse(
            request, "onboarding.html",
            {"project": proj, "result": result, "job_id": job_id, "job_status": "error"},
        )
    return templates.TemplateResponse(
        request, "onboarding.html",
        {"project": proj, "result": None, "job_id": job_id, "job_status": "pending"},
    )


@app.get("/project/{project_id}/onboard/history", response_class=HTMLResponse)
async def onboard_history(request: Request, project_id: int):
    """List of every analyze-project round for this project. Lets the user
    see what each round changed over time — applied/skipped/failed counts,
    timestamp, click through to the full per-round result."""
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(404, "unknown project")
    runs = db.list_onboarding_runs(project_id, limit=200)
    return templates.TemplateResponse(
        request, "onboarding_history.html",
        {"project": proj, "runs": runs, "fmt_rel": _fmt_rel},
    )


@app.get("/project/{project_id}/onboard/run/{run_id}", response_class=HTMLResponse)
async def onboard_run_detail(request: Request, project_id: int, run_id: int):
    """Render a historical run using the same template as the live result —
    rehydrate the stored JSON into an OnboardingResult. Marked `historical`
    so the template can show a 'viewing past run' banner instead of the
    auto-poll spinner."""
    import json as _json
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(404, "unknown project")
    row = db.get_onboarding_run(run_id)
    if not row or row["project_id"] != project_id:
        raise HTTPException(404, "unknown run for this project")
    try:
        data = _json.loads(row["result_json"])
    except (_json.JSONDecodeError, TypeError) as e:
        raise HTTPException(500, f"corrupt onboarding_runs.result_json: {e}")
    result = onboarding.result_from_dict(data if isinstance(data, dict) else {})
    return templates.TemplateResponse(
        request, "onboarding.html",
        {"project": proj, "result": result, "job_id": None, "job_status": "done",
         "historical_run": row, "fmt_rel": _fmt_rel},
    )


@app.get("/api/job/{job_id}")
async def api_job_status(job_id: str):
    """Polled by job-pending pages. Returns enough for the client to decide
    whether to keep polling or navigate to the result. For finished
    onboarding jobs we also expose `run_id` and `project_id` so the
    pending page can jump straight to the permanent /run/<id> URL."""
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown or expired job")
    payload = {
        "status": job["status"],
        "kind": job["kind"],
        "error": job["error"],
    }
    # Onboarding-specific: surface the persisted run id once it's known.
    result = job.get("result")
    if result is not None:
        rid = getattr(result, "run_id", None)
        if rid:
            payload["run_id"] = rid
    return JSONResponse(payload)


# ─── proposed-edit apply (phase 8) ───────────────────────────────────────

@app.post("/apply_edits", response_class=HTMLResponse)
async def apply_edits(request: Request):
    """Apply user-checked edits (from the rewrite-preview form), then
    re-render a status page that lets them go back and dispatch.

    The form posts one set of (action, path, content, rationale) tuples
    per checked edit, plus the project_id. Each edit is independently
    validated by `edits.apply_edit` — failures are reported per-row,
    successes are reported per-row, and the user can then choose to
    dispatch the rewritten prompt.
    """
    form = await request.form()
    try:
        project_id = int(form.get("project_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "missing project_id")
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(404, "unknown project")

    actions = form.getlist("edit_action")
    paths = form.getlist("edit_path")
    contents = form.getlist("edit_content")
    rationales = form.getlist("edit_rationale")
    selected = set(form.getlist("apply"))  # checkbox values: "0", "1", ...

    results = []
    for i in range(len(actions)):
        if str(i) not in selected:
            continue
        proposal = edits_mod.EditProposal(
            action=actions[i] if i < len(actions) else "",
            path=paths[i] if i < len(paths) else "",
            content=contents[i] if i < len(contents) else "",
            rationale=rationales[i] if i < len(rationales) else "",
        )
        results.append((proposal, edits_mod.apply_edit(proposal, proj["path"])))

    return templates.TemplateResponse(
        request, "apply_edits.html",
        {
            "project": proj,
            "results": results,
            "rewritten_prompt": form.get("rewritten_task", ""),
            "original_task": form.get("original_task", ""),
            "wall_cap_s": int(form.get("wall_cap_s", 1800)),
        },
    )


# ─── context bundle preview ──────────────────────────────────────────────

@app.get("/bundle/{project_id}", response_class=HTMLResponse)
async def view_bundle(request: Request, project_id: int):
    """Full-page preview of the context pack the rewriter (phase 4) will see."""
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(404, "unknown project")
    loop = asyncio.get_running_loop()
    pack = await loop.run_in_executor(None, bundle_mod.build_bundle, proj["path"])
    return templates.TemplateResponse(
        request,
        "bundle.html",
        {
            "project": proj,
            "pack": pack,
            "markdown": pack.to_markdown(),
        },
    )


@app.get("/bundle/{project_id}/raw")
async def bundle_raw(project_id: int):
    """Plain-text markdown of the bundle — useful for piping or testing."""
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(404, "unknown project")
    loop = asyncio.get_running_loop()
    pack = await loop.run_in_executor(None, bundle_mod.build_bundle, proj["path"])
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(pack.to_markdown())


MAX_TRANSCRIPT_BYTES = 2 * 1024 * 1024  # 2 MB shown; rest is truncation note


@app.get("/dispatch/{dispatch_id}", response_class=HTMLResponse)
async def view_dispatch(request: Request, dispatch_id: int):
    """Detail page: original task + summary (if available) + link to raw transcript."""
    d = db.get_dispatch_with_project(dispatch_id)
    if not d:
        raise HTTPException(404)
    import json as _json
    tags = []
    if d.get("tags_json"):
        try:
            tags = _json.loads(d["tags_json"])
        except (_json.JSONDecodeError, TypeError):
            pass
    # F5: pull the rewrite stage event so the fusion panel breakdown + cost are
    # inspectable AFTER the run (the live timeline scrolls it away). Newest match
    # wins (a re-send reuses the row only on resume, but be safe).
    rewrite = None
    enrichment = None     # F7: the multi-model analysis block (fusion_ok event)
    for ev in db.get_events(dispatch_id, since_id=0, limit=200):
        if ev["kind"] != "stage":
            continue
        st = ev["payload"].get("stage")
        if st in ("rewrite_ok", "rewrite_skipped"):
            rewrite = ev["payload"]
        elif st in ("fusion_ok", "fusion_skipped"):
            enrichment = ev["payload"]
    return templates.TemplateResponse(
        request, "dispatch.html",
        {"d": d, "tags": tags, "rewrite": rewrite, "enrichment": enrichment,
         "fmt_duration": _fmt_duration, "fmt_rel": _fmt_rel},
    )


@app.get("/dispatch/{dispatch_id}/transcript", response_class=HTMLResponse)
async def view_transcript(dispatch_id: int):
    d = db.get_dispatch(dispatch_id)
    if not d:
        raise HTTPException(404)
    tp = d.get("transcript_path")
    if not tp or not Path(tp).is_file():
        return HTMLResponse(f"<pre>No transcript available for dispatch #{dispatch_id}</pre>")
    p = Path(tp)
    size = p.stat().st_size
    truncated_note = ""
    if size > MAX_TRANSCRIPT_BYTES:
        with p.open("rb") as f:
            f.seek(size - MAX_TRANSCRIPT_BYTES)
            body = f.read().decode("utf-8", errors="replace")
        truncated_note = (
            f"<p style='color:#c97; font-style:italic'>"
            f"Showing last {MAX_TRANSCRIPT_BYTES // 1024 // 1024} MB of "
            f"{size // 1024 // 1024} MB transcript. Full file: <code>{tp}</code></p>"
        )
    else:
        body = p.read_text(errors="replace")
    safe = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return HTMLResponse(
        f"<html><body style='font-family:monospace;font-size:12px;padding:1em'>"
        f"<a href='/dispatch/{dispatch_id}'>← back</a><h3>Dispatch #{dispatch_id} transcript (raw)</h3>"
        f"{truncated_note}"
        f"<pre style='white-space:pre-wrap'>{safe}</pre></body></html>"
    )


# ─── F8: Fusion settings (registry + preset management) ──────────────────────

def _settings_ctx(err: str = "", ok: str = "") -> dict:
    """Read-model for the Settings page. Surfaces the EFFECTIVE registry
    (seeds merged with config.json) + presets + availability. Crucially it
    NEVER exposes an api_key — only a derived `has_key` boolean (whether a key
    resolves), the way active_providers() does for the dispatch form."""
    fcfg = config.fusion_config()
    active = config.active_providers()
    providers = []
    for name, prov in fcfg["providers"].items():
        providers.append({
            "name": name,
            "model": prov.get("model", ""),
            "script": prov.get("script", ""),
            "key_env": prov.get("key_env", ""),
            "price_in": prov.get("price_in", 0),
            "price_out": prov.get("price_out", 0),
            "enabled": prov.get("enabled", True) is not False,
            "kind": prov.get("kind", ""),
            "active": name in active,                         # keyed + enabled
            "has_key": bool(config.get_provider_key(name)),   # key resolves (never the key)
        })
    presets = fcfg["presets"]
    # F8.4: lenses are NOT secrets (they're perspective prompts), so the text is
    # shown + editable here, unlike api_keys.
    lenses = [{"name": n, "text": t} for n, t in fcfg.get("lenses", {}).items()]
    return {
        "fusion_available": config.is_fusion_available(),
        "claude_cli_available": config.claude_cli_available(),
        "preset": fcfg.get("preset"),
        "presets": presets,
        "preset_names": list(presets.keys()),
        "providers": providers,
        "lenses": lenses,
        "active_count": len(active),
        "verify_enabled": fcfg.get("verify", False),   # F11.c.1: verifier toggle state
        "err": err, "ok": ok,
    }


def _settings_redirect(ok: str = "", err: str = "") -> RedirectResponse:
    q = f"?ok={quote(ok)}" if ok else (f"?err={quote(err)}" if err else "")
    return RedirectResponse(f"/settings{q}", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
async def view_settings(request: Request, err: str = "", ok: str = ""):
    return templates.TemplateResponse(request, "settings.html", _settings_ctx(err=err, ok=ok))


@app.post("/settings/preset")
async def settings_set_preset(preset: str = Form(...)):
    try:
        config.set_preset(preset)
        return _settings_redirect(ok=f"preset → {preset}")
    except config.ConfigWriteError as e:
        return _settings_redirect(err=str(e))


@app.post("/settings/verify")
async def settings_set_verify(verify_enabled: str = Form("false")):
    """F11.c.1: toggle the opt-in verifier seat (fusion.verify) on/off."""
    try:
        on = verify_enabled.lower() in ("1", "true", "yes", "on")
        config.set_verify(on)
        return _settings_redirect(ok=f"verifier {'enabled' if on else 'disabled'}")
    except config.ConfigWriteError as e:
        return _settings_redirect(err=str(e))


@app.post("/settings/provider")
async def settings_upsert_provider(
    name: str = Form(...), script: str = Form(""), key_env: str = Form(""),
    model: str = Form(""), price_in: float = Form(0.0), price_out: float = Form(0.0),
    enabled: str = Form("true"),
):
    try:
        config.upsert_provider(
            name, script=script, key_env=key_env, model=model,
            price_in=price_in, price_out=price_out,
            enabled=enabled.lower() in ("1", "true", "yes", "on"))
        return _settings_redirect(ok=f"saved {name}")
    except (config.ConfigWriteError, ValueError) as e:
        return _settings_redirect(err=str(e))


@app.post("/settings/provider/{name}/enabled")
async def settings_provider_enabled(name: str, enabled: str = Form("true")):
    try:
        config.set_provider_enabled(name, enabled.lower() in ("1", "true", "yes", "on"))
        return _settings_redirect(ok=f"{name} {'enabled' if enabled else 'disabled'}")
    except config.ConfigWriteError as e:
        return _settings_redirect(err=str(e))


@app.post("/settings/provider/{name}/remove")
async def settings_provider_remove(name: str):
    try:
        config.remove_provider(name)
        return _settings_redirect(ok=f"removed {name}")
    except config.ConfigWriteError as e:
        return _settings_redirect(err=str(e))


# ── F8.4: lens management (per-seat perspective prompts) ─────────────────────
@app.post("/settings/lens")
async def settings_set_lens(name: str = Form(...), text: str = Form("")):
    try:
        config.set_lens(name, text)
        return _settings_redirect(ok=f"saved lens {name}")
    except config.ConfigWriteError as e:
        return _settings_redirect(err=str(e))


@app.post("/settings/lens/{name}/remove")
async def settings_lens_remove(name: str):
    try:
        config.remove_lens(name)
        return _settings_redirect(ok=f"removed lens {name}")
    except config.ConfigWriteError as e:
        return _settings_redirect(err=str(e))


# ── Fusion PROFILES (saveable named panels) + the lens playbook page ─────────
@app.post("/fusion/profile")
async def fusion_save_profile(name: str = Form(...), profile: str = Form(...)):
    """Save (or overwrite) a named Fusion profile — the picker POSTs the current
    seats as a JSON string. Returns the updated profiles map as JSON so the picker
    can refresh its dropdown without a page reload."""
    try:
        data = json.loads(profile or "{}")
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "profile is not valid JSON"}, status_code=400)
    try:
        fcfg = config.save_profile(name, data if isinstance(data, dict) else {})
        return JSONResponse({"ok": True, "profiles": fcfg.get("profiles", {})})
    except config.ConfigWriteError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/fusion/profile/remove")
async def fusion_remove_profile(name: str = Form(...)):
    """Delete a saved profile (NAME in the form body, not the URL path, so a
    profile named anything works). Returns the updated profiles map."""
    try:
        fcfg = config.remove_profile(name)
        return JSONResponse({"ok": True, "profiles": fcfg.get("profiles", {})})
    except config.ConfigWriteError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


def _md_inline(text: str) -> str:
    """Inline markdown → HTML for the playbook page: links, `code`, **bold**.
    HTML-escapes FIRST so the doc can't inject markup; the markdown delimiters
    survive escaping."""
    s = html.escape(text, quote=False)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
               lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>', s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    return s


def _render_markdown(md: str) -> str:
    """A SMALL markdown→HTML renderer scoped to FUSION_LENS_PLAYBOOK.md's syntax
    (headings, ---, > quotes, pipe tables, - lists, paragraphs + inline). Not a
    general renderer — just enough to serve our own doc with no dependency."""
    lines = md.split("\n")
    out: list = []
    i, n, in_list = 0, len(lines), False

    def close_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    while i < n:
        line = lines[i].strip()
        if not line:
            close_list(); i += 1; continue
        if re.match(r"^-{3,}$", line):
            close_list(); out.append("<hr>"); i += 1; continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            close_list(); lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_md_inline(m.group(2))}</h{lvl}>"); i += 1; continue
        # pipe table: a |row| followed by a |---|--- separator
        if (line.startswith("|") and i + 1 < n
                and "-" in lines[i + 1] and re.match(r"^\|?[\s:|-]+\|?$", lines[i + 1].strip())):
            close_list()
            t = ["<table><thead><tr>"]
            t += [f"<th>{_md_inline(c.strip())}</th>" for c in line.strip("|").split("|")]
            t.append("</tr></thead><tbody>")
            i += 2
            while i < n and lines[i].strip().startswith("|"):
                cells = lines[i].strip().strip("|").split("|")
                t.append("<tr>" + "".join(f"<td>{_md_inline(c.strip())}</td>" for c in cells) + "</tr>")
                i += 1
            t.append("</tbody></table>")
            out.append("".join(t)); continue
        if line.startswith(">"):
            close_list(); buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip()[1:].strip()); i += 1
            out.append(f"<blockquote>{_md_inline(' '.join(buf))}</blockquote>"); continue
        if re.match(r"^[-*]\s+", line):
            if not in_list:
                out.append("<ul>"); in_list = True
            item = re.sub(r"^[-*]\s+", "", line)
            out.append(f"<li>{_md_inline(item)}</li>"); i += 1; continue
        # paragraph: gather consecutive plain lines
        close_list(); para = [line]; i += 1
        while (i < n and lines[i].strip()
               and not re.match(r"^(#{1,6}\s|[-*]\s|>|\|)", lines[i].strip())
               and not re.match(r"^-{3,}$", lines[i].strip())):
            para.append(lines[i].strip()); i += 1
        out.append(f"<p>{_md_inline(' '.join(para))}</p>")
    close_list()
    return "\n".join(out)


@app.get("/playbook", response_class=HTMLResponse)
async def playbook(request: Request):
    """Render FUSION_LENS_PLAYBOOK.md (repo root) as a themed page — the in-UI
    'lens guide' linked from the dispatch picker + settings. Read-only."""
    path = BASE_DIR.parent / "FUSION_LENS_PLAYBOOK.md"
    try:
        content = _render_markdown(path.read_text(encoding="utf-8"))
    except OSError:
        content = "<p>Playbook not found.</p>"
    return templates.TemplateResponse(request, "playbook.html", {"content": content})
