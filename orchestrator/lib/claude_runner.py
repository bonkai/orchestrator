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

import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from orchestrator.lib import config, spawn
from orchestrator.lib.db import DATA_DIR


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

# Fusion panel scripts run from the data dir (materialized by
# spawn.ensure_fusion_providers). A registry entry's "script" is the relative
# path "providers/<name>.py", joined onto this base.
PROVIDERS_DIR = str(DATA_DIR / "bin")


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


# ─────────────────────────── codex calls (the codex twin of run_claude_json) ──
# A $0 subscription `codex exec` call in a watchable iTerm2 tab — the codex
# analogue of run_claude_json. Codex's stream schema is NOT claude's, so it gets
# its OWN parser (_envelope_from_codex_stream + _build_codex_run), but reuses the
# engine-neutral plumbing: ClaudeRun, _strip_fences, _read_pid, _tail, the
# poll-loop shape, _STARTUP_GRACE_S/_POLL_INTERVAL_S. Branch A (CODEX_PLAN.md §0,
# verified 2026-06-22 on codex-cli 0.141.0): subscription auth works
# non-interactively at $0, so cost_usd is 0.0 by POLICY — usage IS present and is
# stashed in `raw` so a future paid seat stays priceable, we just never bill it.
# Schema + flags are version-pinned to 0.144.4 (originally 0.141.0; schema
# re-verified live 2026-07-14); codex churns them, so the parser is fail-soft
# (ok=False, never a raise) and should be re-verified on upgrade.

# The codex model id, IMPORTED from the config SEED (C4) — single source of truth,
# no duplicate literal here (this re-points the old inline model literal). It is the
# default-param / safety-net value (callers pass `-m` EXPLICITLY — dispatch #3); the
# RUNTIME judge path reads the MERGED model (config.codex_engine()), so a config.json
# `fusion.codex.model` override wins there. Reading the SEED (not the merged config)
# here keeps this a static module constant — the headless flag set below pulls from
# the same seed, so neither does a config-file read on the fallback path.
DEFAULT_CODEX_MODEL = config.CODEX_ENGINE_SEED["model"]

# The kimi-code model ALIAS, IMPORTED from the config SEED (K3) — single source of
# truth (mirror of DEFAULT_CODEX_MODEL). Default-param / safety-net value; callers pass
# `-m` EXPLICITLY. kimi-code/k3 = K3; NOT a Claude/codex id.
DEFAULT_KIMI_MODEL = config.KIMI_ENGINE_SEED["model"]


def _codex_envelope_from_lines(lines) -> Optional[dict]:
    """Aggregate a `codex exec --json` JSONL transcript (an iterable of lines)
    into ONE envelope _build_codex_run consumes. Codex's schema (codex-cli
    0.144.4, §0 — re-verified 2026-07-14) keys off `type`, NOT claude's
    result/system/assistant:
        {"type":"thread.started","thread_id":...}
        {"type":"turn.started"}
        {"type":"item.completed","item":{"type":"agent_message","text":...}}
        {"type":"turn.completed","usage":{...}}
    The final text is the LAST item.completed whose item.type=="agent_message" →
    .text (so a trailing reasoning/command item never leaks in); token usage is
    on the terminal `turn.completed`. The final text and the usage live in
    DIFFERENT events, so this must AGGREGATE across the whole stream rather than
    read one terminal event. Returns None if no `turn.completed` is present
    (codex died / the tab was cut mid-stream) — the codex analogue of claude's
    "no result event". Never raises on bad lines (they're skipped)."""
    text = ""
    usage: Optional[dict] = None
    thread_id = ""
    saw_turn_completed = False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        ttype = obj.get("type")
        if ttype == "item.completed":
            item = obj.get("item") or {}
            if item.get("type") == "agent_message":
                text = item.get("text") or text       # keep the LAST non-empty
        elif ttype == "turn.completed":
            saw_turn_completed = True
            usage = obj.get("usage") or usage
        elif ttype == "thread.started":
            thread_id = obj.get("thread_id") or thread_id
    if not saw_turn_completed:
        return None
    return {"result": text, "usage": usage, "thread_id": thread_id}


