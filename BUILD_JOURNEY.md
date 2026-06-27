<!--
DRAFT — Twitter/X "build journey" thread. Posting is your step, not mine.
Each block below is one tweet, plain text (no markdown/backticks) so it pastes
straight into X. The "N/20" prefix is part of the tweet and counts toward 280.
Every claim is grounded in PLAN.md / the *_PLAN.md docs AND verified against the
real code (orchestrator/lib/ + app.py) before it was written — see the symbol
names. All 20 tweets verified <= 280 chars including the prefix.
NOTE: writing this file trips the auto-push daemon -> it commits + pushes to
origin/main within seconds. "Draft" is not private if origin is public.
-->

# Orchestrator — Build Journey (draft thread)

---

1/20 I swapped "10 terminal tabs, 10 manual Claude sessions" for one browser UI that dispatches enriched tasks to Claude Code across every project on my laptop — then learns from each run. Built MVP→today in 10 phases + a multi-model brain. Here's the real story 🧵

---

2/20 Three hard rules shaped every decision:
• No Anthropic API calls — all "brain" work runs through the claude CLI on my existing subscription.
• Visible, never headless — every call runs in a watchable iTerm2 tab.
• Local only — everything runs on this one laptop.

---

3/20 Phase 1 — the walking skeleton. FastAPI + HTMX + stdlib SQLite in ~/.orchestrator. spawn_iterm2() drives iTerm2 via AppleScript to open a tab running claude "$task". 10+ concurrent dispatches, each killable, plus a ~30-min wall-clock cap. No rewriting yet.

---

4/20 Phase 2 — completion logging. A global Stop hook (notify_complete.sh) POSTs to /api/complete, writing an outcomes row + copying the transcript. It's env-gated: a no-op unless ORCHESTRATOR_RUN_ID is set, so my own manual claude sessions stay untouched. The learning loop starts.

---

5/20 Phase 3 — the context bundler (bundle.py). It assembles CLAUDE.md + memory/ + knowledge/ + recent tasks + git state from the project, per its .forge.json layout. 5KB/file, 50KB total caps, path-traversal hardened so a bundle can't escape the project root.

---

6/20 Phase 4 — the rewriter, "Call A" (rewriter.py + prompts/REWRITER.md). It feeds the bundle to a claude brain call that rewrites your prompt + proposes file edits. claude_runner scrubs ORCHESTRATOR_RUN_ID from the child env, so the call never trips its own Stop hook.

---

7/20 Phase 5 — the summarizer, "Call B" (summarizer.py + SUMMARIZER.md). It distills the completed transcript (noise dropped, blocks capped) into {summary_md, what_worked, what_broke, lessons, tags}. It runs as a background task, and only the race winner fires.

---

8/20 Phase 6 — cross-project retrieval. embeddings.py hits local Ollama (embeddinggemma, 768-dim) — zero new Python deps. retrieval.py stores vectors as BLOBs + hand-rolls cosine. Top-5 similar past tasks from EVERY project feed the rewriter. Dim-mismatched rows are skipped, not trusted.

---

9/20 Phase 7 — the loop watchdog (loop_watchdog.py). A PreToolUse hook feeds /api/tool_use a (tool, input) fingerprint per call; 8 identical in a row → auto-kill, reason loop:<tool>. (CLAUDE.md still calls this "planned" — it's been live since Phase 7. Code wins.)

---

10/20 Phase 8 — auto file-edits (edits.py). The rewriter proposes three: append_to_memory, append_to_knowledge, create_task_file. A strict gate: .md only, no "..", no dotfiles, no symlink escaping the project, parent dir in the forge layout, and it never overwrites.

---

11/20 Phase 9 — onboarding (onboarding.py + ONBOARDING.md). One sweep scans a project's rule files (CLAUDE.md, .cursorrules, AGENTS.md, copilot, README), stack signals, and layout. A brain call then proposes baseline memory/knowledge/task files via Phase 8's safe edits.

---

12/20 Phase 10 — the payoff: "visible, never headless." Every brain call left its hidden subprocess for a watchable tab. run_claude_json + spawn_brain_tab stream claude -p stream-json, tee it to a sidecar, and re-parse the same result. Headless is now just the no-iTerm2 fallback.

---

13/20 Those tabs set ORCHESTRATOR_BRAIN_ID, not RUN_ID — so brain calls stay out of the dispatch log and never fire the Stop hook. The upshot: today BOTH brain calls and dispatched executors run in tabs I can watch live. Nothing the model does happens off-screen.

---

14/20 The advanced layer: Fusion (FUSION_PLAN.md). Opt-in, default-off — it fans a task out to a panel of models at DIFFERENT labs in parallel, then a local judge synthesizes them into one. Toggle off = byte-for-byte the old path, and a flaky provider can never abort a dispatch.

---

15/20 Fusion calls each lab's NATIVE API directly — DeepSeek, Grok, Gemini, MiniMax, GLM, Qwen — one providers/<name>.py per lab, no OpenRouter, no shared adapter. The judge is the local claude CLI, so Fusion still makes ZERO Anthropic API calls. Cost = real panel tokens only.

---

16/20 The judge doesn't just merge — it verifies, then re-judges (run_fusion_json). Per-seat "lenses" (risks / simplest path / what's ambiguous) decorrelate the panel. Enrichment mode (fusion.py) appends a Multi-model analysis block instead of authoring. F0–F9 built, 300+ tests green.

---

17/20 Codex (CODEX_PLAN.md, C0–C6): OpenAI's codex CLI, $0 on a ChatGPT sub, no API key. It's a Fusion seat, a selectable judge, AND a watchable executor. No Stop hook, so an in-band poller tails its sidecar to finalize + loop-watch it. Live testing corrected its model id to gpt-5.5.

---

18/20 Supermax (SUPERMAX_PLAN.md): mid-session, refine your next follow-up through the same Fusion panel — POST /dispatch/{id}/refine. Key fix: the judge returns an ANSWER, so I wrap it to improve-not-answer, briefed by a live-transcript summary. v2 live-injection stays gated.

---

19/20 Not everything shipped. I designed a FAPO-style prompt-optimizer (FAPO_PLAN.md), then called NO-GO: no valid non-circular scorer exists (Claude grading Claude just flatters itself). An earlier call killed a token-compressor too. Knowing what NOT to build is half the job.

---

20/20 Today: Phases 1–10, Fusion F0–F9, Codex C0–C6, Supermax v1 — all built + tested. Next: the paid cross-lab live verify, Supermax v2 injection, and codex's open questions (caps, ToS, judge calibration). It dispatches, watches, and learns — every run teaches the next one. /end
