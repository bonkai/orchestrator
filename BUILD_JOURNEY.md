<!--
DRAFT — Twitter/X "build journey" thread (long-form; X Premium, no 280 cap).
Posting is your step, not mine. Each block below is one tweet, plain text so it
pastes straight into X. Tweet 2 quotes the three hard rules verbatim from
CLAUDE.md (backticks included, on purpose). Every claim is grounded in PLAN.md /
the *_PLAN.md docs AND verified against the real code (orchestrator/lib/ + app.py)
before it was written — see the symbol names. Nothing here is invented.
NOTE: writing this file trips the auto-push daemon -> it commits + pushes to
origin/main within seconds. "Draft" is not private if origin is public.
-->

# Orchestrator — Build Journey (draft thread)

---

1/27 I replaced my "10 terminal tabs, 10 manual Claude Code sessions" workflow with one local browser UI that dispatches enriched tasks to Claude Code across every project on my laptop — and then learns from every run so the next task starts better-informed.

The loop: enrich a task with that project's memory + the most similar past tasks from ANY project → have Claude rewrite the prompt → spawn the session in its own iTerm2 tab → capture the outcome → summarize it → embed it → feed it back in. Closed loop, on one machine.

Built MVP → today across 10 phases, then a multi-model "Fusion" brain, a $0 OpenAI-codex integration, and a mid-session refiner. Everything below is grounded in the actual code. Here's the whole build 🧵

---

2/27 Three hard rules governed every decision — quoted verbatim from my own CLAUDE.md, because they shaped (and constrained) everything that came after:

"No Anthropic API calls. All 'brain' work … runs through the `claude` CLI on your existing subscription."
"Brain calls are visible, never headless. Every `claude` invocation … runs in a watchable iTerm2 tab."
"Local only. Everything runs on this laptop."

So: $0 marginal cost on my existing subscription, every LLM call watchable in a real terminal tab, and nothing leaves the machine. Those three lines are why the architecture looks the way it does.

---

3/27 Phase 1 — the walking skeleton. FastAPI + HTMX + the stdlib sqlite3 module (no ORM), with the DB at ~/.orchestrator/orchestrator.db, deliberately outside the repo so the repo stays clean.

spawn_iterm2() drives iTerm2 purely through AppleScript (osascript) — no iterm2 Python lib — to open a tab that runs claude "$task" verbatim. You can fire 10+ dispatches at once; each gets its own tab and is tracked per-project.

Safety from day one: a per-dispatch stop (SIGTERM → 5s grace → SIGKILL), a global stop-all, and a wall-clock cap (watchdog.py, 1800s / 30 min default) that auto-kills a runaway. No prompt rewriting yet — this phase just proves the dispatch-and-control spine.

---

4/27 Phase 2 — completion logging, where the learning loop is born. install.sh merges a global Stop hook (notify_complete.sh) into ~/.claude/settings.json that POSTs to /api/complete when a session ends. The handler writes an outcomes row (status enum: completed / killed / failed_to_spawn / orphaned / paused) and copies the transcript to ~/.orchestrator/transcripts/.

The crucial detail: the hook is env-gated — it's a complete no-op unless ORCHESTRATOR_RUN_ID is set in the session's env. My own manual claude sessions never set it, so they're completely untouched. Every dispatch the orchestrator spawns sets it; nothing else does.

---

5/27 Phase 3 — the context bundler (bundle.py). Before a task ever runs, it assembles a context pack from the project: CLAUDE.md, memory/, knowledge/, recent task files, and live git state — reading the project's .forge.json "layout" block to know where each lives, with sane defaults when there's no forge file.

It's hardened: a 5,000-char cap per file and a 50,000-char cap total (so a giant file can't blow the prompt), and every path goes through _safe_join / _within_project so a malicious or symlinked path can't escape the project root. There's a /bundle/<id> HTML view and a raw markdown view, so you can inspect exactly what context a task will get.

---

6/27 Phase 4 — the rewriter, "Call A" (rewriter.py + prompts/REWRITER.md). It feeds the bundle to a Claude brain call that rewrites your terse prompt into a fuller one and returns structured JSON: {rewritten_prompt, rationale, files_to_read, hazards_acknowledged, proposed_edits}. The preview UI shows original → editable rewrite + rationale + hazards, so you stay in control.

