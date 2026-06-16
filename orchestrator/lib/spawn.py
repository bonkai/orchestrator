"""iTerm2 tab spawning + PID tracking for kills.

Strategy:
  - Write the task body to ~/.orchestrator/tasks/<id>.txt (avoids shell-quoting hell).
  - Tell iTerm2 (via osascript) to open a new tab and exec ~/.orchestrator/bin/run.sh,
    with ORCHESTRATOR_RUN_ID=<id> in the env.
  - run.sh writes its own PID (which is about to become claude via exec) to
    ~/.orchestrator/pids/<id>.pid, then `exec claude "$TASK"`.
  - Orchestrator polls the pid file briefly to learn the claude PID for later kills.
"""

import asyncio
import os
import signal
import subprocess
import time
from pathlib import Path

from orchestrator.lib.db import DATA_DIR

TASKS_DIR = DATA_DIR / "tasks"
PIDS_DIR = DATA_DIR / "pids"
BIN_DIR = DATA_DIR / "bin"
RUN_SH = BIN_DIR / "run.sh"
# Brain calls (rewriter/summarizer/onboarding) get their own iTerm2 tabs so
# they're watchable like dispatches; sidecar files live here. See brain_run.sh.
BRAIN_DIR = DATA_DIR / "brain"
BRAIN_RUN_SH = BIN_DIR / "brain_run.sh"

RUN_SH_CONTENT = """#!/bin/bash
# Orchestrator runner — execed inside an iTerm2 tab.
# Records this shell's PID (which becomes the claude PID after exec),
# then runs claude with --dangerously-skip-permissions so background
# dispatches don't get stuck waiting on per-tool permission prompts the
# user can't see (no one's watching the iTerm2 tab when 10 are running).
#
# Two modes, distinguished by which sidecar file exists:
#   - <id>.resume  → `claude --resume <session_id>` (tracked resume)
#   - <id>.txt     → `claude "$TASK"`               (fresh dispatch)
#
# Reasoning effort is read from the <id>.effort sidecar (one of
# medium/high/xhigh/max), chosen per-dispatch in the UI and defaulting to
# max (deepest reasoning, no token constraints) when the file is absent.
# Internal brain calls (rewriter/summarizer in claude_runner.py) are
# separate and stay at medium — max can hurt their structured-JSON output.
set -e
if [ -z "$ORCHESTRATOR_RUN_ID" ]; then
    echo "Orchestrator: ORCHESTRATOR_RUN_ID not set" >&2
    exit 2
fi
PID_FILE="$HOME/.orchestrator/pids/${ORCHESTRATOR_RUN_ID}.pid"
RESUME_FILE="$HOME/.orchestrator/tasks/${ORCHESTRATOR_RUN_ID}.resume"
TASK_FILE="$HOME/.orchestrator/tasks/${ORCHESTRATOR_RUN_ID}.txt"
EFFORT=$(cat "$HOME/.orchestrator/tasks/${ORCHESTRATOR_RUN_ID}.effort" 2>/dev/null || echo max)
MODEL=$(cat "$HOME/.orchestrator/tasks/${ORCHESTRATOR_RUN_ID}.model" 2>/dev/null || echo "")
echo $$ > "$PID_FILE"
if [ -f "$RESUME_FILE" ]; then
    SID=$(cat "$RESUME_FILE")
    exec claude --dangerously-skip-permissions --effort "$EFFORT" ${MODEL:+--model "$MODEL"} --resume "$SID"
fi
if [ ! -f "$TASK_FILE" ]; then
    echo "Orchestrator: missing task file $TASK_FILE" >&2
    exit 2
fi
TASK=$(cat "$TASK_FILE")
exec claude --dangerously-skip-permissions --effort "$EFFORT" ${MODEL:+--model "$MODEL"} "$TASK"
"""


