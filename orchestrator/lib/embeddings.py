"""Embedding generation via Ollama HTTP. Local, no API.

Defaults to `embeddinggemma` (Google, 768-dim) but the model is configurable
in case we want to swap later. Never raises — returns None on failure so
callers can degrade gracefully (skip retrieval rather than crash a dispatch).
"""

from __future__ import annotations

import json
import logging
import struct
import urllib.error
import urllib.request

log = logging.getLogger("orchestrator.embeddings")

OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "embeddinggemma"
DEFAULT_TIMEOUT_S = 30
# Hard cap so we don't send 100KB of summary text to the embedding endpoint
MAX_INPUT_CHARS = 8_000


def embed(text: str, model: str = DEFAULT_MODEL, timeout_s: int = DEFAULT_TIMEOUT_S) -> list[float] | None:
    """Return embedding vector for `text`, or None on failure."""
    if not text or not text.strip():
        return None
    text = text[:MAX_INPUT_CHARS]
    body = json.dumps({"model": model, "prompt": text}).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embeddings",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            data = json.loads(r.read().decode())
    except urllib.error.URLError as e:
        log.warning("Ollama unreachable: %s", e)
        return None
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Embedding response parse error: %s", e)
        return None
    except Exception as e:
        log.warning("Unexpected embedding failure: %s", e)
        return None

    vec = data.get("embedding")
    if not isinstance(vec, list) or not vec:
        # Ollama returns {"error": "..."} when the model isn't pulled etc.
        err = data.get("error", "no embedding in response")
        log.warning("Embedding failed: %s", err)
        return None
    # Guard against NaN/Inf — would corrupt cosine. Drop the vector and
    # log; caller treats as "no embedding available".
    import math
    if any(not math.isfinite(x) for x in vec if isinstance(x, (int, float))):
        log.warning("Embedding contained non-finite values; dropping")
        return None
    # Also: every element should be a number. Defensive.
    if not all(isinstance(x, (int, float)) for x in vec):
        log.warning("Embedding contained non-numeric values; dropping")
        return None
    return vec


def is_available(model: str = DEFAULT_MODEL) -> bool:
    """Cheap probe: can we get an embedding from the configured model right now?
    Used at orchestrator startup to log a clear warning rather than failing later."""
    return embed("ok", model=model, timeout_s=5) is not None


# ─── BLOB <-> vector serialization ───────────────────────────────────────
# Float32 packed little-endian. 768 dims = 3 KB per vector. Decode is
# numpy-free (we only need cosine which we hand-roll).

def vec_to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def blob_to_vec(blob: bytes) -> list[float]:
    """Decode a float32 BLOB. Returns [] on any decode failure so callers
    can skip a corrupt row rather than crash the whole query."""
    try:
        n = len(blob) // 4
        if n == 0:
            return []
        return list(struct.unpack(f"<{n}f", blob))
    except struct.error:
        return []
