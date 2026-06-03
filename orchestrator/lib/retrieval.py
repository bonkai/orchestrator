"""Cross-project semantic retrieval over past dispatch summaries.

Storage: one row per (dispatch_id) in `dispatch_embeddings`, indexed by
project_id for the filter case. Vectors are float32 BLOBs.

Search: brute-force cosine in Python. At Tre's scale (hundreds of
dispatches per year) this is microseconds; no need for sqlite-vec or FAISS
until we're at 100K+ rows.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from orchestrator.lib import db, embeddings

log = logging.getLogger("orchestrator.retrieval")


@dataclass
class Hit:
    dispatch_id: int
    project_id: int
    project_slug: str
    user_task: str
    summary_md: str
    lessons: str
    score: float       # cosine similarity, range [-1, 1]


# ─── embedding write side ────────────────────────────────────────────────

def _embed_text_for_dispatch(user_task: str, summary_md: str, lessons: str, tags_csv: str) -> str:
    """Combine the fields that semantically describe a dispatch into one
    string to embed. Each piece is short — sum stays well under MAX_INPUT_CHARS."""
    parts = []
    if user_task:
        parts.append(f"TASK: {user_task}")
    if summary_md:
        parts.append(f"SUMMARY: {summary_md}")
    if lessons:
        parts.append(f"LESSONS: {lessons}")
    if tags_csv:
        parts.append(f"TAGS: {tags_csv}")
    return "\n".join(parts)


def index_dispatch(dispatch_id: int) -> bool:
    """Embed and store a single dispatch. Returns True on success."""
    d = db.get_dispatch_with_project(dispatch_id)
    if not d:
        return False
    if not (d.get("summary_md") or "").strip():
        # Don't index dispatches that have no summary yet; they'd just embed
        # the user_task alone (low signal).
        return False
    tags_csv = ""
    if d.get("tags_json"):
        try:
            import json
            tags = json.loads(d["tags_json"])
            tags_csv = ", ".join(tags) if isinstance(tags, list) else ""
        except (ValueError, TypeError):
            pass
    text = _embed_text_for_dispatch(
        d.get("user_task", ""), d.get("summary_md", ""),
        d.get("lessons", ""), tags_csv,
    )
    vec = embeddings.embed(text)
    if vec is None:
        return False
    blob = embeddings.vec_to_blob(vec)
    with db.conn() as c:
        c.execute(
            "INSERT INTO dispatch_embeddings(dispatch_id, project_id, model, dim, vector) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(dispatch_id) DO UPDATE SET "
            "  model=excluded.model, dim=excluded.dim, vector=excluded.vector",
            (dispatch_id, d["project_id"], embeddings.DEFAULT_MODEL, len(vec), blob),
        )
    return True


def backfill_missing() -> int:
    """Embed every dispatch with a summary but no embedding row yet.
    Returns count of newly-indexed rows. Useful one-time after enabling
    phase 6 on an existing database."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT d.id FROM dispatches d "
            "JOIN outcomes o ON o.dispatch_id = d.id "
            "LEFT JOIN dispatch_embeddings e ON e.dispatch_id = d.id "
            "WHERE o.summary_md IS NOT NULL AND TRIM(o.summary_md) != '' "
            "  AND e.dispatch_id IS NULL"
        ).fetchall()
    n = 0
    for r in rows:
        if index_dispatch(r["id"]):
            n += 1
    return n


# ─── search side ─────────────────────────────────────────────────────────

def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. Assumes equal length (we filter by dim before calling)."""
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def find_similar(
    query_text: str,
    k: int = 5,
    exclude_dispatch_id: int | None = None,
    same_project_only: int | None = None,
    min_score: float = 0.3,
) -> list[Hit]:
    """Top-K similar past dispatches to `query_text`.

    exclude_dispatch_id: skip this row (use when re-embedding self).
    same_project_only:   if set, restrict to this project_id; default = cross-project.
    min_score:           drop hits below this cosine threshold (filters noise).
    """
    query_vec = embeddings.embed(query_text)
    if query_vec is None:
        return []
    query_dim = len(query_vec)

    with db.conn() as c:
        q = (
            "SELECT e.dispatch_id, e.project_id, e.dim, e.vector, "
            "p.slug as project_slug, d.user_task, "
            "IFNULL(o.summary_md,'') as summary_md, IFNULL(o.lessons,'') as lessons "
            "FROM dispatch_embeddings e "
            "JOIN dispatches d ON d.id = e.dispatch_id "
            "JOIN projects p ON p.id = e.project_id "
            "LEFT JOIN outcomes o ON o.dispatch_id = e.dispatch_id "
            "WHERE e.dim = ?"
        )
        args: tuple = (query_dim,)
        if exclude_dispatch_id is not None:
            q += " AND e.dispatch_id != ?"
            args = args + (exclude_dispatch_id,)
        if same_project_only is not None:
            q += " AND e.project_id = ?"
            args = args + (same_project_only,)
        rows = c.execute(q, args).fetchall()

    scored: list[Hit] = []
    for r in rows:
        vec = embeddings.blob_to_vec(r["vector"])
        # Skip rows where the declared dim doesn't match the actual blob
        # (corrupted/truncated row) — would compute a garbage cosine.
        if len(vec) != query_dim:
            continue
        score = _cosine(query_vec, vec)
        if score < min_score:
            continue
        scored.append(Hit(
            dispatch_id=r["dispatch_id"],
            project_id=r["project_id"],
            project_slug=r["project_slug"],
            user_task=r["user_task"] or "",
            summary_md=r["summary_md"],
            lessons=r["lessons"],
            score=score,
        ))
    scored.sort(key=lambda h: h.score, reverse=True)
    return scored[:k]


def render_hits_for_prompt(hits: list[Hit], max_chars: int = 8_000) -> str:
    """Render a list of hits as markdown that can be injected into the
    rewriter prompt. Caps total chars so retrieval doesn't bloat the prompt."""
    if not hits:
        return ""
    lines = []
    used = 0
    for i, h in enumerate(hits, 1):
        block = (
            f"### Past task {i} — project `{h.project_slug}` (similarity {h.score:.2f})\n"
            f"**Task:** {h.user_task[:300]}\n"
            f"**What happened:** {h.summary_md[:600]}\n"
        )
        if h.lessons:
            block += f"**Lessons:** {h.lessons[:400]}\n"
        if used + len(block) > max_chars:
            lines.append("\n[... more matches dropped to keep prompt bounded]")
            break
        lines.append(block)
        used += len(block)
    return "\n".join(lines)
