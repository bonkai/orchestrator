"""Proposed-edit validation and application.

The rewriter can propose small file changes (e.g. "add this hazard to
memory/", "scaffold tasks/<new>.md"). These are powerful, so the
validation rules are intentionally narrow:

  Allowed actions (only these — anything else is rejected):
    - append_to_memory:   append `content` to an existing memory/*.md file,
                          OR create memory/<name>.md if it doesn't exist.
    - append_to_knowledge: same but for knowledge/*.md.
    - create_task_file:   create a NEW tasks/<name>.md file. Refuses if
                          a file at that path already exists (we never
                          overwrite without an explicit user action).

  Path rules: must resolve inside the project root (bundle-style _safe_join).
  Hidden files / dotfiles / paths containing .. are rejected up front.
  File size: each applied write is capped at 50 KB.

  This module never auto-applies — the orchestrator's UI must explicitly
  call apply_edit() per user-checked edit. Defense in depth: the rewriter
  could go off-script and propose dangerous edits, so the only path to
  disk is through this validator.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("orchestrator.edits")

ALLOWED_ACTIONS = {"append_to_memory", "append_to_knowledge", "create_task_file"}
MAX_CONTENT_BYTES = 50_000

# Maps action -> the layout key that names the allowed parent dir(s).
# This means the .forge.json layout overrides apply here too — if a project
# stores memory under "notes/", an append_to_memory edit goes there.
ACTION_TO_LAYOUT_KEY = {
    "append_to_memory": "memory_dirs",
    "append_to_knowledge": "knowledge_dirs",
    "create_task_file": "task_dirs",
}


@dataclass
class EditProposal:
    """One proposed change. Untrusted until validated."""
    action: str
    path: str         # relative to project root
    content: str
    rationale: str = ""


@dataclass
class EditResult:
    ok: bool
    action: str = ""
    path: str = ""        # final absolute path, on success
    written_bytes: int = 0
    error: str = ""
    skipped: bool = False  # for "already exists" on create_task_file etc.


def _within_project(project_root: Path, candidate: Path) -> bool:
    """Same check the bundler uses — must stay under project root after
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


def _read_layout(project_root: Path) -> dict:
    """Tiny duplicate of bundle._read_layout — kept independent so a
    bundler refactor can't accidentally widen what edits is allowed to write."""
    import json
    defaults = {
        "memory_dirs": ["memory"],
        "knowledge_dirs": ["knowledge"],
        "task_dirs": ["tasks"],
    }
    forge = project_root / ".forge.json"
    if not forge.is_file():
        return defaults
    try:
        data = json.loads(forge.read_text())
    except (json.JSONDecodeError, OSError):
        return defaults
    layout = dict(defaults)
    raw = data.get("layout") if isinstance(data, dict) else None
    if isinstance(raw, dict):
        for k in defaults:
            v = raw.get(k)
            if isinstance(v, list) and all(isinstance(x, str) for x in v):
                layout[k] = v
    return layout


def validate(proposal: EditProposal, project_path: str) -> tuple[bool, str]:
    """Type-check + path-check a proposal. Returns (ok, error_msg)."""
    action = proposal.action
    if action not in ALLOWED_ACTIONS:
        return False, f"action {action!r} not in {sorted(ALLOWED_ACTIONS)}"

    rel = (proposal.path or "").strip()
    if not rel:
        return False, "empty path"
    if rel.startswith("/") or rel.startswith("~"):
        return False, "absolute paths not allowed"
    if ".." in Path(rel).parts:
        return False, "'..' not allowed in path"
    # Disallow dotfiles / hidden dirs anywhere in the path
    if any(part.startswith(".") for part in Path(rel).parts):
        return False, "hidden paths (starting with '.') not allowed"
    if not rel.endswith(".md"):
        return False, "only .md files allowed"

    project_root = Path(project_path).resolve()
    if not project_root.is_dir():
        return False, f"project path does not exist: {project_root}"

    target = project_root / rel
    if not _within_project(project_root, target):
        return False, f"path escapes project root: {rel}"

    # The parent dir must be one of the layout dirs for this action.
    # Path.resolve(strict=False) works on non-existent paths since Python 3.6,
    # so we always resolve — both target parent and allowed parents — to
    # avoid the bug where one side is canonical and the other isn't, which
    # would break the comparison when the project root has symlinks.
    layout = _read_layout(project_root)
    layout_dirs = layout.get(ACTION_TO_LAYOUT_KEY[action], [])
    target_parent = (project_root / Path(rel).parent).resolve()
    allowed_parents = {str((project_root / d).resolve()) for d in layout_dirs}
    if str(target_parent) not in allowed_parents:
        return False, (
            f"action {action} can only write into {sorted(layout_dirs)} "
            f"(target parent: {target_parent})"
        )

    if not isinstance(proposal.content, str):
        return False, "content must be a string"
    encoded = proposal.content.encode("utf-8", errors="replace")
    if len(encoded) > MAX_CONTENT_BYTES:
        return False, f"content too large: {len(encoded)}B > {MAX_CONTENT_BYTES}B cap"

    # Action-specific extra checks
    if action == "create_task_file" and target.exists():
        return False, f"create_task_file refuses to overwrite existing {rel}"

    return True, ""


def apply_edit(proposal: EditProposal, project_path: str) -> EditResult:
    """Validate + apply. Never raises (returns EditResult.ok=False on error)."""
    ok, err = validate(proposal, project_path)
    if not ok:
        return EditResult(ok=False, action=proposal.action, error=err)

    project_root = Path(project_path).resolve()
    target = project_root / proposal.path
    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        if proposal.action == "create_task_file":
            # validate() already confirmed it doesn't exist
            target.write_text(proposal.content)
            mode = "created"
        else:
            # append_to_memory / append_to_knowledge
            if target.exists():
                # Add a blank line separator if file isn't empty and doesn't end in one
                existing = target.read_text(errors="replace")
                sep = "" if existing.endswith("\n\n") else ("\n\n" if existing.endswith("\n") else "\n\n")
                target.write_text(existing + sep + proposal.content + ("\n" if not proposal.content.endswith("\n") else ""))
                mode = "appended"
            else:
                target.write_text(proposal.content + ("\n" if not proposal.content.endswith("\n") else ""))
                mode = "created"
    except OSError as e:
        return EditResult(ok=False, action=proposal.action, error=f"write failed: {e}")

    log.info("edit %s %s → %s", mode, proposal.action, target)
    return EditResult(
        ok=True, action=proposal.action,
        path=str(target.relative_to(project_root)),
        written_bytes=len(proposal.content.encode("utf-8", errors="replace")),
    )
