# Orchestrator

A local browser UI that dispatches enriched tasks to a coding agent — **Claude Code**,
**Codex**, or **Kimi Code** — across many projects on one machine, with project memory,
cross-project retrieval, and an optional multi-model "Fusion" brain layer auto-injected
into every task.

Each dispatch opens a real CLI session in its own iTerm2 tab that you can watch live.
Every completed session is summarized and embedded, so the next similar task — in any
project — starts better-informed. Dispatches are independent and run concurrently.

Everything runs on your existing **subscriptions**: the orchestrator itself spends zero
Anthropic API tokens, and the Codex and Kimi engines run on their own subscription
logins. Only the optional cross-lab Fusion provider seats cost per-token money.

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

1. **Add a project, type a task.** Optionally run **analyze project** to generate a
   baseline of memory/knowledge/task files. It's re-runnable any time to check for new
   gaps, and every past round is browsable under **history**. Drag files anywhere into
   the project pane (or use the file picker) to attach them to the next dispatch.
2. **Context bundle** — orchestrator assembles a bundle from the project (`CLAUDE.md`,
   `memory/`, `knowledge/`, recent tasks, git state) and retrieves the top semantically
   similar past tasks from *every* project (Ollama + `embeddinggemma`, local).
3. **Rewrite** — a `claude` brain call rewrites your prompt with that context, and can
   propose small file edits (memory entries, new task files) for you to apply.
4. **Send** — the form is fire-and-forget: **`rewrite & send →`** runs the rewrite then
   dispatches, **`skip rewrite & send`** dispatches your prompt verbatim. Either way the
   browser never navigates; the runs panel below picks up the new row when it's live.
   A new iTerm2 tab opens with the chosen engine running the task.
5. **Safety** — per-dispatch stop, global stop-all, and a wall-clock cap (**4h default /
   6h max**); on timeout the dispatch is **paused and resumable**, not killed. A **loop
   watchdog** kills a session that repeats the same tool call 8× in a row. Every kill is
   logged with a reason (`manual`, `timeout`, `loop:Bash`, …) so the learning corpus
   knows what went wrong.
6. **Completion** — a Stop hook posts to `/api/complete`, the transcript is captured, and
   a background **summarizer** emits `{summary_md, what_worked, what_broke, lessons,
   tags}` plus an embedding for future retrieval.

## Picking the executor — the model picker is the engine picker

The dispatch form's **executor model** dropdown selects both the model *and* the engine:
a Codex id routes to the Codex executor, a Kimi alias to the Kimi executor, everything
else to Claude. A second, optional **brain** picker (Claude-only) sets the tier for the
*pre*-executor work — the rewrite and the Fusion judge — so you can run a cheap brain in
front of an expensive executor. Left on `default`, the rewrite stays Opus/high and the
Fusion judge follows the executor.

| Engine | Models | Effort ladder | Auth |
|---|---|---|---|
| **Anthropic** | `fable` · `opus` *(default)* · `sonnet` · `haiku` | `low` `medium` `high` `xhigh` `max` | Claude subscription |
| **Codex** | `gpt-5.6-sol` *(default)* · `gpt-5.6-terra` · `gpt-5.6-luna` · `gpt-5.5` · `gpt-5.4` · `gpt-5.4-mini` | `minimal` `low` `medium` `high` `xhigh`, plus *default* (the model's own) | ChatGPT subscription (`codex login`) |
| **Kimi** | `kimi-code/k3` *(default)* · `kimi-code/kimi-for-coding` · `kimi-code/kimi-for-coding-highspeed` | — (kimi-code has no per-call effort flag) | Kimi subscription (`kimi login`) |

The two id vocabularies are **not** interchangeable, and the difference is easy to get
wrong:

- **Claude ids are versionless aliases** the CLI resolves (`opus` → the current Opus).
- **Codex ids are always versioned — there is no alias layer.** The bare family name
  `gpt-5.6` is *rejected* on a ChatGPT account, exactly like a nonsense id.

An effort value that doesn't fit the chosen engine safely falls back to that engine's
default rather than failing. Codex and Kimi dispatches are each capped at **2 concurrent
runs** (a shared 5-hour subscription window is easy to exhaust); over the cap the
dispatch fails with a visible row — never a silent fallback to Claude. Claude dispatches
are uncapped.

> **Status:** the Kimi *seat* is live-verified; the Kimi **executor** has not yet been
> run end-to-end. See `KIMI_PLAN.md`.

## Fusion — optional multi-model brain

Fusion is an **opt-in, default-off** layer that fans a task out to a panel of models in
parallel, then has a judge synthesize the results. Cross-lab providers are called through
each lab's **native API directly** — no OpenRouter, no aggregator, no router margin. With
the toggle off, behavior is byte-for-byte identical to the local-only path, and Fusion
**never raises**: any panel failure silently falls back, so a flaky provider can never
abort a dispatch.

- **Three per-dispatch toggles.**
  - *fusion* — the panel **authors** the dispatched prompt (a drop-in upgrade to the
    single-model rewriter), judged into one result.
  - *enrich* — the panel **reasons about** the task and the judge distills it into a
    `## Multi-model analysis` block (consensus, contradictions, partial coverage, unique
    insights, blind spots) appended to the prompt. The executor weighs it as context, not
    gospel — often safer than trusting non-frontier models to author the final artifact.
    Works with both send buttons.
  - *verify* — after the judge synthesizes, a **$0 Claude-CLI critic** checks the result
    and, on a found defect, triggers **one** re-judge to fix it. Requires fusion; its
    default comes from server config (`fusion.verify`), not the browser.
- **Four kinds of seat, mixed freely.** Add as many of each as you like — duplicates
  included, which is the point when each carries a different lens:
  - **Claude Code seats** — local `claude` CLI, model + effort, **no API, $0**.
  - **Codex seats** — local `codex` CLI on the ChatGPT subscription, **no API, $0**.
  - **Kimi seats** — local `kimi` CLI on the Kimi subscription, **no billed API, $0**.
  - **Cross-lab provider seats** — external APIs that **cost tokens**, key-gated.
- **Six seeded cross-lab providers, native APIs.** Each lab is called through its own
  small `providers/<name>.py` speaking that lab's native API — DeepSeek (`deepseek-chat`,
  $0.44→$0.87 /M), xAI (`grok-4`, $1.25→$2.50), Gemini (`gemini-2.5-flash`, $0.30→$1.50),
  MiniMax (`MiniMax-Text-01`, $0.30→$1.20), GLM (`glm-4.6`, $1.40→$4.40), and Qwen
  (`qwen-max`, $1.25→$3.75). No shared "OpenAI-compatible" adapter — a non-OpenAI-shaped
  API is just a different script. Every script returns the same normalized result, so
  adding a provider is a config seed plus a matching `install.sh` block (kept in sync by
  a drift test).
