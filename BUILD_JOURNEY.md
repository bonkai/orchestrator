<!--
DRAFT — "build journey" thread for Twitter/X.
Posting is your step, not mine.

This file is the readable working draft.
The numbered sections (1/5, 2/5, etc.) map to individual tweets when posted.
Each section is long-form — trim/split as you post.

Heads up: saving this file auto-commits and pushes to origin/main within seconds.
-->

---

## 1/5 — The origin

So this whole thing started because I was sick of having 10 terminal tabs open with 10 manual Claude Code sessions going at once.

So I built one browser UI that dispatches tasks to Claude Code across every project on my laptop — except it enriches each one first, and it actually learns from every run so the next one starts off smarter.

The loop looks like this:

- Grab the project's memory + most similar past tasks from any project I've ever run
- Have Claude rewrite my prompt with all that context
- Spawn the session in its own iTerm2 tab
- When it finishes, capture what happened, summarize it, embed it, and feed it back for next time

It's a closed loop and it all runs on one machine.

---

Three rules I set for myself up front that shaped everything:

1. **No Anthropic API calls** — every bit of brain work goes through the claude CLI on my existing subscription
2. **Brain calls have to be visible, never headless** — every single Claude invocation runs in an iTerm2 tab I can actually watch
3. **Local only** — everything runs on this laptop, nothing leaves it

That's $0 marginal cost on a sub I already pay for, everything watchable, nothing phoning home.

---

I built it up from a tiny MVP through 10 phases, then added a multi-model Fusion brain, a $0 OpenAI Codex integration, and a thing that refines your follow-ups mid-session.

Everything I'm about to say is grounded in the actual code. I checked every symbol before writing this.

---

## 2/5 — The core build (Phases 1–10)

**Phase 1 — Walking skeleton**

FastAPI + HTMX for the UI, stdlib `sqlite3` for storage (no ORM), DB at `~/.orchestrator/orchestrator.db`, kept outside the repo so the repo stays clean.

`spawn_iterm2()` drives iTerm2 entirely through AppleScript — no iTerm2 Python lib, it just opens a tab and runs the task.

Safety from day one:

- Per-dispatch stop: SIGTERM → 5s grace → SIGKILL
- Global stop-all
- Wall-clock cap in `watchdog.py` (1800s = 30 min by default)

---

**Phase 2 — Completion logging**

A global Stop hook (`notify_complete.sh`) gets merged into `~/.claude/settings.json`.

When a session ends it posts to `/api/complete`, which:

- Writes an `outcomes` row (status: `completed`, `killed`, `failed_to_spawn`, `orphaned`, or `paused`)
- Copies the transcript to `~/.orchestrator/transcripts/`

The hook is env-gated — it's a total no-op unless `ORCHESTRATOR_RUN_ID` is set in the env.

My own manual Claude sessions never trigger it.

---

**Phase 3 — Context bundler (`bundle.py`)**

Before a task ever runs, this assembles a bundle from the project:

- `CLAUDE.md`
- `memory/` and `knowledge/` folders
- Recent task files
- Live git state

It figures out where everything lives by reading `.forge.json` layout, with sane defaults if there isn't one.

Caps:

- 5,000 chars per file
- 50,000 chars total

So one giant file can't blow the prompt. Every path goes through `_safe_join` and `_within_project` so nothing can escape the project root through symlinks.

There's a `/bundle/<id>` view so you can see exactly what context a task is going to get.

---

**Phase 4 — The rewriter ("Call A")**

`rewriter.py` + `prompts/REWRITER.md`.

Takes the bundle, calls Claude, gets back structured JSON:

- `rewritten_prompt`
- `rationale`
- `files_to_read`
- `hazards_acknowledged`
- `proposed_edits`

Shows all of that in an editable preview so you stay in control.

Two things I'm proud of here:

- The rewriter runs at Opus/high on purpose — it's the highest-leverage call in the system
- The runner scrubs `ORCHESTRATOR_RUN_ID` out of the child env before calling Claude, so a brain call can never accidentally trip its own Stop hook and pollute the log

---

**Phase 5 — The summarizer ("Call B")**

`summarizer.py` + `SUMMARIZER.md`.

The second a dispatch finishes, this distills the raw transcript JSONL:

- Drops the noise
- Caps each block around 1.5KB, whole distillation around 30KB
- Produces: `summary_md`, `what_worked`, `what_broke`, `lessons`, `tags`

That becomes the project's memory for next time.

This one is deliberately Sonnet/medium — a distillation shouldn't escalate to the expensive model.

It fires as a background asyncio task held in a strong-reference set so the GC can't kill it mid-flight. There's an atomic guard so only the race winner fires — no double summaries.

---

**Phase 6 — Cross-project retrieval**

Real semantic search, not keyword matching.

`embeddings.py` talks to a local Ollama running Google's `embeddinggemma` (768-dim) over HTTP at `127.0.0.1:11434`.

Zero new Python deps. $0. Nothing leaves the laptop.

`retrieval.py`:

- Stores every vector as a packed float32 BLOB in SQLite
- Hand-rolls the cosine in pure Python — no numpy
- `find_similar()` pulls the top-5 most similar past tasks across any project (cosine ≥ 0.3) and feeds them into the rewriter

Defensive details:

- Vectors are NaN/Inf-guarded before storage
- Any row whose stored dimension doesn't match the query gets skipped, so swapping the embedding model can't quietly poison results with garbage cosines

---

**Phase 7 — Loop watchdog (`loop_watchdog.py`)**

A second hook, `notify_tool_use.sh` (a PreToolUse one, same env-gating), posts a fingerprint of `(tool_name, input_hash)` to `/api/tool_use` on every tool call.

Each dispatch keeps a ring buffer — a `deque` with `maxlen=8`. When all 8 recent fingerprints are identical, it fires `watchdog.manual_kill` with `reason="loop:<tool>"`.

Funny enough `CLAUDE.md` still lists this as "planned, not in the MVP." It's been live since Phase 7.

When the doc and the code disagree, the code wins.

---

**Phase 8 — Auto file-edits (`edits.py`)**

The rewriter can propose three kinds of edit:

- `append_to_memory`
- `append_to_knowledge`
- `create_task_file`

Each shows up as a checkbox you apply through `/apply_edits`.

Validation is paranoid:

- `.md` files only
- No `..` in the path
- No dotfiles
- No absolute paths
- No symlink that escapes the project root
- Parent dir must be declared in that project's `.forge.json` layout
- 50KB content cap
- `create_task_file` flat-out refuses to overwrite anything

It can enrich memory but it physically can't write outside the lines.

---

**Phase 9 — Onboarding (`onboarding.py`)**

A one-time "analyze setup" sweep per project.

It scans for:

- Rule files: `CLAUDE.md`, `.cursorrules`, `.cursor/rules/*.mdc`, `AGENTS.md`, `.github/copilot-instructions.md`, `README`
- Stack signals: `package.json`, `requirements.txt`, `Cargo.toml`, etc.
- Top-level directory structure

Produces: `project_summary`, `strengths`, `gaps`, `recommendations`, `proposed_edits` — and those edits go through the exact same validation gate as Phase 8.

I ran it on the orchestrator itself: 50s, nailed all 4 strengths, all 3 missing-dir gaps, and gave me 4 one-click edits plus 1 manual `CLAUDE.md` addition.

---

**Phase 10 — Visible brain calls (the payoff)**

Every brain call moved out of a hidden subprocess into its own iTerm2 tab.

`claude_runner.run_claude_json` + `spawn.spawn_brain_tab`:

- Runs `claude -p --output-format stream-json --verbose`
- Pipes it through `tee` into a sidecar file
- The reasoning and tool use scroll by live
- The structured result gets rebuilt from the `type:result` event (which carries `result`, `total_cost_usd`, `duration_ms`)

Brain tabs set `ORCHESTRATOR_BRAIN_ID` instead of `ORCHESTRATOR_RUN_ID` — so they stream live but never fire the Stop hook.

Now both brain calls and dispatched executors run in tabs I can watch.

The old headless path only survives as a fallback for machines without iTerm2.

Nothing the model does happens off-screen.

---

## 3/5 — Fusion (F0–F9)

Once the local thing was solid, I built an optional multi-model layer on top — Fusion, all in `FUSION_PLAN.md`.

It's opt-in and off by default.

When you flip it on, instead of one Claude rewrite you get a panel of models from different labs answering in parallel, and then a local judge synthesizes them.