BRAIN_RUN_SH_CONTENT = """#!/bin/bash
# Orchestrator brain-call runner — execed inside an iTerm2 tab so the
# rewriter / summarizer / onboarding calls are WATCHABLE live (the user asked
# for no hidden headless brain calls). claude runs with --output-format
# stream-json --verbose so its reasoning + tool use scroll in the tab; `tee`
# mirrors the same stream to a sidecar JSONL that the orchestrator parses to
# recover the structured result the caller needs.
#
# A small python3 pretty-printer sits AFTER tee, so the live tab shows readable
# [assistant]/[tool]/[done] lines instead of raw JSONL. It formats only the
# terminal copy of the stream — tee has already written raw JSONL to the
# sidecar, which the orchestrator parses unchanged. PIPESTATUS[0] still captures
# claude's exit code (claude is first in the pipe; the formatter is last).
#
# Completion signalling for the waiting Python process:
#   <id>.done  — claude's exit code, written AFTER tee flushes (so when this
#                file exists, <id>.jsonl is complete).
#   <id>.pid   — this shell's PID; lets Python detect a closed/killed tab
#                (no .done will ever arrive in that case).
#
# ORCHESTRATOR_RUN_ID is deliberately NOT set for brain tabs, so the env-gated
# Stop hook stays a no-op and these don't post to /api/complete.
if [ -z "${ORCHESTRATOR_BRAIN_ID:-}" ]; then
    echo "Orchestrator brain: ORCHESTRATOR_BRAIN_ID not set" >&2
    exit 2
fi
ID="$ORCHESTRATOR_BRAIN_ID"
BRAIN_DIR="$HOME/.orchestrator/brain"
PROMPT_FILE="$BRAIN_DIR/${ID}.prompt"
OUT_FILE="$BRAIN_DIR/${ID}.jsonl"
DONE_FILE="$BRAIN_DIR/${ID}.done"
PID_FILE="$BRAIN_DIR/${ID}.pid"
MODEL=$(cat "$BRAIN_DIR/${ID}.model" 2>/dev/null || echo sonnet)
EFFORT=$(cat "$BRAIN_DIR/${ID}.effort" 2>/dev/null || echo medium)
MAXTURNS=$(cat "$BRAIN_DIR/${ID}.maxturns" 2>/dev/null || echo 30)
echo $$ > "$PID_FILE"
if [ ! -f "$PROMPT_FILE" ]; then
    echo "Orchestrator brain: missing prompt file $PROMPT_FILE" >&2
    echo 2 > "$DONE_FILE"
    exit 2
fi
PROMPT=$(cat "$PROMPT_FILE")
echo "---- orchestrator brain call: $ID ($MODEL / $EFFORT) ----"
echo "(watching live; the structured result is captured for the orchestrator)"
echo
claude -p "$PROMPT" \
    --model "$MODEL" \
    --max-turns "$MAXTURNS" \
    --output-format stream-json \
    --verbose \
    --dangerously-skip-permissions \
    --effort "$EFFORT" < /dev/null | tee "$OUT_FILE"
code=${PIPESTATUS[0]}
echo "$code" > "$DONE_FILE"
echo
echo "---- brain call finished (exit $code) ----"
"""


def ensure_runner():
    """One-time: create dirs and write the run.sh wrapper."""
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    PIDS_DIR.mkdir(parents=True, exist_ok=True)
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    RUN_SH.write_text(RUN_SH_CONTENT)
    RUN_SH.chmod(0o755)


def ensure_brain_runner():
    """One-time: create the brain sidecar dir and write the brain_run.sh
    wrapper. Lazy — called on the first brain call, so install.sh needs no
    change."""
    BRAIN_DIR.mkdir(parents=True, exist_ok=True)
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    BRAIN_RUN_SH.write_text(BRAIN_RUN_SH_CONTENT)
    BRAIN_RUN_SH.chmod(0o755)


