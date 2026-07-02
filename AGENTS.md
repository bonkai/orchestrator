# Orchestrator — Claude Instructions

## What this project is
A local browser UI that dispatches tasks to Claude Code sessions across many projects on this laptop. The orchestrator enriches each task with project memory and past-outcome context before spawning a `claude` session in a new iTerm2 tab, then logs the result so future similar tasks get better.

## Hard rules
- **No Anthropic API calls.** All "brain" work (rewriter, summarizer, onboarding) runs through the `claude` CLI on your existing subscription — not the Anthropic API.
- **Brain calls are visible, never headless.** Every `claude` invocation — brain calls *and* dispatched executors — runs in a **watchable iTerm2 tab** (`claude_runner.run_claude_json` → `spawn.spawn_brain_tab`, streaming `claude -p --output-format stream-json --verbose` tee'd to a sidecar that's parsed back into the structured result). The headless captured subprocess (`run_claude_headless`) is a **fallback only**, used when iTerm2 isn't installed. Don't add hidden/background LLM calls.
- **Local only.** Everything runs on this laptop. No remote workers, no hosted services. *Sole planned exception:* the opt-in, default-off OpenRouter "Fusion" multi-model brain layer (see `FUSION_PLAN.md`) — and even that runs its calls in a visible iTerm2 tab, never a hidden HTTP request.
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
