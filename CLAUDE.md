# Orchestrator — Claude Instructions

## What this project is
A local browser UI that dispatches tasks to Claude Code sessions across many projects on this laptop. The orchestrator enriches each task with project memory and past-outcome context before spawning a headless `claude` in a new iTerm2 tab, then logs the result so future similar tasks get better.

## Hard rules
- **No Anthropic API calls.** All "brain" work (rewriter, summarizer) goes through headless `claude` subprocesses, matching the `~/Documents/verse_sites/run_pipeline_v2.py` pattern.
- **Local only.** Everything runs on this laptop. No remote workers, no hosted services.
- **Data lives in `~/.orchestrator/`**, not in the repo. The repo stays clean.
- **Stop hook is env-var gated.** It must be a no-op unless `ORCHESTRATOR_RUN_ID` is set — your manual `claude` sessions must not be affected.

## Stack
- Python 3.11, FastAPI + uvicorn, HTMX, vanilla SQLite (stdlib `sqlite3`).
- iTerm2 controlled via `osascript` (AppleScript) — no `iterm2` Python lib needed.

## Layout
- `orchestrator/app.py` — FastAPI routes
- `orchestrator/lib/` — db, spawn, watchdog, (later) bundle/retrieval/claude_runner
- `orchestrator/templates/` — HTMX views
- `bin/install.sh` — one-time setup: ~/.orchestrator/, Stop hook merge, forge registration
- `bin/notify_complete.sh` — the Stop hook payload

## Safety
- Manual kill (per dispatch) + global kill-all + wall-clock cap (default 30 min).
- Loop watchdog (repeated identical tool calls) is a planned addition; not in MVP.
- Every kill writes an `outcomes` row with reason so the future learning loop sees it.
