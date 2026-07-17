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
import base64
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from orchestrator.lib import config
from orchestrator.lib.db import DATA_DIR

# Serializes iTerm2 tab CREATION across threads. Tab spawns go through
# osascript / Apple Events; firing several at once (e.g. a Fusion panel of N
# Claude Code seats, each its own brain tab, plus the fusion tab) can race on
# "current window" and the focus save/restore. The osascript call is fast (well
# under a second), so serializing only the spawn moment costs ~nothing while the
# long part — polling each tab to completion — still runs fully in parallel.
_TAB_SPAWN_LOCK = threading.Lock()

# Per-osascript wall-clock ceiling. A *single* tab spawn is normally well under a
# second, but a Fusion burst (panel tab + N Claude seats + the dispatch tab, all
# spawned back-to-back) saturates iTerm2 — it's still rendering the just-created
# tabs and launching their `claude` processes — so an individual `write text`
# call can block far past the old 15s. That timeout failed dispatch #175 outright
# (a Fusion send). 45s is a genuine-hang ceiling, not a normal wait.
_OSASCRIPT_TIMEOUT_S = 45.0

# Minimum spacing between consecutive tab spawns, enforced while holding
# _TAB_SPAWN_LOCK. Spawns are ALREADY fully serialized (each _osascript blocks
# inside the lock), so this isn't about concurrency — it's a beat for iTerm2 to
# drain the previous tab's work (render + shell launch) before the next
# `write text`, which is what actually backed up under the Fusion burst.
_INTER_SPAWN_GAP_S = 0.5
_last_spawn_monotonic = 0.0  # guarded by _TAB_SPAWN_LOCK

TASKS_DIR = DATA_DIR / "tasks"
PIDS_DIR = DATA_DIR / "pids"
BIN_DIR = DATA_DIR / "bin"
RUN_SH = BIN_DIR / "run.sh"
# Brain calls (rewriter/summarizer/onboarding) get their own iTerm2 tabs so
# they're watchable like dispatches; sidecar files live here. See brain_run.sh.
BRAIN_DIR = DATA_DIR / "brain"
BRAIN_RUN_SH = BIN_DIR / "brain_run.sh"
# Fusion (optional, default-off) gets its own watchable tab too. The panel
# fan-out runs in a `fusion` tab (fusion_run.sh → fusion_call.py → the provider
# scripts); the judge then runs in a normal `brain` tab. Canonical templates
# live in the repo (orchestrator/providers/*.py + orchestrator/fusion_call.py);
# ensure_fusion_runner() materializes them into the data dir so they run
# editable per-machine (repo stays clean), the same way brain_run.sh is written.
FUSION_DIR = DATA_DIR / "fusion"
FUSION_RUN_SH = BIN_DIR / "fusion_run.sh"
FUSION_CALL_PY = BIN_DIR / "fusion_call.py"
FUSION_PROVIDERS_DIR = BIN_DIR / "providers"
_REPO_DIR = Path(__file__).resolve().parent.parent          # the repo's orchestrator/
_REPO_PROVIDERS_DIR = _REPO_DIR / "providers"

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
    --effort "$EFFORT" < /dev/null | tee "$OUT_FILE" | python3 -u -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except Exception:
        print(line)
        continue
    t = obj.get('type', '')
    if t == 'assistant':
        for blk in (obj.get('message') or {}).get('content', []):
            if not isinstance(blk, dict):
                continue
            bt = blk.get('type')
            if bt == 'text':
                txt = (blk.get('text') or '').strip()
                if txt:
                    print('[assistant]', txt)
            elif bt == 'tool_use':
                inp = json.dumps(blk.get('input') or {}, default=str)
                print('[tool]', blk.get('name') or '?', inp[:200])
    elif t == 'result':
        cost = obj.get('total_cost_usd') or obj.get('cost_usd') or 0
        print(f'[done] cost=\\${cost:.4f}')
    elif t == 'system' and obj.get('subtype') == 'init':
        print('[brain]', obj.get('model') or '', '/', (obj.get('session_id') or '')[:8])
"
code=${PIPESTATUS[0]}
echo "$code" > "$DONE_FILE"
echo
echo "---- brain call finished (exit $code) ----"
"""


FUSION_RUN_SH_CONTENT = """#!/bin/bash
# Orchestrator fusion-panel runner — execed inside an iTerm2 tab so the panel
# fan-out is WATCHABLE live (same principle as brain_run.sh). fusion_call.py runs
# each panel provider's script in parallel, interleaving their stderr on SCREEN,
# and prints ONLY the collected answers JSON to stdout — which `tee` captures to
# <id>.json for the orchestrator to read back. The judge then runs in a brain tab.
#
# ORCHESTRATOR_FUSION_ID (never ORCHESTRATOR_RUN_ID) so the env-gated Stop hook
# stays a no-op for fusion tabs.
if [ -z "${ORCHESTRATOR_FUSION_ID:-}" ]; then
    echo "Orchestrator fusion: ORCHESTRATOR_FUSION_ID not set" >&2
    exit 2
fi
ID="$ORCHESTRATOR_FUSION_ID"
DIR="$HOME/.orchestrator/fusion"
REQ="$DIR/${ID}.request.json"
PID_FILE="$DIR/${ID}.pid"
OUT_FILE="$DIR/${ID}.json"
DONE_FILE="$DIR/${ID}.done"
echo $$ > "$PID_FILE"
if [ ! -f "$REQ" ]; then
    echo "Orchestrator fusion: missing request file $REQ" >&2
    echo 2 > "$DONE_FILE"
    exit 2
fi
echo "---- orchestrator fusion panel: $ID (watching live) ----"
echo
python3 "$HOME/.orchestrator/bin/fusion_call.py" "$REQ" | tee "$OUT_FILE"
code=${PIPESTATUS[0]}
echo "$code" > "$DONE_FILE"
echo
echo "---- fusion panel finished (exit $code; judge runs next in a brain tab) ----"
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


def ensure_fusion_providers():
    """Lazy: copy the repo-canonical Fusion provider scripts
    (orchestrator/providers/*.py) into ~/.orchestrator/bin/providers/ so the
    panel fan-out can run them as standalone subprocesses. The repo is the
    source of truth — always overwritten, exactly like ensure_brain_runner
    rewrites brain_run.sh. Called on the first fusion call, so install.sh needs
    no change. Safe no-op if the repo templates dir is missing."""
    FUSION_PROVIDERS_DIR.mkdir(parents=True, exist_ok=True)
    if not _REPO_PROVIDERS_DIR.is_dir():
        return
    for src in _REPO_PROVIDERS_DIR.glob("*.py"):
        dst = FUSION_PROVIDERS_DIR / src.name
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        dst.chmod(0o755)


def ensure_fusion_runner():
    """One-time (lazy): create the fusion sidecar dir, write fusion_run.sh, copy
    the standalone fusion_call.py into the data dir, and materialize the provider
    scripts. Mirrors ensure_brain_runner; called before spawning a fusion tab, so
    install.sh needs no change. The repo is the source of truth (always rewritten)."""
    FUSION_DIR.mkdir(parents=True, exist_ok=True)
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    FUSION_RUN_SH.write_text(FUSION_RUN_SH_CONTENT)
    FUSION_RUN_SH.chmod(0o755)
    src = _REPO_DIR / "fusion_call.py"
    if src.is_file():
        FUSION_CALL_PY.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        FUSION_CALL_PY.chmod(0o755)
    ensure_fusion_providers()


def _osascript(script: str, timeout: float = _OSASCRIPT_TIMEOUT_S) -> str:
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout,
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


def _spawn_osascript(script: str) -> str:
    """Run a TAB-SPAWNING osascript serialized behind _TAB_SPAWN_LOCK and spaced
    so a Fusion burst (panel + N seats + dispatch) doesn't saturate iTerm2 past
    the timeout. The single choke point all four spawn_* functions share — keeps
    the lock-hold + inter-spawn beat in one place instead of copy-pasted.

    We DON'T retry a TimeoutExpired here: spawns are serial (the lock), so a
    45s timeout means genuine saturation a retry won't clear — and a retried
    dispatch-tab spawn risks a *duplicate* tab (two claude sessions on one run
    id). Spacing prevents the pile-up; the caller surfaces a failure as a
    `spawn_failed` event instead."""
    global _last_spawn_monotonic
    with _TAB_SPAWN_LOCK:
        if _last_spawn_monotonic:
            wait = _INTER_SPAWN_GAP_S - (time.monotonic() - _last_spawn_monotonic)
            if wait > 0:
                time.sleep(wait)
        try:
            return _osascript(script)
        finally:
            _last_spawn_monotonic = time.monotonic()


def iterm2_installed() -> bool:
    """Cheap check: does the iTerm2 app exist?"""
    for path in ("/Applications/iTerm.app", "/Applications/iTerm2.app",
                 str(Path.home() / "Applications/iTerm.app")):
        if Path(path).exists():
            return True
    return False