def _envelope_from_codex_stream(path) -> Optional[dict]:
    """File wrapper around _codex_envelope_from_lines for the tab path's sidecar
    JSONL (the codex twin of _envelope_from_stream_jsonl). Returns None if the
    file is unreadable or carries no terminal `turn.completed`. Never raises."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return _codex_envelope_from_lines(f)
    except OSError:
        return None


def _build_codex_run(envelope: dict, requested_model: str) -> ClaudeRun:
    """Turn a codex envelope (from _envelope_from_codex_stream) into a ClaudeRun,
    so every brain caller and run_fusion_json treat a codex result identically to
    a claude one. Reuses _strip_fences for JSON extraction. Two codex specifics
    (§4): there is NO model field, so the model falls back to the one we passed
    via `-m` (requested_model); and under Branch A cost_usd is 0.0 by POLICY —
    usage IS present (kept in `raw` so a future paid seat is priceable), we just
    don't bill the subscription. duration_s is 0.0 — codex's stream carries no
    duration. Never raises."""
    text = (envelope or {}).get("result", "") or ""

    parsed = None
    stripped = _strip_fences(text)
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None

    if parsed is None and text:
        print(f"[claude_runner] codex JSON parse failed; first 400 chars of "
              f"response:\n{text[:400]}")

    return ClaudeRun(
        ok=True,
        text=text,
        parsed_json=parsed,
        cost_usd=0.0,                 # Branch A POLICY — subscription, never billed
        duration_s=0.0,               # codex's stream carries no duration
        model=requested_model,        # codex --json has no model field (§0)
        raw=envelope,                 # usage kept here → a future paid seat is priceable
    )


def _codex_tool_event(obj: dict) -> Optional[dict]:
    """Map ONE codex `exec --json` stream event (C6.0 schema, codex-cli 0.144.4 —
    re-verified 2026-07-14) to
    a normalized tool event the C6 EXECUTOR poller feeds to the UI timeline + the
    loop watchdog — or None for a non-tool event (agent_message / thread / turn /
    anything else). The codex twin of the PreToolUse/PostToolUse hook payloads the
    claude executor gets for free; codex has no hooks (§5), so the poller derives
    this from the streamed JSONL instead.

    Returns {"id","phase":'start'|'end',"tool_name","input_hash","detail"}.
      - `command_execution` → tool_name="command_execution", fingerprint over the
        full `item.command` string; detail carries the command + exit_code + a
        bounded aggregated_output preview.
      - `file_change` → tool_name="file_change", fingerprint over the sorted
        changed `kind:path` pairs; detail carries those paths.
    `tool_name`+`input_hash` mirror claude's PreToolUse fingerprint so
    loop_watchdog.record treats codex identically (N consecutive identical calls →
    kill). C6.0 confirmed both event types emit `item.started` THEN `item.completed`
    (agent_message emits only `item.completed`); the poller dedups by `item.id` so a
    tool call is fingerprinted ONCE (first sighting), the PreToolUse analogue.
    Never raises — returns None on any malformed shape."""
    if not isinstance(obj, dict):
        return None
    ttype = obj.get("type")
    if ttype not in ("item.started", "item.completed"):
        return None
    item = obj.get("item")
    if not isinstance(item, dict):
        return None
    itype = item.get("type")
    if itype not in ("command_execution", "file_change"):
        return None
    phase = "start" if ttype == "item.started" else "end"
    item_id = str(item.get("id") or "")
    if itype == "command_execution":
        cmd = str(item.get("command") or "")
        input_hash = hashlib.sha1(cmd.encode("utf-8", "replace")).hexdigest()[:16]
        detail = {"command": cmd[:400], "exit_code": item.get("exit_code"),
                  "output_preview": str(item.get("aggregated_output") or "")[:400]}
    else:  # file_change
        changes = item.get("changes") or []
        norm = sorted(f"{c.get('kind', '')}:{c.get('path', '')}"
                      for c in changes if isinstance(c, dict))
        input_hash = hashlib.sha1("\n".join(norm).encode("utf-8", "replace")).hexdigest()[:16]
        detail = {"changes": norm[:20]}
    return {"id": item_id, "phase": phase, "tool_name": itype,
            "input_hash": input_hash, "detail": detail}


def run_codex_headless(
    prompt: str,
    cwd: str,
    model: str = DEFAULT_CODEX_MODEL,
    effort: str = "",
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> ClaudeRun:
    """FALLBACK: captured headless `codex exec --json`, used when iTerm2 is
    absent — the codex twin of run_claude_headless. Both the tab and this path
    emit `--json` JSONL, parsed by the SAME codex parser. Never raises — returns
    ok=False with `error` set on any failure (timeout, nonzero exit, bad stream,
    missing binary). Scrubs OPENAI_API_KEY (so codex never routes through the
    billed API — CLAUDE.md hard rule) and ORCHESTRATOR_RUN_ID (so no Stop hook
    fires), and closes stdin (codex exec hangs reading stdin otherwise)."""
    # C4: the exec/-s/--json flag set comes from the config SEED (single source of
    # truth, no inline literals). The seed (not the merged codex_engine()) keeps this
    # fallback path free of a config-file read; these flags are version-pinned codex
    # protocol constants, while the swappable bit — `model` — is passed in explicitly.
    eng = config.CODEX_ENGINE_SEED
    cmd = ["codex", eng["exec_subcmd"], prompt, "-m", model, "-s", eng["sandbox"],
           eng["json_flag"]]
    if effort:
        cmd += ["-c", f"model_reasoning_effort={effort}"]
    # $0 subscription path only: drop the billed-API key AND the Stop-hook trigger
    # from the child env (mirror of run_claude_headless's ORCHESTRATOR_RUN_ID scrub).
    env = {k: v for k, v in os.environ.items()
           if k not in ("OPENAI_API_KEY", "ORCHESTRATOR_RUN_ID")}

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout_s,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return ClaudeRun(ok=False, error=f"codex timed out after {timeout_s}s")
    except FileNotFoundError:
        return ClaudeRun(ok=False, error="`codex` binary not found on PATH")
    except Exception as e:
        return ClaudeRun(ok=False, error=f"codex spawn failed: {e}")

    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip()[-500:]
        return ClaudeRun(ok=False, error=f"codex exit {proc.returncode}: {stderr_tail}")

    envelope = _codex_envelope_from_lines((proc.stdout or "").splitlines())
    if envelope is None:
        return ClaudeRun(ok=False,
                         error="codex produced no turn.completed event",
                         text=(proc.stdout or "")[:1000])
    return _build_codex_run(envelope, model)


def run_codex_json(
    prompt: str,
    cwd: str,
    model: str = DEFAULT_CODEX_MODEL,
    effort: str = "",
    timeout_s: Optional[int] = None,
    label: str = "codex",
) -> ClaudeRun:
    """PRIMARY codex-call entrypoint — the codex twin of run_claude_json. Runs in
    a watchable iTerm2 tab (`codex exec --json` tee'd to a sidecar we parse back
    into structured data), falling back to headless if iTerm2 isn't installed.
    `timeout_s=None` → no wall-clock limit (the tab is visible; a closed tab is
    detected via PID). Never raises — returns ok=False on any failure
    (auth-expired, rate-limit, closed tab, timeout). `model` is passed EXPLICITLY
    to spawn_codex_tab so the parser's model fallback is the model we asked for
    (dispatch #3)."""
    if not spawn.iterm2_installed():
        print("[claude_runner] iTerm2 not installed — running codex call "
              f"headless ({label}). Install iTerm2 to watch codex calls live.")
        return run_codex_headless(prompt, cwd, model, effort,
                                  timeout_s or DEFAULT_TIMEOUT_S)

    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "codex"
    codex_id = f"{slug}-{uuid.uuid4().hex[:8]}"
    try:
        spawn.spawn_codex_tab(codex_id, prompt, cwd, model=model, effort=effort,
                              label=label)
    except Exception as e:
        print(f"[claude_runner] codex tab spawn failed ({e}); headless fallback")
        spawn.cleanup_codex_files(codex_id)
        return run_codex_headless(prompt, cwd, model, effort,
                                  timeout_s or DEFAULT_TIMEOUT_S)

    out_file = spawn.CODEX_DIR / f"{codex_id}.jsonl"
    done_file = spawn.CODEX_DIR / f"{codex_id}.done"
    pid_file = spawn.CODEX_DIR / f"{codex_id}.pid"
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
                    result = ClaudeRun(ok=False, error=f"codex exit {exit_code}",
                                       text=_tail(out_file, 800))
                else:
                    envelope = _envelope_from_codex_stream(out_file)
                    if envelope is None:
                        result = ClaudeRun(ok=False,
                                           error="codex call produced no result event")
                    else:
                        result = _build_codex_run(envelope, model)
                        success = True
                break

            # Detect a tab the user closed / a codex that died before writing
            # .done (no completion marker would ever arrive otherwise).
            if pid is None:
                pid = _read_pid(pid_file)
                if pid is None and (time.time() - started_at) > _STARTUP_GRACE_S:
                    result = ClaudeRun(ok=False,
                                       error="codex call tab failed to start "
                                             f"(no PID after {_STARTUP_GRACE_S}s)")
                    break
            elif not spawn.pid_alive(pid):
                if done_file.is_file():
                    continue  # race: .done landed; handle on next loop top
                result = ClaudeRun(ok=False,
                                   error="codex call tab closed before completion")
                break

            if deadline and time.time() > deadline:
                result = ClaudeRun(ok=False,
                                   error=f"codex call timed out after {timeout_s}s")
                break

            time.sleep(_POLL_INTERVAL_S)
    finally:
        spawn.finish_codex_tab(codex_id, label=label, success=success)

    return result if result is not None else ClaudeRun(
        ok=False, error="codex call ended unexpectedly")


# ── Kimi Code CLI engine (K1) — the kimi-code twin of the codex invoker above ──
# Subscription-backed ($0, OAuth), headless via `kimi -p <prompt> --output-format
# stream-json`. Pinned to kimi-code 0.27.0 (KIMI_PLAN.md §4; schema verified live
# 2026-07-17); re-verify on `kimi upgrade`. Fail-soft (ok=False, never a raise).

def _kimi_bin() -> str:
    """The kimi-code binary — PATH, else the seeded ~/.kimi-code/bin fallback (the
    installer exports that dir only in the interactive .zshrc, which the server
    subprocess may not source). Returns 'kimi' if neither resolves (the spawn then
    fails cleanly with a not-found error)."""
    return config._resolve_kimi_bin() or "kimi"


def _kimi_envelope_from_lines(lines) -> Optional[dict]:
    """Aggregate a `kimi -p --output-format stream-json` JSONL transcript into ONE
    envelope _build_kimi_run consumes. kimi-code 0.27.0 schema (§4, verified live):
        {"role":"assistant","content":"..."}                      (may repeat)
        {"role":"tool","tool_call_id":...,"content":...}
        {"role":"meta","type":"session.resume_hint","session_id":"session_...",...}
    The final text is the LAST line with role=="assistant" → .content (so a trailing
    tool/meta line never leaks in); the resume session_id is on the meta
    `session.resume_hint` line (the codex thread_id analogue, for the K5 continuable
    tab). Returns None if NO assistant line was seen (kimi died / tab cut mid-stream)
    — the kimi analogue of codex's 'no turn.completed'. Never raises (bad lines skipped)."""
    text = ""
    session_id = ""
    saw_assistant = False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        role = obj.get("role")
        if role == "assistant":
            content = obj.get("content")
            if isinstance(content, str) and content:
                text = content            # keep the LAST non-empty assistant message
                saw_assistant = True
            elif content is not None:
                saw_assistant = True      # an (empty) assistant turn still counts as "kimi answered"
        elif role == "meta" and obj.get("type") == "session.resume_hint":
            session_id = obj.get("session_id") or session_id
    if not saw_assistant:
        return None
    return {"result": text, "session_id": session_id}


def _envelope_from_kimi_stream(path) -> Optional[dict]:
    """File wrapper around _kimi_envelope_from_lines for the K5 tab sidecar JSONL
    (the kimi twin of _envelope_from_codex_stream). Returns None if unreadable or no
    assistant line. Never raises."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return _kimi_envelope_from_lines(f)
    except OSError:
        return None


def _build_kimi_run(envelope: dict, requested_model: str) -> ClaudeRun:
    """Turn a kimi envelope into a ClaudeRun, so every caller treats a kimi result
    identically to a claude/codex one. kimi specifics (mirror codex): no model field
    → model falls back to the `-m` we passed (requested_model); cost_usd=0.0 by
    POLICY (subscription — no usage field in the stream anyway); duration_s=0.0. The
    session_id stays in `raw` for the K5 resume hand-off. Never raises."""
    text = (envelope or {}).get("result", "") or ""

    parsed = None
    stripped = _strip_fences(text)
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None

    return ClaudeRun(
        ok=True,
        text=text,
        parsed_json=parsed,
        cost_usd=0.0,                 # subscription — never billed (no usage in the stream)
        duration_s=0.0,               # kimi's stream carries no duration
        model=requested_model,        # kimi stream-json has no model field
        raw=envelope,                 # carries session_id for the K5 resume hand-off
    )


def run_kimi_headless(
    prompt: str,
    cwd: str,
    model: str = DEFAULT_KIMI_MODEL,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> ClaudeRun:
    """Captured headless `kimi -p <prompt> --output-format stream-json` — the kimi
    twin of run_codex_headless. Never raises (ok=False + `error` on any failure).
    Scrubs MOONSHOT_API_KEY/OPENAI_API_KEY (so kimi runs on the $0 SUBSCRIPTION, not
    a billed key) and ORCHESTRATOR_RUN_ID (no Stop hook), and closes stdin. The
    non-interactive `-p` flag handles approvals itself and CANNOT combine with -y
    (§4); kimi-code has no sandbox/effort flags, so neither is passed."""
    kbin = _kimi_bin()
    eng = config.KIMI_ENGINE_SEED
    cmd = [kbin, eng["prompt_flag"], prompt,
           eng["output_format_flag"], eng["output_format"], "-m", model]
    env = {k: v for k, v in os.environ.items()
           if k not in ("MOONSHOT_API_KEY", "OPENAI_API_KEY", "ORCHESTRATOR_RUN_ID")}
    # Put ~/.kimi-code/bin on the child PATH when we resolved kimi by absolute path,
    # so kimi's own sub-helpers resolve too.
    if os.path.sep in kbin:
        env["PATH"] = os.path.dirname(kbin) + os.pathsep + env.get("PATH", "")

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout_s,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return ClaudeRun(ok=False, error=f"kimi timed out after {timeout_s}s")
    except FileNotFoundError:
        return ClaudeRun(ok=False, error="`kimi` binary not found on PATH")
    except Exception as e:
        return ClaudeRun(ok=False, error=f"kimi spawn failed: {e}")

    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip()[-500:]
        return ClaudeRun(ok=False, error=f"kimi exit {proc.returncode}: {stderr_tail}")

    envelope = _kimi_envelope_from_lines((proc.stdout or "").splitlines())
    if envelope is None:
        return ClaudeRun(ok=False,
                         error="kimi produced no assistant message",
                         text=(proc.stdout or "")[:1000])
    return _build_kimi_run(envelope, model)


def run_kimi_json(
    prompt: str,
    cwd: str,
    model: str = DEFAULT_KIMI_MODEL,
    timeout_s: Optional[int] = None,
    label: str = "kimi",
) -> ClaudeRun:
    """PRIMARY kimi-call entrypoint — the kimi twin of run_codex_json. K2 (now):
    delegates to the captured headless path. K5 adds the watchable iTerm2 tab
    (spawn_kimi_tab) + `.done`/`.pid` polling, exactly as run_codex_json does, without
    changing this signature. Never raises."""
    # K5 TODO: watchable tab via spawn.spawn_kimi_tab with a headless fallback, mirroring
    # run_codex_json. Until then the seat answers headlessly (still $0 subscription).
    return run_kimi_headless(prompt, cwd, model, timeout_s or DEFAULT_TIMEOUT_S)


# ─────────────────────────── Fusion (optional, opt-in) ─────────────────────
# A panel of per-provider scripts (run in parallel) answers the SAME prompt;
# the local `claude` CLI then JUDGES them into one synthesis. The judge is free
# (subscription) and runs in a visible brain tab, so "No Anthropic API calls"
# stays intact. cost_usd = Σ panel provider token costs (the only out-of-pocket
# spend). Everything degrades to the plain claude path when <2 providers answer.
#
# This is the IN-PROCESS fan-out: the panel runs as captured subprocesses here.
# A later phase puts the panel in a watchable iTerm2 "fusion" tab in front of
# this same logic (the judge is already a visible brain tab via run_claude_json).


def _apply_lens(prompt: str, lens: str) -> str:
    """F8.4: prepend a per-seat LENS so this seat answers the SAME task through a
    particular perspective (§5 decorrelation). The original prompt is kept
    verbatim and LAST, so any output-format / JSON-schema instructions it carries
    still travel to the seat unmodified (and stay the last thing the model reads).
    An empty lens returns the prompt unchanged — lenses are opt-in, so a lens-free
    panel is byte-for-byte the pre-F8.4 behavior.

    ⚠ Kept textually identical to fusion_call._apply_lens so the watchable-tab and
    in-process panel paths build the SAME lensed prompt (fusion_call.py can't
    import this module — it runs standalone in the tab)."""
    lens = (lens or "").strip()
    if not lens:
        return prompt
    return ("Approach the task below through this specific lens — let it shape "
            "what you emphasize, but still answer the task in full:\n"
            f"{lens}\n\n--- TASK ---\n{prompt}")


def _panel_answer(name: str, prov: dict, prompt: str, timeout_s: int) -> dict:
    """Run ONE provider's script as a subprocess → normalized dict + computed
    cost. NEVER raises — a spawn/timeout/parse failure or an ok=false script
    both come back as {"ok": False, ...}. The script owns the lab's native API;
    we only read its normalized stdout and price its tokens from the registry."""
    req = json.dumps({"prompt": prompt, "model": prov.get("model", ""),
                      "timeout_s": timeout_s})
    script_path = os.path.join(PROVIDERS_DIR, prov["script"])
    try:
        p = subprocess.run(["python3", script_path], input=req,
                           capture_output=True, text=True, timeout=timeout_s + 15)
        out = json.loads(p.stdout or "{}")
    except Exception as e:                       # spawn / timeout / bad JSON
        return {"name": name, "ok": False, "error": str(e)}
    if not out.get("ok"):
        return {"name": name, "ok": False, "error": out.get("error", "unknown")}
    in_tok = out.get("prompt_tokens", 0) or 0
    out_tok = out.get("completion_tokens", 0) or 0
    cost = (in_tok * prov.get("price_in", 0) + out_tok * prov.get("price_out", 0)) / 1e6
    return {"name": name, "model": out.get("model", prov.get("model", "")),
            "text": out.get("text", ""), "cost": cost,
            "prompt_tokens": in_tok, "completion_tokens": out_tok, "ok": True}


def _run_panel(prompt: str, panel: list, providers: dict, timeout_s: int,
               lenses: Optional[dict] = None) -> list:
    """Fan out to the panel's providers IN PARALLEL (wall-clock ≈ slowest seat,
    not the sum). Order of the returned answers matches `panel`. `lenses` maps a
    provider name → its resolved lens TEXT (F8.4); a seat with no entry gets the
    shared prompt verbatim, so `lenses=None` is the pre-F8.4 behavior."""
    if not panel:
        return []
    lenses = lenses or {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(panel))) as ex:
        return list(ex.map(
            lambda n: _panel_answer(n, providers[n],
                                    _apply_lens(prompt, lenses.get(n, "")), timeout_s),
            panel))


def _judge_prompt(orig: str, answers: list) -> str:
    """Reuse the ORIGINAL prompt verbatim (so any output-format/JSON-schema
    instructions travel with it), append the N panel answers, then ask the
    judge to synthesize the single best response in that same format."""
    blocks = [orig.strip(),
              f"\n\n---\nA panel of {len(answers)} independent models each "
              "answered the task above. Their answers:\n"]
    for i, a in enumerate(answers, 1):
        blocks.append(f"\n### Panel answer {i} — {a.get('name', '?')} "
                      f"({a.get('model', '?')})\n{a.get('text', '')}\n")
    blocks.append("\n---\nYou are the judge. Synthesize the single best response "
                  "to the original task: resolve disagreements, keep what is "
                  "correct, discard what is wrong. Respond in the EXACT same "
                  "format the task requested — output only that, with no preamble "
                  "and no mention of the panel.")
    return "".join(blocks)


def _verify_prompt(orig: str, synthesis: str, answers: list) -> str:
    """F11.c.1: ask a CRITIC seat to check the judge's synthesis against the original
    task and the panel answers. It must return ONLY a strict JSON verdict
    {"defect": bool, "issues": [str, ...]} — `defect` true ONLY for a concrete,
    correctable error/omission/over-claim, never style. Conservative by design so
    re-judges stay rare (§10.d: a zealous critic on a partial view inflates false
    positives)."""
    blocks = ["You are a strict verifier. A panel of models answered a task and a "
              "judge synthesized their answers into one response. Check that synthesis "
              "for CONCRETE, CORRECTABLE problems: factual or logical errors, a correct "
              "point from the panel the judge dropped, or claims the synthesis "
              "over-states. Do NOT nitpick style, tone, or formatting.\n",
              "\n--- ORIGINAL TASK ---\n", orig.strip(), "\n",
              "\n--- JUDGE'S SYNTHESIS ---\n", (synthesis or "").strip(), "\n",
              "\n--- PANEL ANSWERS ---\n"]
    for i, a in enumerate(answers, 1):
        blocks.append(f"\n### Panel answer {i} — {a.get('name', '?')} "
                      f"({a.get('model', '?')})\n{a.get('text', '')}\n")
    blocks.append('\n--- OUTPUT ---\nRespond with ONLY a single JSON object — no prose, '
                  'no markdown fences, first character `{` and last `}`:\n'
                  '{"defect": true|false, "issues": ["short, specific, correctable '
                  'problem", ...]}\n'
                  'Set "defect" false (and "issues" []) unless there is at least one '
                  'concrete, correctable problem. Be conservative.')
    return "".join(blocks)


def _rejudge_prompt(orig: str, answers: list, prior_synthesis: str,
                    issues: list) -> str:
    """F11.c.1: re-judge after the verifier flagged a defect. Reuses the ORIGINAL
    prompt verbatim (so any output-format/JSON-schema travels), shows the prior
    synthesis + the specific issues, and asks for a CORRECTED synthesis in that same
    format. Mirrors _judge_prompt's format-preservation contract."""
    blocks = [orig.strip(),
              f"\n\n---\nA panel of {len(answers)} independent models each answered "
              "the task above. Their answers:\n"]
    for i, a in enumerate(answers, 1):
        blocks.append(f"\n### Panel answer {i} — {a.get('name', '?')} "
                      f"({a.get('model', '?')})\n{a.get('text', '')}\n")
    blocks.append("\n---\nA first synthesis was produced:\n\n"
                  + (prior_synthesis or "").strip() + "\n")
    issue_lines = "\n".join(f"- {it}" for it in issues if str(it).strip())
    blocks.append("\n---\nA verifier found these correctable problems with that "
                  "synthesis:\n" + (issue_lines or "- (unspecified)") + "\n")
    blocks.append("\n---\nProduce a CORRECTED single best response to the original "
                  "task: fix the problems above, resolve disagreements, keep what is "
                  "correct, discard what is wrong. Respond in the EXACT same format the "
                  "task requested — output only that, with no preamble and no mention "
                  "of the panel or the verifier.")
    return "".join(blocks)


