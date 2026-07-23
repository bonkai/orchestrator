# USAGE_PLAN.md — unified usage-limits dashboard (`/usage`): one place to see "where am I at?"

Status: **DESIGN (2026-07-23) — U0 partially pinned (§3), nothing built.**

Trigger: on 2026-07-23 the kimi cycle quota exhausted mid-day; panels showed bare
`kimi exit 1` while the operator was reading a web meter (prepaid/API console) the CLI never
touches. A visibility problem, not an engine problem — the orchestrator should be the single
place that answers "which engines are limited right now, and how hard have I been hitting
each one?"

## 1. Goal

One server-rendered page **`/usage`** + a health strip on the dispatch form, answering per
engine (claude / codex / kimi / glm / gemini):

1. **what we consumed** locally (calls; tokens where the engine reports them) — today / 7d,
2. **are we limited RIGHT NOW** (last limit-hit, parsed reset hint, last-ok time),
3. **the vendor's own meter** — deep link + the exact local check command — since true
   "remaining" for subscriptions lives vendor-side.

## 2. Truth model — why "single source of truth" ≠ one API

Subscriptions (claude, codex, kimi) mostly don't expose quota APIs; flat coding plans (glm)
meter server-side; only per-token APIs (gemini) are simple. So the SSOT is **layered**:

- **Layer A — local metering (exact, already flowing).** Every call passes through
  claude_runner / the pollers, and `panel_breakdown` events already record per-seat
  ok/tokens/error. Aggregate, don't re-instrument.
- **Layer B — limit-hit state (the actionable bit).** Classify per-engine error text
  (kimi 403 cycle-quota, codex/glm 429s, claude limit warnings) into a current state:
  OK / LIMITED since T (+ reset hint). This is what the dispatch form needs.
- **Layer C — vendor readouts.** Scriptable where pinned (codex rollout files carry real
  `used_percent` + `resets_at` — §3); deep links + manual commands elsewhere.

## 3. Per-engine signals — PINNED 2026-07-23 (re-verify on CLI upgrades)

| engine | consumption (local) | limited-now signal | remaining %, scriptable? | web meter |
|---|---|---|---|---|
| claude | stream-json result usage; `dispatches.cost_usd` | limit-warning text **NOT OBSERVED** locally (U0 2026-07-23): census of every `"isApiErrorMessage":true` event across `~/.claude/projects/*/*.jsonl` finds only auth/connection errors (`Login expired · Please run /login`, `Failed to authenticate: OAuth session expired and could not be refreshed`, ConnectionRefused, mid-response drops) — a usage-limit has never been hit on record; pin verbatim from the FIRST real event (it lands in the transcript + any `-p` brain sidecar), never guess | **NO** headless readout (pinned): `claude --help` (2.1.218, 2026-07-23) lists no usage subcommand — `/usage` is interactive-REPL-only; re-verify on upgrade | claude.ai → Settings → Usage |
| codex | `turn.completed` token counts | 429 / limit error strings (⧗ pin) | **YES** — newest `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` carries `"rate_limits":{"primary":{"used_percent":…,"window_minutes":10080,"resets_at":<epoch>}}` (verified live 2026-07-23: 40% of the weekly window) | chatgpt.com → Settings → Codex usage |
| kimi | **calls only** — stream-json has NO usage field (KIMI_PLAN §4) | `kimi exit 1` + `403 … usage limit for this billing cycle` in `~/.kimi-code/logs/kimi-code.log` (UTC; verified 07-20 + 07-23) | **NO** — v0.27.0 has no usage/status subcommand (pinned from `--help`); ⧗ `kimi server` REST may expose something | kimi.com — **log in as the CLI's OAuth account** |
| glm | per-call prompt/completion tokens — already in `panel_breakdown` (verified: 14428/2724) | 429 / subscription-error strings (⧗ pin) | ⧗ z.ai console API unknown | z.ai console → coding-plan usage |
| gemini | `usageMetadata` per call | 429 | AI Studio quota page | aistudio.google.com |

**Account-mismatch trap (the 0% incident):** a web meter reading 0% while the CLI 403s means
wrong product (prepaid `api.moonshot.ai` console vs the coding subscription at
`api.kimi.com/coding/v1`) or wrong web login. Layer B reports the CLI account's actual state.

## 4. Phases

### U0 — pin the full signal matrix ($0, forensic)
Fill every ⧗ in §3 from EXISTING local artifacts only: codex rollout files (do OUR sidecar
streams carry `rate_limits`, or only the rollout files?), claude transcripts under
`~/.claude/projects/` (exact limit-warning text), kimi log, one recorded glm error. No new
deps, no billed calls. Update §3 in place as facts land (guard-centralized-values
convention: the table IS the source; later code imports its constants from config seeds).

### U1 — schema + collector + backfill
- `db.py`: `usage_events(id, ts, engine, model, role seat|executor|judge|brain,
  dispatch_id NULL, calls, prompt_tokens, completion_tokens, ok, error_class, raw_error)`
  + `engine_limit_state(engine PK, limited_since NULL, reset_hint NULL, last_ok_at,
  last_error)`.
- Writers at existing choke points ONLY (claude_runner `_build_*_run` fns, seat-answer
  builders, the codex/kimi pollers). Engine list enumerated from config seeds — no literals
  in app.py (drift-guard convention).
- **Backfill script**: historical `panel_breakdown` rows → usage_events; kimi-log 403s →
  limit events. The page is useful on day one, retroactively.

### U2 — limit classifier + error-detail fix
- Per-engine error→class map, fixture-tested against the REAL strings pinned in U0.
- Fix the detail loss found on 07-23: the tab paths build `error` from the exit code alone
  (`claude_runner.py:877` and its codex twin) — tail sidecar stderr into the error so the
  classifier and the panel row see `403 usage limit`, not bare `kimi exit 1`.
- Transitions: limit-hit ⇒ LIMITED (+ parsed reset hint when the message has one); next ok
  call clears. Codex additionally refreshes `used_percent`/`resets_at` from the newest
  rollout file on each poll of `/usage`.

### U3 — `/usage` page (HTMX, server-rendered, stdlib sqlite)
Per-engine card: state badge (OK / LIMITED since T, resets ~T), today/7d calls + tokens,
codex used_percent bar, last error, deep link + local check command. Below: recent limit
events table. No new JS deps.

### U4 — dispatch-form health strip (soft, never a gate)
Colored dot per engine beside the seat/executor pickers, fed by `engine_limit_state`
(+ codex %). Dispatching with a LIMITED engine shows an inline warning — **never
auto-reroute, auto-drop, or substitute a seat** (parity with the no-silent-fallback rule);
the existing kimi/codex concurrency caps remain the only hard gates.

### U5 — tests (mirror the codex/kimi suites)
Classifier fixtures per engine; backfill idempotency; state-transition units; `/usage`
renders; seed-driven engine list drift guard. Restart note: reload=False — collectors and
page are inert until `python -m orchestrator` is restarted.

## 5. Non-goals
- No headless-browser scraping of vendor dashboards; no storing web credentials.
- No Anthropic API (hard rule) — claude numbers stay CLI/subscription-side.
- No auto-gating or auto-rerouting on limits — visibility only; the operator decides.
- Not a billing/cost system — subscription cost stays $0; provider token counts are usage,
  not spend.

## 6. Open (⧗ recap)
claude limit strings + any scriptable `/usage`; codex sidecar-vs-rollout `rate_limits`;
`kimi server` REST quota; z.ai usage endpoint; gemini seat — set `GEMINI_API_KEY` or drop it
from the default panel (today it fails every panel with a visible error row).
