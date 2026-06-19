# Orchestrator

A local browser UI that dispatches enriched tasks to **Claude Code** across many
projects on one machine — with project memory, cross-project retrieval, and an
optional multi-model "Fusion" brain layer auto-injected into every task.

Each dispatch opens a real `claude` session in its own iTerm2 tab. Every completed
session is summarized and embedded, so the next similar task — in any project — starts
better-informed. Run 10+ dispatches at once; each is independent.

## Quick start

```bash
# One-time: creates .venv, installs deps, sets up ~/.orchestrator/,
# merges Stop + PreToolUse hooks into ~/.claude/settings.json (no-ops unless
# orchestrator sets an env var, so your manual claude sessions are unaffected).
bash bin/install.sh
ollama pull embeddinggemma          # ~300MB, for semantic retrieval

source .venv/bin/activate
python -m orchestrator              # → http://127.0.0.1:7878
```

## How a dispatch works

1. **Add a project, type a task.** Optionally run **analyze setup** once per project to
   generate a baseline of memory/knowledge/task files.
2. **Context bundle** — orchestrator assembles a bundle from the project (`CLAUDE.md`,
   `memory/`, `knowledge/`, recent tasks, git state) and retrieves the top semantically
   similar past tasks from *every* project (Ollama + `embeddinggemma`, local).
3. **Rewrite** — a `claude` brain call rewrites your prompt with that context, and can
   propose small file edits (memory entries, new task files) for you to apply.
4. **Review & dispatch** — edit the rewritten prompt, then dispatch. A new iTerm2 tab
   opens with `claude` running the task.
5. **Safety** — per-dispatch stop, global stop-all, a wall-clock cap (30 min default),
   and a **loop watchdog** that kills a session if it repeats the same tool call 8× in a
   row. Every kill is logged with a reason (`manual`, `timeout`, `loop:Bash`, …) so the
   learning corpus knows what went wrong.
6. **Completion** — a Stop hook posts to `/api/complete`, the transcript is captured, and
   a background **summarizer** emits `{summary_md, what_worked, what_broke, lessons,
   tags}` plus an embedding for future retrieval.

## Fusion — optional multi-model brain

Fusion is an **opt-in, default-off** layer that fans a task out to a panel of models at
**different labs in parallel**, then has a local judge synthesize the results. It calls
each provider's **native API directly** — no OpenRouter, no aggregator, no router margin.
With the toggle off, behavior is byte-for-byte identical to the local-only path, and
Fusion **never raises**: any panel failure silently falls back, so a flaky provider can
never abort a dispatch.

- **Two modes (per-dispatch toggles).**
  - *Rewriter panel → judge* — the panel **authors** the dispatched prompt (a drop-in
    upgrade to the single-model rewriter).
  - *Enrichment* — the panel **reasons about** the task and the judge distills it into a
    `## Multi-model analysis` block (consensus, contradictions, partial coverage, unique
    insights, blind spots) appended to the prompt. The executor weighs it as context, not
    gospel — often safer than trusting non-frontier models to author the final artifact.
- **A six-provider panel, native APIs.** Each lab is called through its own small
  `providers/<name>.py` speaking that lab's native API — DeepSeek (`deepseek-chat`), xAI
  (`grok-4`), Gemini (`gemini-2.5-flash`), MiniMax (`MiniMax-Text-01`), GLM (`glm-4.6`),
  and Qwen (`qwen-max`). No shared "OpenAI-compatible" adapter — a non-OpenAI-shaped API
  is just a different script. Every script returns the same normalized result, so adding
  a provider = one script + one registry line.
- **Presets + per-dispatch picker.** Named presets choose the panel — `budget`,
  `balanced`, or `max` (all six) — and the dispatch form exposes a checkbox picker so you
  pick seats per task (providers with no key resolve greyed-out).
- **Per-seat lenses (decorrelation).** Each seat can answer *through* a named
  perspective — `risks`, `simplest`, `ambiguity`, or your own — so the panel makes less
  correlated errors and the judge gets genuinely different angles to synthesize. Opt-in;
  a lens-free panel is unchanged.
- **Cost accounting.** Every provider carries list prices ($/M in→out) in the registry,
  so each Fusion run reports its `cost_usd` from actual token usage. The judge itself is
  free — it runs on the local `claude` CLI (Opus) in a visible tab, so **Fusion never
  calls the Anthropic API**; only the non-Anthropic panelists egress.
- **Config.** Provider registry, models, prices, presets, and lenses live in `config.json`
  and are editable from the settings page (no restart). API keys live in a separate
  `chmod 600` file, never shown or logged in the UI. Fusion is "available" only with ≥2
  active providers.

## Why not the Anthropic API?

All "brain" work — rewriter, summarizer, onboarding, and the Fusion judge — runs through
the `claude` CLI in **visible iTerm2 tabs** you can watch live (a headless subprocess is
only the fallback when iTerm2 is absent), using your existing Claude subscription. The
orchestrator itself spends zero API tokens. Embeddings run locally via Ollama.

## Where data lives

`~/.orchestrator/` — SQLite DB + transcripts + vector embeddings. Outside the repo.

## Stack

Python 3.11 · FastAPI + uvicorn · HTMX · stdlib `sqlite3` · iTerm2 driven via AppleScript
(`osascript`) · Ollama (`embeddinggemma`) for local embeddings.

## Requirements (macOS)

- iTerm2 — `brew install --cask iterm2`
- `claude` CLI on PATH
- Ollama running with `embeddinggemma` pulled
- Python 3.11+

See `USAGE.md` for the full walkthrough, `PLAN.md` for the per-phase breakdown, and
`FUSION_PLAN.md` for the Fusion design and its hard-rule deviations.