def _price_tab_answers(raw: list, providers: dict) -> list:
    """Turn fusion_call.py's collected outputs (normalized JSON + a `name`) into
    the SAME priced shape _panel_answer returns, so run_fusion_json treats tab
    and in-process answers identically. Cost is computed here from the registry —
    the standalone fusion_call.py never sees prices."""
    out = []
    for a in raw:
        if not isinstance(a, dict):
            continue
        name = a.get("name")
        prov = providers.get(name, {})
        if a.get("ok"):
            in_tok = a.get("prompt_tokens", 0) or 0
            out_tok = a.get("completion_tokens", 0) or 0
            cost = (in_tok * prov.get("price_in", 0)
                    + out_tok * prov.get("price_out", 0)) / 1e6
            out.append({"name": name, "model": a.get("model", prov.get("model", "")),
                        "text": a.get("text", ""), "cost": cost,
                        "prompt_tokens": in_tok, "completion_tokens": out_tok, "ok": True})
        else:
            out.append({"name": name, "ok": False, "error": a.get("error", "unknown")})
    return out


def _run_fusion_in_tab(prompt: str, panel: list, providers: dict,
                       timeout_s: int, cwd: str = "",
                       lenses: Optional[dict] = None) -> Optional[list]:
    """Run the panel in a WATCHABLE iTerm2 fusion tab: write the request, spawn
    the tab (fusion_run.sh → fusion_call.py), poll <id>.done/.pid like the brain
    loop (simpler — <id>.json is already the final collected answers), then price
    them from the registry. Returns the answers list, or None on any tab failure
    (→ caller falls back to the in-process panel). Never raises. `lenses` maps a
    provider name → its resolved lens TEXT (F8.4); only NON-empty entries are sent,
    so a lens-free panel's request body is byte-for-byte the pre-F8.4 shape."""
    fusion_id = f"fusion-{uuid.uuid4().hex[:8]}"
    lenses = lenses or {}
    # The request carries only what the standalone fusion_call.py needs (it can't
    # import the package or read the registry): each seat's script + model, plus
    # any per-seat lens text (fusion_call.py applies it exactly like _run_panel).
    body = {"prompt": prompt, "timeout_s": timeout_s, "panel": panel,
            "providers": {n: {"script": providers[n].get("script", ""),
                              "model": providers[n].get("model", "")} for n in panel},
            "lenses": {n: lenses[n] for n in panel if lenses.get(n)}}
    try:
        spawn.spawn_fusion_tab(fusion_id, body, cwd or os.getcwd())
    except Exception as e:
        print(f"[claude_runner] fusion tab spawn failed ({e})")
        spawn.cleanup_fusion_files(fusion_id)
        return None

    done_file = spawn.FUSION_DIR / f"{fusion_id}.done"
    json_file = spawn.FUSION_DIR / f"{fusion_id}.json"
    pid_file = spawn.FUSION_DIR / f"{fusion_id}.pid"
    deadline = time.time() + timeout_s + 60      # panel clock; the judge has its own
    answers: Optional[list] = None
    success = False
    pid: Optional[int] = None
    started_at = time.time()
    try:
        while True:
            if done_file.is_file():
                try:
                    code = int((done_file.read_text().strip() or "1"))
                except (ValueError, OSError):
                    code = 1
                if code == 0 and json_file.is_file():
                    try:
                        raw = json.loads(json_file.read_text() or "[]")
                    except (ValueError, OSError):
                        raw = None
                    if isinstance(raw, list):
                        answers = _price_tab_answers(raw, providers)
                        success = True
                break

            if pid is None:
                pid = _read_pid(pid_file)
                if pid is None and (time.time() - started_at) > _STARTUP_GRACE_S:
                    break                          # tab never started its runner
            elif not spawn.pid_alive(pid):
                if done_file.is_file():
                    continue                       # race: .done just landed
                break                              # tab closed before completion

            if time.time() > deadline:
                break
            time.sleep(_POLL_INTERVAL_S)
    finally:
        spawn.finish_fusion_tab(fusion_id, success=success)
    return answers


