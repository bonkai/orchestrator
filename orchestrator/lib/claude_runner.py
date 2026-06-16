"""Headless `claude -p` invoker for orchestrator's brain calls (rewriter,
summarizer). Vendored + simplified from verse_sites/pipeline_lib.py.

Key differences vs the verse_sites version:
- No streaming UI (we want a single JSON result, not console output).
- Sync subprocess.run with timeout — kept simple.
- No global console / rich dependencies.
- Returns (result_dict, error) so callers can handle failures explicitly.

The Stop hook in ~/.claude/settings.json is env-gated on ORCHESTRATOR_RUN_ID,
which we do NOT set for these brain calls — so internal claude invocations
don't pollute the dispatch log.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Optional


DEFAULT_MODEL = "sonnet"
DEFAULT_EFFORT = "medium"
DEFAULT_MAX_TURNS = 30
DEFAULT_TIMEOUT_S = 900


@dataclass
class ClaudeRun:
    """Result of a single headless `claude -p` invocation."""
    ok: bool
    text: str = ""              # the assistant's final text output
    parsed_json: Optional[dict] = None   # populated if text was JSON-parseable
    cost_usd: float = 0.0
    duration_s: float = 0.0
    model: str = ""
    error: str = ""             # populated if ok == False
    raw: Optional[dict] = None  # full JSON envelope from claude -p


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


def run_claude_json(
    prompt: str,
    cwd: str,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    max_turns: int = DEFAULT_MAX_TURNS,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> ClaudeRun:
    """Run `claude -p` headlessly. If the assistant's output is JSON,
    parses it into `parsed_json`. Never raises — returns a ClaudeRun with
    `ok=False` and `error` set on any failure (timeout, nonzero exit, bad JSON)."""

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

    text = envelope.get("result", "") or ""
    cost = float(envelope.get("total_cost_usd") or envelope.get("cost_usd") or 0.0)
    duration = float(envelope.get("duration_ms", 0)) / 1000.0
    resolved_model = envelope.get("model") or (envelope.get("message") or {}).get("model") or model

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