def _spawn_tab_script(safe_title: str, apple_cmd: str) -> str:
    """AppleScript to open a new iTerm2 tab that runs `apple_cmd`, titled
    `safe_title`, WITHOUT stealing focus from whatever the user is working in.
    Both args must already be AppleScript-escaped by the caller.

    Focus handling:
      * No `activate` — we never explicitly bring iTerm forward on spawn.
      * We capture whatever app is frontmost BEFORE touching iTerm, then ALWAYS
        restore it afterward — not only in the `create window` case. Some iTerm2
        versions raise the window on `create tab` (and `create window` always
        does), so an unconditional restore is the only reliable way to leave the
        user's foreground app untouched. Both System Events steps are wrapped in
        `try`, so a missing Accessibility permission degrades to "tab still
        opens, focus not restored" rather than failing the spawn.

    (Do NOT add `activate` here — select_iterm2_tab is the one place that
    intentionally brings a tab to the front.)"""
    return f'''
set frontApp to ""
try
    tell application "System Events"
        set frontApp to name of first application process whose frontmost is true
    end tell
end try
tell application "iTerm"
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
if frontApp is not "" then
    try
        tell application "System Events"
            tell process frontApp to set frontmost to true
        end tell
    end try
end if
'''


def _setuservar_printf(name: str, value: str) -> str:
    """A shell `printf` (with trailing ` && `) that sets an iTerm2 session user
    variable `user.<name>` via the OSC 1337 SetUserVar escape (value base64-
    encoded, as iTerm requires). We tag each tab with `user.orch_id` so the
    close path can find it by a STABLE id even after the running program
    (claude) overwrites the tab TITLE — which makes `name`-based matching
    unreliable. Read back in AppleScript via `variable named "user.<name>"`.
    The doubled backslashes survive the later cmd->AppleScript escaping in the
    spawn functions, exactly like the adjacent `\\033]0;` title printf."""
    b64 = base64.b64encode(value.encode()).decode()
    return f'printf "\\033]1337;SetUserVar={name}={b64}\\007" && '


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
        f'{_setuservar_printf("orch_id", str(dispatch_id))}'
        f'printf "\\033]0;{safe_title}\\007" && '
        f'exec "$HOME/.orchestrator/bin/run.sh"'
    )
    apple_cmd = cmd.replace("\\", "\\\\").replace('"', '\\"')

    script = _spawn_tab_script(safe_title, apple_cmd)
    try:
        _spawn_osascript(script)
    except Exception:
        # Clean up the orphan sidecar files so we don't leak files on failure.
        for f in (task_file, effort_file, TASKS_DIR / f"{dispatch_id}.model"):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
        raise


def auto_close_enabled() -> bool:
    """Opt-IN switch for main-dispatch tab auto-close. Defaults OFF so finished
    main-task tabs stay open and the user can keep the session going; set
    ORCHESTRATOR_AUTO_CLOSE_TABS=true to auto-close completed dispatch tabs.
    (Brain/fusion tabs are governed separately by ORCHESTRATOR_BRAIN_AUTO_CLOSE,
    which still defaults on — see brain_auto_close_enabled.)"""
    return os.environ.get("ORCHESTRATOR_AUTO_CLOSE_TABS", "false").lower() in (
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


def close_iterm2_session_by_var(var_name: str, value: str) -> bool:
    """Close the iTerm2 tab whose session variable `user.<var_name>` == `value`.
    The reliable handle for a tab whose TITLE the running program (claude)
    overwrites via its own OSC escape: the user variable set at spawn persists
    for the session's life. Returns True if found and closed, False otherwise;
    no-ops if iTerm2 isn't installed.

    AppleScript note: a session var MUST be read via `tell <session> to
    (variable named "...")`; the property-of form raises -1723 'Access not
    allowed'."""
    if not iterm2_installed():
        return False
    full_name = f"user.{var_name}".replace('"', '\\"')
    target = value.replace('"', '\\"')
    script = f'''
tell application "iTerm"
    set foundIt to false
    repeat with w in windows
        repeat with t in tabs of w
            try
                tell (current session of t) to set v to (variable named "{full_name}")
                if v is "{target}" then
                    tell current session of t to close
                    set foundIt to true
                    exit repeat
                end if
            end try
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
    """Close the dispatch's iTerm2 tab. Primary match is the `user.orch_id`
    session variable set at spawn (survives claude overwriting the tab title);
    falls back to the legacy `orch #<id>` title for tabs spawned before user-var
    tagging existed, or that died before claude changed the title."""
    if close_iterm2_session_by_var("orch_id", str(dispatch_id)):
        return True
    return close_iterm2_tab_by_title(f"orch #{dispatch_id}")


def close_iterm2_tabs(dispatch_ids: list[int]) -> int:
    """Bulk-close tabs for many dispatches in a single AppleScript pass.
    Used by `/tabs/close_completed` for cleaning up accumulated stale tabs.
    Returns the count actually closed."""
    if not dispatch_ids or not iterm2_installed():
        return 0
    # Match the stable `user.orch_id` session variable first (survives claude
    # overwriting the tab title), with the legacy `orch #<id>` title as fallback
    # for tabs spawned before user-var tagging. Two-pass per window (collect then
    # close) so the tab list is never mutated mid-iteration.
    id_items = ", ".join(f'"{int(d)}"' for d in dispatch_ids)
    title_items = ", ".join(f'"orch #{int(d)}"' for d in dispatch_ids)
    script = f'''
tell application "iTerm"
    set idTargets to {{{id_items}}}
    set titleTargets to {{{title_items}}}
    set closedCount to 0
    repeat with w in windows
        set toClose to {{}}
        repeat with t in tabs of w
            set matched to false
            try
                tell (current session of t) to set v to (variable named "user.orch_id")
                repeat with tgt in idTargets
                    if v is (tgt as string) then
                        set matched to true
                        exit repeat
                    end if
                end repeat
            end try
            if not matched then
                try
                    set sName to name of current session of t
                    repeat with tgt in titleTargets
                        if sName is (tgt as string) then
                            set matched to true
                            exit repeat
                        end if
                    end repeat
                end try
            end if
            if matched then set end of toClose to t
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
        f'{_setuservar_printf("orch_id", str(dispatch_id))}'
        f'printf "\\033]0;{safe_title}\\007" && '
        f'exec "$HOME/.orchestrator/bin/run.sh"'
    )
    apple_cmd = cmd.replace("\\", "\\\\").replace('"', '\\"')
    script = _spawn_tab_script(safe_title, apple_cmd)
    try:
        _spawn_osascript(script)
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
        # C6: codex EXECUTOR sidecars (int-keyed by dispatch_id in CODEX_DIR). A
        # codex dispatch shares the PID path above (note 2) but its prompt/stream/
        # done/model/fifo live here. Safe to clear: the completion core copies the
        # .jsonl transcript to TRANSCRIPTS_DIR BEFORE calling cleanup, so the
        # summary/artifact survive this delete (a no-op for a claude dispatch).
        CODEX_DIR / f"{dispatch_id}.prompt",
        CODEX_DIR / f"{dispatch_id}.model",
        CODEX_DIR / f"{dispatch_id}.effort",
        CODEX_DIR / f"{dispatch_id}.jsonl",
        CODEX_DIR / f"{dispatch_id}.done",
        CODEX_DIR / f"{dispatch_id}.fifo",
        # K5: kimi EXECUTOR sidecars (int-keyed by dispatch_id in KIMI_DIR; no .effort —
        # kimi has none). Same PID path above; cleared here after the poller copied the
        # .jsonl transcript to TRANSCRIPTS_DIR (a no-op for a claude/codex dispatch). Also
        # clears the is_kimi_dispatch marker (the .prompt), so a finalized row stops matching.
        KIMI_DIR / f"{dispatch_id}.prompt",
        KIMI_DIR / f"{dispatch_id}.model",
        KIMI_DIR / f"{dispatch_id}.jsonl",
        KIMI_DIR / f"{dispatch_id}.done",
        KIMI_DIR / f"{dispatch_id}.fifo",
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
        f'{_setuservar_printf("orch_brain", brain_id)}'
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

    script = _spawn_tab_script(safe_title, apple_cmd)
    try:
        _spawn_osascript(script)
    except Exception:
        cleanup_brain_files(brain_id)
        raise


def finish_brain_tab(brain_id: str, label: str = "brain", success: bool = False):
    """Post-call teardown for a brain tab. Closes the tab when the call
    succeeded AND auto-close is enabled (failed calls stay open for
    inspection); always removes the sidecar files. Never raises."""
    if success and brain_auto_close_enabled():
        try:
            if not close_iterm2_session_by_var("orch_brain", brain_id):
                close_iterm2_tab_by_title(_brain_tab_title(brain_id, label))
        except Exception:
            pass
    cleanup_brain_files(brain_id)


# ─── fusion panel fan-out in a watchable tab (mirrors the brain-tab block) ────

def _fusion_tab_title(fusion_id: str) -> str:
    """Unique, readable iTerm2 tab title for a fusion panel; the random suffix
    lets us close exactly this tab later."""
    suffix = fusion_id.rsplit("-", 1)[-1]
    return f"orch fusion: panel {suffix}"