def _panel_answers(prompt: str, panel: list, providers: dict,
                   timeout_s: int, cwd: str = "",
                   lenses: Optional[dict] = None) -> list:
    """Get the panel's answers. PREFERS the watchable iTerm2 fusion tab; falls
    back to the in-process subprocess fan-out only when iTerm2 is absent or the
    tab fails — so a panel is never hidden when iTerm2 is available. `lenses`
    (provider name → resolved lens TEXT, F8.4) is threaded to BOTH paths so the
    seat prompts are identical whichever path runs."""
    if spawn.iterm2_installed():
        tab = _run_fusion_in_tab(prompt, panel, providers, timeout_s, cwd=cwd,
                                 lenses=lenses)
        if tab is not None:
            return tab
        print("[claude_runner] fusion tab unavailable; in-process panel fallback")
    return _run_panel(prompt, panel, providers, timeout_s, lenses=lenses)


def _anthropic_seat_answer(seat: dict, prompt: str, cwd: str) -> dict:
    """One Claude Code panel seat: a LOCAL `claude` CLI call (visible brain tab),
    differentiated from its siblings by --effort. Free ($0 out-of-pocket — it's
    the subscription) and makes NO Anthropic API call, so a Claude seat keeps the
    'No Anthropic API calls' rule intact, exactly like the judge.

    Returns the SAME normalized shape _panel_answer returns, so run_fusion_json
    treats CLI seats and provider seats identically. Never raises —
    run_claude_json already returns ok=False on any failure. NOTE: model is passed
    EXPLICITLY (run_claude_json defaults to sonnet, so an Opus seat that didn't
    would silently downgrade — dispatch #3 lesson)."""
    model = (seat.get("model") or "opus").strip()
    effort = (seat.get("effort") or "high").strip()
    name = seat.get("name") or f"{model}-{effort}"
    # F8.4: a per-seat lens prefixes the prompt (resolved TEXT in lens_text; the
    # NAME in lens is carried only for the surface/breakdown). No lens ⇒ the
    # prompt is unchanged, so an un-lensed Claude seat behaves exactly as before.
    lens_name = (seat.get("lens") or "").strip()
    lens_text = (seat.get("lens_text") or "").strip()
    label = f"fusion-seat:{name}" + (f"+{lens_name}" if lens_name else "")
    run = run_claude_json(prompt=_apply_lens(prompt, lens_text), cwd=cwd or os.getcwd(),
                          model=model, effort=effort, label=label)
    if not run.ok:
        return {"name": name, "ok": False, "error": run.error or "claude seat failed",
                "lens": lens_name}
    return {"name": name, "model": run.model or model, "text": run.text,
            "cost": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
            "effort": effort, "subscription": True, "lens": lens_name, "ok": True}