Two things make it safe to leave in:

- With the toggle off, behavior is byte-for-byte identical to the normal local path
- `run_fusion_json` literally never raises — if the panel comes up short it silently falls back to the plain single-Claude call

It hands back the same `ClaudeRun` dataclass everything else already expects, so it just slots in.

---

**The panel — six labs, native APIs**

Each provider is its own standalone script under `orchestrator/providers/`:

- `deepseek.py` — `deepseek-chat`
- `xai.py` — `grok-4`
- `gemini.py` — `gemini-2.5-flash`
- `minimax.py` — `MiniMax-Text-01`
- `glm.py` — `glm-4.6`
- `qwen.py` — `qwen-max`

Each one speaks its lab's native API in whatever shape that lab wants, and they all return the same normalized JSON: `ok`, `text`, `model`, `prompt_tokens`, `completion_tokens`, `error`.

Adding a new lab is one script + one registry line.

No OpenRouter, no aggregator. Every dollar spent goes direct to the lab.

One detail that took a while: GLM has to hit `/api/coding/paas/v4` (the flat-subscription endpoint), not `/api/paas/v4` (the prepaid one that times out at 1113s without a balance).

---

**Presets and cost**

Three presets pick which seats fire:

- `budget` — DeepSeek + MiniMax + Gemini
- `balanced` — DeepSeek + Grok + Qwen
- `max` — all six

The fan-out runs through a `ThreadPoolExecutor` so wall-clock time is the slowest seat, not the sum of all of them.

Every run reports real money:

- Each script returns true token counts
- `_panel_answer` prices them against per-provider dollar-per-million rates
- Cost = `(input × price_in + output × price_out) / 1e6`, summed across seats
- The judge adds $0 because it's the local Claude CLI

---

**Two modes**

1. **Rewriter panel** — seats author the dispatched prompt (drop-in upgrade to the single-model rewriter)

2. **Enrichment** (`fusion.py`) — the panel reasons about the task and the judge appends a fenced `## Multi-model analysis` block:
   - consensus
   - contradictions
   - partial_coverage
   - unique_insights
   - blind_spots

   The executor weighs that as context, not gospel. With non-frontier panelists, this mode is often the safer one — you're not trusting them to write the final thing.

---

**The judge pipeline**

Not just a merge — it's judge → verify → re-judge:

1. `_judge_prompt` synthesizes the panel
2. `_verify_prompt` (opt-in critic) returns a strict JSON verdict: `defect` true/false + an issues list — deliberately conservative
3. If there's a real defect, `_rejudge_prompt` fixes it. Otherwise it keeps the original.

The engine is selectable (C3): a `judge_engine` param routes the judge, verifier, and re-judge through Claude (default) or Codex.

Every one of those steps is a local-CLI call. Even the full multi-model path makes zero Anthropic API calls.

---

**Per-seat lenses (`_apply_lens`)**

A lens makes one seat answer the same task through a single perspective — prepended so the original prompt stays verbatim and last.

Ten lenses seeded:

- `risks`, `simplest`, `ambiguity`, `first-principles`, `user-intent`
- `long-horizon`, `concrete`, `adversary`, `precedent`, `evidence`

Why this matters: clones share weights and blind spots. They tend to make the same mistake and agree on it. Diverse lenses force genuinely different angles — uncorrelated errors — which is what makes an ensemble worth anything.

Assignment strategy in `FUSION_LENS_PLAYBOOK.md`:

- Put your smartest model as judge, not a seat
- Deep lenses (`first-principles`, `adversary`) go on strong seats; grounding lenses (`concrete`, `precedent`) on weaker ones
- Pick lenses by the task's dominant failure mode

---

## 4/5 — Codex (C0–C6) + Supermax

**Codex — $0 OpenAI executor**

`CODEX_PLAN.md`, phases C0–C6.

The whole thing hinged on whether `codex exec --json` could run non-interactively on a ChatGPT subscription with no `OPENAI_API_KEY`. Verified live — it does. That's the Branch A path, pinned to codex-cli 0.141.0.

Codex shows up three ways:

- Fusion panel seat (`kind:"codex_cli"`)
- Selectable judge engine
- Watchable dispatch executor at `-s danger-full-access` (full parity with Claude, reversible per machine in config)

---

**The hook gap problem**