Two details I'm proud of: the rewriter runs at opus/high on purpose (it's the highest-leverage call — worth the best model), and the vendored runner scrubs ORCHESTRATOR_RUN_ID out of the child env before calling claude, so a brain call can never trip its OWN Stop hook and pollute the dispatch log. (When first built this call was a captured headless subprocess — Phase 10 moves it into a visible tab.)

---

7/27 Phase 5 — the summarizer, "Call B" (summarizer.py + SUMMARIZER.md). The moment a dispatch completes, it distills the raw transcript JSONL — dropping noise, capping each block at ~1.5KB and the whole thing at ~30KB — and asks Claude for {summary_md, what_worked, what_broke, lessons, tags}. That becomes the project's institutional memory.

Two deliberate calls here: it runs at sonnet/medium, NOT the rewriter's opus/high — a distillation shouldn't escalate to the expensive model (this is a choice, not an oversight). And it fires as a background asyncio task that's held in a strong-reference set so the garbage collector can't kill it mid-flight, with an atomic "only the race winner fires" guard so a double Stop-hook can't double-summarize.

---

8/27 Phase 6 — cross-project retrieval, and it's genuinely semantic, not keyword FTS. embeddings.py talks to a LOCAL Ollama running Google's embeddinggemma (768-dim) over HTTP at 127.0.0.1:11434 — zero new Python deps, $0, nothing leaving the laptop.

retrieval.py stores each vector as a packed float32 little-endian BLOB right in SQLite and hand-rolls cosine similarity in pure Python (no numpy). After each summary it auto-embeds the dispatch; backfill_missing() catches up old ones. find_similar() pulls the top-5 most similar past tasks from ANY project (cosine ≥ 0.3 threshold) and injects them into the rewriter's context. It's defensive too: vectors are NaN/Inf-guarded on encode, and any row whose stored dim doesn't match the query is skipped — so swapping the embedding model can't silently poison results with garbage cosines.

---

9/27 Phase 7 — the loop watchdog (loop_watchdog.py), so a session stuck in a tool-call loop kills itself. A second hook, notify_tool_use.sh (PreToolUse, same env-gating), POSTs a (tool_name, input_hash) fingerprint to /api/tool_use on every tool call. Each dispatch keeps a ring buffer (a deque, maxlen = DEFAULT_LOOP_THRESHOLD = 8); when all 8 recent fingerprints are identical, trigger_kill fires watchdog.manual_kill(reason="loop:<tool>"). install.sh merges this hook idempotently alongside the Stop hook, preserving any hooks you already had.

