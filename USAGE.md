# Orchestrator — Usage Guide

A browser UI that dispatches enriched tasks to Claude Code across many projects on your laptop. Each dispatch gets project memory + similar past-task context auto-injected, then opens a real `claude` session in its own iTerm2 tab. Every completed session gets summarized and embedded so the next similar task is better-informed.

---

## Prerequisites (macOS)

- **iTerm2** — `brew install --cask iterm2`
- **Claude Code CLI** — `claude` must be on your PATH
- **Ollama** — `brew install ollama` (or download from ollama.com). Open the app once to start the daemon.
- **Python 3.11+**

## One-time setup

```bash
cd /path/to/orchestrator
bash bin/install.sh
ollama pull embeddinggemma   # ~300MB, used for semantic retrieval
```

What `install.sh` does:
- Creates a `.venv` and installs deps
- Creates `~/.orchestrator/` (where the DB + transcripts + vectors live)
- Merges two hooks into `~/.claude/settings.json` (Stop + PreToolUse). Both are no-ops unless an env var is set, so they don't affect your manual `claude` sessions.

## Start it

```bash
source .venv/bin/activate
python -m orchestrator
```

Open <http://127.0.0.1:7878>.

---

## Daily use

### 1. Add a project

Paste the absolute path in the box at the top → **+ add project**. A tab opens.

### 2. (Optional, recommended on the first add) Analyze the project

Click **analyze setup →** on the project pane. After 30–60s you get:
- A **summary** of what the project is.
- **Strengths** — things the orchestrator can lean on (existing `CLAUDE.md`, `.cursorrules`, layout, etc.).
- **Gaps** — what's missing for orchestrator-driven work.
- **One-click edits** — proposed `memory/`, `knowledge/`, `tasks/` files. Check the ones you want, click **apply selected →**.
- **Manual recommendations** — root-level edits (e.g. add a `CLAUDE.md` section). Copy-paste the content yourself.

You only need to do this once per project, but you can re-run anytime.

### 3. Dispatch a task

1. Type your task into the textarea.
2. Click **preview rewrite →** (or **skip rewrite** if you want to send it verbatim).
3. After ~10s you see:
   - **Rewritten prompt** — editable. Claude saw your project's `CLAUDE.md`, recent memory, and similar past tasks (cross-project) before rewriting.
   - **Rationale** — what it changed and why.
   - **Proposed edits** — optional memory/task files to save lessons from this task. Check + apply before dispatching.
   - **Similar past tasks** — top 5 semantically-matched dispatches across all your projects, with links.
4. Click **dispatch rewritten →** (or **dispatch original**). A new iTerm2 tab opens with `claude` running your task.

### 4. Multiple dispatches at once

Just keep dispatching. Each opens its own iTerm2 tab. 10+ concurrent is fine.

### 5. Stop a dispatch

- **stop** button on each running row → SIGTERM + 5s grace + SIGKILL.
- **stop all** button in the header → kills every running dispatch.
- **Wall-clock cap** auto-kills after 30 min by default (configurable per dispatch).
- **Loop watchdog** auto-kills if claude repeats the same tool call 8 times in a row.

Every kill is logged with a reason (`manual`, `killall`, `timeout`, `loop:Bash`, etc.) so the learning corpus knows what went wrong.

### 6. View results

The runs panel auto-refreshes every 3s. Click **view** on any completed dispatch → `/dispatch/<id>` shows:
- Original task
- Auto-generated summary
- Tags (e.g. `["refactor", "tests", "ci"]`)
- What worked / what broke / lessons
- Link to raw transcript JSONL

### 7. Tabs

- **Closing a tab (×)** hides it from the UI but does NOT kill running dispatches or delete the project.
- Re-open from the **saved projects** sidebar.
- **forget** (in the project pane header) permanently deletes the project AND its dispatch history.

---

## Where things live

| What | Where |
|---|---|
| SQLite DB | `~/.orchestrator/orchestrator.db` |
| Transcripts | `~/.orchestrator/transcripts/<dispatch_id>.jsonl` |
| Vector embeddings | inside the DB |
| Stop + PreToolUse hooks | `~/.orchestrator/bin/notify_*.sh` |
| Hook registration | `~/.claude/settings.json` (env-var-gated) |

To start fresh: `rm -rf ~/.orchestrator/` (then re-run `bash bin/install.sh`).

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `iTerm2 not installed (or not accessible to AppleScript)` | `brew install --cask iterm2` |
| `WARNING: embedding backend not reachable` (in startup log) | Open Ollama.app to start the daemon, then `ollama pull embeddinggemma` |
| Dispatch sits as "running" forever | Orchestrator was down when Stop hook fired. Restart it — the reaper will mark stale dispatches as `orphaned` on next boot. Or use the stop button. |
| No "similar past tasks" shown | First time using orchestrator → corpus is empty. Each completed dispatch adds one. |
| `database is locked` errors under load | Should never happen (WAL mode + 10s busy_timeout). If you see it, file a bug. |
| Rewriter says "model returned non-JSON" | The brain Claude went off-script. Click **dispatch original** instead, or **skip rewrite** next time. |

---

## Cost picture

| Operation | Approx cost | Where |
|---|---|---|
| Rewrite (per dispatch) | $0.01–0.03 | `claude` (visible tab) |
| Summarize (per completion) | $0.02–0.05 | `claude` (visible tab) |
| Onboarding (per project, one-time) | $0.05–0.15 | `claude` (visible tab) |
| Semantic retrieval | $0 | Local Ollama (CPU/GPU only) |
| Dispatched session | Same as your normal `claude` usage | The actual work |

All brain calls use your existing Claude subscription — no separate API key needed.

---

## Architecture in one sentence

FastAPI + HTMX on `localhost:7878` → `claude` in visible iTerm2 tabs for rewrite/summarize/onboarding → AppleScript opens more iTerm2 tabs for real dispatches → SQLite + Ollama embeddings + Stop/PreToolUse hooks close the learning loop.

See `PLAN.md` for the per-phase breakdown.