def _codex_seat_answer(seat: dict, prompt: str, cwd: str) -> dict:
    """One codex panel seat: a LOCAL `codex exec` call (visible iTerm2 tab) on the
    ChatGPT subscription — the codex twin of _anthropic_seat_answer. Free ($0
    out-of-pocket — it's the subscription) and makes NO OpenAI API call
    (run_codex_json scrubs OPENAI_API_KEY on the headless path; the tab runs the
    subscription CLI), so a codex seat keeps the extended 'No OpenAI API calls'
    hard rule intact, exactly as a Claude seat keeps 'No Anthropic API calls'.

    Returns the SAME normalized shape _panel_answer / _anthropic_seat_answer
    return, so run_fusion_json treats codex seats, Claude seats, and provider seats
    identically. Never raises — run_codex_json already returns ok=False on any
    failure (auth-expired, rate-limit, closed tab, timeout).

    Two codex divergences from the Claude seat that are CORRECT, not inconsistencies
    to 'fix':
      - model defaults to the module constant DEFAULT_CODEX_MODEL (not a literal
        'opus'), passed EXPLICITLY to run_codex_json so a non-default seat isn't
        silently downgraded to the placeholder (dispatch #3 lesson).
      - effort defaults to "" — empty means codex uses the MODEL's own reasoning
        default (C0), so we do NOT inject 'high' the way the Claude seat does; and
        the seat name omits the trailing '-' an empty effort would otherwise add."""
    model = (seat.get("model") or DEFAULT_CODEX_MODEL).strip()
    effort = (seat.get("effort") or "").strip()
    # F8.4: a per-seat lens prefixes the prompt (resolved TEXT in lens_text; the
    # NAME in lens is carried only for the surface/breakdown). No lens ⇒ the prompt
    # is unchanged, so an un-lensed codex seat behaves exactly as a lens-free call.
    lens_name = (seat.get("lens") or "").strip()
    lens_text = (seat.get("lens_text") or "").strip()
    name = seat.get("name") or (f"{model}-{effort}" if effort else model)
    label = f"fusion-seat:{name}" + (f"+{lens_name}" if lens_name else "")
    run = run_codex_json(prompt=_apply_lens(prompt, lens_text), cwd=cwd or os.getcwd(),
                         model=model, effort=effort, label=label)
    if not run.ok:
        return {"name": name, "ok": False, "error": run.error or "codex seat failed",
                "lens": lens_name}
    return {"name": name, "model": run.model or model, "text": run.text,
            "cost": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
            "effort": effort, "subscription": True, "lens": lens_name, "ok": True}


