# Orchestrator

Local browser UI that dispatches enriched tasks to Claude Code across many projects on this laptop.

## Quick start

```bash
# 1. One-time setup: creates .venv, installs deps, sets up ~/.orchestrator/,
#    merges Stop hook into ~/.claude/settings.json (preserves existing hooks),
#    registers with forge.
bash bin/install.sh

# 2. Run
source .venv/bin/activate
python -m orchestrator
# → open http://127.0.0.1:7878
```

Requires iTerm2 (`brew install --cask iterm2`) for dispatching.

## How a dispatch works

1. **Add a project, open a tab, type a task.** Click **preview rewrite** (or **skip rewrite** to bypass).
2. **Rewriter** (phase 4) — orchestrator builds a context bundle from your project (CLAUDE.md, memory/, knowledge/, recent tasks, git state — phase 3) and retrieves up to 5 semantically-similar past tasks from any project (phase 6, via Ollama + embeddinggemma). A `claude` brain call (in a visible iTerm2 tab) rewrites your prompt with that context. The rewriter can also propose small file edits (memory entries, new task files — phase 8).
3. **Review.** Edit the rewritten prompt, optionally apply proposed edits, then **dispatch rewritten**.
4. **Spawn** (phase 1) — opens an iTerm2 tab with your task. Each dispatch is independent — 10+ concurrent is fine.
5. **Safety** — manual stop, global stop-all, wall-clock cap (30 min default), and a **loop watchdog** (phase 7) that kills the session if Claude repeats the same tool call 8 times in a row.
6. **Completion** (phase 2) — Stop hook posts to `/api/complete`, transcript is copied, and a **summarizer** (phase 5) runs in the background producing `{summary_md, what_worked, what_broke, lessons, tags}` plus an embedding for future retrieval.
7. **`/dispatch/<id>`** shows the original task, summary, tags, and a link to the raw transcript.

## Why not the Anthropic API?
All "brain" calls (rewriter + summarizer + onboarding) use the `claude` CLI — not the Anthropic API — running in **visible iTerm2 tabs** you can watch live (a headless subprocess is only the fallback when iTerm2 is absent). Uses your existing Claude subscription. Zero API tokens for the orchestrator itself.

Embeddings use Ollama + `embeddinggemma` locally (also zero token cost).

## Where data lives
`~/.orchestrator/` — SQLite DB + transcripts + vector embeddings. Outside the repo.

## Hard requirements
- macOS (uses AppleScript to drive iTerm2)
- iTerm2 (`brew install --cask iterm2`)
- Ollama running with `embeddinggemma` pulled (`ollama pull embeddinggemma`)
- `claude` CLI on PATH

See `PLAN.md` for the per-phase breakdown.