(Fun aside: CLAUDE.md STILL lists this as "planned, not in MVP." It's been live since Phase 7 — app.py calls loop_watchdog.record/trigger_kill today. When a doc and the code disagree, the code wins; I verified it before writing this.)

---

10/27 Phase 8 — auto file-edits (edits.py), so the orchestrator can grow a project's memory, not just read it. The rewriter can propose three edit types: append_to_memory, append_to_knowledge, and create_task_file. Each is shown as a checkbox with a content preview; you apply only what you want, via /apply_edits, which validates each and shows per-row pass/fail.

The validation is paranoid by design: .md files only, no ".." in the path, no dotfiles, no absolute paths, no symlink that escapes the project, the parent dir must be declared in that project's .forge.json layout for that action, a 50KB content cap, and create_task_file refuses to overwrite an existing file. The orchestrator can enrich a project's memory — but it physically cannot write outside the lines.

---

11/27 Phase 9 — project onboarding (onboarding.py + ONBOARDING.md): a one-time "analyze setup" sweep per project. It scans for every flavor of existing rule file (CLAUDE.md, .cursorrules, .cursor/rules/*.mdc, AGENTS.md, .github/copilot-instructions.md, README), detects the tech stack (package.json / requirements.txt / Cargo.toml / …), and reads the top-level structure. A Claude brain call then returns {project_summary, strengths, gaps, recommendations, proposed_edits}.

Recommendations are manual (root-level files like CLAUDE.md you copy in yourself); the proposed_edits reuse Phase 8's exact validated actions and /apply_edits path — so onboarding can't bypass the safety gate either. I ran it on the orchestrator itself: ~50s, it correctly named all 4 strengths, all 3 missing-dir gaps, and produced 4 one-click edits + 1 manual CLAUDE.md addition.

---

12/27 Phase 10 — "visible, never headless," delivered. Every brain call (rewriter, summarizer, onboarding) moved out of a hidden subprocess and into its own watchable iTerm2 tab. run_claude_json is now the primary path: spawn_brain_tab opens a labeled tab running claude -p --output-format stream-json --verbose | tee <sidecar>, so its reasoning and tool use scroll live on screen, while the structured result is reconstructed from the teed JSONL (the type:result event carries result / total_cost_usd / duration_ms).

Nice properties that fall out: the default timeout is unlimited because visible work is abortable — you just close the tab; tabs auto-close on success and stay open on failure so you can read what went wrong; and the sidecars live in ~/.orchestrator/brain/, outside the repo.

---

13/27 The detail that keeps Phase 10 honest: brain tabs set ORCHESTRATOR_BRAIN_ID, NOT ORCHESTRATOR_RUN_ID. So they stream live, but they never fire the env-gated Stop hook and never show up in the dispatch log as if they were real work.

Net result today: BOTH the brain calls AND the dispatched executors run in iTerm2 tabs I can watch live. The old headless path (run_claude_headless) survives only as a fallback for machines without iTerm2. Nothing the model does happens off-screen — the "visible, never headless" rule is real, not aspirational.

---

14/27 With the local-only system complete, the advanced layer: Fusion (FUSION_PLAN.md) — an optional, opt-in, default-OFF multi-model "brain." When it's on, instead of one Claude rewrite you get a PANEL of models at DIFFERENT labs answering in parallel, and a local judge synthesizes them into one.

Two guarantees make it safe to ship: with the toggle off, behavior is byte-for-byte identical to the local-only path; and run_fusion_json NEVER raises — any panel shortfall silently falls back to the plain single-Claude call, so a flaky provider can never abort a dispatch. It returns the same ClaudeRun dataclass every existing brain caller already expects, so it drops straight into the pipeline.

---

15/27 How the panel actually calls six labs: each provider is ONE standalone script under orchestrator/providers/ — deepseek.py (deepseek-chat), xai.py (grok-4), gemini.py (gemini-2.5-flash), minimax.py (MiniMax-Text-01), glm.py (glm-4.6), qwen.py (qwen-max) — each speaking that lab's NATIVE API in its own shape, each printing the SAME normalized JSON to stdout ({ok, text, model, prompt_tokens, completion_tokens, error}). Adding a lab = one script + one registry line. No shared "OpenAI-compatible" adapter; a non-OpenAI-shaped API is just a different script.

And the headline: the earlier design routed the whole panel through OpenRouter's hosted Fusion — I dropped that ENTIRELY. The only money spent is per-token, paid straight to each lab, with nothing to a middleman. (A hard-won detail: GLM points at z.ai's coding-plan endpoint, /api/coding/paas/v4 — the flat-subscription host — not the prepaid /api/paas/v4, which 1113s without a top-up.)

---

16/27 Cost and speed are first-class. A named preset chooses which seats actually fire so you don't pay six-ways every time: budget (DeepSeek + MiniMax + Gemini — 3 cheap, cross-vendor), balanced (DeepSeek + Grok + Qwen — 3 strong), or max (all six, high-stakes only). The fan-out runs through a ThreadPoolExecutor so wall-clock ≈ the slowest seat, never the sum.

Every run reports real money: each provider script returns its true prompt/completion token counts, and _panel_answer prices them against the per-provider $/M rates in the registry — cost = Σ (in × price_in + out × price_out) / 1e6. The judge itself adds $0 (it's the local claude CLI on the subscription), so a Fusion run's entire out-of-pocket is the panel egress, surfaced per-seat in the dispatch breakdown and stamped onto the outcomes row.

---

17/27 There are two distinct ways a panel can help, and I built both. Mode 1 — rewriter-panel: the seats AUTHOR the dispatched prompt, a drop-in upgrade to the single-model rewriter (the judge reuses your original prompt verbatim so its output-format/JSON contract travels intact).

Mode 2 — enrichment (fusion.py): instead of replacing the rewrite, the panel REASONS about the task and the judge distills it into a fenced "## Multi-model analysis" block — consensus, contradictions, partial_coverage, unique_insights, blind_spots — appended to the prompt the executor sees. The executor weighs it as context, not gospel. With strong-but-non-frontier panelists this is often the safer mode: you're not trusting them to write the final artifact, just to surface disagreement. enrich() caps its input (MAX_INPUT_CHARS = 12,000) and, like the rest of Fusion, never raises.

---

18/27 The judge doesn't just merge answers — it runs a judge → verify → re-judge cycle. _judge_prompt synthesizes the single best response. Then an opt-in critic seat (_verify_prompt) checks that synthesis against the original task and the panel and must return a STRICT JSON verdict {"defect": bool, "issues": [...]}, conservative by design (defect = a concrete, correctable error/omission/over-claim, never style). On a real defect, ONE re-judge (_rejudge_prompt) produces a corrected synthesis; if the re-judge falls short, it keeps the original. Fail-safe at every step.

And it's engine-selectable (C3): a judge_engine param routes the judge, verifier, AND re-judge through Claude (default) or Codex via an in-function engine map. Every step here is a local-CLI call on the subscription — so even the multi-model path still makes ZERO Anthropic API calls.

---

19/27 The decorrelation trick I like most: per-seat LENSES (_apply_lens). A lens makes one seat answer the SAME task through a single perspective. It's prepended so the original prompt stays verbatim and LAST — its output-format instructions still travel, and the judge always sees the unmodified task — so a bold lens can never deform the final format. A lens-free panel is byte-for-byte the pre-lens behavior; it's purely opt-in.

There are 10 seed lenses, each owning a DISTINCT failure axis: risks (downside enumeration), simplest (minimal path), ambiguity (what's underspecified in the question), first-principles (reject the framing, re-derive), user-intent (the goal behind the request), long-horizon (future-change cost), concrete (the exact runnable artifact), adversary (red-team a committed answer), precedent (reuse prior art), evidence (distrust the facts, seek disconfirmation). You assign them per-seat in the dispatch picker; a "· <lens>" badge in the breakdown proves the panel actually ran decorrelated.

---

20/27 WHY lenses beat just running N copies of the same model: clones share weights, training, and blind spots, so they tend to make the SAME mistake and AGREE on it — the judge ends up looking at one wrong answer three times and gains nothing. Diverse lenses force genuinely different angles, which gives the judge real disagreement to resolve. An ensemble only beats a solo call when its members make UNCORRELATED errors — lenses are how you manufacture that on demand.

There's a ceiling, and the design respects it: there are only ~6–7 truly orthogonal angles, so past ~6 seats you start repeating axes and just tax the judge. More DISTINCT angles beat more headcount, every time — which is why the knob is "which lenses," not "how many clones."

---

21/27 The assignment strategy is documented in FUSION_LENS_PLAYBOOK.md (it ships in the repo), and it's opinionated. Three rules: (1) your SMARTEST model is the JUDGE, not a panel seat — resolving disagreement is the hardest job and caps final quality, so don't spend your #1 as one voice among many. (2) Tier lenses by reasoning demand — deep/generative lenses (first-principles, adversary, long-horizon) go on your strongest seats; grounding/lookup lenses (concrete, precedent) are weak-model-safe. (3) Pick lenses by the task's DOMINANT failure mode, not by maxing seat count.

It even names productive PAIRS that give the judge real tension instead of echoes — first-principles ↔ precedent (invent vs. reuse), simplest ↔ long-horizon (minimal-now vs. resilient-later), user-intent ↔ concrete (the goal vs. the exact artifact). The playbook has per-scenario loadouts for architecture, debugging, security, data, UIs, and more.

---

22/27 Codex (CODEX_PLAN.md, phases C0–C6): the SAME pattern, for OpenAI's codex CLI. The load-bearing premise — "$0, exactly like Claude Code" — was verified live: codex exec --json runs non-interactively on a ChatGPT subscription with NO OPENAI_API_KEY (Branch A confirmed, pinned to codex-cli 0.141.0). So codex shows up three ways: a Fusion panel seat (kind:"codex_cli"), a selectable judge, AND a watchable dispatch executor — at -s danger-full-access for full claude parity (config-reversible per machine).

The clever part is the executor's missing-hooks problem. Codex never reads ~/.claude/settings.json, so it gets no Stop/PreToolUse hooks. Fix (§5): the run.sh writes codex's REAL pid (via a FIFO, not a pipeline $$ that would orphan it) to the SAME pids path the claude watchdog uses — so manual-kill, kill-all, the cap, and the orphan reaper all work for free. Then an in-band poller (_codex_dispatch_poller) tails the sidecar JSONL → live timeline + the loop-watchdog fingerprint → and on completion calls _finalize_dispatch, the exact same completion core /api/complete uses. No Claude hooks, no self-POST. (A live test caught the seed model id: gpt-5-codex is rejected by a ChatGPT account — corrected to gpt-5.5. A codex cap hard-kills, not pauses, since there's no resume; and a default 2-dispatch concurrency cap protects the shared subscription window.)

---

23/27 Supermax (SUPERMAX_PLAN.md): once a session is RUNNING, every follow-up you're about to type gets improved through the same Fusion panel first. POST /dispatch/{id}/refine takes your raw follow-up, and crucially it reuses the EXACT panel of seats you picked when you dispatched the task (recorded on the rewrite stage event) — so it fuses for free with seats you already know answer, instead of the global default preset that might silently degrade to one model.

The whole two-step thing (build context, then fuse) runs in a threadpool via run_in_executor, so a minutes-long panel never stalls the FastAPI event loop. cwd, the original task, and the transcript all come from the DB / disk — never from the client.

---

24/27 The make-or-break insight that made Supermax actually work: run_fusion_json's .text is an ANSWER, not a prompt. The judge is told to ANSWER "the task" in the task's format — so if you hand it a raw follow-up like "also do the other file," every seat tries to DO the work and the judge hands back an answer. Paste that into your live session and your message gets answered twice.

The fix (_supermax_refine_prompt): wrap the follow-up so "the task" literally becomes "rewrite this message" and the required output format becomes "plain improved message text." Now each seat returns an improved instruction, the judge synthesizes the best one, and .text is paste-ready. The single-model fallback gets the SAME wrapper, so even with <2 seats it improves rather than silently answering.

---

25/27 Supermax also got context-aware. It resolves the live transcript (stored path, or a codex sidecar, or a running-claude transcript found by task-fingerprint so it doesn't grab one of your unrelated manual sessions), distills it, and writes a PURPOSE-AWARE summary that keeps the concrete details a follow-up depends on — so a vague "do the same to the other one" resolves to real symbols and the exact verify recipe.

That's v1, and it's shipped + live. v2 — writing the improved text straight into the live session's stdin — is deliberately DESIGNED but GATED. Targeting is solved (each tab is tagged with the user.orch_id session var; close_iterm2_session_by_var already finds it reliably). What's NOT solved is reliable mid-TUI injection: there's no idle/readiness signal over osascript, a trailing newline auto-submits (and multi-line text submits line-by-line), quoting has to survive the cmd→AppleScript hop, and the codex executor is a one-shot exec with no live stdin at all. I built the targeting half and refused to ship the fragile half. (I confirmed write_text_to_session_by_var does not exist in the code — v2 is genuinely unbuilt, not quietly half-done.)

---

26/27 Not everything got built — and the restraint is part of the story. I did a full design study of a FAPO-style closed-loop prompt-optimizer (FAPO_PLAN.md) against this codebase and called NO-GO. The integration seam was actually fine (FAPO's optimizer is itself a Claude-CLI agent — exactly this project's pattern). The blocker was internal: there is no valid scorer. The only cheap one is LLM-as-judge, i.e. Claude grading Claude — circular, it just converges on prompts that flatter the judge — and the process-status outcome enum is confounded, not a quality grade. Plus the auto-push daemon would ship any un-reviewed REWRITER.md edit to origin/main within seconds.

Same judgment earlier killed a token-compression tool (no API seam on a $0 subscription) and an unattended optimizer. Knowing what NOT to build — and writing down exactly why — is half the engineering.

---

27/27 Where it stands today: Phases 1–10, Fusion F0–F9, Codex C0–C6, and Supermax v1 — all built and offline-tested (the suite was at 491 green / 4 skipped at last count). Genuinely remaining: the paid cross-lab live verify for the four providers I don't yet have keys for, Supermax v2 live-injection (designed, gated), and codex's open operational questions (subscription caps, ToS, judge calibration).

The whole thing runs on one laptop, spends $0 of API budget on its own "thinking," keeps every LLM call in a terminal tab I can watch, and gets a little smarter with every dispatch — because each finished session is summarized, embedded, and fed back into the next one. That's the build. /end
