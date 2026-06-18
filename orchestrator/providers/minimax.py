#!/usr/bin/env python3
"""Fusion provider script — MiniMax (OpenAI-compatible endpoint).

STANDALONE: imports NOTHING from the orchestrator package — it runs as its own
subprocess (the in-process panel fan-out today; a visible iTerm2 fusion tab
later). Stdlib only. Resolves its own key (env → config.json), streams progress +
the answer to STDERR (watchable), and prints exactly one line of normalized JSON
to STDOUT. NEVER raises.

F1.2b spike — resolved: MiniMax used to be the one non-OpenAI seat (its native
`/v1/text/chatcompletion_v2` path wraps results in a top-level `base_resp`
envelope). As of the 2026-06-18 docs it ALSO exposes a fully OpenAI-COMPATIBLE
endpoint — `POST https://api.minimax.io/v1/chat/completions`, standard
`messages:[{role,content}]`, `choices[0].message.content`, and an OpenAI-shaped
`usage` block — so it no longer needs a bespoke body/parser and is just another
copy of the OpenAI-shaped template here. Uses the INTERNATIONAL host
(`api.minimax.io`), NOT the China host.

Normalized stdout contract (identical for every provider script):
    {"ok": true, "text": "...", "model": "MiniMax-Text-01",
     "prompt_tokens": 0, "completion_tokens": 0, "error": ""}

Request — argv[1] is a path to a JSON file, or "-"/absent reads STDIN:
    {"prompt": "...", "model": "MiniMax-Text-01", "timeout_s": 300}
"""
import json
import os
import sys
import urllib.request

BASE_URL = "https://api.minimax.io/v1"
KEY_ENV, NAME = "MINIMAX_API_KEY", "minimax"


def _key():
    """Precedence: env MINIMAX_API_KEY → config.json api_key → '' (mirrors
    orchestrator.lib.config; read INSIDE the script, never passed via argv)."""
    if os.environ.get(KEY_ENV):
        return os.environ[KEY_ENV]
    try:
        cfg = json.load(open(os.path.expanduser("~/.orchestrator/config.json")))
        return cfg["fusion"]["providers"][NAME].get("api_key") or ""
    except Exception:
        return ""


def _read_req():
    if len(sys.argv) > 1 and sys.argv[1] not in ("-", ""):
        with open(sys.argv[1]) as f:
            return json.load(f)
    return json.load(sys.stdin)


def main():
    req = _read_req()
    key = _key()
    if not key:
        print(json.dumps({"ok": False, "error": f"{KEY_ENV} not set (env or config.json)"}))
        return

    prompt = req.get("prompt", "")
    model = req.get("model") or "MiniMax-Text-01"
    body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    r = urllib.request.Request(
        BASE_URL + "/chat/completions",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})

    sys.stderr.write(f"→ {NAME} {model} … ({len(prompt)} chars in)\n")
    sys.stderr.flush()
    try:
        resp = urllib.request.urlopen(r, timeout=req.get("timeout_s", 300))
        env = json.loads(resp.read().decode())
    except Exception as e:
        detail = ""
        try:
            detail = e.read().decode()[:600]   # HTTPError carries the response body
        except Exception:
            pass
        sys.stderr.write(f"✗ {NAME} failed: {e} {detail}\n")
        print(json.dumps({"ok": False, "error": f"{e} {detail}".strip()}))
        return

    text = (env.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    u = env.get("usage") or {}
    sys.stderr.write(f"← {NAME} {model} ({u.get('completion_tokens', 0)} out tok):\n{text}\n")
    print(json.dumps({"ok": True, "text": text, "model": model,
                      "prompt_tokens": u.get("prompt_tokens", 0),
                      "completion_tokens": u.get("completion_tokens", 0), "error": ""}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:                          # belt-and-suspenders: never raise
        print(json.dumps({"ok": False, "error": str(e)}))
