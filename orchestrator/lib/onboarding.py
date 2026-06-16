"""Project onboarding — one-time sweep of an existing project that produces
an analysis of what's there, what's missing for orchestrator integration,
and proposed phase-8-style edits the user can apply.

Reads only existing rule files / structure — never writes. Application
happens through the same `/apply_edits` endpoint as phase 8, so all the
path-traversal / layout-validation hardening applies for free.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.lib import bundle as bundle_mod
from orchestrator.lib import claude_runner, edits as edits_mod

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "ONBOARDING.md"

# Per-file read cap so a huge .cursorrules can't blow the analyzer prompt
PER_FILE_CHARS = 5_000
# Hard caps so a project with many rule files / huge top-level can't
# explode the analyzer prompt
MAX_CURSOR_RULES = 20
MAX_TOP_LEVEL = 100

# Rule files we look for, in order of preference
RULE_FILE_PATTERNS = [
    "CLAUDE.md",
    ".cursorrules",                       # legacy cursor format
    "AGENTS.md",
    ".github/copilot-instructions.md",
    "README.md",                          # often has structure clues
]

# Tech stack signals — presence indicates a stack, content rarely needed
STACK_SIGNALS = [
    "package.json", "package-lock.json",
    "requirements.txt", "pyproject.toml", "setup.py", "Pipfile",
    "Cargo.toml",
    "go.mod",
    "Gemfile",
    "composer.json",
    "pom.xml", "build.gradle",
    "Dockerfile", "docker-compose.yml",
    ".python-version", ".node-version", ".ruby-version",
    "tsconfig.json",
]


@dataclass
class ProjectScan:
    """Everything the analyzer needs to know that isn't already in the bundle."""
    rule_files: dict[str, str] = field(default_factory=dict)   # path -> content
    cursor_rules_dir: list[tuple[str, str]] = field(default_factory=list)  # (rel_path, content)
    stack_signals: list[str] = field(default_factory=list)      # paths that exist
    has_forge_json: bool = False
    has_memory_dir: bool = False
    has_knowledge_dir: bool = False
    has_tasks_dir: bool = False
    memory_file_count: int = 0
    knowledge_file_count: int = 0
    task_file_count: int = 0
    top_level_entries: list[str] = field(default_factory=list)

    def to_prompt_section(self) -> str:
        """Render the scan as markdown for the analyzer prompt."""
        lines = ["## Existing rule files\n"]
        if not self.rule_files and not self.cursor_rules_dir:
            lines.append("*(none found — no CLAUDE.md, .cursorrules, AGENTS.md, README, etc.)*\n")
        for path, content in self.rule_files.items():
            lines.append(f"### `{path}`\n```\n{content}\n```\n")
        for path, content in self.cursor_rules_dir:
            lines.append(f"### `{path}` (cursor rules)\n```\n{content}\n```\n")

        lines.append("\n## Forge / orchestrator structure")
        lines.append(f"- `.forge.json` present: {self.has_forge_json}")
        lines.append(f"- `memory/` dir present: {self.has_memory_dir}"
                     + (f" ({self.memory_file_count} .md files)" if self.has_memory_dir else ""))
        lines.append(f"- `knowledge/` dir present: {self.has_knowledge_dir}"
                     + (f" ({self.knowledge_file_count} .md files)" if self.has_knowledge_dir else ""))
        lines.append(f"- `tasks/` dir present: {self.has_tasks_dir}"
                     + (f" ({self.task_file_count} .md files)" if self.has_tasks_dir else ""))

        lines.append("\n## Tech stack signals (files present)")
        if self.stack_signals:
            for s in self.stack_signals:
                lines.append(f"- `{s}`")
        else:
            lines.append("*(none — likely a notes/docs/data project, not a code project)*")

        lines.append("\n## Top-level layout")
        lines.append("```")
        lines.extend(self.top_level_entries[:40])
        if len(self.top_level_entries) > 40:
            lines.append(f"... ({len(self.top_level_entries)-40} more)")
        lines.append("```")
        return "\n".join(lines)


@dataclass
class Recommendation:
    """Manual recommendation — content the user can copy-paste; we don't
    auto-apply because the action is outside the safe phase-8 categories
    (e.g., creating CLAUDE.md or .forge.json at project root)."""
    title: str
    rationale: str
    target_path: str
    manual_content: str = ""


