"""Claude invokers for the orchestrator's brain calls (rewriter, summarizer,
onboarding). Two execution modes, one structured return type (`ClaudeRun`):

  run_claude_json     — PRIMARY. Opens a dedicated iTerm2 tab and runs
                        `claude -p --output-format stream-json --verbose` so the
                        call is WATCHABLE live, exactly like a task dispatch. The
                        stream is tee'd to a sidecar JSONL which we parse back
                        into the structured result the caller needs. No
                        wall-clock limit by default — the work is visible, so a
                        hang is something you can see and abort by closing the
                        tab (we also detect a closed tab via its PID).

  run_claude_headless — FALLBACK, used only when iTerm2 isn't installed. The
                        original behaviour: a captured `claude -p
                        --output-format json` subprocess with a finite timeout.

Both deliberately avoid ORCHESTRATOR_RUN_ID so the env-gated Stop hook in
~/.claude/settings.json stays a no-op for internal brain calls (it would
otherwise post to /api/complete and pollute the dispatch log). The tab path
sets ORCHESTRATOR_BRAIN_ID instead (see spawn.brain_run.sh); the headless path
scrubs the var from the subprocess env.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from orchestrator.lib import spawn


DEFAULT_MODEL = "sonnet"
DEFAULT_EFFORT = "medium"
DEFAULT_MAX_TURNS = 30
# Finite safety-net timeout for the HEADLESS FALLBACK only. The primary
# iTerm2-tab path runs unlimited (timeout_s=None) since it's user-visible.
DEFAULT_TIMEOUT_S = 900
# How often the tab path polls the .done / .pid sidecars.
_POLL_INTERVAL_S = 0.3
# brain_run.sh writes its PID almost immediately; if no PID appears within this
# window the tab never started its runner, so give up rather than spin forever
# (matters because the default timeout is unlimited). Independent of how long
# the call itself may run.
_STARTUP_GRACE_S = 60


@dataclass
class ClaudeRun:
    """Result of a single `claude` brain call (tab or headless)."""
    ok: bool
    text: str = ""              # the assistant's final text output
    parsed_json: Optional[dict] = None   # populated if text was JSON-parseable
    cost_usd: float = 0.0
    duration_s: float = 0.0
    model: str = ""
    error: str = ""             # populated if ok == False
    raw: Optional[dict] = None  # full result envelope from claude


def _strip_fences(text: str) -> str:
    """Pull a JSON object/array out of a model response.

    Models sometimes wrap JSON in ```json fences, sometimes prefix it with
    prose like "Here is the JSON:\n```...```", sometimes put trailing
    chatter after. We try increasingly permissive strategies:
      1. Whole string is a fenced block → strip the fence.
      2. Whole string is bare JSON → return as-is.
      3. Find a fenced block anywhere in the text → use it.
      4. Find the first `{` or `[` and extract the matching balanced block.

    The downstream parser tries `json.loads` and falls back to ok=False on
    failure, so returning something close-to-valid is enough — we don't
    need to perfectly preprocess."""
    text = text.strip()

    # Strategy 1: whole string is a code block
    m = re.match(r"^```(?:json|JSON)?\s*\n?(.*?)\n?```\s*$", text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # Strategy 2: bare JSON, no fences
    if text.startswith("{") or text.startswith("["):
        return text

    # Strategy 3: fenced block somewhere in the middle (with surrounding prose)
    m = re.search(r"```(?:json|JSON)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # Strategy 4: greedy match from first { (or [) to last } (or ])
    for opener, closer in [("{", "}"), ("[", "]")]:
        i = text.find(opener)
        j = text.rfind(closer)
        if 0 <= i < j:
            return text[i:j + 1].strip()

    return text


def _build_claude_run(envelope: dict, requested_model: str) -> ClaudeRun:
    """Shared: turn a claude result envelope (from `--output-format json`, or
    reconstructed from a stream-json transcript) into a ClaudeRun, parsing the
    assistant text as JSON when possible."""
    text = envelope.get("result", "") or ""
    cost = float(envelope.get("total_cost_usd") or envelope.get("cost_usd") or 0.0)
    duration = float(envelope.get("duration_ms", 0)) / 1000.0
    resolved_model = (envelope.get("model")
                      or (envelope.get("message") or {}).get("model")
                      or requested_model)

    parsed = None
    stripped = _strip_fences(text)
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None

    # If we got a result but couldn't parse JSON, log what came back so the
    # failure is diagnosable from the orchestrator console.
    if parsed is None and text:
        print(f"[claude_runner] JSON parse failed; first 400 chars of response:\n"
              f"{text[:400]}")

    return ClaudeRun(
        ok=True,
        text=text,
        parsed_json=parsed,
        cost_usd=cost,
        duration_s=duration,
        model=resolved_model,
        raw=envelope,
    )


def _envelope_from_stream_jsonl(path) -> Optional[dict]:
    """Reconstruct the `--output-format json` result envelope from a stream-json
    transcript file. The terminal `{"type":"result", ...}` event carries
    `result` / `total_cost_usd` / `duration_ms`; the resolved model id comes
    from the `system/init` event (or any assistant message). Returns None if no
    result event is present (e.g. claude crashed mid-stream)."""
    result_event: Optional[dict] = None
    model = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ttype = obj.get("type")
                if ttype == "result":
                    result_event = obj  # keep the last one
                elif ttype == "system" and obj.get("subtype") == "init":
                    model = obj.get("model") or model
                elif ttype == "assistant":
                    model = (obj.get("message") or {}).get("model") or model
    except OSError:
        return None

    if result_event is None:
        return None
    if not result_event.get("model") and model:
        result_event = {**result_event, "model": model}
    return result_event


def run_claude_headless(
    prompt: str,
    cwd: str,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    max_turns: int = DEFAULT_MAX_TURNS,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> ClaudeRun:
    """FALLBACK: captured headless `claude -p`, used when iTerm2 is absent.
    Never raises — returns a ClaudeRun with `ok=False` and `error` set on any
    failure (timeout, nonzero exit, bad JSON)."""

    cmd = [
        "claude", "-p", prompt,
        "--model", model,
        "--max-turns", str(max_turns),
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--effort", effort,
    ]
    # Scrub ORCHESTRATOR_RUN_ID from env so our Stop hook doesn't fire for
    # internal brain calls and accidentally post to /api/complete.
    env = {k: v for k, v in os.environ.items() if k != "ORCHESTRATOR_RUN_ID"}

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return ClaudeRun(ok=False, error=f"claude timed out after {timeout_s}s")
    except FileNotFoundError:
        return ClaudeRun(ok=False, error="`claude` binary not found on PATH")
    except Exception as e:
        return ClaudeRun(ok=False, error=f"claude spawn failed: {e}")

    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip()[-500:]
        return ClaudeRun(ok=False, error=f"claude exit {proc.returncode}: {stderr_tail}")

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        return ClaudeRun(ok=False, error=f"claude returned non-JSON envelope: {e}",
                         text=(proc.stdout or "")[:1000])

    return _build_claude_run(envelope, model)


def _read_pid(pid_file: Path) -> Optional[int]:
    try:
        pid = int(pid_file.read_text().strip())
        return pid if pid > 0 else None
    except (ValueError, OSError):
        return None


def _tail(path: Path, n: int) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-n:]
    except OSError:
        return ""


def run_claude_json(
    prompt: str,
    cwd: str,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    max_turns: int = DEFAULT_MAX_TURNS,
    timeout_s: Optional[int] = None,
    label: str = "brain",
) -> ClaudeRun:
    """PRIMARY brain-call entrypoint. Runs the call in a watchable iTerm2 tab
    (stream-json tee'd to a sidecar we parse back into structured data). Falls
    back to headless if iTerm2 isn't installed. `timeout_s=None` → no
    wall-clock limit (the tab is visible; a closed tab is detected via PID).
    Never raises — returns ok=False on any failure.

    `label` ("rewriter"/"summarizer"/"onboarding") titles the tab so you can
    tell brain calls apart at a glance."""
    if not spawn.iterm2_installed():
        print("[claude_runner] iTerm2 not installed — running brain call "
              f"headless ({label}). Install iTerm2 to watch brain calls live.")
        return run_claude_headless(prompt, cwd, model, effort, max_turns,
                                   timeout_s or DEFAULT_TIMEOUT_S)

    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "brain"
    brain_id = f"{slug}-{uuid.uuid4().hex[:8]}"
    try:
        spawn.spawn_brain_tab(brain_id, prompt, cwd, model=model, effort=effort,
                              max_turns=max_turns, label=label)
    except Exception as e:
        print(f"[claude_runner] brain tab spawn failed ({e}); headless fallback")
        spawn.cleanup_brain_files(brain_id)
        return run_claude_headless(prompt, cwd, model, effort, max_turns,
                                   timeout_s or DEFAULT_TIMEOUT_S)

    out_file = spawn.BRAIN_DIR / f"{brain_id}.jsonl"
    done_file = spawn.BRAIN_DIR / f"{brain_id}.done"
    pid_file = spawn.BRAIN_DIR / f"{brain_id}.pid"
    deadline = (time.time() + timeout_s) if timeout_s else None

    result: Optional[ClaudeRun] = None
    success = False
    pid: Optional[int] = None
    started_at = time.time()
    try:
        while result is None:
            if done_file.is_file():
                try:
                    exit_code = int((done_file.read_text().strip() or "1"))
                except (ValueError, OSError):
                    exit_code = 1
                if exit_code != 0:
                    result = ClaudeRun(ok=False, error=f"claude exit {exit_code}",
                                       text=_tail(out_file, 800))
                else:
                    envelope = _envelope_from_stream_jsonl(out_file)
                    if envelope is None:
                        result = ClaudeRun(ok=False,
                                           error="brain call produced no result event")
                    else:
                        result = _build_claude_run(envelope, model)
                        success = True
                break

            # Detect a tab the user closed / a claude that died before writing
            # .done (no completion marker would ever arrive otherwise).
            if pid is None:
                pid = _read_pid(pid_file)
                if pid is None and (time.time() - started_at) > _STARTUP_GRACE_S:
                    result = ClaudeRun(ok=False,
                                       error="brain call tab failed to start "
                                             f"(no PID after {_STARTUP_GRACE_S}s)")
                    break
            elif not spawn.pid_alive(pid):
                if done_file.is_file():
                    continue  # race: .done landed; handle on next loop top
                result = ClaudeRun(ok=False,
                                   error="brain call tab closed before completion")
                break

            if deadline and time.time() > deadline:
                result = ClaudeRun(ok=False,
                                   error=f"brain call timed out after {timeout_s}s")
                break

            time.sleep(_POLL_INTERVAL_S)
    finally:
        spawn.finish_brain_tab(brain_id, label=label, success=success)

    return result if result is not None else ClaudeRun(
        ok=False, error="brain call ended unexpectedly")