Codex never reads `~/.claude/settings.json`, so a codex dispatch gets none of the Stop or PreToolUse hooks.

Solution — the in-band approach:

- The run.sh writes codex's real PID through a FIFO (a pipeline `$$` would have orphaned the actual codex process)
- It writes to the same `pids/` path the Claude watchdog uses, so manual-kill, kill-all, the cap, and the orphan reaper all just work for free
- `_codex_dispatch_poller` tails the sidecar JSONL live, builds the timeline and loop-watchdog fingerprint, and on completion calls `_finalize_dispatch` — the exact same core that `/api/complete` uses
- No Claude hooks, no self-POST

---

**Codex details worth knowing**

- Live testing caught a stale model ID: `gpt-5-codex` gets rejected by a ChatGPT account. Corrected to `gpt-5.5`
- Cap hits hard-kill (no resume — codex has no session-resume equivalent)
- Default 2-dispatch concurrency cap so a fan-out doesn't blow the shared subscription window
- `OPENAI_API_KEY` is scrubbed from the child env to ensure the $0 subscription path, never the billed API path

---

**Supermax — mid-session follow-up refining**

`SUPERMAX_PLAN.md`.

Once a session is running, every follow-up you type gets improved through the Fusion panel first.

`POST /dispatch/{id}/refine`:

- Reuses the exact seats you picked at dispatch time (so it fuses for free with seats you already know answer)
- Two steps run in a `ThreadPoolExecutor` so a slow panel never stalls the FastAPI event loop
- Resolves the live transcript (fingerprint-matched so it doesn't grab an unrelated manual session)
- Builds a purpose-aware summary of that transcript so vague follow-ups like "do the same to the other one" resolve to real symbols

The key fix that made this work: `run_fusion_json`'s `.text` is an **answer**, not a prompt. If you hand a raw follow-up to the panel, every seat tries to go do the work. `_supermax_refine_prompt` wraps the follow-up so "the task" literally becomes "rewrite this message" and the required output is plain improved text. The single-model fallback gets the same wrapper.

---

**Supermax v2 — designed but gated**

Live injection (writing the improved text straight into the session's stdin) is designed but I deliberately didn't ship it.

The targeting is solved — every tab is tagged with `user.orch_id` so I can find it. But reliable mid-TUI injection isn't there:

- No idle signal over AppleScript
- A trailing newline auto-submits
- Quoting has to survive the cmd-to-AppleScript hop
- The Codex executor is one-shot with no live stdin

So I built the targeting half and refused to ship the fragile half.

`write_text_to_session_by_var` genuinely doesn't exist in the code yet.

---

## 5/5 — What didn't get built, and where it stands

**The things I evaluated and declined**

**FAPO-style prompt optimizer** (`FAPO_PLAN.md` — design study, no-go):

The integration seam was fine — FAPO's optimizer is itself a Claude CLI agent, which is exactly this project's pattern.

The problem was internal:

- No valid scorer. The only cheap one I could stand up is LLM-as-judge — Claude grading Claude — which is circular. It converges on prompts that flatter the judge.
- The `outcomes` status enum I already have is confounded — it's not a quality grade.
- The auto-push daemon would ship any un-reviewed `REWRITER.md` edit to origin/main within seconds.

**Token compression tool** — evaluated and declined. No API seam on a $0 subscription, so there was nowhere to hook it in cleanly.

Knowing what not to build, and writing down exactly why, is half the engineering.

---

**Where it stands**

Built and offline-tested:

- Phases 1–10 (walking skeleton through visible brain calls)
- Fusion F0–F9 (six-provider panel, native APIs, judge/verify/re-judge, lenses)
- Codex C0–C6 ($0 executor, Fusion seat, selectable judge)
- Supermax v1 (follow-up refining through the Fusion panel)

Suite: 491 green, 4 skipped.

---

Still open:

- Paid live-provider verify for the four providers I don't have keys for yet
- Supermax v2 live-injection (designed, gated)
- Codex operational questions: subscription caps, ToS, judge calibration

---

The whole thing runs on one laptop.

It spends $0 of API budget on its own thinking.

It keeps every LLM call in a terminal tab I can watch.

And it gets a little smarter with every dispatch — because every finished session gets summarized, embedded, and fed back into the next one.

That's the build.
