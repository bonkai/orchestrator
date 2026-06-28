<!--
DRAFT -- Twitter/X "build journey" thread. Posting is your step, not mine.
Each block below is one tweet (X Premium long-form). Plain text, pastes straight
into X. All claims grounded in PLAN.md / the *_PLAN.md docs AND verified against
the actual code (orchestrator/lib/ + app.py). Nothing invented.
NOTE: writing this file trips the auto-push daemon -> commits + pushes to
origin/main within seconds. "Draft" is not private if origin is public.
-->

# Orchestrator -- Build Journey (draft thread, 5 chapters)

---

1/5 ok so i had this problem where i was running like 10 different claude code sessions across different projects and i was basically just manually managing 10 terminal tabs and it was kind of a mess. so i built a local browser ui that does all of that for me and also makes each session smarter than the last one.

the basic loop is: you type a task, it grabs context from that project (memory files, recent tasks, git state), finds the most similar past tasks from any project you've worked on, has claude rewrite the prompt with all that context, spawns a real claude session in its own iterm2 tab, and when that session finishes it summarizes what happened and embeds it so next time is better. that's the whole thing. it runs on my laptop and doesn't call any cloud apis on my behalf.

there were three rules i set at the start and never broke:

"no anthropic api calls. all brain work runs through the claude cli on your existing subscription."

"brain calls are visible, never headless. every claude invocation runs in a watchable iterm2 tab."

"local only. everything runs on this laptop."

so like zero marginal cost, every llm call i can see in a real terminal tab, nothing leaves the machine. here's how i built it from scratch.

---

2/5 chapter 1 -- the local learning loop (phases 1-10)

phase 1 was just getting something running. fastapi + htmx for the ui, sqlite for the db (stdlib sqlite3, no orm), db lives at ~/.orchestrator/orchestrator.db outside the repo. spawn_iterm2() opens iterm2 tabs using applescript, no python library needed. you could run 10+ dispatches at once. from the very beginning i had safety stuff: per-dispatch stop (sigterm then sigkill after 5s), a global kill-all, and a wall-clock cap in watchdog.py that defaults to 1800 seconds.

phase 2 was where the learning loop actually started. i merged a stop hook (notify_complete.sh) into ~/.claude/settings.json that posts to /api/complete when a session ends and writes an outcomes row plus copies the transcript. the key thing is it's env-gated: it's a no-op unless ORCHESTRATOR_RUN_ID is set, so my own manual claude sessions are never touched by it.

phase 3 was the context bundler (bundle.py). before a task runs it assembles claude.md plus memory files plus recent task files plus live git state, following whatever .forge.json layout the project has. each file is capped at 5000 chars and the whole thing is capped at 50k. and every path goes through _safe_join and _within_project so you can't accidentally or maliciously escape the project root.

phase 4 was the rewriter (rewriter.py + prompts/REWRITER.md). it feeds the bundle to a claude call and gets back structured json: rewritten_prompt, rationale, files_to_read, hazards_acknowledged, proposed_edits. it's editable before you dispatch, so you're still in control. two things i thought were worth doing: it runs at opus/high on purpose because it's the highest-leverage call in the pipeline, and the runner scrubs ORCHESTRATOR_RUN_ID from the child env so a brain call can never trip its own stop hook.

phase 5 was the summarizer (summarizer.py + SUMMARIZER.md). when a dispatch finishes it distills the transcript jsonl (blocks capped around 1.5kb, total around 30kb) into summary_md, what_worked, what_broke, lessons, tags. that becomes the project's institutional memory. i kept it at sonnet/medium on purpose, not opus. it fires as a background asyncio task held in a strong-reference set so the gc doesn't kill it, with a guard so only one instance wins if there's a race.

phase 6 was cross-project retrieval and it's actually semantic, not just keyword matching. embeddings.py talks to a local ollama running google's embeddinggemma at 127.0.0.1:11434, 768-dim vectors, no new python deps, zero cost. retrieval.py stores vectors as packed float32 blobs in sqlite and hand-rolls cosine similarity (no numpy). find_similar() pulls the top 5 most similar past tasks from any project with cosine above 0.3. vectors are nan/inf guarded and any row with a mismatched dimension gets skipped, so swapping the embedding model doesn't corrupt results.

phase 7 was the loop watchdog (loop_watchdog.py). a second hook, notify_tool_use.sh, posts a fingerprint (tool name + hash of the input) to /api/tool_use on every tool call. each dispatch keeps a ring buffer of 8. when all 8 recent fingerprints are the same it calls watchdog.manual_kill with reason "loop:<tool>". i should mention: CLAUDE.md still says this is "planned, not in MVP." it's been live since phase 7. when a doc and the code disagree i go with the code, and i checked before writing this.

