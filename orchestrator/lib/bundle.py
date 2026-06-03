"""Context bundler — scans a project for the standard memory/knowledge
files and renders them to a single markdown "context pack" that downstream
phases (the rewriter in phase 4) will feed to the orchestrator's brain.

This phase only builds and renders the bundle. It does not yet feed it to
anything. The UI gets a "Preview context" button so Tre can sanity-check
the output before the rewriter starts using it.

Layout discovery:
  1. If `.forge.json` exists in the project, read its `layout` block to
     learn where memory / knowledge / tasks live. Fields:
       layout.memory_dirs    (default: ["memory"])
       layout.knowledge_dirs (default: ["knowledge"])
       layout.task_dirs      (default: ["tasks"])
       layout.claude_md      (default: "CLAUDE.md")
       layout.plan_md        (default: "PLAN.md")
  2. Otherwise, scan with those defaults. Missing dirs are silently skipped
     so it works on projects that don't follow the forge layout.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# Per-source caps so one giant file can't eat the whole budget
PER_FILE_CHARS = 5_000
TOTAL_CHARS_DEFAULT = 50_000
RECENT_TASKS = 5
GIT_LOG_N = 5

DEFAULT_LAYOUT = {
    "memory_dirs": ["memory"],
    "knowledge_dirs": ["knowledge"],
    "task_dirs": ["tasks"],
    "claude_md": "CLAUDE.md",
    "plan_md": "PLAN.md",
}


@dataclass
class Section:
    title: str
    source: str       # path or label like "git"
    body: str
    chars: int = 0    # set in __post_init__
    truncated: bool = False

    def __post_init__(self):
        self.chars = len(self.body)


@dataclass
class ContextPack:
    project_path: str
    project_slug: str
    generated_at: int
    sections: list[Section] = field(default_factory=list)
    total_chars: int = 0
    over_budget: bool = False

    def to_markdown(self) -> str:
        lines = [
            f"# Context pack — {self.project_slug}",
            f"*Generated: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.generated_at))}*  ",
            f"*Path: `{self.project_path}`*  ",
            f"*Total: {self.total_chars:,} chars across {len(self.sections)} section(s)*",
            "",
        ]
        if self.over_budget:
            lines.append(
                "> ⚠️ Over budget — later sections were dropped. Increase the cap or trim files.\n"
            )
        for s in self.sections:
            lines.append(f"## {s.title}")
            lines.append(f"*Source: `{s.source}` — {s.chars:,} chars"
                         f"{' (truncated)' if s.truncated else ''}*")
            lines.append("")
            lines.append(s.body.rstrip())
            lines.append("")
        return "\n".join(lines)


# ─── helpers ──────────────────────────────────────────────────────────────

def _read_layout(project_path: Path) -> dict:
    """Read .forge.json layout, falling back to defaults on any error or
    type mismatch. Each layout key has a required type; anything else is
    silently dropped so a broken .forge.json can't crash the bundler."""
    expected_types = {
        "memory_dirs": list,
        "knowledge_dirs": list,
        "task_dirs": list,
        "claude_md": str,
        "plan_md": str,
    }
    forge_json = project_path / ".forge.json"
    layout = dict(DEFAULT_LAYOUT)
    if not forge_json.is_file():
        return layout
    try:
        data = json.loads(forge_json.read_text())
    except (json.JSONDecodeError, OSError):
        return layout
    if not isinstance(data.get("layout"), dict):
        return layout
    for k, v in data["layout"].items():
        if k not in DEFAULT_LAYOUT:
            continue
        if not isinstance(v, expected_types[k]):
            continue
        if isinstance(v, list) and not all(isinstance(x, str) for x in v):
            continue
        layout[k] = v
    return layout


def _within_project(project_root: Path, candidate: Path) -> bool:
    """True iff `candidate`, resolved, lives inside `project_root` (resolved).

    Resolves symlinks too, so a symlink pointing out of the project is rejected.
    `project_root` must already be resolved by the caller.
    """
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):  # RuntimeError on symlink loops
        return False
    try:
        resolved.relative_to(project_root)
        return True
    except ValueError:
        return False


def _safe_join(project_root: Path, rel: str) -> Path | None:
    """Join `rel` onto `project_root` and verify the result stays inside.
    Returns None if `rel` escapes (via .., absolute path, or a symlink)."""
    candidate = project_root / rel
    return candidate if _within_project(project_root, candidate) else None


def _read_file_capped(path: Path) -> tuple[str, bool]:
    """Read a file, truncate to PER_FILE_CHARS. Returns (body, truncated)."""
    try:
        text = path.read_text(errors="replace")
    except OSError as e:
        return f"[read error: {e}]", False
    if len(text) > PER_FILE_CHARS:
        return text[:PER_FILE_CHARS] + "\n…[truncated]", True
    return text, False


def _list_md_files(project: Path, rel_dirs: list[str]) -> list[Path]:
    """Recursively list *.md files inside the given relative dirs.
    All results are guaranteed to be inside the project root (symlinks
    pointing out are rejected). Hidden subdirs (.git/, etc.) are skipped."""
    out: list[Path] = []
    project_resolved = project.resolve()
    for rel in rel_dirs:
        d = _safe_join(project_resolved, rel)
        if d is None or not d.is_dir():
            continue
        for p in sorted(d.rglob("*.md")):
            if any(part.startswith(".") for part in p.relative_to(d).parts):
                continue
            # Re-check each result — symlinked file pointing outside must
            # not be included, even if its parent dir was inside.
            if not _within_project(project_resolved, p):
                continue
            out.append(p)
    return out