@dataclass
class OnboardingResult:
    ok: bool
    project_summary: str = ""
    strengths: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    recommendations: list[Recommendation] = field(default_factory=list)
    proposed_edits: list = field(default_factory=list)   # rewriter.ProposedEditView
    cost_usd: float = 0.0
    duration_s: float = 0.0
    model: str = ""
    error: str = ""
    raw_assistant_text: str = ""
    scan: ProjectScan | None = None
    # Populated by app._auto_apply_onboarding after analyze() returns. Shape:
    # {"applied": [entry, ...], "skipped": [entry, ...], "failed": [entry, ...]}
    # Each entry: {"kind": "edit"|"recommendation", "label": str, "path": str,
    #              "reason": str (skipped/failed only), "bytes": int (applied only)}
    apply_log: dict = field(default_factory=dict)
    # Set after db.save_onboarding_run by _run_onboard_job. Lets the polling
    # UI redirect to the permanent /project/<id>/onboard/run/<run_id> URL
    # so the result survives tab navigation and is reachable from history.
    run_id: int | None = None


def result_to_dict(result: "OnboardingResult") -> dict:
    """Serialize for the onboarding_runs table. Mirror of result_from_dict —
    keep field names stable; the JSON blob is read back into a dataclass."""
    return {
        "ok": result.ok,
        "project_summary": result.project_summary,
        "strengths": list(result.strengths),
        "gaps": list(result.gaps),
        "recommendations": [
            {"title": r.title, "rationale": r.rationale,
             "target_path": r.target_path, "manual_content": r.manual_content}
            for r in result.recommendations
        ],
        "proposed_edits": [
            {"action": e.action, "path": e.path, "content": e.content,
             "rationale": e.rationale, "valid": e.valid,
             "validation_error": getattr(e, "validation_error", "") or ""}
            for e in result.proposed_edits
        ],
        "cost_usd": result.cost_usd,
        "duration_s": result.duration_s,
        "model": result.model,
        "error": result.error,
        "raw_assistant_text": result.raw_assistant_text,
        "scan_text": result.scan.to_prompt_section() if result.scan else "",
        "apply_log": result.apply_log or {},
    }


def result_from_dict(data: dict) -> "OnboardingResult":
    """Inverse of result_to_dict. Used by the history detail view to
    re-render the same template without re-running the analyzer.

    Note: `scan_text` is a frozen string (the rendered analyzer scan, not a
    live ProjectScan dataclass) — the template only ever calls
    `result.scan.to_prompt_section()`, so we hand back a tiny shim that
    returns that string. The on-disk project state may have moved on since
    the run, which is the point: this view shows what we saw at run time.
    """
    from orchestrator.lib.rewriter import ProposedEditView

    recs = [
        Recommendation(
            title=str(r.get("title", "")),
            rationale=str(r.get("rationale", "")),
            target_path=str(r.get("target_path", "")),
            manual_content=str(r.get("manual_content", "")),
        )
        for r in data.get("recommendations", []) if isinstance(r, dict)
    ]
    edits: list = []
    for e in data.get("proposed_edits", []):
        if not isinstance(e, dict):
            continue
        v = ProposedEditView(
            action=str(e.get("action", "")),
            path=str(e.get("path", "")),
            content=str(e.get("content", "")),
            rationale=str(e.get("rationale", "")),
            valid=bool(e.get("valid", False)),
        )
        v.validation_error = str(e.get("validation_error", "") or "")
        edits.append(v)

    class _FrozenScan:
        """Tiny shim — the template only calls .to_prompt_section()."""
        def __init__(self, text: str):
            self._text = text
        def to_prompt_section(self) -> str:
            return self._text

    scan_text = str(data.get("scan_text", "") or "")
    scan_obj = _FrozenScan(scan_text) if scan_text else None

    return OnboardingResult(
        ok=bool(data.get("ok", False)),
        project_summary=str(data.get("project_summary", "")),
        strengths=[str(x) for x in data.get("strengths", []) if isinstance(x, (str, int, float))],
        gaps=[str(x) for x in data.get("gaps", []) if isinstance(x, (str, int, float))],
        recommendations=recs,
        proposed_edits=edits,
        cost_usd=float(data.get("cost_usd") or 0.0),
        duration_s=float(data.get("duration_s") or 0.0),
        model=str(data.get("model", "")),
        error=str(data.get("error", "")),
        raw_assistant_text=str(data.get("raw_assistant_text", "")),
        scan=scan_obj,
        apply_log=data.get("apply_log") or {},
    )


