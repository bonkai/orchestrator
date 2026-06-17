"""Rewriter — takes user task + project bundle, asks `claude` (visible iTerm2 tab) to
produce a richer prompt, returns structured result for the preview UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from orchestrator.lib import bundle as bundle_mod
from orchestrator.lib import claude_runner, edits as edits_mod, retrieval

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "REWRITER.md"


def _fill_template(template: str, values: dict[str, str]) -> str:
    """Single-pass {key} substitution. Each placeholder is replaced exactly
    once with the literal value — values that themselves contain `{key}`
    text are NOT recursively expanded.
    """
    import re
    pattern = re.compile(r"\{(" + "|".join(re.escape(k) for k in values) + r")\}")
    return pattern.sub(lambda m: values[m.group(1)], template)


@dataclass
class ProposedEditView:
    """A proposed edit + whether it passed validation. Surfaced to the UI."""
    action: str
    path: str
    content: str
    rationale: str
    valid: bool
    validation_error: str = ""


@dataclass
class RewriteResult:
    ok: bool
    rewritten_prompt: str = ""
    rationale: str = ""
    files_to_read: list[str] = field(default_factory=list)
    hazards_acknowledged: list[str] = field(default_factory=list)
    proposed_edits: list[ProposedEditView] = field(default_factory=list)
    cost_usd: float = 0.0
    duration_s: float = 0.0
    model: str = ""
    error: str = ""
    raw_assistant_text: str = ""   # for debugging when parse fails
    bundle_chars: int = 0
    similar_hits: list = field(default_factory=list)  # retrieval.Hit objects, for UI display


def _coerce_list_of_str(v) -> list[str]:
    """Defensive: model occasionally returns a single string instead of a list."""
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, list):
        return [str(x) for x in v if isinstance(x, (str, int, float))]
    return []


def rewrite(user_task: str, project_path: str,
            fusion: bool = False, panel: Optional[list] = None) -> RewriteResult:
    """Build the project bundle, ask claude to rewrite the task, return result.

    fusion=False is byte-for-byte the original single-claude path. fusion=True
    routes the ONE rewrite brain call through run_brain_json (multi-model panel →
    judge), degrading to the same single-claude call if the panel is unavailable.
    `panel` (provider names) overrides the configured preset for this call. The
    auto-retry below always stays single-model — it never re-fans-out the panel."""
    user_task = (user_task or "").strip()
    if not user_task:
        return RewriteResult(ok=False, error="empty task")
    project = Path(project_path).expanduser()
    if not project.is_dir():
        return RewriteResult(ok=False, error=f"project path does not exist: {project}")

    pack = bundle_mod.build_bundle(str(project))
    bundle_md = pack.to_markdown()
    # Phase 6: pull semantically similar past tasks across all projects.
    # Failures (Ollama down, no embeddings yet) return [] silently.
    hits = retrieval.find_similar(user_task, k=5)
    similar_md = retrieval.render_hits_for_prompt(hits) or "(no similar past tasks indexed yet)"

    template = PROMPT_PATH.read_text()
    # Single-pass placeholder fill — values containing literal `{key}` text
    # are NOT recursively expanded (see _fill_template comment).
    prompt = _fill_template(template, {
        "bundle": bundle_md,
        "user_task": user_task,
        "similar_tasks": similar_md,
    })

    run = claude_runner.run_brain_json(prompt=prompt, cwd=str(project), fusion=fusion,
                                       panel=panel, model="opus", effort="high",
                                       label="rewriter")
    if not run.ok:
        return RewriteResult(ok=False, error=run.error,
                             cost_usd=run.cost_usd, model=run.model,
                             bundle_chars=pack.total_chars)

    data = run.parsed_json
    rewritten = (str(data.get("rewritten_prompt", "")).strip()
                 if isinstance(data, dict) else "")

    # Auto-retry once when the first call returned prose or an empty prompt.
    # Most "silent fallback to original task" failures are the model wrapping
    # JSON in prose despite the instructions; a stricter reminder usually fixes
    # it without us having to inspect logs.
    retry_cost = 0.0
    retry_duration = 0.0
    if not isinstance(data, dict) or not rewritten:
        first_preview = (run.text or "")[:400]
        retry_prompt = (
            prompt
            + "\n\n# RETRY — your previous response could not be used\n\n"
            + "Your previous output was not parseable as the required JSON object, "
            + "or `rewritten_prompt` was empty. Reply with ONLY a single JSON object "
            + "matching the schema above — no prose before or after, no markdown "
            + "fences, no preamble, no commentary. The very first character of your "
            + "response MUST be `{` and the last MUST be `}`.\n\n"
            + f"For reference, the start of your previous (rejected) reply was:\n{first_preview}"
        )
        # F2.2: the retry is ALWAYS a single model (run_claude_json directly),
        # never the panel — a flaky/parse-failing fusion call must not re-fan-out.
        retry = claude_runner.run_claude_json(prompt=retry_prompt, cwd=str(project), label="rewriter",
                                              model="opus", effort="high")
        retry_cost = retry.cost_usd
        retry_duration = retry.duration_s
        if retry.ok and isinstance(retry.parsed_json, dict):
            retry_rewritten = str(retry.parsed_json.get("rewritten_prompt", "")).strip()
            if retry_rewritten:
                data = retry.parsed_json
                rewritten = retry_rewritten
                run = retry  # so downstream uses retry's text / model

    total_cost = run.cost_usd + retry_cost
    total_duration = run.duration_s + retry_duration

    if not isinstance(data, dict):
        return RewriteResult(
            ok=False,
            error="model returned non-JSON (or extra prose) — retry also failed",
            raw_assistant_text=run.text[:2000],
            cost_usd=total_cost, duration_s=total_duration, model=run.model,
            bundle_chars=pack.total_chars,
        )

    if not rewritten:
        # Fall back to the original task with a note — never lose the user's intent
        return RewriteResult(
            ok=False,
            error="model returned empty rewritten_prompt — retry also failed",
            rewritten_prompt=user_task,
            raw_assistant_text=run.text[:2000],
            cost_usd=total_cost, duration_s=total_duration, model=run.model,
            bundle_chars=pack.total_chars,
        )

    # Parse proposed_edits — pre-validate each so the UI can show invalid
    # ones disabled (rather than letting the user check, click apply, fail).
    raw_edits = data.get("proposed_edits")
    edit_views: list[ProposedEditView] = []
    if isinstance(raw_edits, list):
        for raw in raw_edits:
            if not isinstance(raw, dict):
                continue
            view = ProposedEditView(
                action=str(raw.get("action", "")),
                path=str(raw.get("path", "")),
                content=str(raw.get("content", "")),
                rationale=str(raw.get("rationale", "")),
                valid=False,
            )
            proposal = edits_mod.EditProposal(
                action=view.action, path=view.path,
                content=view.content, rationale=view.rationale,
            )
            ok_v, err = edits_mod.validate(proposal, project_path)
            view.valid = ok_v
            view.validation_error = err
            edit_views.append(view)

    return RewriteResult(
        ok=True,
        rewritten_prompt=rewritten,
        rationale=str(data.get("rationale", "")).strip(),
        files_to_read=_coerce_list_of_str(data.get("files_to_read")),
        hazards_acknowledged=_coerce_list_of_str(data.get("hazards_acknowledged")),
        proposed_edits=edit_views,
        cost_usd=total_cost,
        duration_s=total_duration,
        model=run.model,
        bundle_chars=pack.total_chars,
        similar_hits=hits,
    )