phase 8 was auto file edits (edits.py). the rewriter can propose three kinds of edit: append_to_memory, append_to_knowledge, create_task_file. each shows up as a checkbox in the ui. the validation is strict: .md files only, no ".." in paths, no dotfiles, no absolute paths, no symlink that escapes the project, the parent dir has to be in the forge.json layout, 50kb content cap, and create_task_file refuses to overwrite. it can grow a project's memory but it physically can't write outside the lines.

phase 9 was onboarding (onboarding.py + ONBOARDING.md). a one-time sweep: it scans every flavor of rule file (claude.md, .cursorrules, cursor/rules/*.mdc, agents.md, .github/copilot-instructions.md, readme), detects the stack from package.json / requirements.txt / Cargo.toml and friends, reads the top-level structure, and returns a project_summary, strengths, gaps, recommendations, and proposed_edits that reuse phase 8's same validated path. i ran it on the orchestrator itself: about 50 seconds, got all 4 strengths right, all 3 missing-dir gaps, 4 one-click edits and 1 manual claude.md suggestion.

phase 10 was getting every call visible. originally the brain calls ran in a hidden subprocess. i moved them all into their own iterm2 tabs: run_claude_json + spawn_brain_tab stream claude -p --output-format stream-json --verbose piped to tee, which writes a sidecar, and the structured result gets rebuilt from the type:result event. brain tabs set ORCHESTRATOR_BRAIN_ID instead of ORCHESTRATOR_RUN_ID so they stream live without firing the stop hook. today both brain calls and dispatched executors run in tabs i can watch. the old headless path is just a fallback for machines without iterm2.

---

3/5 chapter 2 -- fusion: an optional multi-model brain (fusion_plan.md, F0-F9)

once the local system was solid i added an opt-in, default-off layer where instead of one claude rewrite you get a panel of models from different labs answering in parallel, then a local judge synthesizes them. two things make it safe: toggle it off and behavior is byte-for-byte identical; run_fusion_json never raises, so a flaky provider silently falls back and nothing aborts. it returns the same ClaudeRun dataclass the rest of the pipeline expects.

six labs, native apis. each provider is one standalone script under orchestrator/providers/: deepseek.py (deepseek-chat), xai.py (grok-4), gemini.py (gemini-2.5-flash), minimax.py (MiniMax-Text-01), glm.py (glm-4.6), qwen.py (qwen-max). each speaks that lab's native api in whatever shape they chose, and each outputs the same normalized json: ok, text, model, prompt_tokens, completion_tokens, error. adding a lab is one script plus one registry line. the earlier design went through openrouter, i dropped that entirely. the only money spent is per-token, straight to each lab. (one thing that tripped me up: glm has to hit z.ai's coding-plan endpoint at /api/coding/paas/v4, the flat-subscription host, not /api/paas/v4 which is prepaid and will just time out if you don't have a balance.)

cost and speed are tracked for real. a preset picks which seats fire: budget is deepseek + minimax + gemini, balanced is deepseek + grok + qwen, max is all six. the fan-out runs through a threadpoolexecutor so wall-clock is roughly the slowest seat, not the sum. each run reports actual spend: _panel_answer prices from real token counts against per-provider $/M rates, cost equals sum of (in times price_in plus out times price_out) divided by 1e6. the judge adds $0 because it's the local claude cli.

there are two modes. rewriter-panel is where the seats actually author the dispatched prompt, a drop-in upgrade to the single-model rewriter. enrichment mode (fusion.py) has the panel reason about the task and the judge appends a fenced "## Multi-model analysis" block: consensus, contradictions, partial_coverage, unique_insights, blind_spots, which the executor weighs as context not as instructions. with models that are strong but not frontier, enrichment is often safer because you're not trusting them to write the final artifact.

the judge pipeline is judge then verify then re-judge. _judge_prompt synthesizes, an opt-in critic (_verify_prompt) checks it and must return strict json with a defect boolean and an issues list, it's conservative by design, and on a real defect one re-judge corrects it. if there's no defect it keeps the original. it's engine-selectable: a judge_engine param routes judge plus verifier plus re-judge through claude by default or codex. every step is a local cli call so even the multi-model path makes zero anthropic api calls.

the thing i like best about the design is per-seat lenses (_apply_lens). a lens makes one seat answer through a single perspective, prepended so the original prompt stays last and the output format still comes through. there are 10 seed lenses: risks, simplest, ambiguity, first-principles, user-intent, long-horizon, concrete, adversary, precedent, evidence. the reason this matters: if you just run the same model 3 times it shares the same blind spots and will confidently agree on the same wrong answer. distinct lenses force genuinely different angles. FUSION_LENS_PLAYBOOK.md turns this into rules: put your strongest model as judge not as a seat, put deep lenses like first-principles and adversary on strong seats, pick lenses based on what failure mode you're actually worried about.

---

4/5 chapter 3 -- codex and supermax

codex (CODEX_PLAN.md, C0-C6): same pattern, for openai's codex cli. the thing i had to verify first was whether "dollar zero, just like claude code" was actually true. i ran codex exec --json on a chatgpt subscription with no OPENAI_API_KEY set and it worked. so codex shows up in three ways: a fusion panel seat (kind:"codex_cli"), a selectable judge engine, and a watchable dispatch executor. the executor runs at -s danger-full-access for full claude parity, that's configurable per machine.

the tricky part was the missing hooks. codex doesn't read ~/.claude/settings.json so it gets no stop hook and no pretooluse hook. the fix: the dispatch run.sh writes codex's real pid via a fifo (not a pipeline $$ which would orphan it) to the same pids directory the claude watchdog already uses. that means manual kill, kill-all, the wall-clock cap, and the orphan reaper all work for free without any changes to the watchdog. then an in-band poller (_codex_dispatch_poller) tails the sidecar jsonl, feeds tool events (command_execution and file_change, confirmed from live capture) into the loop watchdog, and when the .done file appears it calls _finalize_dispatch, the same core /api/complete uses. no claude hooks involved, no self-posting.

one thing that came up during testing: the model id gpt-5-codex gets rejected by chatgpt accounts with a 400. corrected to gpt-5.5. also codex hits cap limits on the chatgpt plus subscription faster than you'd expect, especially if you're running a fusion fan-out at the same time, so there's a default max of 2 concurrent codex dispatches. and when the wall-clock cap fires on a codex session it hard-kills, there's no resume in v1 because codex doesn't have a session_id equivalent in the same way.

supermax (SUPERMAX_PLAN.md): once a session is going, you can take a follow-up message and run it through the fusion panel for improvement before sending it in. POST /dispatch/{id}/refine takes your raw follow-up and reuses whatever seats you picked at dispatch time, so you're fusing with seats you already know work for this task. it runs in a threadpool so a long panel doesn't block the event loop.

the thing that took some thinking: run_fusion_json's .text is an answer, not a prompt. if you hand the judge "also do the other file" as-is, every seat tries to actually do the work, and you get answered instead of improved. the fix (_supermax_refine_prompt) wraps it so the task literally becomes "rewrite this follow-up message" and the output format is "plain improved message text." it's also context-aware: it resolves the live transcript by fingerprint match so it doesn't grab an unrelated manual session.

v1 is live. v2, where the improved text gets written directly into the live session's stdin, is designed but not shipped. i worked out the targeting (every tab is tagged with user.orch_id via an applescript session variable), but reliable mid-tui injection isn't there: no idle signal over osascript, a trailing newline auto-submits, quoting has to survive the cmd to applescript hop, and codex executor is one-shot with no live stdin. i built the targeting half and stopped before shipping the fragile half.

---

5/5 where it stands and the one i didn't build

i did a full design study of a fapo-style closed-loop prompt optimizer (FAPO_PLAN.md) and called no-go. the integration seam was fine, fapo's optimizer is itself a claude-cli agent which is exactly this project's pattern. the blocker was internal: there's no valid scorer. the only cheap option is llm-as-judge, which is claude grading claude, and that just converges on prompts that flatter the judge. the outcome enum in the db (completed / killed / failed_to_spawn / orphaned / paused) is a process status, not a quality grade. plus the auto-push daemon would ship any un-reviewed prompt file to origin/main in seconds. i also looked at a token-compression tool and declined that too: no api seam on a dollar-zero subscription. knowing what not to build is part of the engineering.

where it stands: phases 1-10, fusion F0-F9, codex C0-C6, supermax v1, all built and tested (test suite was at 491 green / 4 skipped last i checked). what's still open: the paid live verify for the four fusion providers i don't have keys for yet, supermax v2 live-injection (designed, gated), and codex operational questions around subscription caps, terms of service, and whether the judge prompts are calibrated enough for a codex judge.

the whole thing runs on one laptop, costs $0 of api budget for its own thinking, keeps every llm call in a terminal tab i can watch, and gets a bit smarter with every dispatch because each finished session gets summarized, embedded, and fed back into the next one. that's the build.