def _within_project(project_root: Path, candidate: Path) -> bool:
    """Same check bundle/edits use — must stay under project root after
    resolving symlinks. project_root must already be resolved."""
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return False
    try:
        resolved.relative_to(project_root)
        return True
    except ValueError:
        return False


def _read_capped_safe(project_root: Path, path: Path) -> str | None:
    """Read a file but refuse if it (or any ancestor) is a symlink that
    resolves outside the project root. Returns None to signal 'skip this
    file', not an empty string (which would still be reported).

    Why this matters: onboarder reads rule files and sends their content
    BOTH to claude (in the analyzer prompt) AND into the UI for display.
    A CLAUDE.md symlinked to /etc/passwd would leak in both places.
    """
    if not _within_project(project_root, path):
        return None
    try:
        text = path.read_text(errors="replace")
    except OSError as e:
        return f"[read error: {e}]"
    if len(text) > PER_FILE_CHARS:
        return text[:PER_FILE_CHARS] + "\n…[truncated]"
    return text


def _git_changes_since(project_root: Path, since_ts: int) -> str:
    """Compact summary of files touched in git since `since_ts` (Unix epoch).
    Returns a human-readable markdown block, or a note if git is unavailable."""
    try:
        result = subprocess.run(
            ["git", "log", f"--since=@{since_ts}", "--name-only", "--format=",
             "--diff-filter=ACDMR"],
            cwd=str(project_root),
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return "*(git unavailable for this project — cannot compute diff)*"
        raw = [f.strip() for f in result.stdout.splitlines() if f.strip()]
        if not raw:
            return "*(no git commits since last analysis — working tree may have uncommitted changes)*"
        # Deduplicate preserving first-seen order
        unique = list(dict.fromkeys(raw))
        total = len(unique)
        cap = 60
        shown = unique[:cap]
        lines = [f"**{total} file path(s) touched** in git commits since last analysis:"]
        for f in shown:
            lines.append(f"- `{f}`")
        if total > cap:
            lines.append(f"- *(… {total - cap} more — only first {cap} shown)*")
        return "\n".join(lines)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return "*(git unavailable for this project — cannot compute diff)*"


def scan_project(project_path: str) -> ProjectScan:
    """Walk the project root collecting what's already there. Read-only."""
    root = Path(project_path).expanduser().resolve()
    scan = ProjectScan()
    if not root.is_dir():
        return scan

    for rel in RULE_FILE_PATTERNS:
        p = root / rel
        if p.is_file():
            content = _read_capped_safe(root, p)
            if content is not None:
                scan.rule_files[rel] = content
            # else: symlink escaping the project — silently skip

    # Modern cursor rules: .cursor/rules/*.mdc — scan recursively, capped
    cursor_rules = root / ".cursor" / "rules"
    if cursor_rules.is_dir() and _within_project(root, cursor_rules):
        kept = 0
        for mdc in sorted(cursor_rules.rglob("*.mdc")):
            if kept >= MAX_CURSOR_RULES:
                break
            try:
                rel = str(mdc.relative_to(root))
            except ValueError:
                continue
            content = _read_capped_safe(root, mdc)
            if content is None:
                continue
            scan.cursor_rules_dir.append((rel, content))
            kept += 1

    for sig in STACK_SIGNALS:
        if (root / sig).exists():
            scan.stack_signals.append(sig)

    scan.has_forge_json = (root / ".forge.json").is_file()
    for dirname, hasattr_name, count_attr in [
        ("memory", "has_memory_dir", "memory_file_count"),
        ("knowledge", "has_knowledge_dir", "knowledge_file_count"),
        ("tasks", "has_tasks_dir", "task_file_count"),
    ]:
        d = root / dirname
        if d.is_dir():
            setattr(scan, hasattr_name, True)
            try:
                setattr(scan, count_attr, sum(1 for _ in d.rglob("*.md")))
            except OSError:
                pass

    try:
        for entry in sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if entry.name.startswith(".") and entry.name not in (".forge.json", ".cursorrules", ".cursor", ".github"):
                continue
            scan.top_level_entries.append(entry.name + ("/" if entry.is_dir() else ""))
            if len(scan.top_level_entries) >= MAX_TOP_LEVEL:
                scan.top_level_entries.append(f"… (more entries — capped at {MAX_TOP_LEVEL})")
                break
    except OSError:
        pass

    return scan


def _fill_template(template: str, values: dict[str, str]) -> str:
    """Same single-pass substitution as rewriter — prevents recursive
    expansion if a rule file happens to contain a literal placeholder."""
    import re
    pattern = re.compile(r"\{(" + "|".join(re.escape(k) for k in values) + r")\}")
    return pattern.sub(lambda m: values[m.group(1)], template)


def _coerce_list_of_str(v) -> list[str]:
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, list):
        return [str(x) for x in v if isinstance(x, (str, int, float))]
    return []