def _fusion_tab_cmd(fusion_id: str, cwd: str, title: str) -> str:
    """Shell command the fusion tab runs. Sets ORCHESTRATOR_FUSION_ID (NOT
    ORCHESTRATOR_RUN_ID — so the Stop hook stays a no-op), titles the tab, then
    execs fusion_run.sh. Pure/string-only so it's unit-testable."""
    safe_proj = cwd.replace('"', '\\"')
    safe_title = title.replace('"', '\\"')
    return (
        f'cd "{safe_proj}" && '
        f'export ORCHESTRATOR_FUSION_ID={fusion_id} && '
        f'{_setuservar_printf("orch_fusion", fusion_id)}'
        f'printf "\\033]0;{safe_title}\\007" && '
        f'exec "$HOME/.orchestrator/bin/fusion_run.sh"'
    )


def cleanup_fusion_files(fusion_id: str):
    """Remove all sidecar files for a fusion panel. The tab (and its on-screen
    output) is unaffected."""
    for suf in ("request.json", "json", "done", "pid"):
        try:
            (FUSION_DIR / f"{fusion_id}.{suf}").unlink()
        except FileNotFoundError:
            pass


def spawn_fusion_tab(fusion_id: str, body: dict, cwd: str) -> None:
    """Open a new iTerm2 tab and run the fusion panel in it via fusion_run.sh.

    Writes the request body to <fusion_id>.request.json (avoids shell-quoting the
    prompt into AppleScript), then tells iTerm2 to open a tab and exec the runner.
    The caller polls <fusion_id>.done for completion and reads <fusion_id>.json
    for the collected answers. Raises on spawn failure (caller falls back to the
    in-process panel)."""
    if not iterm2_installed():
        raise RuntimeError("iTerm2 not installed")
    ensure_fusion_runner()
    (FUSION_DIR / f"{fusion_id}.request.json").write_text(
        json.dumps(body), encoding="utf-8")

    title = _fusion_tab_title(fusion_id)
    safe_title = title.replace('"', '\\"')
    cmd = _fusion_tab_cmd(fusion_id, cwd, title)
    apple_cmd = cmd.replace("\\", "\\\\").replace('"', '\\"')

    script = _spawn_tab_script(safe_title, apple_cmd)
    try:
        _spawn_osascript(script)
    except Exception:
        cleanup_fusion_files(fusion_id)
        raise


def finish_fusion_tab(fusion_id: str, success: bool = False):
    """Post-panel teardown for a fusion tab. Closes the tab when the panel
    SUCCEEDED and auto-close is enabled (failed panels stay open for
    inspection); always removes the sidecar files. Never raises."""
    if success and brain_auto_close_enabled():
        try:
            if not close_iterm2_session_by_var("orch_fusion", fusion_id):
                close_iterm2_tab_by_title(_fusion_tab_title(fusion_id))
        except Exception:
            pass
    cleanup_fusion_files(fusion_id)


# ─── codex calls (seat / judge) in watchable tabs (mirrors the brain-tab block) ─
# The codex twin of the brain-tab plumbing: a $0 subscription `codex exec` call
# streamed to a sidecar JSONL the orchestrator parses back into a structured
# result (claude_runner._build_codex_run). Engine-neutral machinery
# (_spawn_tab_script, _spawn_osascript, _setuservar_printf,
# close_iterm2_session_by_var, brain_auto_close_enabled) is REUSED as-is — only a
# new sidecar dir + codex_run.sh + the `user.orch_codex` tab tag differ. Sets
# ORCHESTRATOR_CODEX_ID, NEVER ORCHESTRATOR_RUN_ID, so the env-gated Stop hook
# stays a no-op for codex tabs. Flags + event schema are version-pinned to
# codex-cli 0.144.4 (originally 0.141.0, CODEX_PLAN.md §0; re-verified live
# 2026-07-14); codex churns them, so re-verify on upgrade.

CODEX_DIR = DATA_DIR / "codex"
CODEX_RUN_SH = BIN_DIR / "codex_run.sh"

_CODEX_RUN_SH_TEMPLATE = """#!/bin/bash
# Orchestrator codex-call runner — execed inside an iTerm2 tab so a codex seat /
# judge call is WATCHABLE live, the codex twin of brain_run.sh. codex runs with
# `exec --json` so its events stream as JSONL; `tee` mirrors the raw stream to a
# sidecar JSONL the orchestrator parses (claude_runner._build_codex_run) to
# recover the structured result. A python3 pretty-printer AFTER tee renders
# readable lines in the tab keyed off CODEX event types (not claude's
# assistant/result) — it formats only the terminal copy; tee already wrote raw
# JSONL to the sidecar, which the orchestrator parses unchanged. PIPESTATUS[0]
# keeps codex's exit code (codex is first in the pipe; the formatter is last).
#
# codex specifics vs claude (codex-cli 0.144.4 — re-verify on upgrade):
#   - subcommand `exec` (the `claude -p` analogue), `--json` (NOT stream-json)
#   - `-m MODEL` passed EXPLICITLY: codex's JSON omits the model, so the parser
#     falls back to it (dispatch #3 lesson)
#   - `-s read-only`: a seat/judge only READS to answer — this also stops a
#     mid-run write-approval from HANGING the non-TTY tab. The write-capable
#     `--dangerously-bypass-approvals-and-sandbox` belongs to the C6 executor,
#     NOT here.
#   - reasoning effort via `-c model_reasoning_effort=<e>`, applied ONLY when an
#     effort is given (else codex uses the model default — what C0 verified)
#   - MUST run `< /dev/null`: codex exec otherwise blocks "Reading additional
#     input from stdin…" on a non-TTY (exactly like claude -p). The redirect
#     attaches to codex (first pipe stage), so PIPESTATUS[0] stays codex's exit.
#
# ⚠ The MODEL fallback below is INTERPOLATED from config.CODEX_ENGINE_SEED at
#   import (@@MODEL_DEFAULT@@ — the C4-deferred seed→bash interp, done 2026-07-14
#   exactly like the executor runner), so a seed bump moves it by construction.
#   The FLAG set (exec/-s/--json/-c) still DUPLICATES the seed in bash and stays
#   PINNED by tests/test_codex_config.py (TestSpawnCodexRunShPinnedToSeed), so a
#   seed flag change that forgets this runner fails LOUDLY. Edit the SEED first.
#
# Completion signalling mirrors brain_run.sh:
#   <id>.done — codex's exit code, written AFTER tee flushes (so .jsonl is whole)
#   <id>.pid  — this shell's PID; lets the poller detect a closed/killed tab.
#
# ORCHESTRATOR_CODEX_ID (never ORCHESTRATOR_RUN_ID) so the env-gated Stop hook
# stays a no-op for codex tabs. OPENAI_API_KEY is scrubbed before the call so the
# $0 subscription path is used, NEVER the billed OpenAI API (CLAUDE.md hard rule).
if [ -z "${ORCHESTRATOR_CODEX_ID:-}" ]; then
    echo "Orchestrator codex: ORCHESTRATOR_CODEX_ID not set" >&2
    exit 2
fi
ID="$ORCHESTRATOR_CODEX_ID"
CODEX_DIR="$HOME/.orchestrator/codex"
PROMPT_FILE="$CODEX_DIR/${ID}.prompt"
OUT_FILE="$CODEX_DIR/${ID}.jsonl"
DONE_FILE="$CODEX_DIR/${ID}.done"
PID_FILE="$CODEX_DIR/${ID}.pid"
MODEL=$(cat "$CODEX_DIR/${ID}.model" 2>/dev/null || echo @@MODEL_DEFAULT@@)
EFFORT=$(cat "$CODEX_DIR/${ID}.effort" 2>/dev/null || echo "")
echo $$ > "$PID_FILE"
if [ ! -f "$PROMPT_FILE" ]; then
    echo "Orchestrator codex: missing prompt file $PROMPT_FILE" >&2
    echo 2 > "$DONE_FILE"
    exit 2
fi
PROMPT=$(cat "$PROMPT_FILE")
# $0 subscription path only — never route codex through the billed OpenAI API.
unset OPENAI_API_KEY
EFFORT_ARG=()
if [ -n "$EFFORT" ]; then
    EFFORT_ARG=(-c "model_reasoning_effort=$EFFORT")
fi
echo "---- orchestrator codex call: $ID ($MODEL${EFFORT:+ / $EFFORT}) ----"
echo "(watching live; the structured result is captured for the orchestrator)"
echo
codex exec "$PROMPT" \
    -m "$MODEL" \
    -s read-only \
    --json \
    "${EFFORT_ARG[@]}" < /dev/null | tee "$OUT_FILE" | python3 -u -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except Exception:
        print(line)
        continue
    t = obj.get('type', '')
    if t == 'thread.started':
        print('[codex]', (obj.get('thread_id') or '')[:8])
    elif t == 'item.completed':
        item = obj.get('item') or {}
        it = item.get('type')
        if it == 'agent_message':
            txt = (item.get('text') or '').strip()
            if txt:
                print('[assistant]', txt)
        else:
            print('[item]', it or '?')
    elif t == 'turn.completed':
        u = obj.get('usage') or {}
        print('[done] in=%s cached=%s out=%s reasoning=%s' % (
            u.get('input_tokens', 0), u.get('cached_input_tokens', 0),
            u.get('output_tokens', 0), u.get('reasoning_output_tokens', 0)))
    elif t and t != 'turn.started':
        print('[codex:%s]' % t)
"
code=${PIPESTATUS[0]}
echo "$code" > "$DONE_FILE"
echo
echo "---- codex call finished (exit $code) ----"
"""