def _recent_task_files(project: Path, task_dirs: list[str]) -> list[Path]:
    cands: list[tuple[float, Path]] = []
    project_resolved = project.resolve()
    for rel in task_dirs:
        d = _safe_join(project_resolved, rel)
        if d is None or not d.is_dir():
            continue
        for p in d.rglob("*.md"):
            if not _within_project(project_resolved, p):
                continue
            try:
                cands.append((p.stat().st_mtime, p))
            except OSError:
                continue
    cands.sort(reverse=True)
    return [p for _, p in cands[:RECENT_TASKS]]


def _git_context(project: Path) -> Section | None:
    """If project is a git repo, summarize branch + recent commits + dirty state."""
    if not (project / ".git").exists():
        return None

    def git(*args: str) -> str:
        try:
            r = subprocess.run(
                ["git", "-C", str(project), *args],
                capture_output=True, text=True, timeout=5,
            )
            return r.stdout.strip()
        except Exception:
            return ""

    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    commits = git("log", f"-n{GIT_LOG_N}", "--pretty=format:%h %s (%cr)")
    status = git("status", "--porcelain")
    dirty_count = len([l for l in status.splitlines() if l.strip()])

    body_lines = [f"**Branch:** `{branch or '(detached)'}`",
                  f"**Uncommitted changes:** {dirty_count} file(s)",
                  "",
                  "**Recent commits:**",
                  "```"]
    body_lines.extend(commits.splitlines() or ["(no commits)"])
    body_lines.append("```")
    if dirty_count and dirty_count <= 30:
        body_lines.append("")
        body_lines.append("**Dirty files:**")
        body_lines.append("```")
        body_lines.extend(status.splitlines())
        body_lines.append("```")
    return Section(title="Git context", source="git", body="\n".join(body_lines))


def _dir_tree(project: Path, max_entries: int = 60) -> Section:
    """Two-level directory listing, files + dirs, capped."""
    entries: list[str] = []
    try:
        top = sorted(project.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError:
        return Section("Directory tree", "fs", "[unreadable]")
    for p in top:
        if p.name.startswith(".") and p.name not in (".forge.json", ".gitignore"):
            continue
        if p.is_dir():
            entries.append(f"{p.name}/")
            try:
                kids = sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower()))
                for c in kids[:10]:
                    if c.name.startswith("."):
                        continue
                    entries.append(f"  {c.name}{'/' if c.is_dir() else ''}")
                if len(kids) > 10:
                    entries.append(f"  …({len(kids)-10} more)")
            except OSError:
                continue
        else:
            entries.append(p.name)
        if len(entries) >= max_entries:
            entries.append("…")
            break
    return Section(title="Directory tree", source="fs",
                   body="```\n" + "\n".join(entries) + "\n```")


# ─── main entry point ─────────────────────────────────────────────────────

def build_bundle(project_path: str, total_chars: int = TOTAL_CHARS_DEFAULT) -> ContextPack:
    project = Path(project_path).expanduser().resolve()
    slug = project.name
    pack = ContextPack(
        project_path=str(project),
        project_slug=slug,
        generated_at=int(time.time()),
    )

    if not project.is_dir():
        pack.sections.append(Section(
            title="ERROR",
            source="bundle",
            body=f"Project path does not exist: {project}",
        ))
        return pack

    layout = _read_layout(project)
    project_resolved = project.resolve()
    sections: list[Section] = []

    # 1. CLAUDE.md (path-safe)
    cm = _safe_join(project_resolved, layout["claude_md"])
    if cm and cm.is_file():
        body, trunc = _read_file_capped(cm)
        sections.append(Section(title="CLAUDE.md (Claude instructions)",
                                source=str(cm.relative_to(project_resolved)),
                                body=body, truncated=trunc))

    # 2. PLAN.md (path-safe)
    pm = _safe_join(project_resolved, layout["plan_md"])
    if pm and pm.is_file():
        body, trunc = _read_file_capped(pm)
        sections.append(Section(title="PLAN.md (project plan)",
                                source=str(pm.relative_to(project_resolved)),
                                body=body, truncated=trunc))

    # 3. Memory files
    for f in _list_md_files(project, layout["memory_dirs"]):
        body, trunc = _read_file_capped(f)
        sections.append(Section(
            title=f"Memory: {f.relative_to(project)}",
            source=str(f.relative_to(project)),
            body=body, truncated=trunc,
        ))

    # 4. Knowledge files
    for f in _list_md_files(project, layout["knowledge_dirs"]):
        body, trunc = _read_file_capped(f)
        sections.append(Section(
            title=f"Knowledge: {f.relative_to(project)}",
            source=str(f.relative_to(project)),
            body=body, truncated=trunc,
        ))

    # 5. Recent tasks
    for f in _recent_task_files(project, layout["task_dirs"]):
        body, trunc = _read_file_capped(f)
        sections.append(Section(
            title=f"Recent task: {f.relative_to(project)}",
            source=str(f.relative_to(project)),
            body=body, truncated=trunc,
        ))

    # 6. Git context
    git_sec = _git_context(project)
    if git_sec:
        sections.append(git_sec)

    # 7. Directory tree (last — only useful as orientation if budget allows)
    sections.append(_dir_tree(project))

    # Enforce overall budget by dropping later sections that overflow.
    running = 0
    for s in sections:
        if running + s.chars > total_chars:
            pack.over_budget = True
            break
        pack.sections.append(s)
        running += s.chars
    pack.total_chars = running
    return pack
