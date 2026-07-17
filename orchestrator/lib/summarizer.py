"""Summarizer — distill a Stop-hook transcript into a structured summary
via a visible-tab `claude` brain call, then write to the outcomes table.

Runs as a background asyncio task fired from /api/complete. Failures are
logged but never break the completion flow."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.lib import claude_runner

log = logging.getLogger("orchestrator.summarizer")

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "SUMMARIZER.md"

# Caps so we don't blow up claude's context with huge transcripts.
DISTILLED_MAX_CHARS = 30_000
PER_BLOCK_MAX = 1_500
PER_TOOL_INPUT_MAX = 300


@dataclass
class SummaryResult:
    ok: bool
    summary_md: str = ""
    what_worked: str = ""
    what_broke: str = ""
    lessons: str = ""
    tags: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    duration_s: float = 0.0
    model: str = ""
    error: str = ""
    raw_assistant_text: str = ""


def _fill_template(template: str, values: dict[str, str]) -> str:
    """Single-pass placeholder substitution — same approach as rewriter to
    avoid recursive expansion of {transcript} or {user_task} inside values."""
    pattern = re.compile(r"\{(" + "|".join(re.escape(k) for k in values) + r")\}")
    return pattern.sub(lambda m: values[m.group(1)], template)


def _trunc(s: str, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[:n] + "…[trunc]"


def _block_text(block) -> str:
    """Extract text from a content block (handles dict / list / str / None)."""
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        if block.get("type") == "text":
            return block.get("text", "")
        if block.get("type") == "tool_result":
            inner = block.get("content", "")
            if isinstance(inner, list):
                return " ".join(_block_text(b) for b in inner)
            return str(inner) if inner else ""
        return block.get("text", "") or ""
    if isinstance(block, list):
        return " ".join(_block_text(b) for b in block)
    return ""


def distill_transcript(transcript_path: str, max_chars: int = DISTILLED_MAX_CHARS) -> str:
    """Read the JSONL transcript and produce a clean markdown distillation
    of just the conversation flow (user/assistant text + tool calls + tool
    results). Drops noise (file-history-snapshot, attachments, thinking)."""
    p = Path(transcript_path)
    if not p.is_file():
        return f"[transcript file missing: {transcript_path}]"

    blocks: list[str] = []
    total = 0
    truncated = False

    with p.open(encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue

            ttype = obj.get("type")
            if ttype == "user":
                content = obj.get("message", {}).get("content", "")
                if isinstance(content, str):
                    text = content
                    if text.strip():
                        blocks.append(f"### USER\n{_trunc(text, PER_BLOCK_MAX)}")
                elif isinstance(content, list):
                    # Mixed user content — usually tool_result entries
                    for blk in content:
                        if not isinstance(blk, dict):
                            continue
                        if blk.get("type") == "tool_result":
                            tr = _block_text(blk)
                            if tr.strip():
                                blocks.append(f"### TOOL_RESULT\n{_trunc(tr, PER_BLOCK_MAX)}")
                        elif blk.get("type") == "text":
                            t = blk.get("text", "")
                            if t.strip():
                                blocks.append(f"### USER\n{_trunc(t, PER_BLOCK_MAX)}")

            elif ttype == "assistant":
                content = obj.get("message", {}).get("content", [])
                # Some claude API responses use a plain string for content
                # instead of a list of blocks. Handle both shapes so we don't
                # silently drop assistant text.
                if isinstance(content, str):
                    if content.strip():
                        blocks.append(f"### ASSISTANT\n{_trunc(content, PER_BLOCK_MAX)}")
                elif isinstance(content, list):
                    for blk in content:
                        if not isinstance(blk, dict):
                            continue
                        bt = blk.get("type")
                        if bt == "text":
                            t = blk.get("text", "")
                            if t.strip():
                                blocks.append(f"### ASSISTANT\n{_trunc(t, PER_BLOCK_MAX)}")
                        elif bt == "tool_use":
                            name = blk.get("name", "?")
                            inp = blk.get("input", {})
                            try:
                                inp_str = json.dumps(inp, default=str)
                            except Exception:
                                inp_str = str(inp)
                            blocks.append(f"### TOOL_USE: {name}\n{_trunc(inp_str, PER_TOOL_INPUT_MAX)}")
                        # Intentionally skip 'thinking' — too verbose for summaries.

            # C6 (note 4): a codex EXECUTOR transcript is the codex `exec --json` sidecar,
            # NOT a claude Stop-hook JSONL — different schema (CODEX_PLAN.md §C6.0). This
            # ADDITIVE branch distills it to the SAME markdown the claude path produces, so
            # a codex dispatch gets a real (non-empty) summary through the unchanged
            # summarizer pipeline. Chosen over translating the sidecar / skipping the
            # summary because it's the most reversible (one additive branch, delete to
            # revert) and never emits an empty summary. Keyed on `item.completed` only (not
            # `item.started`) so each tool call appears once. Claude transcripts never carry
            # these `type`s, so the claude path above is byte-for-byte unchanged.
            elif ttype == "item.completed":
                item = obj.get("item") or {}
                itype = item.get("type")
                if itype == "agent_message":
                    t = item.get("text", "")
                    if t.strip():
                        blocks.append(f"### ASSISTANT\n{_trunc(t, PER_BLOCK_MAX)}")
                elif itype == "command_execution":
                    cmd = item.get("command", "")
                    blocks.append(f"### TOOL_USE: command_execution\n{_trunc(cmd, PER_TOOL_INPUT_MAX)}")
                    out = item.get("aggregated_output", "")
                    if str(out).strip():
                        blocks.append(f"### TOOL_RESULT\n{_trunc(out, PER_BLOCK_MAX)}")
                elif itype == "file_change":
                    changes = item.get("changes") or []
                    paths = ", ".join(f"{c.get('kind', '')} {c.get('path', '')}"
                                      for c in changes if isinstance(c, dict))
                    blocks.append(f"### TOOL_USE: file_change\n{_trunc(paths, PER_TOOL_INPUT_MAX)}")

            # K5: a kimi EXECUTOR transcript is kimi-code's `-p --output-format stream-json`
            # sidecar — ROLE-based (assistant/tool/user), NOT claude's `type` or codex's item
            # events. This ADDITIVE branch distills it to the SAME markdown. Keyed on `role`
            # (kimi lines carry no top-level `type`), so the claude/codex paths are unchanged.
            elif obj.get("role") in ("assistant", "tool", "user"):
                role, content = obj.get("role"), obj.get("content")
                if role == "assistant":
                    if isinstance(content, str) and content.strip():
                        blocks.append(f"### ASSISTANT\n{_trunc(content, PER_BLOCK_MAX)}")
                    for tc in (obj.get("tool_calls") or []):
                        if not isinstance(tc, dict):
                            continue
                        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                        name = fn.get("name", "?")
                        args = str(fn.get("arguments", ""))
                        blocks.append(f"### TOOL_USE: {name}\n{_trunc(args, PER_TOOL_INPUT_MAX)}")
                elif role == "tool":
                    if isinstance(content, str) and content.strip():
                        blocks.append(f"### TOOL_RESULT\n{_trunc(content, PER_BLOCK_MAX)}")
                elif role == "user":
                    if isinstance(content, str) and content.strip():
                        blocks.append(f"### USER\n{_trunc(content, PER_BLOCK_MAX)}")

            # Skip permission-mode, file-history-snapshot, attachment, ai-title.

            total = sum(len(b) for b in blocks)
            if total > max_chars:
                truncated = True
                break

    if truncated:
        blocks.append(f"\n[... transcript truncated at {max_chars:,} chars to keep summarizer prompt bounded]")
    if not blocks:
        return "[no conversational content found in transcript]"
    return "\n\n".join(blocks)


def _coerce_list_of_str(v) -> list[str]:
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, list):
        return [str(x) for x in v if isinstance(x, (str, int, float))]
    return []


def summarize(transcript_path: str, user_task: str, cwd: str,
              fusion: bool = False, panel: "list | None" = None) -> SummaryResult:
    """Run the summarizer end-to-end. Never raises — returns a SummaryResult
    with ok=False on any failure so the caller can log without breaking.

    F6.1: with fusion=False this is byte-for-byte the original single-claude
    (sonnet/medium) path. fusion=True routes the one brain call through a
    multi-model panel → judge, degrading to that same single-claude call if the
    panel is unavailable. The tier stays DELIBERATELY low (sonnet/medium, judge
    included) — a transcript distillation rarely justifies an Opus panel (§7)."""
    transcript_md = distill_transcript(transcript_path)
    if transcript_md.startswith("[transcript file missing"):
        return SummaryResult(ok=False, error="transcript file missing")

    template = PROMPT_PATH.read_text()
    prompt = _fill_template(template, {
        "transcript": transcript_md,
        "user_task": user_task or "(unknown — not recorded)",
    })

    run = claude_runner.run_brain_json(
        prompt=prompt, cwd=cwd, fusion=fusion, panel=panel,
        model="sonnet", effort="medium", label="summarizer",
        judge_model="sonnet", judge_effort="medium",
    )
    if not run.ok:
        return SummaryResult(ok=False, error=run.error,
                             cost_usd=run.cost_usd, model=run.model)

    data = run.parsed_json
    if not isinstance(data, dict):
        return SummaryResult(
            ok=False, error="summarizer returned non-JSON",
            raw_assistant_text=run.text[:2000],
            cost_usd=run.cost_usd, duration_s=run.duration_s, model=run.model,
        )

    return SummaryResult(
        ok=True,
        summary_md=str(data.get("summary_md", "")).strip(),
        what_worked=str(data.get("what_worked", "")).strip(),
        what_broke=str(data.get("what_broke", "")).strip(),
        lessons=str(data.get("lessons", "")).strip(),
        tags=_coerce_list_of_str(data.get("tags")),
        cost_usd=run.cost_usd,
        duration_s=run.duration_s,
        model=run.model,
    )