def _kimi_seat_answer(seat: dict, prompt: str, cwd: str) -> dict:
    """One kimi panel seat: a LOCAL `kimi -p` call on the subscription — the kimi twin
    of _codex_seat_answer. Free ($0 — subscription) and makes NO billed API call
    (run_kimi_headless scrubs MOONSHOT_API_KEY/OPENAI_API_KEY), so a kimi seat keeps the
    'No billed API calls' rule intact like a Claude/codex seat. Returns the SAME
    normalized shape, so run_fusion_json treats all seat kinds identically. Never raises
    (run_kimi_json returns ok=False on any failure). kimi-code has NO reasoning effort
    (§4), so — unlike the codex seat — there is no effort field at all (the output keeps
    an empty "effort" only for shape-parity with the other seat kinds)."""
    model = (seat.get("model") or DEFAULT_KIMI_MODEL).strip()
    lens_name = (seat.get("lens") or "").strip()
    lens_text = (seat.get("lens_text") or "").strip()
    name = seat.get("name") or model
    label = f"fusion-seat:{name}" + (f"+{lens_name}" if lens_name else "")
    run = run_kimi_json(prompt=_apply_lens(prompt, lens_text), cwd=cwd or os.getcwd(),
                        model=model, label=label)
    if not run.ok:
        return {"name": name, "ok": False, "error": run.error or "kimi seat failed",
                "lens": lens_name}
    return {"name": name, "model": run.model or model, "text": run.text,
            "cost": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
            "effort": "", "subscription": True, "lens": lens_name, "ok": True}