- **Saved profiles.** Any panel — every seat, model, effort, and lens — can be saved
  under a name and re-applied to a later dispatch in one click.
- **Per-seat lenses (decorrelation).** Each seat can answer *through* a named
  perspective, so the panel makes less correlated errors and the judge gets genuinely
  different angles to synthesize. Ten ship by default — `risks`, `simplest`, `ambiguity`,
  `first-principles`, `user-intent`, `long-horizon`, `concrete`, `adversary`, `precedent`,
  `evidence` — and you can add your own. Opt-in; a lens-free panel is unchanged. See
  `FUSION_LENS_PLAYBOOK.md` (also linked in-app as the **lens guide**) for combos by task
  type.
- **Cost accounting.** Every provider carries list prices ($/M in→out) in the registry,
  so each Fusion run reports its `cost_usd` from actual token usage. CLI seats report
  `$0 (subscription)`. The judge is free too — it runs on a local CLI in a visible tab,
  so **Fusion never calls the Anthropic API**; only the cross-lab panelists egress.
- **Availability.** The Fusion toggle enables as soon as a ≥2-seat panel is buildable —
  the `claude` CLI alone is enough (add two free Claude seats), as is a logged-in `codex`
  or `kimi`, or ≥2 keyed providers.
- **Config.** Provider registry, models, prices, presets, and lenses live in
  `~/.orchestrator/config.json` and are editable from the settings page (no restart).
  Named presets (`budget`, `balanced`, `max`) seed the default provider seats. API keys
  live in that same `chmod 600` file, never shown or logged in the UI.

## Supermax — refine a follow-up through the panel

Once a dispatch is running, `POST /dispatch/{id}/refine` takes a follow-up message you're
about to send, builds context from the conversation so far (a purpose-aware summary of
the session transcript), and runs both through the Fusion panel — returning an improved
version to copy back into the live session. Because the panel's output is an *answer*,
the endpoint wraps the request to **improve the follow-up, not answer it**. The response
reports honestly whether a real panel ran or it fell back to a single Claude call.
Live injection into the running session is designed but gated — see `SUPERMAX_PLAN.md`.

## `/usage` — where you stand against every limit

A dashboard of per-engine usage and limit state, collected from dispatches as they run:

- **LIMITED badges** driven by error classification — quota/billing-cycle exhaustion and
  rate-limit throttles flip an engine to LIMITED; the next successful call clears it.
- **Codex percentage meter** read from codex's own rollout files, the only place its
  `used_percent` / `resets_at` are exposed.
- **Vendor deep links** plus the exact local command to check each engine by hand.

Limit strings are only recognized once they've been *observed and pinned* — Claude's and
Codex's usage-limit texts aren't pinned yet, so those surface as raw strings rather than
guesses. See `USAGE_PLAN.md`.

## Why not the Anthropic API?

All "brain" work — rewriter, summarizer, onboarding, and the Fusion judge — runs through
the `claude` CLI in **visible iTerm2 tabs** you can watch live (a headless subprocess is
only the fallback when iTerm2 is absent), using your existing Claude subscription. The
orchestrator itself spends zero API tokens. Embeddings run locally via Ollama.

## Where data lives

`~/.orchestrator/` — SQLite DB + transcripts + vector embeddings + `config.json`.
Outside the repo.

## Stack

Python 3.11 · FastAPI + uvicorn · HTMX · stdlib `sqlite3` · iTerm2 driven via AppleScript
(`osascript`) · Ollama (`embeddinggemma`) for local embeddings.

## Requirements (macOS)

- iTerm2 — `brew install --cask iterm2`
- `claude` CLI on PATH
- Ollama running with `embeddinggemma` pulled
- Python 3.11+
- *Optional:* `codex` CLI, logged in (`codex login`) — for Codex executors and seats
- *Optional:* `kimi` CLI (kimi-code), logged in (`kimi login`) — for Kimi executors and seats

## Tests

```bash
source .venv/bin/activate && python -m unittest
```

## Docs

`USAGE.md` — full walkthrough · `PLAN.md` — per-phase breakdown ·
`FUSION_PLAN.md` — Fusion design and its hard-rule deviations ·
`FUSION_LENS_PLAYBOOK.md` — lens combos by task type ·
`CODEX_PLAN.md` · `KIMI_PLAN.md` · `USAGE_PLAN.md` · `SUPERMAX_PLAN.md`
