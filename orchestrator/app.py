"""FastAPI app — orchestrator UI + dispatch endpoints + Stop hook receiver."""

import asyncio
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from orchestrator.lib import attachments as attachments_mod
from orchestrator.lib import bundle as bundle_mod
from orchestrator.lib import config, db, edits as edits_mod, embeddings, idle_notifier, jobs, loop_watchdog, onboarding, retrieval, rewriter, spawn, summarizer, watchdog

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    spawn.ensure_runner()
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
    return {
        "tabs": tabs,
        "saved_projects": saved,
        "fmt_duration": _fmt_duration,
        "fmt_rel": _fmt_rel,
        "fusion_providers": fusion_providers,
        "fusion_available": len(active) >= 2,
        "fusion_default_panel": fusion_default_panel,
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
    caller has no HTTP response to fail."""
    proj = db.get_project(project_id)
    if not proj:
        return None, "unknown project"
    if not Path(proj["path"]).is_dir():
        return None, f"project path no longer exists: {proj['path']}"
    task = task.strip()
    if not task:
        return None, "empty task"
    wall_cap_s = max(60, min(21600, int(wall_cap_s)))  # ceiling: 6h
    # Effort flag for the dispatched session only; brain calls stay at medium.
    effort = (effort or "").strip()
    if effort not in ("medium", "high", "xhigh", "max"):
        effort = "max"
    model = (model or "").strip()

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

    # Mark completed FIRST (atomic; loser of a race sees changed=False).
    # That way only the winning POST does the transcript copy / artifact row.
    watchdog.cancel(dispatch_id)
    changed = db.complete_dispatch(
        dispatch_id,
        session_id=session_id,
        transcript_path=src_transcript,
        exit_reason=exit_reason,
        outcome="completed",
    )
    if not changed:
        return {"ok": True, "note": "already finalized"}

    # Now safe to copy transcript and insert artifact — we're the winner.
    if src_transcript:
        src = Path(src_transcript).expanduser()
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

    # Fire-and-forget summarizer. Backgrounded so the Stop hook gets its
    # quick 200 — Claude shouldn't wait on the orchestrator's internal work.
    # IMPORTANT: store the task reference; without it Python may GC the
    # task mid-run (see asyncio.create_task docs).
    task = asyncio.create_task(_run_summarizer(dispatch_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

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


# ─── transcript view ──────────────────────────────────────────────────────

# ─── fire-and-forget send (rewrite optional) ─────────────────────────────

async def _send_in_background(project_id: int, task: str, wall_cap_s: int, do_rewrite: bool, effort: str = "max", model: str = "", do_fusion: bool = False, panel: list | None = None):
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
            # send asked for it. fusion/panel are passed positionally
            # (run_in_executor takes *args, not kwargs) — rewriter.rewrite's
            # signature is (user_task, project_path, fusion, panel). With
            # do_fusion=False this is byte-for-byte the original single-claude
            # path (panel is inert downstream when fusion is off).
            result = await loop.run_in_executor(
                None, rewriter.rewrite, task_for_rewriter, proj["path"],
                do_fusion, panel,
            )
            if result.ok and result.rewritten_prompt:
                final_task = result.rewritten_prompt
                rewrite_event = {
                    "stage": "rewrite_ok",
                    "cost_usd": round(result.cost_usd, 4),
                    "duration_s": round(result.duration_s, 1),
                    "model": result.model,
                    "bundle_chars": result.bundle_chars,
                    "fusion": do_fusion,
                    "panel": panel,
                }
            else:
                reason = result.error or "rewrite returned empty prompt"
                # Include a preview of the assistant's raw text so non-JSON /
                # empty-prompt failures are diagnosable from the timeline.
                # Without this we only see "model returned non-JSON" with no
                # clue what it actually said.
                raw_preview = (result.raw_assistant_text or "")[:600]
                rewrite_event = {
                    "stage": "rewrite_skipped",
                    "reason": reason,
                    "cost_usd": round(result.cost_usd, 4),
                    "duration_s": round(result.duration_s, 1),
                    "model": result.model,
                    "bundle_chars": result.bundle_chars,
                    "raw_preview": raw_preview,
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
        db.mark_failed_to_spawn(dispatch_id, f"rewrite failed: {rewrite_error}")
        if rewrite_event:
            db.record_event(dispatch_id, "stage", rewrite_event)
        print(f"[orchestrator] /send created failed dispatch #{dispatch_id} (rewrite failed)")
        return

    dispatch_id, err = await _run_dispatch(project_id, final_task, wall_cap_s, effort, model)
    if err:
        print(f"[orchestrator] /send dispatch failed: {err}")
        return
    if rewrite_event:
        db.record_event(dispatch_id, "stage", rewrite_event)


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

    # F3.1: optional multi-model Fusion for the rewrite brain call. Validate the
    # requested panel against providers whose key actually resolves right now,
    # silently dropping any unkeyed/unknown name — so a stale UI selection can
    # never force a provider we can't call. active_providers() reads config
    # once; an empty panel lets run_fusion_json fall back to the configured
    # preset. Threaded into _send_in_background; _run_dispatch needs no change.
    do_fusion = fusion.lower() in ("1", "true", "yes", "on")
    active = config.active_providers()
    panel = [p for p in fusion_panel.split(",") if p in active]
    if do_fusion:
        print(f"[orchestrator] /send fusion=on, panel={panel or '(preset)'}")

    # Strong-ref the task so Python's GC can't drop the rewrite+dispatch
    # mid-run when the browser tab disconnects after the immediate response.
    task = asyncio.create_task(_send_in_background(
        project_id, task, wall_cap_s, do_rewrite, effort, model,
        do_fusion=do_fusion, panel=panel,
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
    return templates.TemplateResponse(
        request, "dispatch.html",
        {"d": d, "tags": tags, "fmt_duration": _fmt_duration, "fmt_rel": _fmt_rel},
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
