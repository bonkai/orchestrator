"""Per-project staged attachments — files the user drags onto the
dispatch form. They're stored in ~/.orchestrator/attachments/<stash_id>/
and made available to:

  - the rewriter (mentioned in the prompt so it can reference them)
  - the dispatched claude session (paths appended to the task)

Stash IDs are scoped per-project so attachments don't bleed between
projects. The stash is cleared after a successful dispatch — drops
that you never dispatched stay around until the user clears or the
nightly reaper (future) sweeps them.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from orchestrator.lib.db import DATA_DIR

ATTACHMENTS_DIR = DATA_DIR / "attachments"

# Per-file cap so a dropped 1GB video doesn't fill disk
MAX_FILE_BYTES = 20 * 1024 * 1024   # 20 MB
MAX_FILES_PER_STASH = 20


@dataclass
class Attachment:
    name: str         # original filename (sanitized)
    path: str         # absolute path on disk
    size: int


def _sanitize_filename(name: str) -> str:
    """Strip path components and unusual characters. Never trust filenames
    from a drag-drop — they can contain ../ etc."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._\- ]", "_", name).strip()
    return name or "unnamed"


def _stash_dir(stash_id: str) -> Path:
    """Stash dir for a given id. The id is just project_id as string —
    we validate it's all digits before this is called."""
    return ATTACHMENTS_DIR / stash_id


def save(stash_id: str, original_name: str, data: bytes) -> tuple[bool, str, Attachment | None]:
    """Save a single attachment. Returns (ok, error_msg, attachment_or_None)."""
    if not stash_id.isdigit():
        return False, "bad stash id", None
    if len(data) > MAX_FILE_BYTES:
        return False, f"file too large ({len(data)/1024/1024:.1f} MB > {MAX_FILE_BYTES//1024//1024} MB cap)", None

    safe = _sanitize_filename(original_name)
    d = _stash_dir(stash_id)
    d.mkdir(parents=True, exist_ok=True)

    existing = list_files(stash_id)
    if len(existing) >= MAX_FILES_PER_STASH:
        return False, f"already at {MAX_FILES_PER_STASH}-file cap; remove some first", None

    # Avoid clobbering an existing file by suffixing -1, -2, etc.
    target = d / safe
    if target.exists():
        stem, dot, ext = safe.rpartition(".")
        if not dot:
            stem, ext = safe, ""
        n = 1
        while target.exists():
            candidate = f"{stem}-{n}" + (f".{ext}" if ext else "")
            target = d / candidate
            n += 1

    try:
        target.write_bytes(data)
    except OSError as e:
        return False, f"write failed: {e}", None
    return True, "", Attachment(name=target.name, path=str(target), size=len(data))


def list_files(stash_id: str) -> list[Attachment]:
    if not stash_id.isdigit():
        return []
    d = _stash_dir(stash_id)
    if not d.is_dir():
        return []
    out = []
    try:
        for p in sorted(d.iterdir(), key=lambda x: x.name.lower()):
            if not p.is_file():
                continue
            try:
                out.append(Attachment(name=p.name, path=str(p), size=p.stat().st_size))
            except OSError:
                continue
    except OSError:
        pass
    return out


def remove(stash_id: str, filename: str) -> bool:
    """Remove one attachment by filename (sanitized). Returns True if removed."""
    if not stash_id.isdigit():
        return False
    safe = _sanitize_filename(filename)
    target = _stash_dir(stash_id) / safe
    if not target.is_file():
        return False
    # Defense in depth: target must resolve inside the stash dir
    try:
        target.resolve().relative_to(_stash_dir(stash_id).resolve())
    except (ValueError, OSError):
        return False
    try:
        target.unlink()
        return True
    except OSError:
        return False


def clear(stash_id: str):
    """Drop everything in this stash. Called after successful dispatch."""
    if not stash_id.isdigit():
        return
    d = _stash_dir(stash_id)
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)


def move_to_dispatch(stash_id: str, dispatch_id: int) -> list[Attachment]:
    """Move stash contents to ~/.orchestrator/attachments/dispatch_<id>/
    so the attachments outlive the project stash (which gets cleared)."""
    src = _stash_dir(stash_id)
    if not src.is_dir():
        return []
    dest = ATTACHMENTS_DIR / f"dispatch_{dispatch_id}"
    dest.mkdir(parents=True, exist_ok=True)
    moved = []
    for p in src.iterdir():
        if not p.is_file():
            continue
        target = dest / p.name
        try:
            p.rename(target)
            moved.append(Attachment(name=target.name, path=str(target), size=target.stat().st_size))
        except OSError:
            continue
    # Clean up stash dir
    try:
        src.rmdir()
    except OSError:
        pass
    return moved


def render_for_prompt(atts: list[Attachment]) -> str:
    """Render the attachment list as markdown to prepend to the task.
    Tells the dispatched claude how to access them (absolute paths)."""
    if not atts:
        return ""
    lines = ["## Attached files (user dropped these — read them as needed)\n"]
    for a in atts:
        kb = a.size / 1024
        size_str = f"{kb:.1f} KB" if kb < 1024 else f"{kb/1024:.1f} MB"
        lines.append(f"- `{a.path}` ({size_str})")
    return "\n".join(lines)