def analyze(project_path: str, prior_runs_context: str = "",
            last_run_ts: int | None = None) -> OnboardingResult:
    """Scan + call headless claude + parse → return OnboardingResult.

    `prior_runs_context` is a rendered summary of recent onboarding rounds.
    `last_run_ts` (Unix epoch) is the timestamp of the most recent prior run;
    when provided, a git-diff summary of changes since that run is injected
    into the analyzer prompt so re-runs focus on new/changed areas.
    """
    root = Path(project_path).expanduser().resolve()
    if not root.is_dir():
        return OnboardingResult(ok=False, error=f"project path not found: {root}")

    scan = scan_project(str(root))
    bundle_md = bundle_mod.build_bundle(str(root)).to_markdown()
    template = PROMPT_PATH.read_text()

    if last_run_ts is not None:
        git_changes = _git_changes_since(root, last_run_ts)
    else:
        git_changes = "*(first analysis — no previous run to diff against)*"

    prompt = _fill_template(template, {
        "scan": scan.to_prompt_section(),
        "bundle": bundle_md,
        "prior_runs": prior_runs_context or "*(no previous analyze-project rounds for this project)*",
        "git_changes": git_changes,
    })

    run = claude_runner.run_claude_json(prompt=prompt, cwd=str(root), effort="medium", label="onboarding")
    if not run.ok:
        return OnboardingResult(ok=False, error=run.error,
                                cost_usd=run.cost_usd, model=run.model, scan=scan)

    data = run.parsed_json
    if not isinstance(data, dict):
        return OnboardingResult(
            ok=False, error="model returned non-JSON",
            raw_assistant_text=run.text[:2000],
            cost_usd=run.cost_usd, duration_s=run.duration_s, model=run.model,
            scan=scan,
        )

    # Recommendations (manual — root-level files we won't auto-apply)
    recs: list[Recommendation] = []
    raw_recs = data.get("recommendations")
    if isinstance(raw_recs, list):
        for raw in raw_recs:
            if not isinstance(raw, dict):
                continue
            recs.append(Recommendation(
                title=str(raw.get("title", "")).strip(),
                rationale=str(raw.get("rationale", "")).strip(),
                target_path=str(raw.get("target_path", "")).strip(),
                manual_content=str(raw.get("manual_content", "")),
            ))

    # Proposed edits — pre-validate using phase 8 rules (same as rewriter)
    from orchestrator.lib.rewriter import ProposedEditView
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
            ok_v, err = edits_mod.validate(proposal, str(root))
            view.valid = ok_v
            view.validation_error = err
            edit_views.append(view)

    return OnboardingResult(
        ok=True,
        project_summary=str(data.get("project_summary", "")).strip(),
        strengths=_coerce_list_of_str(data.get("strengths")),
        gaps=_coerce_list_of_str(data.get("gaps")),
        recommendations=recs,
        proposed_edits=edit_views,
        cost_usd=run.cost_usd,
        duration_s=run.duration_s,
        model=run.model,
        scan=scan,
    )
