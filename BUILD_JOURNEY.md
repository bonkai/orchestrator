<!--
DRAFT — Twitter/X "build journey" thread, condensed to 5 long-form chapter
tweets (X Premium; each well under the 25k single-post limit). Posting is your
step, not mine. Each block below is one tweet, plain text so it pastes straight
into X. Tweet 1 quotes the three hard rules verbatim from CLAUDE.md (backticks
included, on purpose). Every claim is grounded in PLAN.md / the *_PLAN.md docs
AND verified against the real code (orchestrator/lib/ + app.py). Nothing invented.
The earlier 27-tweet version is recoverable from git history (BUILD_JOURNEY.md).
NOTE: writing this file trips the auto-push daemon -> it commits + pushes to
origin/main within seconds. "Draft" is not private if origin is public.
-->

# Orchestrator — Build Journey (draft thread, 5 chapters)

---

1/5 I replaced my "10 terminal tabs, 10 manual Claude Code sessions" workflow with one local browser UI that dispatches enriched tasks to Claude Code across every project on my laptop — and learns from every run so the next task starts better-informed.

The loop: enrich a task with that project's memory + the most similar past tasks from ANY project → have Claude rewrite the prompt → spawn the session in its own iTerm2 tab → capture the outcome → summarize it → embed it → feed it back in. A closed loop, on one machine.

Three hard rules governed every decision (verbatim from my own CLAUDE.md):
"No Anthropic API calls. All 'brain' work … runs through the `claude` CLI on your existing subscription."
"Brain calls are visible, never headless. Every `claude` invocation … runs in a watchable iTerm2 tab."
"Local only. Everything runs on this laptop."

So: $0 marginal cost on my existing subscription, every LLM call watchable in a real terminal tab, nothing leaving the machine. Built MVP → today across 10 phases, then a multi-model "Fusion" brain, a $0 OpenAI-codex integration, and a mid-session refiner. Everything below is grounded in the actual code. Here's the whole build 🧵

---

2/5 🧱 CHAPTER 1 — THE LOCAL LEARNING LOOP (Phases 1–10)

The entire system before any multi-model stuff: a closed loop that gets smarter each run, on one laptop.

Phase 1 — the walking skeleton. FastAPI + HTMX + the stdlib sqlite3 module (no ORM), DB at ~/.orchestrator/orchestrator.db, deliberately outside the repo. spawn_iterm2() drives iTerm2 purely through AppleScript (no iterm2 Python lib) to open a tab running claude "$task" verbatim. 10+ concurrent dispatches, each tracked per-project. Safety from day one: per-dispatch stop (SIGTERM → 5s grace → SIGKILL), a global stop-all, and a wall-clock cap (watchdog.py, 1800s / 30 min default).

Phase 2 — completion logging, where the learning loop is born. A global Stop hook (notify_complete.sh) merged into ~/.claude/settings.json POSTs to /api/complete on session end → writes an outcomes row (status enum: completed / killed / failed_to_spawn / orphaned / paused) and copies the transcript to ~/.orchestrator/transcripts/. The crucial bit: the hook is env-gated — a complete no-op unless ORCHESTRATOR_RUN_ID is set, so my own manual claude sessions are never touched.

Phase 3 — the context bundler (bundle.py). Before a task ever runs, it assembles CLAUDE.md + memory/ + knowledge/ + recent task files + live git state, reading the project's .forge.json "layout" (with sane defaults). Hardened: a 5,000-char/file cap and a 50,000-char total cap, and every path goes through _safe_join / _within_project so a malicious or symlinked path can't escape the project root. A /bundle/<id> view lets you inspect exactly what context a task will get.

Phase 4 — the rewriter, "Call A" (rewriter.py + prompts/REWRITER.md). It feeds the bundle to a Claude call that returns structured JSON: {rewritten_prompt, rationale, files_to_read, hazards_acknowledged, proposed_edits}, shown in an editable preview so you stay in control. Two things I'm proud of: it runs at opus/high on purpose (the highest-leverage call), and the runner scrubs ORCHESTRATOR_RUN_ID out of the child env so a brain call can never trip its OWN Stop hook.