# The SEAT runner with the seed's model interpolated at import — the same
# seed→bash pattern as CODEX_DISPATCH_RUN_SH_CONTENT below, so the bash model
# fallback can never drift from config.CODEX_ENGINE_SEED["model"] (pinned by
# tests/test_codex_config.py all the same).
CODEX_RUN_SH_CONTENT = _CODEX_RUN_SH_TEMPLATE.replace(
    "@@MODEL_DEFAULT@@", config.CODEX_ENGINE_SEED["model"])


def ensure_codex_runner():
    """One-time (lazy): create the codex sidecar dir and write codex_run.sh.
    Mirrors ensure_brain_runner; called on the first codex call, so install.sh
    needs no change."""
    CODEX_DIR.mkdir(parents=True, exist_ok=True)
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    CODEX_RUN_SH.write_text(CODEX_RUN_SH_CONTENT)
    CODEX_RUN_SH.chmod(0o755)


def _codex_tab_title(codex_id: str, label: str) -> str:
    """Unique, readable iTerm2 tab title for a codex call; the random suffix lets
    us close exactly this tab later."""
    suffix = codex_id.rsplit("-", 1)[-1]
    return f"orch codex: {label} {suffix}"


def _codex_tab_cmd(codex_id: str, cwd: str, title: str) -> str:
    """Shell command the codex tab runs. Sets ORCHESTRATOR_CODEX_ID (NOT
    ORCHESTRATOR_RUN_ID — so the Stop hook stays a no-op), titles the tab, then
    execs codex_run.sh. Pure/string-only so it's unit-testable."""
    safe_proj = cwd.replace('"', '\\"')
    safe_title = title.replace('"', '\\"')
    return (
        f'cd "{safe_proj}" && '
        f'export ORCHESTRATOR_CODEX_ID={codex_id} && '
        f'{_setuservar_printf("orch_codex", codex_id)}'
        f'printf "\\033]0;{safe_title}\\007" && '
        f'exec "$HOME/.orchestrator/bin/codex_run.sh"'
    )


def cleanup_codex_files(codex_id: str):
    """Remove all sidecar files for a codex call. The tab (and its on-screen
    output) is unaffected — tee already wrote to the terminal."""
    for suf in ("prompt", "jsonl", "done", "pid", "model", "effort"):
        try:
            (CODEX_DIR / f"{codex_id}.{suf}").unlink()
        except FileNotFoundError:
            pass


def spawn_codex_tab(codex_id: str, prompt: str, cwd: str,
                    model: str = config.CODEX_ENGINE_SEED["model"], effort: str = "",
                    label: str = "codex") -> None:
    """Open a new iTerm2 tab and run a codex call in it via codex_run.sh.

    Writes the prompt + config to CODEX_DIR sidecars (avoids shell-quoting the
    prompt into AppleScript), then tells iTerm2 to open a tab and exec the
    runner. The caller polls <codex_id>.done for completion. Raises on spawn
    failure (so the caller can fall back to headless). An empty `effort` ⇒ no
    reasoning-effort override (codex uses the model default — what C0 verified);
    a value is applied as `-c model_reasoning_effort=<effort>` by codex_run.sh.
    `model` is the codex model id, defaulting to config.CODEX_ENGINE_SEED["model"]
    (C4 — single source of truth); callers pass it EXPLICITLY (dispatch #3)."""
    if not iterm2_installed():
        raise RuntimeError("iTerm2 not installed")
    ensure_codex_runner()
    (CODEX_DIR / f"{codex_id}.prompt").write_text(prompt, encoding="utf-8")
    (CODEX_DIR / f"{codex_id}.model").write_text(
        (model or config.CODEX_ENGINE_SEED["model"]).strip())
    (CODEX_DIR / f"{codex_id}.effort").write_text((effort or "").strip())

    title = _codex_tab_title(codex_id, label)
    safe_title = title.replace('"', '\\"')
    cmd = _codex_tab_cmd(codex_id, cwd, title)
    apple_cmd = cmd.replace("\\", "\\\\").replace('"', '\\"')

    script = _spawn_tab_script(safe_title, apple_cmd)
    try:
        _spawn_osascript(script)
    except Exception:
        cleanup_codex_files(codex_id)
        raise


def finish_codex_tab(codex_id: str, label: str = "codex", success: bool = False):
    """Post-call teardown for a codex tab. Closes the tab when the call
    succeeded AND auto-close is enabled (failed calls stay open for inspection);
    always removes the sidecar files. Never raises."""
    if success and brain_auto_close_enabled():
        try:
            if not close_iterm2_session_by_var("orch_codex", codex_id):
                close_iterm2_tab_by_title(_codex_tab_title(codex_id, label))
        except Exception:
            pass
    cleanup_codex_files(codex_id)


# ─── kimi SEAT/judge tab (K5) — the $0 kimi-code twin of the codex seat runner ──
# A WATCHABLE kimi call in an iTerm2 tab. Simpler than the codex seat runner: kimi-code
# has NO -s sandbox modes and NO reasoning effort, and its stream-json is ROLE-based
# (assistant/tool/meta) not codex's type events. Sets ORCHESTRATOR_KIMI_ID (never
# ORCHESTRATOR_RUN_ID → Stop hook stays a no-op) + the `user.orch_kimi` tab tag. Flags +
# schema version-pinned to kimi-code 0.27.0 (KIMI_PLAN.md §4); re-verify on `kimi upgrade`.

KIMI_DIR = DATA_DIR / "kimi"
KIMI_RUN_SH = BIN_DIR / "kimi_run.sh"


def _kimi_runner_bin() -> str:
    """The kimi binary the tab runner calls — the resolved abs path
    (config._resolve_kimi_bin, so the runner works even if ~/.kimi-code/bin isn't on the
    tab's PATH), else 'kimi'. Read at import to interpolate @@KIMI_BIN@@ (like the seed
    model), so no PATH assumption is baked into the runner."""
    try:
        return config._resolve_kimi_bin() or "kimi"
    except Exception:
        return "kimi"


_KIMI_RUN_SH_TEMPLATE = """#!/bin/bash
# Orchestrator kimi-call runner — execed inside an iTerm2 tab so a kimi seat/judge call
# is WATCHABLE live, the kimi twin of codex_run.sh. kimi runs with `-p --output-format
# stream-json` so its events stream as JSONL; `tee` mirrors the raw stream to a sidecar
# JSONL the orchestrator parses (claude_runner._build_kimi_run). A python3 pretty-printer
# AFTER tee renders readable lines keyed off kimi's ROLE schema (assistant/tool/meta — NOT
# codex's type events). PIPESTATUS[0] keeps kimi's exit code (kimi is first in the pipe).
#
# kimi-code specifics (v0.27.0 — re-verify on `kimi upgrade`):
#   - `-p PROMPT` IS the non-interactive one-shot flag (NOT --print); it auto-approves
#     tools and CANNOT combine with -y/--auto.
#   - `--output-format stream-json` (JSONL, role-based).
#   - `-m MODEL` passed EXPLICITLY: kimi's JSON omits the model, so the parser falls back
#     to it (dispatch #3 lesson).
#   - NO -s sandbox modes, NO effort flag (kimi-code has neither — simpler than codex).
#   - MUST run `< /dev/null`: non-TTY hygiene (like codex exec / claude -p).
#
# ⚠ @@MODEL_DEFAULT@@ + @@KIMI_BIN@@ are INTERPOLATED from config at import; the FLAG set
#   duplicates the seed in bash and stays PINNED by tests (TestSpawnKimiRunShPinnedToSeed),
#   so a seed flag change that forgets this runner fails LOUDLY. Edit the SEED first.
#
# Completion signalling mirrors codex_run.sh: <id>.done = kimi's exit code (after tee
# flushes); <id>.pid = this shell's PID (lets the poller detect a closed/killed tab).
# MOONSHOT_API_KEY/OPENAI_API_KEY scrubbed → $0 SUBSCRIPTION, never a billed API.
if [ -z "${ORCHESTRATOR_KIMI_ID:-}" ]; then
    echo "Orchestrator kimi: ORCHESTRATOR_KIMI_ID not set" >&2
    exit 2
fi
ID="$ORCHESTRATOR_KIMI_ID"
KIMI_DIR="$HOME/.orchestrator/kimi"
PROMPT_FILE="$KIMI_DIR/${ID}.prompt"
OUT_FILE="$KIMI_DIR/${ID}.jsonl"
DONE_FILE="$KIMI_DIR/${ID}.done"
PID_FILE="$KIMI_DIR/${ID}.pid"
MODEL=$(cat "$KIMI_DIR/${ID}.model" 2>/dev/null || echo @@MODEL_DEFAULT@@)
echo $$ > "$PID_FILE"
if [ ! -f "$PROMPT_FILE" ]; then
    echo "Orchestrator kimi: missing prompt file $PROMPT_FILE" >&2
    echo 2 > "$DONE_FILE"
    exit 2
fi
PROMPT=$(cat "$PROMPT_FILE")
# $0 subscription path only — never a billed API key.
unset MOONSHOT_API_KEY
unset OPENAI_API_KEY
echo "---- orchestrator kimi call: $ID ($MODEL) ----"
echo "(watching live; the structured result is captured for the orchestrator)"
echo
"@@KIMI_BIN@@" -p "$PROMPT" \
    --output-format stream-json \
    -m "$MODEL" < /dev/null | tee "$OUT_FILE" | python3 -u -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except Exception:
        print(line)
        continue
    role = obj.get('role', '')
    if role == 'assistant':
        txt = (obj.get('content') or '').strip()
        if txt:
            print('[assistant]', txt)
    elif role == 'tool':
        print('[tool]', (obj.get('content') or '')[:200])
    elif role == 'meta':
        if obj.get('type') == 'session.resume_hint':
            print('[session]', (obj.get('session_id') or '')[:16])
    elif role:
        print('[kimi:%s]' % role)
"
code=${PIPESTATUS[0]}
echo "$code" > "$DONE_FILE"
echo
echo "---- kimi call finished (exit $code) ----"
"""

