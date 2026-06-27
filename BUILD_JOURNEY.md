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

1/20 I replaced "10 terminal tabs, 10 manual Claude sessions" with one local browser UI that dispatches enriched tasks to Claude Code across every project on my laptop — then learns from each run. I built it MVP→today in 10 phases plus a multi-model brain. The real story 🧵

---

2/20 Three hard rules shaped every decision:
• No Anthropic API calls — all "brain" work runs through the claude CLI on my existing subscription.
• Visible, never headless — every call runs in a watchable iTerm2 tab.
• Local only — everything runs on this one laptop.

---

3/20 Phase 1 — walking skeleton. FastAPI + HTMX + stdlib SQLite in ~/.orchestrator. spawn_iterm2() drives iTerm2 over AppleScript to open a tab running claude "$task". 10+ concurrent dispatches, each killable, plus a ~30-min wall-clock cap (watchdog.py). No rewriting yet — just dispatch.

---

4/20 Phase 2 — completion logging. A global Stop hook (notify_complete.sh) POSTs to /api/complete, which writes an outcomes row and copies the transcript. It's env-gated: a no-op unless ORCHESTRATOR_RUN_ID is set, so my own manual claude sessions are never touched. The loop starts here.

---

5/20 Phase 3 — context bundler (bundle.py). Before a task runs it assembles CLAUDE.md + memory/ + knowledge/ + recent tasks + git state from the project, per its .forge.json layout. 5KB/file, 50KB total caps; path-traversal hardened so a bundle can never escape the project root.

---

6/20 Phase 4 — the rewriter, "Call A" (rewriter.py + prompts/REWRITER.md). It feeds the bundle to a claude brain call that rewrites your prompt and can propose file edits. claude_runner scrubs ORCHESTRATOR_RUN_ID from the child env, so the brain call never trips its own Stop hook.

---

7/20 Phase 5 — the summarizer, "Call B" (summarizer.py + SUMMARIZER.md). On completion it distills the transcript JSONL — noise dropped, blocks capped — into {summary_md, what_worked, what_broke, lessons, tags}. Runs as a background task; only the race winner fires. Each run = a lesson.

---

8/20 Phase 6 — cross-project retrieval. embeddings.py hits local Ollama (embeddinggemma, 768-dim) — zero new Python deps. retrieval.py stores vectors as BLOBs and hand-rolls cosine. Top-5 similar past tasks from EVERY project feed the rewriter; dim-mismatched rows are skipped, not trusted.

---

9/20 Phase 7 — loop watchdog (loop_watchdog.py). A PreToolUse hook feeds /api/tool_use a (tool, input) fingerprint per call; 8 identical in a row → auto-kill, reason loop:<tool>. (CLAUDE.md still calls this "planned, not in MVP" — it's been live since Phase 7. When docs drift, code wins.)

---

10/20 Phase 8 — auto file-edits (edits.py). The rewriter can propose three: append_to_memory, append_to_knowledge, create_task_file. Strict gate — .md only, no "..", no dotfiles, no symlink escaping the project, parent dir must be in the forge layout, never overwrites. You apply selectively.

---

11/20 Phase 9 — onboarding (onboarding.py + ONBOARDING.md). One sweep scans a project's rule files (CLAUDE.md, .cursorrules, AGENTS.md, copilot-instructions, README), stack signals + layout, then a brain call proposes a baseline of memory/knowledge/task files — via Phase 8's safe edits.

---

12/20 Phase 10 — the payoff: "visible, never headless." Every brain call left its hidden subprocess for a watchable iTerm2 tab. run_claude_json + spawn_brain_tab stream claude -p stream-json and tee it to a sidecar, re-parsed into the same result. Headless survives only as the no-iTerm2 fallback.

---

13/20 Those tabs set ORCHESTRATOR_BRAIN_ID, not RUN_ID — so brain calls stay out of the dispatch log and never fire the Stop hook. The result: today BOTH the brain calls and the dispatched executors run in tabs I can watch live. Nothing the model does happens off-screen.

---

14/20 Then the advanced layer — Fusion (FUSION_PLAN.md). Opt-in, default-off: it fans a task out to a panel of models at DIFFERENT labs in parallel, then a local judge synthesizes one answer. Toggle off = byte-for-byte the local path, and a flaky provider can never abort a dispatch — it falls back.

---

15/20 Fusion calls each lab's NATIVE API directly — DeepSeek, Grok, Gemini, MiniMax, GLM, Qwen — one providers/<name>.py per lab, no OpenRouter, no shared adapter. The judge is the local claude CLI in a visible tab, so Fusion still makes ZERO Anthropic API calls. Cost = real panel tokens only.

---

16/20 The judge doesn't just merge — it verifies, then re-judges (run_fusion_json). Per-seat "lenses" (find the risks / the simplest path / what's ambiguous) decorrelate the panel. An enrichment mode (fusion.py) appends a Multi-model analysis block instead of authoring. F0–F9 built, 300+ tests green.

---

17/20 Codex (CODEX_PLAN.md, C0–C6): the OpenAI codex CLI, $0 on a ChatGPT sub — verified live, no API key. It's a Fusion seat, a selectable judge, AND a watchable executor. With no Stop hook, an in-band poller tails its sidecar to finalize + loop-watch it. Live testing fixed the model id to gpt-5.5.

---

18/20 Supermax (SUPERMAX_PLAN.md): once a session is running, refine your next follow-up through the same Fusion panel — POST /dispatch/{id}/refine. The fix that makes it work: the judge returns an ANSWER, so I wrap it to improve-not-answer, briefed by a summary of the live transcript. v2 injection: gated.

---

19/20 Not everything shipped. I designed a FAPO-style prompt-optimizer (FAPO_PLAN.md) against the codebase, then called NO-GO: no valid, non-circular scorer exists (Claude grading Claude just flatters itself). The same judgment earlier killed a token-compressor. Knowing what NOT to build is half of it.

---

20/20 Where it stands: Phases 1–10, Fusion F0–F9, Codex C0–C6, Supermax v1 — all built + tested. The frontier: the paid cross-lab live verify, Supermax v2 live-injection, and codex's open questions (caps, ToS, judge calibration). It dispatches, watches, and learns — every run teaches the next. /end