Phase 5 — the summarizer, "Call B" (summarizer.py + SUMMARIZER.md). The moment a dispatch completes, it distills the transcript JSONL (blocks capped ~1.5KB, whole thing ~30KB) into {summary_md, what_worked, what_broke, lessons, tags} — the project's institutional memory. Deliberately sonnet/medium, NOT the rewriter's opus (a distillation shouldn't escalate). It fires as a background asyncio task held in a strong-reference set so the GC can't kill it mid-flight, with an atomic "only the race winner fires" guard.

Phase 6 — cross-project retrieval, and it's genuinely semantic, not keyword FTS. embeddings.py talks to a LOCAL Ollama running Google's embeddinggemma (768-dim) at 127.0.0.1:11434 — zero new Python deps, $0, nothing leaving the laptop. retrieval.py stores each vector as a packed float32 BLOB in SQLite and hand-rolls cosine (no numpy). find_similar() pulls the top-5 most similar past tasks from ANY project (cosine ≥ 0.3) into the rewriter. It's defensive: vectors are NaN/Inf-guarded, and any row whose stored dim doesn't match is skipped — so swapping the embedding model can't poison results with garbage cosines.

Phase 7 — the loop watchdog (loop_watchdog.py). A second hook, notify_tool_use.sh (PreToolUse, same env-gating), POSTs a (tool_name, input_hash) fingerprint to /api/tool_use per tool call. Each dispatch keeps a ring buffer (a deque, maxlen=8); when all 8 recent fingerprints are identical, it fires watchdog.manual_kill(reason="loop:<tool>"). (CLAUDE.md STILL lists this as "planned, not in MVP" — it's been live since Phase 7. When a doc and the code disagree, the code wins; I verified it before writing this.)

Phase 8 — auto file-edits (edits.py), so the orchestrator can grow a project's memory, not just read it. The rewriter can propose three edit types — append_to_memory, append_to_knowledge, create_task_file — each a checkbox you apply via /apply_edits. The validation is paranoid by design: .md files only, no "..", no dotfiles, no absolute paths, no symlink that escapes the project, the parent dir must be declared in that project's .forge.json layout, a 50KB content cap, and create_task_file refuses to overwrite. It can enrich memory — but it physically cannot write outside the lines.

Phase 9 — project onboarding (onboarding.py + ONBOARDING.md): a one-time "analyze setup" sweep. It scans every flavor of rule file (CLAUDE.md, .cursorrules, .cursor/rules/*.mdc, AGENTS.md, .github/copilot-instructions.md, README), detects the stack (package.json / requirements.txt / Cargo.toml / …), and reads the top-level structure → {project_summary, strengths, gaps, recommendations, proposed_edits}, with the edits reusing Phase 8's exact validated path. I ran it on the orchestrator itself: ~50s, all 4 strengths, all 3 missing-dir gaps, 4 one-click edits + 1 manual CLAUDE.md addition.

Phase 10 — "visible, never headless," delivered. Every brain call moved out of a hidden subprocess into its own watchable iTerm2 tab. run_claude_json + spawn_brain_tab run claude -p --output-format stream-json --verbose | tee <sidecar>, so reasoning and tool use scroll live while the structured result is rebuilt from the type:result event (result / total_cost_usd / duration_ms). Brain tabs set ORCHESTRATOR_BRAIN_ID, NOT RUN_ID, so they stream live yet never fire the Stop hook. Today BOTH brain calls and dispatched executors run in tabs I can watch; the old headless path survives only as a fallback for machines without iTerm2. Nothing the model does happens off-screen.

---

3/5 🧠 CHAPTER 2 — FUSION: AN OPTIONAL MULTI-MODEL BRAIN (FUSION_PLAN.md, F0–F9)

With the local system complete, an opt-in, default-OFF layer. When it's on, instead of one Claude rewrite you get a PANEL of models at DIFFERENT labs answering in parallel, and a local judge synthesizes them into one. Two guarantees make it safe to ship: toggle it off and behavior is byte-for-byte identical to the local path; and run_fusion_json NEVER raises — any panel shortfall silently falls back to the plain single-Claude call, so a flaky provider can't abort a dispatch. It returns the same ClaudeRun dataclass the pipeline already expects.

Six labs, native APIs. Each provider is ONE standalone script under orchestrator/providers/ — deepseek.py (deepseek-chat), xai.py (grok-4), gemini.py (gemini-2.5-flash), minimax.py (MiniMax-Text-01), glm.py (glm-4.6), qwen.py (qwen-max) — each speaking that lab's native API in its own shape, each printing the SAME normalized JSON ({ok, text, model, prompt_tokens, completion_tokens, error}). Adding a lab = one script + one registry line; no shared "OpenAI-compatible" adapter. The earlier design routed the whole panel through OpenRouter — I dropped that ENTIRELY; the only money spent is per-token, straight to each lab. (Hard-won detail: GLM points at z.ai's coding-plan endpoint /api/coding/paas/v4 — the flat-subscription host — not the prepaid /api/paas/v4, which 1113s without a top-up.)

Cost and speed are first-class. A preset picks which seats fire: budget (DeepSeek + MiniMax + Gemini), balanced (DeepSeek + Grok + Qwen), or max (all six, high-stakes only). The fan-out runs through a ThreadPoolExecutor so wall-clock ≈ the slowest seat, not the sum. Every run reports real money: each script returns true token counts, and _panel_answer prices them against per-provider $/M rates — cost = Σ (in × price_in + out × price_out) / 1e6. The judge adds $0 (it's the local claude CLI), so the entire out-of-pocket is the panel egress, surfaced per-seat and stamped onto the outcomes row.

Two modes, both built. (1) Rewriter-panel: the seats AUTHOR the dispatched prompt — a drop-in upgrade to the single-model rewriter. (2) Enrichment (fusion.py): the panel instead REASONS about the task and the judge appends a fenced "## Multi-model analysis" block — consensus, contradictions, partial_coverage, unique_insights, blind_spots — that the executor weighs as context, not gospel. With strong-but-non-frontier panelists, mode 2 is often safer: you're not trusting them to write the final artifact.

The judge doesn't just merge — it runs judge → verify → re-judge. _judge_prompt synthesizes; an opt-in critic (_verify_prompt) checks it and must return a STRICT JSON verdict {"defect": bool, "issues": [...]}, conservative by design; on a real defect ONE re-judge (_rejudge_prompt) corrects it, else it keeps the original (fail-safe). It's engine-selectable (C3): a judge_engine param routes judge, verifier, and re-judge through Claude (default) or Codex. Every step is a local-CLI call — so even the multi-model path makes ZERO Anthropic API calls.

The decorrelation trick I like most: per-seat LENSES (_apply_lens). A lens makes one seat answer the SAME task through a single perspective, prepended so the original prompt stays verbatim and LAST (its output format still travels, and the judge always sees the unmodified task). 10 seed lenses, each owning a distinct failure axis: risks, simplest, ambiguity, first-principles, user-intent, long-horizon, concrete, adversary, precedent, evidence. WHY this beats N identical calls: clones share weights and blind spots, so they make the SAME mistake and AGREE on it — the judge ends up looking at one wrong answer three times. Diverse lenses force genuinely different angles (the uncorrelated errors an ensemble needs), and since there are only ~6–7 truly orthogonal angles, distinct angles beat headcount. FUSION_LENS_PLAYBOOK.md turns this into rules: your SMARTEST model is the JUDGE, not a seat; put deep lenses (first-principles, adversary) on strong seats and grounding ones (concrete, precedent) on weak; pick lenses by the task's dominant failure mode. A "· <lens>" badge in the breakdown proves the panel ran decorrelated.

---

4/5 ⚡ CHAPTER 3 — CODEX & SUPERMAX (the advanced integrations)

Codex (CODEX_PLAN.md, C0–C6): the same pattern, for OpenAI's codex CLI. The load-bearing premise — "$0, exactly like Claude Code" — was verified live: codex exec --json runs non-interactively on a ChatGPT subscription with NO OPENAI_API_KEY (Branch A confirmed, pinned to codex-cli 0.141.0). So codex shows up three ways: a Fusion panel seat (kind:"codex_cli"), a selectable judge, AND a watchable dispatch executor — at -s danger-full-access for full claude parity (config-reversible per machine). The clever part is the executor's missing-hooks problem: codex never reads ~/.claude/settings.json, so it gets no Stop/PreToolUse hooks. The fix — the run.sh writes codex's REAL pid (via a FIFO, not a pipeline $$ that would orphan it) to the SAME pids path the claude watchdog uses, so manual-kill, kill-all, the cap, and the orphan reaper all work for free; then an in-band poller (_codex_dispatch_poller) tails the sidecar JSONL → live timeline + the loop-watchdog fingerprint → and on completion calls _finalize_dispatch, the exact same core /api/complete uses. No Claude hooks, no self-POST. (Live testing caught a stale model id: gpt-5-codex is rejected by a ChatGPT account — corrected to gpt-5.5. A codex cap hard-kills since there's no resume, and a default 2-dispatch concurrency cap protects the shared subscription window.)

Supermax (SUPERMAX_PLAN.md): once a session is RUNNING, every follow-up you're about to type gets improved through the same Fusion panel first. POST /dispatch/{id}/refine takes your raw follow-up and reuses the EXACT seats you picked when you dispatched the task — so it fuses for free with seats you already know answer, instead of a default preset that might silently degrade to one model. The two-step (build context, then fuse) runs in a threadpool so a minutes-long panel never stalls the FastAPI event loop. The make-or-break insight: run_fusion_json's .text is an ANSWER, not a prompt — hand the judge a raw follow-up like "also do the other file" and every seat tries to DO the work, so you'd get answered twice. The fix (_supermax_refine_prompt): wrap it so "the task" literally becomes "rewrite this message" and the output format becomes "plain improved message text"; the single-model fallback gets the SAME wrapper. It's also context-aware — it resolves the live transcript (fingerprint-matched so it doesn't grab an unrelated manual session), writes a purpose-aware summary, and resolves "do the same to the other one" to real symbols. v1 is shipped + live. v2 — writing the improved text straight into the live session's stdin — is DESIGNED but GATED: targeting is solved (each tab is tagged with the user.orch_id session var), but reliable mid-TUI injection isn't (no idle signal over osascript, a trailing newline auto-submits, quoting must survive the cmd→AppleScript hop, and the codex executor is one-shot with no live stdin). I built the targeting half and refused to ship the fragile half — write_text_to_session_by_var genuinely doesn't exist yet.

---

5/5 🚦 WHERE IT STANDS — and the one I deliberately didn't build

Restraint is part of the story. I did a full design study of a FAPO-style closed-loop prompt-optimizer (FAPO_PLAN.md) against this codebase and called NO-GO. The integration seam was actually fine — FAPO's optimizer is itself a Claude-CLI agent, exactly this project's pattern. The blocker was internal: there is no valid scorer. The only cheap one is LLM-as-judge, i.e. Claude grading Claude — circular, it just converges on prompts that flatter the judge — and the process-status outcome enum is confounded, not a quality grade. Plus the auto-push daemon would ship any un-reviewed REWRITER.md edit to origin/main within seconds. The same judgment earlier killed a token-compression tool (no API seam on a $0 subscription). Knowing what NOT to build — and writing down exactly why — is half the engineering.

Where it stands today: Phases 1–10, Fusion F0–F9, Codex C0–C6, and Supermax v1 — all built and offline-tested (the suite was at 491 green / 4 skipped at last count). Genuinely remaining: the paid cross-lab live verify for the four providers I don't yet have keys for, Supermax v2 live-injection (designed, gated), and codex's open operational questions (subscription caps, ToS, judge calibration).

The whole thing runs on one laptop, spends $0 of API budget on its own "thinking," keeps every LLM call in a terminal tab I can watch, and gets a little smarter with every dispatch — because each finished session is summarized, embedded, and fed back into the next one. That's the build. /end