# The SEAT runner with the seed's model + resolved binary interpolated at import (the
# same pattern as CODEX_RUN_SH_CONTENT), so the bash fallbacks can never drift from the
# config SEED (pinned by tests all the same).
KIMI_RUN_SH_CONTENT = (_KIMI_RUN_SH_TEMPLATE
                       .replace("@@MODEL_DEFAULT@@", config.KIMI_ENGINE_SEED["model"])
                       .replace("@@KIMI_BIN@@", _kimi_runner_bin()))


def ensure_kimi_runner():
    """One-time (lazy): create the kimi sidecar dir and write kimi_run.sh. Mirrors
    ensure_codex_runner; called on the first kimi call, so install.sh needs no change."""
    KIMI_DIR.mkdir(parents=True, exist_ok=True)
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    KIMI_RUN_SH.write_text(KIMI_RUN_SH_CONTENT)
    KIMI_RUN_SH.chmod(0o755)


def _kimi_tab_title(kimi_id: str, label: str) -> str:
    """Unique, readable iTerm2 tab title for a kimi call; the random suffix lets us
    close exactly this tab later."""
    suffix = kimi_id.rsplit("-", 1)[-1]
    return f"orch kimi: {label} {suffix}"


def _kimi_tab_cmd(kimi_id: str, cwd: str, title: str) -> str:
    """Shell command the kimi tab runs. Sets ORCHESTRATOR_KIMI_ID (NOT ORCHESTRATOR_RUN_ID
    — so the Stop hook stays a no-op), titles the tab, then execs kimi_run.sh. Pure /
    string-only so it's unit-testable."""
    safe_proj = cwd.replace('"', '\\"')
    safe_title = title.replace('"', '\\"')
    return (
        f'cd "{safe_proj}" && '
        f'export ORCHESTRATOR_KIMI_ID={kimi_id} && '
        f'{_setuservar_printf("orch_kimi", kimi_id)}'
        f'printf "\\033]0;{safe_title}\\007" && '
        f'exec "$HOME/.orchestrator/bin/kimi_run.sh"'
    )


def cleanup_kimi_files(kimi_id: str):
    """Remove all sidecar files for a kimi call. The tab (and its on-screen output) is
    unaffected — tee already wrote to the terminal. (No .effort file — kimi has none.)"""
    for suf in ("prompt", "jsonl", "done", "pid", "model"):
        try:
            (KIMI_DIR / f"{kimi_id}.{suf}").unlink()
        except FileNotFoundError:
            pass


def spawn_kimi_tab(kimi_id: str, prompt: str, cwd: str,
                   model: str = config.KIMI_ENGINE_SEED["model"],
                   label: str = "kimi") -> None:
    """Open a new iTerm2 tab and run a kimi call in it via kimi_run.sh. Writes the prompt
    + model to KIMI_DIR sidecars (avoids shell-quoting the prompt into AppleScript), then
    tells iTerm2 to open a tab and exec the runner. The caller polls <kimi_id>.done. Raises
    on spawn failure (so the caller can fall back to headless). `model` is the kimi alias,
    defaulting to the seed (K3 single source of truth); callers pass it EXPLICITLY. kimi
    has no effort, so — unlike spawn_codex_tab — there is no effort param/sidecar."""
    if not iterm2_installed():
        raise RuntimeError("iTerm2 not installed")
    ensure_kimi_runner()
    (KIMI_DIR / f"{kimi_id}.prompt").write_text(prompt, encoding="utf-8")
    (KIMI_DIR / f"{kimi_id}.model").write_text(
        (model or config.KIMI_ENGINE_SEED["model"]).strip())

    title = _kimi_tab_title(kimi_id, label)
    safe_title = title.replace('"', '\\"')
    cmd = _kimi_tab_cmd(kimi_id, cwd, title)
    apple_cmd = cmd.replace("\\", "\\\\").replace('"', '\\"')

    script = _spawn_tab_script(safe_title, apple_cmd)
    try:
        _spawn_osascript(script)
    except Exception:
        cleanup_kimi_files(kimi_id)
        raise


def finish_kimi_tab(kimi_id: str, label: str = "kimi", success: bool = False):
    """Post-call teardown for a kimi tab. Closes the tab when the call succeeded AND
    auto-close is enabled (failed calls stay open for inspection); always removes the
    sidecar files. Never raises. Mirror of finish_codex_tab."""
    if success and brain_auto_close_enabled():
        try:
            if not close_iterm2_session_by_var("orch_kimi", kimi_id):
                close_iterm2_tab_by_title(_kimi_tab_title(kimi_id, label))
        except Exception:
            pass
    cleanup_kimi_files(kimi_id)


# ─── codex EXECUTOR dispatch (C6) — the $0 codex twin of spawn_iterm2/run.sh ──
# A DISPATCHED codex task in a watchable iTerm2 tab — the codex analogue of the
# `claude` executor (spawn_iterm2 → run.sh). It is NOT the SEAT runner above:
#   - FULL ACCESS: `-s <executor_sandbox>` (danger-full-access), the codex twin of
#     `claude --dangerously-skip-permissions`, so codex acts on the project (and beyond)
#     exactly like a claude dispatch — operator-chosen 2026-06-25 ("no noticeable
#     difference between picking codex vs claude"). Full access via the `-s` sandbox MODE,
#     NOT the auto_bypass_flag (it OVERRIDES -s + is danger-flagged). The SAME value drives
#     the resume hand-off; re-confine via a config.json fusion.codex.executor_sandbox.
#   - PID at the CLAUDE path (PIDS_DIR/<id>.pid), so manual-kill / kill-all / the
#     wall-clock cap / the orphan reaper / boot re-attach all locate it with ZERO
#     watchdog changes (CODEX_PLAN note 2). The seat writes CODEX_DIR/<id>.pid; this
#     does NOT — termination must reach a real dispatch.
#   - FIFO + backgrounded codex: the recorded PID is codex's REAL pid (the kill
#     target), not the shell (a pipeline `$$` would orphan codex on SIGTERM). The
#     consumer (tee → sidecar + pretty-printer) is wait'd so the sidecar is fully
#     flushed before .done (the poller reads the envelope on .done).
#   - ORCHESTRATOR_CODEX_RUN_ID (NOT ORCHESTRATOR_RUN_ID) + the `user.orch_id` tab
#     tag (the DISPATCH tag, so select/close/auto-close work) — Stop hook stays a
#     no-op; the orchestrator's in-process poller is the SOLE finalizer (§5 fix iii).
# Sidecars are int-keyed by dispatch_id in CODEX_DIR (str(dispatch_id) — the codex
# plumbing is string-keyed; a bare-int key never collides with a seat's slug-uuid id).

CODEX_DISPATCH_RUN_SH = BIN_DIR / "codex_dispatch_run.sh"