def run_fusion_json(prompt: str, cwd: str = "", preset: Optional[str] = None,
                    panel: Optional[list] = None, timeout_s: Optional[int] = None,
                    judge_model: str = "opus", judge_effort: str = "high",
                    verify: Optional[bool] = None, verify_model: str = "opus",
                    verify_effort: str = "high",
                    judge_engine: str = "claude") -> ClaudeRun:
    """Fusion sibling of run_claude_json. Runs a PANEL — any mix of external
    per-provider scripts, local Claude Code seats (effort-differentiated `claude`
    CLI calls; $0, NO Anthropic API), AND local codex seats (C2: `codex exec` on
    the ChatGPT subscription; $0, NO OpenAI API) — in parallel, then synthesizes
    via the local `claude` CLI judge (run_claude_json — a visible brain tab, free
    on the subscription). Returns the SAME ClaudeRun the brain callers expect, with
    cost_usd = Σ EXTERNAL panel cost (Claude AND codex seats are subscription →
    $0). NEVER raises — any shortfall returns ok=False so run_brain_json can fall
    back to the plain claude call.

    `panel` is a list whose entries are either a provider NAME (str — an external
    seat), a dict {"kind":"claude_cli","model","effort"} (a local Claude seat), or
    a dict {"kind":"codex_cli","model","effort"} (a local codex seat — C2.3); the
    codex kind is purely ADDITIVE and never touches the claude_cli path. Duplicate
    Claude/codex seats (same model+effort) are allowed. F12: duplicate cross-lab
    provider seats are allowed too (e.g. 3× glm) — each becomes its own seat with a
    unique key (glm, glm#2, glm#3) and may carry its own lens. F8.4: any entry may
    also carry a "lens" (a configured lens NAME or literal text) — CLI seats as
    {"kind":"claude_cli"|"codex_cli",...,"lens"} and external seats as the dict form
    {"name":<provider>,"lens":...}; each lensed seat answers the SAME task through
    that perspective (§5 decorrelation). The judge still sees the original prompt
    verbatim. No lens anywhere ⇒ byte-for-byte the pre-F8.4 behavior.

    ⚠ run_claude_json defaults to sonnet, so the judge model is passed
    EXPLICITLY (default opus/high; a summarizer caller can pass sonnet).

    C3: `judge_engine` ("claude" default | "codex") selects the engine for the
    judge AND verifier AND re-judge in one knob. "claude" is byte-for-byte today's
    behavior (opt-in, reversible); "codex" synthesizes on the ChatGPT subscription
    via run_codex_json. It is INDEPENDENT of the panel seats — a codex judge can
    synthesize a claude/provider panel and vice-versa (codex_cli_available() gates
    seats, not the judge). The codex path resolves a codex-appropriate model: the
    judge/verify defaults are Claude ids, and routing one to `codex -m` would be a
    silent downgrade (dispatch #3). C4: that codex model comes from the config SEED
    (config.codex_engine()["model"]) so a config.json override wins. (C5 reviewed
    adding a per-CALL codex judge model and DECLINED: the C5 dispatch picker selects
    the EXECUTOR engine + codex SEATS, not a per-dispatch judge model, so the
    merged-config model stays the single source of truth — a per-call override would
    be an unused param. Override the codex judge model via `fusion.codex.model`.)"""
    cfg = config.fusion_config()
    providers = cfg["providers"]
    presets = cfg["presets"]
    preset = preset or cfg.get("preset") or config.DEFAULT_FUSION_PRESET
    panel = panel or presets.get(preset) or config.FUSION_PRESETS_SEED["budget"]
    timeout_s = timeout_s or cfg.get("timeout_s") or config.DEFAULT_FUSION_TIMEOUT_S
    # F11.c.1: None ⇒ use the configured default; an explicit True/False overrides it
    # (so a caller can force-off even when fusion.verify is on globally).
    verify = cfg.get("verify", False) if verify is None else verify

    # Normalize the (possibly mixed) panel into usable seats. Each entry is either
    # an EXTERNAL provider — a registry NAME (str), fanned out via its provider
    # script — or a LOCAL CLI seat: a Claude Code seat (dict {"kind":"claude_cli",
    # "model","effort"}) run through the `claude` CLI, or a codex seat (dict
    # {"kind":"codex_cli","model","effort"}, C2.3) run through `codex exec` — both
    # like the judge: visible tab, subscription, $0, NO Anthropic/OpenAI API.
    # Duplicate CLI seats are allowed. External names are kept only if active (key
    # resolves), so "fusion on but <2 usable seats" falls back instead of erroring.
    active = config.active_providers()
    claude_ok = config.claude_cli_available()
    codex_ok = config.codex_cli_available()   # C2.3: mirror claude_ok (PATH + auth probe)
    kimi_ok = config.kimi_cli_available()      # K2: mirror codex_ok (PATH + OAuth probe)
    lenses_cfg = cfg.get("lenses") or config.FUSION_LENSES_SEED  # F8.4 name→text map
    prov_names: list = []          # UNIQUE seat keys (fan out via scripts)
    prov_providers: dict = {}      # seat key → its provider config (script/model/prices)
    prov_lenses: dict = {}         # seat key → resolved lens TEXT (fan-out)
    prov_lens_names: dict = {}     # seat key → lens NAME (surface/tagging)
    prov_seat_counts: dict = {}    # base provider name → seats so far (for #2,#3 keys)
    claude_seats: list = []        # local claude CLI seats
    codex_seats: list = []         # local codex CLI seats (C2.3)
    kimi_seats: list = []          # local kimi-code CLI seats (K2)
    seats_desc: list = []          # readable seat labels (raw / diagnostics)
    lenses_used: list = []         # [{"seat","lens"}] for lensed seats — raw surface

    def _add_provider_seat(base: str, lens_name: str) -> None:
        # F12: a cross-lab provider may appear MORE THAN ONCE in a panel (e.g. 3×
        # glm, each a distinct sample or lens — the provider analogue of duplicate
        # Claude seats). Give every seat a UNIQUE key (glm, glm#2, glm#3) so the
        # name-keyed fan-out (the per-seat providers/lenses maps AND each answer's
        # `name`) never collapses two seats into one. The provider CONFIG —
        # script/model/prices — is resolved from the base registry name; only the
        # SEAT identity is suffixed, so pricing and the tab body stay correct.
        prov_seat_counts[base] = prov_seat_counts.get(base, 0) + 1
        key = base if prov_seat_counts[base] == 1 else f"{base}#{prov_seat_counts[base]}"
        prov_names.append(key)
        prov_providers[key] = providers[base]
        seats_desc.append(key + (f" [lens:{lens_name}]" if lens_name else ""))
        if lens_name:
            prov_lenses[key] = config.resolve_lens(lens_name, lenses_cfg)
            prov_lens_names[key] = lens_name
            lenses_used.append({"seat": key, "lens": lens_name})

    for s in panel:
        if isinstance(s, dict) and s.get("kind") == "claude_cli":
            if not claude_ok:
                continue
            cs_model = (s.get("model") or "opus").strip()
            cs_effort = (s.get("effort") or "high").strip()
            lens_name = (s.get("lens") or "").strip()
            name = f"{cs_model}-{cs_effort}"
            claude_seats.append({"model": cs_model, "effort": cs_effort, "name": name,
                                 "lens": lens_name,
                                 "lens_text": config.resolve_lens(lens_name, lenses_cfg)})
            seats_desc.append(f"{cs_model}-{cs_effort} (cli)"
                              + (f" [lens:{lens_name}]" if lens_name else ""))
            if lens_name:
                lenses_used.append({"seat": name, "lens": lens_name})
        elif isinstance(s, dict) and s.get("kind") == "codex_cli":
            # C2.3: a THIRD seat kind, ADDITIVE — the claude_cli branch above is
            # untouched. A local codex seat ($0 subscription, NO OpenAI API), run
            # like the Claude seat but via run_codex_json. Gated on codex_ok (PATH
            # + auth probe), so a logged-out/absent codex is silently skipped (the
            # <2-seat fallback then handles it). Codex divergences from the Claude
            # seat: model defaults to DEFAULT_CODEX_MODEL; effort defaults to ""
            # (codex's own model default — no injected 'high'); and an empty-effort
            # name omits the trailing '-'.
            if not codex_ok:
                continue
            cs_model = (s.get("model") or DEFAULT_CODEX_MODEL).strip()
            cs_effort = (s.get("effort") or "").strip()
            lens_name = (s.get("lens") or "").strip()
            name = f"{cs_model}-{cs_effort}" if cs_effort else cs_model
            codex_seats.append({"model": cs_model, "effort": cs_effort, "name": name,
                                "lens": lens_name,
                                "lens_text": config.resolve_lens(lens_name, lenses_cfg)})
            seats_desc.append(f"{name} (codex)"
                              + (f" [lens:{lens_name}]" if lens_name else ""))
            if lens_name:
                lenses_used.append({"seat": name, "lens": lens_name})
        elif isinstance(s, dict) and s.get("kind") == "kimi_cli":
            # K2: a FOURTH seat kind, ADDITIVE — the branches above are untouched. A
            # local kimi-code seat ($0 subscription, NO billed API), run like the codex
            # seat but via run_kimi_json. Gated on kimi_ok (PATH + OAuth probe), so a
            # logged-out/absent kimi is silently skipped (the <2-seat fallback then
            # handles it). kimi-code has NO reasoning effort (§4), so — unlike codex —
            # there is no effort field at all.
            if not kimi_ok:
                continue
            ks_model = (s.get("model") or DEFAULT_KIMI_MODEL).strip()
            lens_name = (s.get("lens") or "").strip()
            name = ks_model
            kimi_seats.append({"model": ks_model, "name": name, "lens": lens_name,
                               "lens_text": config.resolve_lens(lens_name, lenses_cfg)})
            seats_desc.append(f"{name} (kimi)"
                              + (f" [lens:{lens_name}]" if lens_name else ""))
            if lens_name:
                lenses_used.append({"seat": name, "lens": lens_name})
        elif isinstance(s, str) and s in active:
            _add_provider_seat(s, "")
        elif (isinstance(s, dict) and isinstance(s.get("name"), str)
              and s["name"].strip() in active):
            # External seat carrying a lens: {"name": <provider>, "lens": ...}.
            _add_provider_seat(s["name"].strip(), (s.get("lens") or "").strip())
    total = len(prov_names) + len(claude_seats) + len(codex_seats) + len(kimi_seats)
    if total < 2:
        return ClaudeRun(ok=False,
                         error=f"fusion: need >=2 usable panel seats, have {total}")

    cwd_eff = cwd or os.getcwd()
    if prov_names:
        spawn.ensure_fusion_providers()                # materialize scripts (lazy)

    # Fan out ALL groups in parallel: external providers through the watchable
    # fusion tab (in-process fallback if no iTerm2), each Claude seat AND each codex
    # seat as its own watchable tab. Wall-clock ~= slowest seat, not the sum. (iTerm2
    # tab CREATION is serialized by a lock in spawn.py so concurrent spawns don't
    # race; only the spawn moment is serial — the polling overlaps.) codex_seats are
    # in max_workers too, else a pure-codex pair would run serially.
    answers: list = []
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=(len(claude_seats) + len(codex_seats) + len(kimi_seats)
                         + (1 if prov_names else 0)) or 1) as ex:
        prov_future = (ex.submit(_panel_answers, prompt, prov_names, prov_providers,
                                 timeout_s, cwd_eff, prov_lenses) if prov_names else None)
        claude_futures = [ex.submit(_anthropic_seat_answer, cs, prompt, cwd_eff)
                          for cs in claude_seats]
        codex_futures = [ex.submit(_codex_seat_answer, cs, prompt, cwd_eff)
                         for cs in codex_seats]
        kimi_futures = [ex.submit(_kimi_seat_answer, ks, prompt, cwd_eff)
                        for ks in kimi_seats]
        if prov_future is not None:
            try:
                answers.extend(prov_future.result() or [])
            except Exception as e:
                print(f"[claude_runner] fusion provider group failed: {e}")
        for fut in claude_futures:
            try:
                answers.append(fut.result())
            except Exception as e:
                print(f"[claude_runner] fusion claude seat failed: {e}")
        for fut in codex_futures:
            try:
                answers.append(fut.result())
            except Exception as e:
                print(f"[claude_runner] fusion codex seat failed: {e}")
        for fut in kimi_futures:
            try:
                answers.append(fut.result())
            except Exception as e:
                print(f"[claude_runner] fusion kimi seat failed: {e}")

    # F8.4: tag each external answer with its lens NAME for the surface/breakdown
    # (Claude seats already carry their own "lens" from _anthropic_seat_answer).
    for a in answers:
        if isinstance(a, dict) and not a.get("lens"):
            a["lens"] = prov_lens_names.get(a.get("name"), "")

    ok = [a for a in answers if a.get("ok")]
    if len(ok) < 2:
        errs = "; ".join(f"{a.get('name')}: {a.get('error')}"
                         for a in answers if not a.get("ok"))
        return ClaudeRun(ok=False,
                         error=f"fusion panel: only {len(ok)} seat(s) answered ({errs})")

    # C3: the judge / verifier / re-judge engine is SELECTABLE (claude | codex).
    # The map is built HERE, not at module level: a module-level
    # `{"claude": run_claude_json, ...}` literal would bind the function OBJECTS at
    # import, so the tests' mock.patch.object(claude_runner, "run_claude_json")
    # wouldn't reach it and every fusion call would fire a REAL tab. Building it
    # in-function resolves each name from the module namespace at CALL TIME, so
    # monkeypatching still intercepts. An unknown engine fails SAFE to claude
    # (reversible, no surprise tab) — and keeps the "never raises" contract.
    engine_fn = {"claude": run_claude_json,
                 "codex": run_codex_json}.get(judge_engine, run_claude_json)
    # judge_model/verify_model default to "opus" (a Claude id). With the codex
    # engine, feeding that to `codex -m` is the dispatch #3 'no silent downgrade'
    # trap — resolve a codex model instead. C4: that model is the config SEED's,
    # read from the ALREADY-loaded `cfg` (cfg["codex"]["model"], no extra file read)
    # so a config.json `fusion.codex.model` override wins; it falls back to the seed
    # (DEFAULT_CODEX_MODEL) when unset. (C5 DECLINED a per-CALL codex judge model: no
    # dispatch surface selects one — C5 ships the executor engine + codex seats, not
    # a per-dispatch judge — so the merged-config model stays the single source of
    # truth; override it via config.json `fusion.codex.model`.)
    codex_model = (cfg.get("codex") or {}).get("model") or DEFAULT_CODEX_MODEL
    judge_engine_model = codex_model if judge_engine == "codex" else judge_model
    verify_engine_model = codex_model if judge_engine == "codex" else verify_model

    # The judge synthesizes from the ORIGINAL prompt verbatim — lenses bias only
    # the panel seats (decorrelation), never the synthesis (§5 / F9.e). C3: routed
    # through engine_fn with ONLY the kwargs BOTH engines accept (prompt/cwd/model/
    # effort/label — never max_turns; run_codex_json has no such param).
    judge = engine_fn(prompt=_judge_prompt(prompt, ok), cwd=cwd_eff,
                      model=judge_engine_model, effort=judge_effort, label="fusion-judge")
    judge.cost_usd = sum(a.get("cost", 0.0) for a in ok)   # out-of-pocket = external seats only

    # F11.c.1: opt-in VERIFIER seat. A $0 local-CLI critic checks the judge's
    # synthesis against the panel; on a concrete defect it triggers ONE re-judge
    # (also $0). Fail-safe at every step — a verifier shortfall NEVER worsens or fails
    # the result, it just keeps the original synthesis. cost_usd is untouched (verifier
    # + re-judge are subscription, NO Anthropic API), so the §2 cost rule holds.
    verifier_info: Optional[dict] = None
    if verify and judge.ok:
        verifier_info = {"ran": True, "defect": False, "rejudged": False, "issues": []}
        vrun = engine_fn(prompt=_verify_prompt(prompt, judge.text, ok),
                         cwd=cwd_eff, model=verify_engine_model, effort=verify_effort,
                         label="fusion-verify")
        verdict = vrun.parsed_json if (vrun.ok and isinstance(vrun.parsed_json, dict)) else None
        if verdict is None and vrun.ok:                    # parsed_json may be None; retry
            try:
                verdict = json.loads(_strip_fences(vrun.text or ""))
            except (ValueError, TypeError):
                verdict = None
        if isinstance(verdict, dict) and verdict.get("defect"):
            issues = verdict.get("issues") or []
            if not isinstance(issues, list):
                issues = [str(issues)]
            issues = [str(i).strip() for i in issues if str(i).strip()][:10]
            verifier_info.update(defect=True, issues=issues)
            rejudge = engine_fn(
                prompt=_rejudge_prompt(prompt, ok, judge.text, issues),
                cwd=cwd_eff, model=judge_engine_model, effort=judge_effort,
                label="fusion-rejudge")
            if rejudge.ok and (rejudge.text or "").strip():
                judge.text = rejudge.text                  # corrected synthesis
                judge.parsed_json = rejudge.parsed_json
                verifier_info["rejudged"] = True
            # else: re-judge fell short → keep the original synthesis (fail-safe).

    judge.raw = {"panel": answers, "preset": preset, "seats": seats_desc,
                 "lenses": lenses_used}
    if verifier_info is not None:
        judge.raw["verifier"] = verifier_info
    return judge


def run_brain_json(prompt: str, cwd: str, fusion: bool = False,
                   model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT,
                   max_turns: int = DEFAULT_MAX_TURNS, label: str = "brain",
                   **kw) -> ClaudeRun:
    """Single entry point for brain calls. Routes through Fusion when requested
    AND available; otherwise — or if the panel comes up short — the standard
    visible-tab claude call, so a flaky panel never hard-fails a dispatch.

    `model`/`effort`/`max_turns`/`label` govern the NON-fusion (and fallback)
    claude call (`label` titles its tab); the fusion judge uses
    `judge_model`/`judge_effort` (default opus/high) forwarded via **kw alongside
    preset/panel/timeout_s."""
    if fusion:
        run = run_fusion_json(prompt=prompt, cwd=cwd, **kw)
        if run.ok:
            return run
        print(f"[claude_runner] fusion unavailable ({run.error}); falling back to claude")
    return run_claude_json(prompt=prompt, cwd=cwd, model=model, effort=effort,
                           max_turns=max_turns, label=label)
