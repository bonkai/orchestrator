"""F7 — multi-model ENRICHMENT mode (optional, opt-in).

Instead of REPLACING the rewrite (that's rewriter.py — the drop-in panel→judge
that AUTHORS the prompt), enrichment runs a panel purely to *reason about* the
task and appends its synthesis to the executor's prompt as a fenced
"## Multi-model analysis" block. The executor weighs it as context, not gospel —
so with a panel of strong-but-non-frontier models this "inject disagreement as
context" mode is often safer than trusting the panel to write the final artifact
(FUSION_PLAN.md §3b).

The panel + judge run through the SAME machinery as run_fusion_json (visible
fusion tab for the providers, a visible brain tab for the judge), so every hard
rule the rest of Fusion honors holds here too.

`enrich()` NEVER raises: on ANY failure it returns FusionResult(ok=False) and the
caller leaves the prompt unchanged — a flaky panel must never abort a dispatch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from orchestrator.lib import claude_runner

# Cap how much of the (already-built) prompt we feed the analysis panel — mirrors
# embeddings.MAX_INPUT_CHARS. The prompt can carry a large bundle; the panel only
# needs enough task context to reason, and each extra char is billed per seat.
MAX_INPUT_CHARS = 12_000

# The analysis schema the judge must emit — each value an array of short strings.
ANALYSIS_KEYS = ["consensus", "contradictions", "partial_coverage",
                 "unique_insights", "blind_spots"]
_TITLES = {
    "consensus": "Consensus",
    "contradictions": "Contradictions / trade-offs",
    "partial_coverage": "Partial coverage",
    "unique_insights": "Unique insights",
    "blind_spots": "Blind spots / risks",
}


@dataclass
class FusionResult:                   # distinct from claude_runner.ClaudeRun
    ok: bool
    analysis: Optional[dict] = None   # {consensus, contradictions, partial_coverage,
                                      #  unique_insights, blind_spots} → list[str]
    enrichment_md: str = ""           # rendered "## Multi-model analysis" block
    panel_models: list = field(default_factory=list)
    cost_usd: float = 0.0
    error: str = ""


_ANALYSIS_PROMPT = '''You are analyzing a software task BEFORE an autonomous coding agent executes it. Do NOT solve, plan, or rewrite the task — only analyze how one should think about it.

TASK:
"""
{task}
"""

Respond with ONLY a single JSON object — no prose, no markdown fences, the first character `{{` and the last `}}`. It must have exactly these keys, each mapping to an array of short strings (use [] when you have nothing for a key):
- "consensus": points most reasonable analysts would agree on about approaching this.
- "contradictions": where sensible approaches conflict or trade off against each other.
- "partial_coverage": aspects an obvious approach addresses only partly — easy to under-do.
- "unique_insights": non-obvious observations a single pass would likely miss.
- "blind_spots": risks, edge cases, or hidden assumptions worth flagging.'''


def _coerce_list(v) -> list:
    """Normalize one analysis value to a clean list[str]. The judge occasionally
    returns a bare string or non-string items — keep this defensive (mirrors
    rewriter._coerce_list_of_str)."""
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    if isinstance(v, list):
        return [str(x).strip() for x in v if isinstance(x, (str, int, float)) and str(x).strip()]
    return []


def render_block(analysis: dict) -> str:
    """Render the analysis dict as a self-delimited markdown block appended to the
    executor's prompt. Empty sections are dropped so the block stays tight."""
    lines = ["## Multi-model analysis",
             "",
             "_A panel of models analyzed this task before dispatch. Treat it as "
             "context to weigh — not instructions to follow._",
             ""]
    for key in ANALYSIS_KEYS:
        items = analysis.get(key) or []
        if not items:
            continue
        lines.append(f"### {_TITLES[key]}")
        lines.extend(f"- {it}" for it in items)
        lines.append("")
    return "\n".join(lines).strip()


def enrich(prompt: str, project_path: str = "",
           panel: Optional[list] = None, preset: Optional[str] = None,
           timeout_s: Optional[int] = None,
           judge_model: str = "opus", judge_effort: str = "high",
           verify: bool = False,
           verify_model: str = "opus", verify_effort: str = "high") -> FusionResult:
    """Run the analysis panel + judge over `prompt`'s task and return a rendered
    "## Multi-model analysis" block. NEVER raises — any shortfall (panel
    unavailable, unparseable/empty analysis, crash) returns ok=False so the caller
    dispatches the un-enriched prompt. `panel`/`preset` pick the seats exactly as
    run_fusion_json does; an empty panel uses the configured preset.

    `judge_model`/`judge_effort` (and `verify_model`/`verify_effort` for the opt-in
    verifier) steer the enrich judge/verify/rejudge — the dispatch form points them
    at the OPTIONAL brain picker so enrichment runs on the brain model too. All
    default opus/high (today's behavior), so an omitting caller is unchanged."""
    try:
        task = (prompt or "").strip()
        if not task:
            return FusionResult(ok=False, error="empty prompt")

        analysis_prompt = _ANALYSIS_PROMPT.format(task=task[:MAX_INPUT_CHARS])
        run = claude_runner.run_fusion_json(
            prompt=analysis_prompt, cwd=project_path or "",
            panel=panel, preset=preset, timeout_s=timeout_s,
            judge_model=judge_model, judge_effort=judge_effort, verify=verify,
            verify_model=verify_model, verify_effort=verify_effort)
        if not run.ok:
            return FusionResult(ok=False, error=run.error or "fusion panel unavailable",
                                cost_usd=run.cost_usd)

        raw = run.raw if isinstance(run.raw, dict) else {}
        models = [a.get("name") or a.get("model") or "?"
                  for a in (raw.get("panel") or [])
                  if isinstance(a, dict) and a.get("ok")]

        analysis = None
        stripped = claude_runner._strip_fences(run.text or "")
        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            analysis = {k: _coerce_list(parsed.get(k)) for k in ANALYSIS_KEYS}

        if not analysis or not any(analysis.values()):
            return FusionResult(ok=False, error="analysis JSON unparseable or empty",
                                cost_usd=run.cost_usd, panel_models=models)

        return FusionResult(ok=True, analysis=analysis,
                            enrichment_md=render_block(analysis),
                            panel_models=models, cost_usd=run.cost_usd)
    except Exception as e:                          # never propagate to the dispatch
        return FusionResult(ok=False, error=f"enrich crashed: {e}")
