#!/usr/bin/env python3
"""Standalone Fusion panel runner — executed inside a watchable iTerm2 tab by
fusion_run.sh. Imports NOTHING from the orchestrator package (it runs in the
tab's own process). Stdlib only. Never raises a traceback into the tab.

It reads the request (argv[1]), runs each panel provider's script as a PARALLEL
subprocess, lets each script's STDERR stream to the screen (so the panel is
watchable live), collects each script's normalized STDOUT, and prints the
collected answers as one JSON array to STDOUT — which fusion_run.sh's `tee`
captures to <id>.json for the orchestrator to read back.

Request (argv[1] → JSON file):
    {"prompt": "...", "timeout_s": 300, "panel": ["gemini", "gemini2"],
     "providers": {"gemini": {"script": "providers/gemini.py", "model": "..."}},
     "lenses": {"gemini": "<per-seat lens text>"}}   # optional (F8.4); absent → none

Stdout (the ONLY thing printed to stdout):
    [{"name": "gemini", "ok": true, "text": "...", "model": "...",
      "prompt_tokens": 0, "completion_tokens": 0, "error": ""}, ...]
"""
import concurrent.futures
import json
import os
import subprocess
import sys

BIN_DIR = os.path.expanduser("~/.orchestrator/bin")   # where providers/*.py live


def _apply_lens(prompt, lens):
    """F8.4: prepend a per-seat lens, keeping the original prompt verbatim and
    LAST (so its output-format instructions still travel). Empty lens → unchanged.
    ⚠ Kept textually identical to claude_runner._apply_lens so the watchable-tab
    panel (this file) and the in-process fallback build the SAME lensed prompt."""
    lens = (lens or "").strip()
    if not lens:
        return prompt
    return ("Approach the task below through this specific lens — let it shape "
            "what you emphasize, but still answer the task in full:\n"
            + lens + "\n\n--- TASK ---\n" + prompt)


def _run_one(name, prov, prompt, timeout_s):
    """Run ONE provider script as a subprocess. Its STDERR is NOT captured, so it
    streams to the tab (watchable); its normalized STDOUT is captured + returned
    with the seat name attached. Never raises."""
    script = os.path.join(BIN_DIR, prov.get("script", ""))
    req = json.dumps({"prompt": prompt, "model": prov.get("model", ""),
                      "timeout_s": timeout_s})
    sys.stderr.write(f"\n=== panel seat: {name} ({prov.get('model', '?')}) ===\n")
    sys.stderr.flush()
    try:
        p = subprocess.run(["python3", script], input=req,
                           stdout=subprocess.PIPE, stderr=None,   # stderr → screen
                           text=True, timeout=timeout_s + 15)
        out = json.loads(p.stdout or "{}")
    except Exception as e:                       # spawn / timeout / bad JSON
        sys.stderr.write(f"!! {name} runner error: {e}\n")
        return {"name": name, "ok": False, "error": str(e)}
    out["name"] = name
    return out


def main(req_path):
    req = json.load(open(req_path))
    panel = req.get("panel", [])
    providers = req.get("providers", {})
    prompt = req.get("prompt", "")
    timeout_s = req.get("timeout_s", 300)
    lenses = req.get("lenses", {}) or {}        # F8.4: per-seat lens text (optional)

    sys.stderr.write(f"fusion panel: {len(panel)} seat(s) — {', '.join(panel)}\n")
    sys.stderr.flush()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(panel))) as ex:
        answers = list(ex.map(
            lambda n: _run_one(n, providers.get(n, {}),
                               _apply_lens(prompt, lenses.get(n, "")), timeout_s),
            panel))

    # The ONLY stdout write — captured by `tee` to <id>.json.
    print(json.dumps(answers))


if __name__ == "__main__":
    try:
        main(sys.argv[1])
    except Exception as e:        # emit valid JSON even on catastrophic failure
        print(json.dumps([{"name": "?", "ok": False, "error": str(e)}]))
