<!--
DRAFT — Twitter/X "build journey" thread. Posting is your step, not mine.
Each block below is one tweet, plain text so it pastes straight into X. The
"N/27" prefix is part of the tweet and counts toward 280. Tweet 2 quotes the
three hard rules verbatim from CLAUDE.md (backticks included, on purpose). Every
claim is grounded in PLAN.md / the *_PLAN.md docs AND verified against the real
code (orchestrator/lib/ + app.py) before it was written — see the symbol names.
All 27 tweets verified <= 280 chars including the prefix.
NOTE: writing this file trips the auto-push daemon -> it commits + pushes to
origin/main within seconds. "Draft" is not private if origin is public.
-->

# Orchestrator — Build Journey (draft thread)

---

1/27 I swapped "10 terminal tabs, 10 manual Claude sessions" for one browser UI that dispatches enriched tasks to Claude Code across every project on my laptop — then learns from each run. Built MVP→today in 10 phases + a multi-model brain. Here's the real story 🧵

---

2/27 3 rules, verbatim:
"No Anthropic API calls. All 'brain' work … runs through the `claude` CLI on your existing subscription."
"Brain calls are visible, never headless. Every `claude` invocation … runs in a watchable iTerm2 tab."
"Local only. Everything runs on this laptop."

---

3/27 Phase 1 — the walking skeleton. FastAPI + HTMX + stdlib SQLite in ~/.orchestrator. spawn_iterm2() drives iTerm2 via AppleScript to open a tab running claude "$task". 10+ concurrent dispatches, each killable, plus a ~30-min wall-clock cap. No rewriting yet.

---

4/27 Phase 2 — completion logging. A global Stop hook (notify_complete.sh) POSTs to /api/complete, writing an outcomes row + copying the transcript. Env-gated: a no-op unless ORCHESTRATOR_RUN_ID is set, so my manual claude sessions stay untouched. The learning loop starts.

---

5/27 Phase 3 — the context bundler (bundle.py). It assembles CLAUDE.md + memory/ + knowledge/ + recent tasks + git state from the project, per its .forge.json layout. 5KB/file, 50KB total caps, path-traversal hardened so a bundle can't escape the project root.

---

6/27 Phase 4 — the rewriter, "Call A" (rewriter.py + prompts/REWRITER.md). It feeds the bundle to a claude brain call that rewrites your prompt + proposes file edits. claude_runner scrubs ORCHESTRATOR_RUN_ID from the child env, so the call never trips its own Stop hook.

---

7/27 Phase 5 — the summarizer, "Call B" (summarizer.py + SUMMARIZER.md). It distills the completed transcript (noise dropped, blocks capped) into {summary_md, what_worked, what_broke, lessons, tags}. It runs as a background task, and only the race winner fires.

---

8/27 Phase 6 — cross-project retrieval. embeddings.py hits local Ollama (embeddinggemma, 768-dim) — zero new Python deps. retrieval.py stores vectors as BLOBs + hand-rolls cosine. Top-5 similar past tasks from ANY project feed the rewriter. Dim-mismatched rows are skipped.

---