def _osascript(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip()
        # Friendlier message for the common "app not installed" failure.
        if ("iTerm" in err or "iTerm2" in err) and "(-1728)" in err:
            raise RuntimeError(
                "iTerm2 not installed (or not accessible to AppleScript). "
                "Install with: brew install --cask iterm2"
            )
        raise RuntimeError(f"osascript failed: {err}")
    return result.stdout.strip()


def iterm2_installed() -> bool:
    """Cheap check: does the iTerm2 app exist?"""
    for path in ("/Applications/iTerm.app", "/Applications/iTerm2.app",
                 str(Path.home() / "Applications/iTerm.app")):
        if Path(path).exists():
            return True
    return False


def spawn_iterm2(project_path: str, dispatch_id: int, task: str, tab_title: str | None = None, effort: str = "max", model: str = "") -> None:
    """Open a new iTerm2 tab and start the runner for this dispatch.

    `effort` (medium/high/xhigh/max) is written to an <id>.effort sidecar
    that run.sh reads to pass `--effort` to the dispatched claude session."""
    if not iterm2_installed():
        raise RuntimeError(
            "iTerm2 not installed. Install with: brew install --cask iterm2"
        )
    ensure_runner()
    task_file = TASKS_DIR / f"{dispatch_id}.txt"
    task_file.write_text(task.strip())
    effort_file = TASKS_DIR / f"{dispatch_id}.effort"
    effort_file.write_text(effort.strip() or "max")
    if model.strip():
        (TASKS_DIR / f"{dispatch_id}.model").write_text(model.strip())

    safe_proj = project_path.replace('"', '\\"')
    title = tab_title or f"orch #{dispatch_id}"
    safe_title = title.replace('"', '\\"')

    cmd = (
        f'cd "{safe_proj}" && '
        f'export ORCHESTRATOR_RUN_ID={dispatch_id} && '
        f'printf "\\033]0;{safe_title}\\007" && '
        f'exec "$HOME/.orchestrator/bin/run.sh"'
    )
    apple_cmd = cmd.replace("\\", "\\\\").replace('"', '\\"')

    script = f"""
tell application "iTerm"
    activate
    if (count of windows) = 0 then
        create window with default profile
    end if
    tell current window
        set newTab to (create tab with default profile)
        tell current session of newTab
            set name to "{safe_title}"
            write text "{apple_cmd}"
        end tell
    end tell
end tell
"""
    try:
        _osascript(script)
    except Exception:
        # Clean up the orphan sidecar files so we don't leak files on failure.
        for f in (task_file, effort_file, TASKS_DIR / f"{dispatch_id}.model"):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
        raise


def auto_close_enabled() -> bool:
    """Opt-out switch for tab auto-close. Defaults on; set
    ORCHESTRATOR_AUTO_CLOSE_TABS=false to keep finished tabs around
    (e.g., to inspect terminal output of a failed session)."""
    return os.environ.get("ORCHESTRATOR_AUTO_CLOSE_TABS", "true").lower() in (
        "1", "true", "yes", "on",
    )


def close_iterm2_tab_by_title(title: str) -> bool:
    """Close the iTerm2 tab whose session name is exactly `title`, if it still
    exists. Walks every window's tabs. Returns True if found and closed, False
    otherwise. Silently no-ops if iTerm2 isn't installed."""
    if not iterm2_installed():
        return False
    target = title.replace('"', '\\"')
    script = f'''
tell application "iTerm"
    set foundIt to false
    repeat with w in windows
        repeat with t in tabs of w
            if name of current session of t is "{target}" then
                tell current session of t to close
                set foundIt to true
                exit repeat
            end if
        end repeat
        if foundIt then exit repeat
    end repeat
    return foundIt as string
end tell
'''
    try:
        out = _osascript(script)
    except Exception:
        return False
    return out.strip().lower() == "true"


def close_iterm2_tab(dispatch_id: int) -> bool:
    """Close the iTerm2 tab named `orch #<dispatch_id>` if it still exists.
    Thin wrapper over `close_iterm2_tab_by_title`."""
    return close_iterm2_tab_by_title(f"orch #{dispatch_id}")


def close_iterm2_tabs(dispatch_ids: list[int]) -> int:
    """Bulk-close tabs for many dispatches in a single AppleScript pass.
    Used by `/tabs/close_completed` for cleaning up accumulated stale tabs.
    Returns the count actually closed."""
    if not dispatch_ids or not iterm2_installed():
        return 0
    # Build AppleScript list literal: {"orch #1", "orch #2", ...}
    items = ", ".join(f'"orch #{int(d)}"' for d in dispatch_ids)
    script = f'''
tell application "iTerm"
    set targets to {{{items}}}
    set closedCount to 0
    repeat with w in windows
        set toClose to {{}}
        repeat with t in tabs of w
            set sName to name of current session of t
            repeat with tgt in targets
                if sName is (tgt as string) then
                    set end of toClose to t
                    exit repeat
                end if
            end repeat
        end repeat
        repeat with t in toClose
            try
                tell current session of t to close
                set closedCount to closedCount + 1
            end try
        end repeat
    end repeat
    return closedCount as string
end tell
'''
    try:
        out = _osascript(script)
        return int(out.strip() or 0)
    except Exception:
        return 0


def select_iterm2_tab(dispatch_id: int) -> bool:
    """Bring the iTerm2 tab for a running dispatch to the front.

    We tagged each tab with name "orch #<id>" at spawn time, so we walk
    every window's tabs looking for that name. Returns False if no match
    (tab was closed, iTerm restarted, etc.).
    """
    if not iterm2_installed():
        raise RuntimeError("iTerm2 not installed")
    target = f"orch #{dispatch_id}"
    script = f'''
tell application "iTerm"
    activate
    set foundIt to false
    repeat with w in windows
        repeat with t in tabs of w
            if name of current session of t is "{target}" then
                select w
                tell w to select t
                set foundIt to true
                exit repeat
            end if
        end repeat
        if foundIt then exit repeat
    end repeat
    return foundIt as string
end tell
'''
    out = _osascript(script)
    return out.strip().lower() == "true"


def spawn_iterm2_resume(project_path: str, session_id: str, dispatch_id: int, effort: str = "max", model: str = "") -> None:
    """Open a new iTerm2 tab and `claude --resume <session_id>` in it,
    tracked under `dispatch_id` so the Stop hook fires `/api/complete`
    and the summarizer updates project memory.

    Goes through the same run.sh as fresh dispatches: writes a `.resume`
    sidecar file with the session id, sets ORCHESTRATOR_RUN_ID, and lets
    run.sh handle PID-file writing + exec. The caller is responsible for
    having created a dispatch row beforehand.
    """
    if not iterm2_installed():
        raise RuntimeError(
            "iTerm2 not installed. Install with: brew install --cask iterm2"
        )
    ensure_runner()
    resume_file = TASKS_DIR / f"{dispatch_id}.resume"
    resume_file.write_text(session_id.strip())
    effort_file = TASKS_DIR / f"{dispatch_id}.effort"
    effort_file.write_text(effort.strip() or "max")
    if model.strip():
        (TASKS_DIR / f"{dispatch_id}.model").write_text(model.strip())

    safe_proj = project_path.replace('"', '\\"')
    title = f"orch #{dispatch_id} (resumed)"
    safe_title = title.replace('"', '\\"')

    cmd = (
        f'cd "{safe_proj}" && '
        f'export ORCHESTRATOR_RUN_ID={dispatch_id} && '
        f'printf "\\033]0;{safe_title}\\007" && '
        f'exec "$HOME/.orchestrator/bin/run.sh"'
    )
    apple_cmd = cmd.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
tell application "iTerm"
    activate
    if (count of windows) = 0 then
        create window with default profile
    end if
    tell current window
        set newTab to (create tab with default profile)
        tell current session of newTab
            set name to "{safe_title}"
            write text "{apple_cmd}"
        end tell
    end tell
end tell
'''
    try:
        _osascript(script)
    except Exception:
        for f in (resume_file, effort_file, TASKS_DIR / f"{dispatch_id}.model"):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
        raise


def read_claude_pid(dispatch_id: int, timeout_s: float = 5.0) -> int | None:
    """Poll for the PID file the runner writes. Returns None on timeout."""
    pid_file = PIDS_DIR / f"{dispatch_id}.pid"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        pid = read_pid_now(dispatch_id)
        if pid:
            return pid
        time.sleep(0.1)
    return None


def read_pid_now(dispatch_id: int) -> int | None:
    """One-shot read of the pid file. Used as a fallback when the initial
    5s poll missed it (iTerm2 was slow to start) and we now need the PID
    (e.g., to kill)."""
    pid_file = PIDS_DIR / f"{dispatch_id}.pid"
    if not pid_file.is_file():
        return None
    try:
        pid = int(pid_file.read_text().strip())
        return pid if pid > 0 else None
    except (ValueError, OSError):
        return None


def pid_alive(pid: int) -> bool:
    """Is this PID still a running (non-zombie) process?

    `os.kill(pid, 0)` alone returns True for zombies. In the real orchestrator
    flow Claude's parent is iTerm2's shell, which reaps quickly — but in tests
    (or any case where orchestrator is the parent), we'd see false positives.
    Verify the process isn't in 'Z' state via `ps`.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by someone else
    try:
        out = subprocess.run(
            ["ps", "-o", "state=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
        state = (out.stdout or "").strip()
        if not state:
            return False  # ps couldn't find it → not alive
        return state[0] != "Z"
    except Exception:
        return True  # fallback: trust the kill-0 result


def kill_pid(pid: int, grace_s: float = 5.0) -> bool:
    """SIGTERM, wait grace_s, then SIGKILL. Returns True if process is gone.

    Blocking version. Use `kill_pid_async` from async contexts so the event
    loop stays free for other dispatches during the SIGTERM grace period.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    deadline = time.time() + grace_s
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True


async def kill_pid_async(pid: int, grace_s: float = 5.0) -> bool:
    """SIGTERM, async-wait grace_s, then SIGKILL. Does not block the event loop."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    # Poll every 200ms via async sleep so the loop can serve other requests
    iters = max(1, int(grace_s / 0.2))
    for _ in range(iters):
        await asyncio.sleep(0.2)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True


def cleanup_dispatch_files(dispatch_id: int):
    for p in [
        TASKS_DIR / f"{dispatch_id}.txt",
        TASKS_DIR / f"{dispatch_id}.resume",
        TASKS_DIR / f"{dispatch_id}.effort",
        TASKS_DIR / f"{dispatch_id}.model",
        PIDS_DIR / f"{dispatch_id}.pid",
    ]:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    if auto_close_enabled():
        # Best-effort; never raise from cleanup. With 10+ concurrent dispatches
        # iTerm tabs would otherwise accumulate until manually closed.
        try:
            close_iterm2_tab(dispatch_id)
        except Exception:
            pass


# ─── brain calls (rewriter / summarizer / onboarding) in watchable tabs ──────

def brain_auto_close_enabled() -> bool:
    """Whether a brain tab auto-closes once its call SUCCEEDS. Defaults on so
    frequent rewriter calls don't pile tabs up; failed calls stay open
    regardless (so you can read what broke). Set
    ORCHESTRATOR_BRAIN_AUTO_CLOSE=false to keep successful tabs around too."""
    return os.environ.get("ORCHESTRATOR_BRAIN_AUTO_CLOSE", "true").lower() in (
        "1", "true", "yes", "on",
    )


def _brain_tab_title(brain_id: str, label: str) -> str:
    """Unique, readable iTerm2 tab title for a brain call: label + id suffix.
    Uniqueness (the random suffix) lets us close exactly this tab later."""
    suffix = brain_id.rsplit("-", 1)[-1]
    return f"orch brain: {label} {suffix}"


def _brain_tab_cmd(brain_id: str, cwd: str, title: str) -> str:
    """The shell command the brain tab runs. Sets ORCHESTRATOR_BRAIN_ID (NOT
    ORCHESTRATOR_RUN_ID — so the Stop hook stays a no-op), titles the tab, then
    execs brain_run.sh. Pure/string-only so it's unit-testable."""
    safe_proj = cwd.replace('"', '\\"')
    safe_title = title.replace('"', '\\"')
    return (
        f'cd "{safe_proj}" && '
        f'export ORCHESTRATOR_BRAIN_ID={brain_id} && '
        f'printf "\\033]0;{safe_title}\\007" && '
        f'exec "$HOME/.orchestrator/bin/brain_run.sh"'
    )


def cleanup_brain_files(brain_id: str):
    """Remove all sidecar files for a brain call. The tab (and its on-screen
    output) is unaffected — tee already wrote to the terminal."""
    for suf in ("prompt", "jsonl", "done", "pid", "model", "effort", "maxturns"):
        try:
            (BRAIN_DIR / f"{brain_id}.{suf}").unlink()
        except FileNotFoundError:
            pass


def spawn_brain_tab(brain_id: str, prompt: str, cwd: str,
                    model: str = "sonnet", effort: str = "medium",
                    max_turns: int = 30, label: str = "brain") -> None:
    """Open a new iTerm2 tab and run a brain call in it via brain_run.sh.

    Writes the prompt + config to BRAIN_DIR sidecars (avoids shell-quoting the
    prompt into AppleScript), then tells iTerm2 to open a tab and exec the
    runner. The caller polls <brain_id>.done for completion. Raises on spawn
    failure (so the caller can fall back to headless)."""
    if not iterm2_installed():
        raise RuntimeError("iTerm2 not installed")
    ensure_brain_runner()
    (BRAIN_DIR / f"{brain_id}.prompt").write_text(prompt, encoding="utf-8")
    (BRAIN_DIR / f"{brain_id}.model").write_text((model or "sonnet").strip())
    (BRAIN_DIR / f"{brain_id}.effort").write_text((effort or "medium").strip())
    (BRAIN_DIR / f"{brain_id}.maxturns").write_text(str(max_turns))

    title = _brain_tab_title(brain_id, label)
    safe_title = title.replace('"', '\\"')
    cmd = _brain_tab_cmd(brain_id, cwd, title)
    apple_cmd = cmd.replace("\\", "\\\\").replace('"', '\\"')

    script = f'''
tell application "iTerm"
    activate
    if (count of windows) = 0 then
        create window with default profile
    end if
    tell current window
        set newTab to (create tab with default profile)
        tell current session of newTab
            set name to "{safe_title}"
            write text "{apple_cmd}"
        end tell
    end tell
end tell
'''
    try:
        _osascript(script)
    except Exception:
        cleanup_brain_files(brain_id)
        raise


def finish_brain_tab(brain_id: str, label: str = "brain", success: bool = False):
    """Post-call teardown for a brain tab. Closes the tab when the call
    succeeded AND auto-close is enabled (failed calls stay open for
    inspection); always removes the sidecar files. Never raises."""
    if success and brain_auto_close_enabled():
        try:
            close_iterm2_tab_by_title(_brain_tab_title(brain_id, label))
        except Exception:
            pass
    cleanup_brain_files(brain_id)