def _build_codex_dispatch_run_sh(eng: dict) -> str:
    """Render the codex EXECUTOR run.sh, INTERPOLATING the flag set + model fallback
    from config.CODEX_ENGINE_SEED (the C4-deferred seed→bash interpolation, finished
    in C6 — bash can't import the Python seed, so we substitute at runner-write time).
    Single source of truth; pinned by tests/test_codex_config.py. Uses the EXECUTOR
    sandbox (write-capable) — NEVER the seat's read-only `sandbox` — and deliberately
    does NOT emit the `auto_bypass_flag` (that would override -s to full-access).

    After the captured one-shot turn (which the orchestrator finalizes from the sidecar),
    the runner HANDS THE TAB OFF to an interactive `codex resume <thread_id>` (the #246
    hybrid fix) so the tab stays open + continuable like the claude executor's REPL; the
    resume subcommand + flags are interpolated from the seed too (`resume_subcmd`/
    `resume_flags`)."""
    template = r'''#!/bin/bash
# Orchestrator codex EXECUTOR runner — the $0 ChatGPT-subscription codex analogue of
# run.sh (the dispatched `claude` session), execed inside a WATCHABLE iTerm2 tab.
#
# NOT the codex SEAT runner (codex_run.sh, -s read-only): a dispatched executor runs
# `-s @@EXECUTOR_SANDBOX@@` — FULL machine access, the codex twin
# of `claude --dangerously-skip-permissions`, so a codex dispatch is indistinguishable
# from a claude one (operator-chosen 2026-06-25). Full access via the `-s` sandbox MODE,
# NOT a bypass flag. The SAME sandbox value drives the interactive resume hand-off below,
# which also adds `-a never` so its follow-up turns act without approval prompts too.
#
# §5 hook-gap convergence (fix iii): codex has NO Stop/PreToolUse/PostToolUse hooks,
# so completion / loop-watchdog / timeline are NOT signalled via ~/.claude/settings.json.
# This runner streams codex's events to a sidecar JSONL the orchestrator's in-process
# poller (app._codex_dispatch_poller) tails — the SAME sidecar+PID-poll mechanism the
# seat uses. Hence ORCHESTRATOR_CODEX_RUN_ID (NOT ORCHESTRATOR_RUN_ID — the env-gated
# Stop hook stays a no-op), AND the PID below is written to the CLAUDE pid path
# ($HOME/.orchestrator/pids/<id>.pid) so kill / kill-all / cap / reaper / boot re-attach
# all locate it unchanged (CODEX_PLAN note 2).
#
# WHY a FIFO + backgrounded codex (not the seat's `codex | tee | python` pipeline):
# the orchestrator must TERMINATE codex by the recorded PID. In a pipeline `$$` is the
# SHELL and codex is a pipeline child — SIGTERM to the shell would orphan codex, not
# kill it. So codex is backgrounded to capture its REAL pid (the kill target); its
# output flows through a FIFO to tee (raw JSONL → sidecar, parsed by
# claude_runner._build_codex_run) + a python3 pretty-printer (readable lines in the
# tab, cosmetic). `wait`ing the consumer guarantees the sidecar is flushed BEFORE
# .done. OPENAI_API_KEY is scrubbed so the $0 SUBSCRIPTION path is used, never the
# billed API (CLAUDE.md hard rule, extended to codex).
#
# The codex subcommand / sandbox / json flag / model fallback are INTERPOLATED from
# config.CODEX_ENGINE_SEED at write time (single source of truth, pinned by tests).
if [ -z "${ORCHESTRATOR_CODEX_RUN_ID:-}" ]; then
    echo "Orchestrator codex executor: ORCHESTRATOR_CODEX_RUN_ID not set" >&2
    exit 2
fi
ID="$ORCHESTRATOR_CODEX_RUN_ID"
CODEX_DIR="$HOME/.orchestrator/codex"
PID_FILE="$HOME/.orchestrator/pids/${ID}.pid"
PROMPT_FILE="$CODEX_DIR/${ID}.prompt"
OUT_FILE="$CODEX_DIR/${ID}.jsonl"
DONE_FILE="$CODEX_DIR/${ID}.done"
FIFO="$CODEX_DIR/${ID}.fifo"
MODEL=$(cat "$CODEX_DIR/${ID}.model" 2>/dev/null || echo @@MODEL_DEFAULT@@)
EFFORT=$(cat "$CODEX_DIR/${ID}.effort" 2>/dev/null || echo "")
if [ ! -f "$PROMPT_FILE" ]; then
    echo "Orchestrator codex executor: missing prompt file $PROMPT_FILE" >&2
    echo 2 > "$DONE_FILE"
    exit 2
fi
PROMPT=$(cat "$PROMPT_FILE")
# $0 subscription path only — never route the executor through the billed OpenAI API.
unset OPENAI_API_KEY
# Reasoning effort (optional): empty ⇒ no -c override ⇒ the model's own default. Mirrors
# the codex SEAT runner's EFFORT_ARG; the value was validated against codex's ladder by
# app._run_dispatch (an unknown / claude-only value already fell back to "" upstream).
EFFORT_FLAG=()
if [ -n "$EFFORT" ]; then
    EFFORT_FLAG=(-c "model_reasoning_effort=$EFFORT")
fi
echo "---- orchestrator codex EXECUTOR: dispatch $ID ($MODEL${EFFORT:+ / $EFFORT}, -s @@EXECUTOR_SANDBOX@@) ----"
echo "(watchable; full access like a claude dispatch; no Stop hook — orchestrator finalizes from the sidecar)"
echo
rm -f "$FIFO"
mkfifo "$FIFO" || { echo "Orchestrator codex executor: mkfifo failed" >&2; echo 2 > "$DONE_FILE"; exit 2; }
# Consumer: raw JSONL -> sidecar (tee) + readable lines -> tab (python). Reads the
# FIFO and blocks until codex (the writer) opens it; backgrounded so we launch codex
# next. Keyed off codex event types (item.started/completed: command_execution /
# file_change / agent_message), NOT claude's assistant/result. Cosmetic only — tee has
# already written raw JSONL to the sidecar, which the orchestrator parses unchanged.
tee "$OUT_FILE" < "$FIFO" | python3 -u -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except Exception:
        print(line)
        continue
    t = obj.get('type', '')
    if t == 'thread.started':
        print('[codex]', (obj.get('thread_id') or '')[:8])
    elif t in ('item.started', 'item.completed'):
        item = obj.get('item') or {}
        it = item.get('type')
        if it == 'agent_message':
            if t == 'item.completed':
                txt = (item.get('text') or '').strip()
                if txt:
                    print('[assistant]', txt)
        elif it == 'command_execution':
            if t == 'item.started':
                print('[run]', (item.get('command') or '')[:200])
            else:
                print('[run-done exit=%s]' % item.get('exit_code'),
                      (item.get('aggregated_output') or '')[:200].replace(chr(10), ' '))
        elif it == 'file_change':
            if t == 'item.started':
                paths = ', '.join('%s %s' % (c.get('kind', ''), c.get('path', ''))
                                  for c in (item.get('changes') or []) if isinstance(c, dict))
                print('[edit]', paths[:200])
        elif t == 'item.completed':
            print('[item]', it or '?')
    elif t == 'turn.completed':
        u = obj.get('usage') or {}
        print('[done] in=%s cached=%s out=%s reasoning=%s' % (
            u.get('input_tokens', 0), u.get('cached_input_tokens', 0),
            u.get('output_tokens', 0), u.get('reasoning_output_tokens', 0)))
    elif t and t != 'turn.started':
        print('[codex:%s]' % t)
" &
CONSUMER_PID=$!
# codex -> FIFO, backgrounded so $! is codex's REAL pid (the kill target). < /dev/null
# keeps codex from blocking 'Reading additional input from stdin...' on a non-TTY.
codex @@EXEC_SUBCMD@@ "$PROMPT" -m "$MODEL" -s @@EXECUTOR_SANDBOX@@ "${EFFORT_FLAG[@]}" @@JSON_FLAG@@ < /dev/null > "$FIFO" &
CODEX_PID=$!
echo "$CODEX_PID" > "$PID_FILE"
wait "$CODEX_PID"
code=$?
wait "$CONSUMER_PID" 2>/dev/null
rm -f "$FIFO"
# Capture turn-1's thread_id from the sidecar BEFORE writing .done. The orchestrator's
# in-band poller finalizes on .done and then DELETES this sidecar (cleanup_dispatch_files),
# so read the id NOW into a shell var that survives the delete. thread.started is codex's
# FIRST event; we take its thread_id (the resume handle). The Python is all-double-quoted
# so the single-quote wrapper needs no escaping and bash does no expansion inside it.
THREAD_ID="$(python3 -c 'import json, sys
tid = ""
try:
    for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("type") == "thread.started" and o.get("thread_id"):
            tid = o["thread_id"]
            break
except Exception:
    pass
sys.stdout.write(tid)
' "$OUT_FILE" 2>/dev/null)"
echo "$code" > "$DONE_FILE"
echo
echo "---- codex executor turn 1 finished (exit $code) — orchestrator finalized from the sidecar ----"
# HYBRID auto-resume — mirror the claude executor's stay-open, continuable tab (the #246
# fix). Turn 1 above was the CAPTURED one-shot the orchestrator finalizes from the sidecar
# (completion row + timeline + loop-watchdog + summary). Now hand THIS SAME tab to an
# INTERACTIVE codex on the SAME thread so the user can read the answer, keep the
# conversation going, and close the tab manually. The orchestrator no longer tracks this
# phase: the dispatch is already 'completed' and its PID file / wall-clock cap / poller
# were all cleared at finalize — exactly a claude dispatch's post-Stop-hook state. So a long
# follow-up holds NO concurrency slot and CANNOT be hard-killed by the cap; the trade-off is
# that these follow-up turns are NOT recorded by the orchestrator.
#
# NB: NO `< /dev/null` here — interactive codex needs the tab's REAL TTY (the one-shot above
# used /dev/null only because a non-interactive `exec` blocks on a non-TTY stdin). `exec`
# replaces this shell so the tab IS the codex session (closing codex closes the tab, like
# `exec claude`). Model/sandbox are inherited from the resumed session; OPENAI_API_KEY stays
# unset (scrubbed above) so this interactive phase is the $0 subscription path too.
if [ -n "$THREAD_ID" ]; then
    echo
    echo "---- resuming interactively on thread $THREAD_ID — continue below; close the tab when done ----"
    echo "(follow-up turns here are NOT recorded by the orchestrator)"
    echo
    exec codex @@RESUME_SUBCMD@@ "$THREAD_ID" @@RESUME_FLAGS@@ -s @@EXECUTOR_SANDBOX@@
fi
# Fallback: no thread id (codex errored before thread.started, or a parse miss) — keep the
# tab OPEN at an interactive shell so the user can read the output above + resume manually,
# instead of the tab vanishing (the #246 regression we are fixing).
echo
echo "---- no thread id captured; leaving this tab open (resume manually with: codex @@RESUME_SUBCMD@@ --last) ----"
exec "${SHELL:-/bin/zsh}" -i
'''
    return (template
            .replace("@@EXEC_SUBCMD@@", eng["exec_subcmd"])
            .replace("@@EXECUTOR_SANDBOX@@", eng["executor_sandbox"])
            .replace("@@JSON_FLAG@@", eng["json_flag"])
            .replace("@@MODEL_DEFAULT@@", eng["model"])
            .replace("@@RESUME_SUBCMD@@", eng["resume_subcmd"])
            .replace("@@RESUME_FLAGS@@", eng["resume_flags"]))