9/27 Phase 7 — the loop watchdog (loop_watchdog.py). A PreToolUse hook feeds /api/tool_use a (tool, input) fingerprint per call; 8 identical in a row → auto-kill, reason loop:<tool>. (CLAUDE.md still calls this "planned" — it's been live since Phase 7. Code wins.)

---

10/27 Phase 8 — auto file-edits (edits.py). The rewriter proposes three: append_to_memory, append_to_knowledge, create_task_file. A strict gate: .md only, no "..", no dotfiles, no symlink escaping the project, parent dir in the forge layout, and it never overwrites.

---

11/27 Phase 9 — onboarding (onboarding.py + ONBOARDING.md). One sweep scans a project's rule files (CLAUDE.md, .cursorrules, AGENTS.md, copilot, README), stack signals, and layout. A brain call then proposes baseline memory/knowledge/task files via Phase 8's safe edits.

---

12/27 Phase 10 — the payoff: "visible, never headless." Every brain call left its hidden subprocess for a watchable tab. run_claude_json + spawn_brain_tab stream claude -p stream-json, tee it to a sidecar, then re-parse the result. Headless is now just the no-iTerm2 fallback.

---

13/27 Those tabs set ORCHESTRATOR_BRAIN_ID, not RUN_ID — so brain calls stay out of the dispatch log and never fire the Stop hook. The upshot: today BOTH brain calls and dispatched executors run in tabs I can watch live. Nothing the model does happens off-screen.

---

14/27 The advanced layer: Fusion (FUSION_PLAN.md). Opt-in, default-off — it fans one task out to a panel at DIFFERENT labs in parallel, then a local judge synthesizes them into one. Toggle off = byte-for-byte the old path, and a flaky provider can never abort a dispatch.

---

15/27 Each lab is one standalone script — providers/deepseek.py, xai, gemini, minimax, glm, qwen — speaking its NATIVE API. OpenRouter? Dropped entirely: the only cost is per-token, paid straight to each lab. (GLM hits z.ai's coding-plan endpoint, not the prepaid one.)

---

16/27 A preset picks which seats fire: budget (3 cheap, cross-vendor), balanced (3 strong), or max (all 6). Each run's cost = every provider's REAL token usage × its registry price, summed — actual out-of-pocket, with no aggregator margin on top.

---

17/27 Two ways a panel helps. Rewriter-panel: the seats AUTHOR the dispatched prompt (a drop-in upgrade to the solo rewriter). Enrichment (fusion.py): it instead appends a Multi-model analysis block — consensus, contradictions, blind spots, and more — context, not gospel.

---

18/27 The judge doesn't just merge: judge → verify → re-judge. It synthesizes, then a $0 critic (_verify_prompt) returns strict JSON {defect, issues}; one real defect triggers ONE re-judge, else it keeps the original (fail-safe). All on the local claude CLI — no Anthropic API.

---

19/27 Decorrelation via lenses (_apply_lens). Each seat answers the SAME task through one angle, prepended so the prompt stays verbatim + last (format survives) and the judge sees the original. 10 seed lenses: risks, adversary, first-principles, evidence, precedent, more.

---

20/27 Why lenses beat N identical calls: clones share weights, training, blind spots — they make the SAME mistake and agree on it, so the judge sees one wrong answer 3×. Distinct angles give it real tension to resolve. Past ~6 seats you run out of orthogonal angles anyway.

---

21/27 FUSION_LENS_PLAYBOOK.md has rules for it: your smartest model is the JUDGE, not a seat; deep lenses (first-principles, adversary) on strong seats, grounding ones (concrete, precedent) on weak; pick by the task's dominant failure mode. A · lens badge confirms it ran.

---

22/27 Codex (CODEX_PLAN.md, C0–C6): the codex CLI, $0 on a ChatGPT sub, no key. It's a Fusion seat, a selectable judge, AND a danger-full-access executor (claude parity). No Stop hook, so an in-band poller finalizes + loop-watches it from its sidecar. Id fixed to gpt-5.5 live.

---

23/27 Supermax (SUPERMAX_PLAN.md): mid-session, run your next follow-up through the SAME panel the dispatch used — POST /dispatch/{id}/refine — before you send it. It reuses the original seats and runs in a threadpool, so the minutes-long panel never stalls the event loop.

---

24/27 Make-or-break: run_fusion_json's .text is an ANSWER, not a prompt — the judge answers "the task," so a raw follow-up gets ANSWERED, not improved. Fix: wrap it so "the task" = "rewrite this message", format = plain improved text. The single-model fallback gets it too.

---

25/27 It briefs the panel with a purpose-aware summary of the transcript, so "the other one" resolves to real symbols. v1 ships + runs live. v2 — injecting it into the session's stdin — stays gated: targeting works (user.orch_id), reliable mid-TUI injection doesn't.

---

26/27 Not everything shipped. I designed a FAPO-style prompt-optimizer (FAPO_PLAN.md), then called NO-GO: no valid non-circular scorer exists (Claude grading Claude just flatters itself). An earlier call killed a token-compressor too. Knowing what NOT to build is half the job.

---

27/27 Today: Phases 1–10, Fusion F0–F9, Codex C0–C6, Supermax v1 — all built + tested. Next: the paid cross-lab live verify, Supermax v2 injection, and codex's open questions (caps, ToS, judge calibration). It dispatches, watches, and learns — every run teaches the next. /end
