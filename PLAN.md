# Orchestrator — Plan

## Goal
Replace the "10 terminal tabs, 10 manual `claude` sessions" workflow with a single browser UI that:
1. Lets me open/close project tabs and dispatch tasks.
2. Pre-enriches every task with project memory + similar past-task context.
3. Spawns a headless `claude` in iTerm2 with the rewritten prompt.
4. Captures outcomes via Stop hook → learns cross-project.

## Phases

### Phase 1 — Walking skeleton (MVP) ✅ scope of this PR
- FastAPI + HTMX UI, project list, open/close tabs, task input.
- Dispatch spawns iTerm2 tab → `claude "$task"` (verbatim, no rewriting yet).
- Multi-dispatch (10+ concurrent) tracked per-project.
- Manual kill + global kill-all + wall-clock cap.
- SQLite at `~/.orchestrator/orchestrator.db`.

### Phase 2 — Completion logging ✅ scope of this PR
- Global Stop hook (env-var gated by `ORCHESTRATOR_RUN_ID`).
- `/api/complete` writes `outcomes` row + copies transcript to `~/.orchestrator/transcripts/`.
- UI shows status / duration / link to transcript per dispatch.

### Phase 3 — Context bundler ✅ done
- `orchestrator/lib/bundle.py` scans CLAUDE.md/PLAN.md/memory/knowledge/recent-tasks/git/dir-tree per `.forge.json` `layout` (with defaults). Per-file 5KB cap, total 50KB cap. Path-traversal hardened.
- `/bundle/<id>` HTML view + `/bundle/<id>/raw` markdown view. "view bundle →" link on the dispatch form.

### Phase 4 — Rewriter (Call A) ✅ done
- Vendored `stream_run` in `orchestrator/lib/claude_runner.py`. Sync subprocess.run, scrubs `ORCHESTRATOR_RUN_ID` from env so brain calls don't fire the Stop hook.
- `orchestrator/lib/rewriter.py` + `prompts/REWRITER.md` produces `{rewritten_prompt, rationale, files_to_read, hazards_acknowledged, proposed_edits}`.
- UI: dispatch form has "preview rewrite" (primary) and "skip rewrite". Preview page shows original → editable rewritten + rationale + hazards + proposed edits (phase 8) + similar past tasks (phase 6). Separate forms for "dispatch rewritten" vs "dispatch original".

### Phase 5 — Summarizer (Call B) ✅ done
- `orchestrator/lib/summarizer.py` distills transcript JSONL (drops noise, caps blocks at 1.5KB, total 30KB) → headless claude with `prompts/SUMMARIZER.md` → `{summary_md, what_worked, what_broke, lessons, tags}`.
- Fires as a background asyncio task from `/api/complete` (stored in a strong-ref set so the GC doesn't kill it). Only the race winner fires.
- `/dispatch/<id>` detail page shows the summary; "raw transcript →" link still works.

### Phase 6 — Cross-project retrieval ✅ done (semantic, not FTS5)
- Ollama + `embeddinggemma` (Google, 768-dim) via HTTP at `localhost:11434`. Zero new Python deps.
- `orchestrator/lib/embeddings.py` (NaN/Inf-guarded vec encode/decode) + `lib/retrieval.py` (BLOB storage, hand-rolled cosine).
- Auto-embeds after summarizer writes. `backfill_missing()` for one-time catchup. Dim-mismatch rows skipped so model swaps don't poison results.
- Top-5 similar past tasks (cross-project, BM25-style filtered at min_score=0.3) injected into the rewriter prompt and shown in the preview UI.

### Phase 7 — Loop watchdog ✅ done
- New PreToolUse hook script `bin/notify_tool_use.sh` (env-gated by `ORCHESTRATOR_RUN_ID` — manual sessions unaffected).
- `/api/tool_use` records (tool_name, input_hash) per dispatch in a ring buffer; on N consecutive identical calls (default 8) fires `manual_kill(reason="loop:<tool>")`.
- `bin/install.sh` merges the PreToolUse hook into `~/.claude/settings.json` alongside the Stop hook (idempotent, preserves user's existing hooks).

### Phase 9 — Project onboarding ✅ done
- One-time sweep per project (button on the project pane: "analyze setup →").
- `orchestrator/lib/onboarding.py` scans for existing rule files (`CLAUDE.md`, `.cursorrules`, `.cursor/rules/*.mdc`, `AGENTS.md`, `.github/copilot-instructions.md`, README), forge layout dirs, tech stack signals (package.json/requirements.txt/Cargo.toml/etc.), and top-level structure.
- Headless `claude` with `prompts/ONBOARDING.md` produces `{project_summary, strengths, gaps, recommendations, proposed_edits}`. Recommendations are manual (root-level files — `CLAUDE.md`, `.forge.json`, etc.). Proposed edits use phase 8 actions (`append_to_memory`/`append_to_knowledge`/`create_task_file`) with the same validation, applied via the existing `/apply_edits` endpoint.
- Live test on orchestrator itself: 50s, correctly identified all 4 strengths, all 3 missing-dir gaps, produced 4 useful auto-applicable edits + 1 manual CLAUDE.md addition.

### Phase 8 — Auto file edits ✅ done
- `orchestrator/lib/edits.py` validates + applies. Three actions: `append_to_memory`, `append_to_knowledge`, `create_task_file`. Strict rules: must be `.md`, no `..`, no dotfiles, no absolute paths, no symlinks escaping project, parent dir must be in the project's `.forge.json` layout for that action, 50KB content cap, `create_task_file` never overwrites.
- REWRITER schema extended with optional `proposed_edits[]`.
- UI: each proposed edit shown as a checkbox with rationale + collapsible content preview. "apply selected →" goes to `/apply_edits` which validates each, shows per-row pass/fail, then offers "dispatch rewritten →".