# Built at import from the SEED (genuine seed→bash interpolation); a module constant
# like the seat runner so the drift test can pin it. Re-import regenerates it after a
# seed edit. (NB: only re-read if the seed is patched + the module re-imported.)
CODEX_DISPATCH_RUN_SH_CONTENT = _build_codex_dispatch_run_sh(config.CODEX_ENGINE_SEED)


def ensure_codex_dispatch_runner():
    """One-time (lazy): create the codex sidecar dir + write codex_dispatch_run.sh.
    Mirrors ensure_runner/ensure_codex_runner; called before spawning a codex
    dispatch tab, so install.sh needs no change."""
    CODEX_DIR.mkdir(parents=True, exist_ok=True)
    PIDS_DIR.mkdir(parents=True, exist_ok=True)
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    CODEX_DISPATCH_RUN_SH.write_text(CODEX_DISPATCH_RUN_SH_CONTENT)
    CODEX_DISPATCH_RUN_SH.chmod(0o755)


def is_codex_dispatch(dispatch_id: int) -> bool:
    """True if `dispatch_id` is a codex EXECUTOR (C6) — detected by its int-keyed
    codex prompt sidecar, which exists from spawn until cleanup. Lets the watchdog
    (resume_watchers_on_boot + the cap watcher) pick the codex branch (a distinct
    hard-kill reason, no claude pause-resume) and re-attach the poller, with NO
    dispatches-table schema change. Only the executor writes a bare-int CODEX_DIR
    key; a seat's id is always a slug-uuid, so there is no collision."""
    return (CODEX_DIR / f"{dispatch_id}.prompt").exists()


def spawn_codex_dispatch(project_path: str, dispatch_id: int, task: str, model: str = "",
                         effort: str = "") -> None:
    """Open a new iTerm2 tab and start the codex EXECUTOR for this dispatch — the
    $0 codex twin of spawn_iterm2. Writes the task + model to int-keyed CODEX_DIR
    sidecars (the run.sh reads them; avoids shell-quoting the prompt into AppleScript),
    tags the tab `user.orch_id` + titles it `orch #<id>` (so select/close/auto-close
    all work, exactly like spawn_iterm2), exports ORCHESTRATOR_CODEX_RUN_ID (NOT
    ORCHESTRATOR_RUN_ID — Stop hook no-op), and execs codex_dispatch_run.sh (which
    writes codex's REAL pid to PIDS_DIR/<id>.pid). `model` is the codex `-m` id, passed
    EXPLICITLY by the caller (dispatch #3); defaults to the seed model as a safety net.
    Raises on spawn failure (the caller marks a VISIBLE failed row — NEVER a silent
    claude fallback)."""
    if not iterm2_installed():
        raise RuntimeError(
            "iTerm2 not installed. Install with: brew install --cask iterm2"
        )
    ensure_codex_dispatch_runner()
    prompt_file = CODEX_DIR / f"{dispatch_id}.prompt"
    model_file = CODEX_DIR / f"{dispatch_id}.model"
    effort_file = CODEX_DIR / f"{dispatch_id}.effort"
    prompt_file.write_text(task.strip(), encoding="utf-8")
    model_file.write_text((model or config.CODEX_ENGINE_SEED["model"]).strip())
    # Optional reasoning effort; "" ⇒ the run.sh emits no -c override (model's own
    # default). Validated against codex's ladder upstream (app._run_dispatch), so a
    # claude-only value like "max" never lands here.
    effort_file.write_text((effort or "").strip())

    safe_proj = project_path.replace('"', '\\"')
    title = f"orch #{dispatch_id}"
    safe_title = title.replace('"', '\\"')

    cmd = (
        f'cd "{safe_proj}" && '
        f'export ORCHESTRATOR_CODEX_RUN_ID={dispatch_id} && '
        f'{_setuservar_printf("orch_id", str(dispatch_id))}'
        f'printf "\\033]0;{safe_title}\\007" && '
        f'exec "$HOME/.orchestrator/bin/codex_dispatch_run.sh"'
    )
    apple_cmd = cmd.replace("\\", "\\\\").replace('"', '\\"')

    script = _spawn_tab_script(safe_title, apple_cmd)
    try:
        _spawn_osascript(script)
    except Exception:
        # Clean up orphan sidecars so a failed spawn leaks no files.
        for f in (prompt_file, model_file, effort_file):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
        raise


# ─── kimi EXECUTOR dispatch (K5) — the $0 kimi twin of spawn_codex_dispatch ───
# A DISPATCHED kimi task in a watchable iTerm2 tab. Simpler than the codex executor:
# kimi-code has NO -s sandbox modes (turn-1 `-p` auto-approves tool use — full access is
# inherent) and NO reasoning effort. PID at the CLAUDE path (PIDS_DIR/<id>.pid) so kill /
# kill-all / cap / reaper / boot re-attach all locate it unchanged. Sidecars int-keyed by
# dispatch_id in KIMI_DIR (a bare-int key never collides with a seat's slug-uuid id).

KIMI_DISPATCH_RUN_SH = BIN_DIR / "kimi_dispatch_run.sh"


def _build_kimi_dispatch_run_sh(eng: dict) -> str:
    """Render the kimi EXECUTOR run.sh, INTERPOLATING the flag set + model fallback + binary
    from the kimi ENGINE SEED (the kimi twin of _build_codex_dispatch_run_sh). Single source
    of truth; pinned by tests. Turn-1 `-p` auto-approves tool use (kimi-code has NO sandbox
    modes — full access is inherent), then the runner HANDS THE TAB OFF to an interactive
    `kimi -r <session_id> -y` (the #246 hybrid, continuable like the claude executor's REPL).
    NO effort (kimi-code has none)."""
    template = r'''#!/bin/bash
# Orchestrator kimi EXECUTOR runner — the $0 Kimi-subscription analogue of run.sh (the
# dispatched `claude` session), execed inside a WATCHABLE iTerm2 tab.
#
# NOT the kimi SEAT runner (kimi_run.sh): a dispatched executor's turn-1 `-p` AUTO-APPROVES
# tool use (verified 2026-07-17 — kimi-code has NO -s sandbox modes, so full machine access
# is inherent, the kimi twin of `claude --dangerously-skip-permissions`), so a kimi dispatch
# is indistinguishable from a claude one.
#
# §5 hook-gap convergence: kimi has NO Stop/PreToolUse/PostToolUse hooks, so completion /
# loop-watchdog / timeline are NOT signalled via ~/.claude/settings.json. This runner streams
# kimi's stream-json to a sidecar the orchestrator's in-process poller (app._kimi_dispatch_poller)
# tails — the SAME sidecar+PID-poll the seat uses. Hence ORCHESTRATOR_KIMI_RUN_ID (NOT
# ORCHESTRATOR_RUN_ID — the env-gated Stop hook stays a no-op), AND the PID below is written to
# the CLAUDE pid path ($HOME/.orchestrator/pids/<id>.pid) so kill / kill-all / cap / reaper /
# boot re-attach all locate it unchanged.
#
# WHY a FIFO + backgrounded kimi (not a `kimi | tee | python` pipeline): the orchestrator must
# TERMINATE kimi by the recorded PID. In a pipeline `$$` is the SHELL and kimi is a child —
# SIGTERM to the shell would orphan kimi. So kimi is backgrounded to capture its REAL pid; its
# output flows through a FIFO to tee (raw JSONL -> sidecar, parsed by _build_kimi_run) + a
# python3 pretty-printer (readable lines, cosmetic). `wait`ing the consumer flushes the sidecar
# BEFORE .done. MOONSHOT_API_KEY/OPENAI_API_KEY scrubbed -> $0 SUBSCRIPTION, never a billed API.
#
# The kimi flag set + model fallback + binary are INTERPOLATED from the kimi ENGINE SEED at
# write time (single source of truth, pinned by tests).
if [ -z "${ORCHESTRATOR_KIMI_RUN_ID:-}" ]; then
    echo "Orchestrator kimi executor: ORCHESTRATOR_KIMI_RUN_ID not set" >&2
    exit 2
fi
ID="$ORCHESTRATOR_KIMI_RUN_ID"
KIMI_DIR="$HOME/.orchestrator/kimi"
PID_FILE="$HOME/.orchestrator/pids/${ID}.pid"
PROMPT_FILE="$KIMI_DIR/${ID}.prompt"
OUT_FILE="$KIMI_DIR/${ID}.jsonl"
DONE_FILE="$KIMI_DIR/${ID}.done"
FIFO="$KIMI_DIR/${ID}.fifo"
MODEL=$(cat "$KIMI_DIR/${ID}.model" 2>/dev/null || echo @@MODEL_DEFAULT@@)
if [ ! -f "$PROMPT_FILE" ]; then
    echo "Orchestrator kimi executor: missing prompt file $PROMPT_FILE" >&2
    echo 2 > "$DONE_FILE"
    exit 2
fi
PROMPT=$(cat "$PROMPT_FILE")
# $0 subscription path only — never a billed API key.
unset MOONSHOT_API_KEY
unset OPENAI_API_KEY
echo "---- orchestrator kimi EXECUTOR: dispatch $ID ($MODEL, full access like a claude dispatch) ----"
echo "(watchable; no Stop hook — orchestrator finalizes from the sidecar)"
echo
rm -f "$FIFO"
mkfifo "$FIFO" || { echo "Orchestrator kimi executor: mkfifo failed" >&2; echo 2 > "$DONE_FILE"; exit 2; }
# Consumer: raw JSONL -> sidecar (tee) + readable lines -> tab (python). Keyed off kimi's ROLE
# schema (assistant/tool/meta). Cosmetic only — tee wrote raw JSONL to the sidecar.
tee "$OUT_FILE" < "$FIFO" | python3 -u -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except Exception:
        print(line)
        continue
    role = obj.get('role', '')
    if role == 'assistant':
        txt = (obj.get('content') or '').strip()
        if txt:
            print('[assistant]', txt)
        for tc in (obj.get('tool_calls') or []):
            fn = (tc.get('function') or {}) if isinstance(tc, dict) else {}
            print('[tool-call]', fn.get('name', '?'), (fn.get('arguments') or '')[:160])
    elif role == 'tool':
        print('[tool]', (obj.get('content') or '')[:200].replace(chr(10), ' '))
    elif role == 'meta':
        if obj.get('type') == 'session.resume_hint':
            print('[session]', (obj.get('session_id') or '')[:16])
    elif role:
        print('[kimi:%s]' % role)
" &
CONSUMER_PID=$!
# kimi -> FIFO, backgrounded so $! is kimi's REAL pid (the kill target). < /dev/null keeps
# kimi's -p mode from blocking on a non-TTY stdin.
"@@KIMI_BIN@@" @@PROMPT_FLAG@@ "$PROMPT" @@OUTPUT_FORMAT_FLAG@@ @@OUTPUT_FORMAT@@ -m "$MODEL" < /dev/null > "$FIFO" &
KIMI_PID=$!
echo "$KIMI_PID" > "$PID_FILE"
wait "$KIMI_PID"
code=$?
wait "$CONSUMER_PID" 2>/dev/null
rm -f "$FIFO"
# Capture turn-1's session_id from the sidecar BEFORE writing .done (the poller deletes the
# sidecar at finalize). kimi's resume handle is on the `session.resume_hint` meta line (NOT
# codex's thread.started). All-double-quoted Python so the single-quote wrapper needs no escaping.
SESSION_ID="$(python3 -c 'import json, sys
sid = ""
try:
    for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("role") == "meta" and o.get("type") == "session.resume_hint" and o.get("session_id"):
            sid = o["session_id"]
except Exception:
    pass
sys.stdout.write(sid)
' "$OUT_FILE" 2>/dev/null)"
echo "$code" > "$DONE_FILE"
echo
echo "---- kimi executor turn 1 finished (exit $code) — orchestrator finalized from the sidecar ----"
# HYBRID auto-resume — mirror the claude/codex executor's stay-open, continuable tab (#246).
# Hand THIS SAME tab to an INTERACTIVE kimi on the SAME session so the user can read the answer,
# keep going, and close the tab manually. The dispatch is already 'completed'; this phase is
# UNTRACKED (no concurrency slot, not cap-killable, follow-ups NOT recorded) — a claude
# dispatch's post-Stop-hook state. NO `< /dev/null` (interactive needs the real TTY). `exec`
# replaces this shell so the tab IS the kimi session. `-y` = never-prompt (claude parity);
# if `-r <id> -y` ever errors, the fallback below keeps the tab open (no data loss).
if [ -n "$SESSION_ID" ]; then
    echo
    echo "---- resuming interactively on session $SESSION_ID — continue below; close the tab when done ----"
    echo "(follow-up turns here are NOT recorded by the orchestrator)"
    echo
    exec "@@KIMI_BIN@@" @@RESUME_FLAG@@ "$SESSION_ID" @@RESUME_APPROVE@@
fi
# Fallback: no session id (kimi errored before the resume_hint, or a parse miss) — keep the tab
# OPEN at an interactive shell so the user can read the output + resume manually.
echo
echo "---- no session id captured; leaving this tab open (resume manually with: kimi @@RESUME_FLAG@@ --last) ----"
exec "${SHELL:-/bin/zsh}" -i
'''
    return (template
            .replace("@@KIMI_BIN@@", _kimi_runner_bin())
            .replace("@@MODEL_DEFAULT@@", eng["model"])
            .replace("@@PROMPT_FLAG@@", eng["prompt_flag"])
            .replace("@@OUTPUT_FORMAT_FLAG@@", eng["output_format_flag"])
            .replace("@@OUTPUT_FORMAT@@", eng["output_format"])
            .replace("@@RESUME_FLAG@@", eng["resume_flag"])
            .replace("@@RESUME_APPROVE@@", eng["resume_approve_flag"]))


# Built at import from the SEED (genuine seed->bash interpolation); a module constant like
# the codex executor runner so the drift test can pin it.
KIMI_DISPATCH_RUN_SH_CONTENT = _build_kimi_dispatch_run_sh(config.KIMI_ENGINE_SEED)


def ensure_kimi_dispatch_runner():
    """One-time (lazy): create the kimi sidecar dir + write kimi_dispatch_run.sh. Mirrors
    ensure_codex_dispatch_runner; called before spawning a kimi dispatch tab."""
    KIMI_DIR.mkdir(parents=True, exist_ok=True)
    PIDS_DIR.mkdir(parents=True, exist_ok=True)
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    KIMI_DISPATCH_RUN_SH.write_text(KIMI_DISPATCH_RUN_SH_CONTENT)
    KIMI_DISPATCH_RUN_SH.chmod(0o755)


def is_kimi_dispatch(dispatch_id: int) -> bool:
    """True if `dispatch_id` is a kimi EXECUTOR — detected by its int-keyed kimi prompt
    sidecar (mirror of is_codex_dispatch; NO dispatches-table schema change). The executor
    writes a bare-int KIMI_DIR key; a seat's id is a slug-uuid, so there is no collision."""
    return (KIMI_DIR / f"{dispatch_id}.prompt").exists()


def spawn_kimi_dispatch(project_path: str, dispatch_id: int, task: str, model: str = "") -> None:
    """Open a new iTerm2 tab and start the kimi EXECUTOR — the $0 kimi twin of spawn_iterm2 /
    spawn_codex_dispatch. Writes task + model to int-keyed KIMI_DIR sidecars, tags the tab
    `user.orch_id` + titles it `orch #<id>`, exports ORCHESTRATOR_KIMI_RUN_ID, and execs
    kimi_dispatch_run.sh (which writes kimi's REAL pid to PIDS_DIR/<id>.pid). Raises on spawn
    failure (caller marks a VISIBLE failed row — NEVER a silent claude fallback). kimi has no
    effort, so — unlike spawn_codex_dispatch — there is no effort sidecar."""
    if not iterm2_installed():
        raise RuntimeError("iTerm2 not installed. Install with: brew install --cask iterm2")
    ensure_kimi_dispatch_runner()
    prompt_file = KIMI_DIR / f"{dispatch_id}.prompt"
    model_file = KIMI_DIR / f"{dispatch_id}.model"
    prompt_file.write_text(task.strip(), encoding="utf-8")
    model_file.write_text((model or config.KIMI_ENGINE_SEED["model"]).strip())

    safe_proj = project_path.replace('"', '\\"')
    title = f"orch #{dispatch_id}"
    safe_title = title.replace('"', '\\"')
    cmd = (
        f'cd "{safe_proj}" && '
        f'export ORCHESTRATOR_KIMI_RUN_ID={dispatch_id} && '
        f'{_setuservar_printf("orch_id", str(dispatch_id))}'
        f'printf "\\033]0;{safe_title}\\007" && '
        f'exec "$HOME/.orchestrator/bin/kimi_dispatch_run.sh"'
    )
    apple_cmd = cmd.replace("\\", "\\\\").replace('"', '\\"')
    script = _spawn_tab_script(safe_title, apple_cmd)
    try:
        _spawn_osascript(script)
    except Exception:
        for f in (prompt_file, model_file):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
        raise
