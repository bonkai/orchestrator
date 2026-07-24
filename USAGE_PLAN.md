# USAGE_PLAN.md — unified usage-limits dashboard (`/usage`): one place to see "where am I at?"

Status: **U0 DONE 2026-07-23 · U1 DONE 2026-07-23 · U2 DONE 2026-07-24 · U3 DONE
2026-07-24 (the `/usage` page is live, linked from the header nav). Remaining: U4
(dispatch-form health strip) + the balance of U5 (most of its test list landed with
U1–U3 in `tests/test_usage.py`, 49 units). See each phase's landed-notes.**

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
| codex | `turn.completed` token counts | 429/limit strings **NOT OBSERVED** (U0 2026-07-23): zero error-event payloads in all 391 rollout files, zero failed codex panel seats in `dispatch_events`; the one surviving exec sidecar (`~/.orchestrator/codex/42.jsonl`, 2026-06-23) proves verbatim API errors DO reach our sidecars — its `"type":"error"` line carries the full 400 body (`The 'gpt-5-codex' model is not supported when using Codex with a ChatGPT account.`) — so pin from the first real 429 (sidecar or rollout), never guess | **YES** — newest `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` carries `"rate_limits":{"limit_id":"codex","primary":{"used_percent":…,"window_minutes":10080,"resets_at":<epoch>},"credits":…}` on `event_msg`/`token_count` lines (verified live 2026-07-23 twice: 40% → 56.0% intra-day). Rollouts ONLY — our exec sidecars carry surface events (`thread.started`/`turn.*`/`item.completed`/`error`) and never `rate_limits` (§6) | chatgpt.com → Settings → Codex usage |
| kimi | **calls only** — stream-json has NO usage field (KIMI_PLAN §4) | `kimi exit 1` + `403 … usage limit for this billing cycle` in `~/.kimi-code/logs/kimi-code.log` (UTC; verified 07-20 + 07-23; panel rows carry only the degraded `kimi exit 1` — dispatches #353/#361/#363, the U2 detail-loss fix target) | **NO** — v0.27.0 has no usage/status subcommand (pinned from `--help`), and `kimi server` REST has NO usage/quota route either (U0 2026-07-23 route census from `~/.kimi-code/bin/kimi` binary strings: `/api/v1/{healthz,meta,sessions,files,connections,debug}` + oauth/user/permissions only; re-verify on upgrade) | kimi.com — **log in as the CLI's OAuth account** |
| glm | per-call prompt/completion tokens — already in `panel_breakdown` (verified: 14428/2724) | **PINNED** — `HTTP Error 429: Too Many Requests {"error":{"code":"1305","message":"The service may be temporarily overloaded, please try again later"}}` (the ONLY distinct glm error on record: 7×, dispatches #224–#251, 2026-06-22→24, `dispatch_events` stage→`panel_breakdown` seat `error`; format = providers/glm.py `f"{e} {detail}"` = `str(HTTPError)` + body[:600]). Caveats: observed pre-coding-endpoint switch; a coding-plan QUOTA-exhausted error is NOT yet observed; 429 + code `1113` (prepaid no-balance) is lesson-known but has no raw row | z.ai console API — **OPEN** (vendor-side; resolve from z.ai docs/console if the operator wants it — no scraping, §5) | z.ai console → coding-plan usage |
| gemini | `usageMetadata` per call | 429 (NEVER observed locally — no gemini call has ever run: key unset, every panel fails the seat with `GEMINI_API_KEY not set (env or config.json)`; §6 decision) | AI Studio quota page | aistudio.google.com |

**Account-mismatch trap (the 0% incident):** a web meter reading 0% while the CLI 403s means
wrong product (prepaid `api.moonshot.ai` console vs the coding subscription at
`api.kimi.com/coding/v1`) or wrong web login. Layer B reports the CLI account's actual state.

## 4. Phases

### U0 — pin the full signal matrix ($0, forensic) — **DONE 2026-07-23**
Fill every ⧗ in §3 from EXISTING local artifacts only: codex rollout files (do OUR sidecar
streams carry `rate_limits`, or only the rollout files?), claude transcripts under
`~/.claude/projects/` (exact limit-warning text), kimi log, one recorded glm error. No new
deps, no billed calls. Update §3 in place as facts land (guard-centralized-values
convention: the table IS the source; later code imports its constants from config seeds).

### U1 — schema + collector + backfill — **DONE 2026-07-23**
- `db.py`: `usage_events(id, ts, engine, model, role seat|executor|judge|brain,
  dispatch_id NULL, calls, prompt_tokens, completion_tokens, ok, error_class, raw_error)`
  + `engine_limit_state(engine PK, limited_since NULL, reset_hint NULL, last_ok_at,
  last_error)`.
- Writers at existing choke points ONLY (claude_runner `_build_*_run` fns, seat-answer
  builders, the codex/kimi pollers). Engine list enumerated from config seeds — no literals
  in app.py (drift-guard convention).
- **Backfill script**: historical `panel_breakdown` rows → usage_events; kimi-log 403s →
  limit events. The page is useful on day one, retroactively.

Landed-notes (2026-07-23, tests: `tests/test_usage.py`, 32 units):
- One spec ADDITION: `usage_events.source` (partial-UNIQUE) — the backfill's idempotency
  key (`pb:<event_id>:<seat_idx>` / `kimilog:<iso>:<hash>`); live rows leave it NULL, and
  the backfill only ingests events OLDER than the first live row (the history/live
  boundary), so re-runs and the live collector can never double-count.
- Writers sit ONE level up from `_build_*_run` — at the run_*_json single-return funnels
  (incl. their headless fallbacks) — because `_build_*_run` sees only SUCCESSES and
  `raw_error` requires recording failures too. Role comes from the existing tab label
  (`fusion-seat:*` → seat, `fusion-judge/-verify/-rejudge` → judge, else brain); external
  provider seats record in `_run_panel`/`_price_tab_answers` (base name of `glm#2`);
  executors in the codex/kimi pollers via `record_*_executor_usage` (all 3 finalize paths).
- Collector is ARMED at server startup (`db.enable_usage_collection()` in lifespan) and by
  the backfill CLI — inert in library/test contexts, and inert LIVE until
  `python -m orchestrator` restarts (reload=False).
- Backfill is MANUAL (no startup hook): `python -m orchestrator.lib.usage`. It also
  recomputes `engine_limit_state` deterministically from the full table; `limited_since`
  is set ONLY by the pinned kimi cycle-quota rule (newest 403 iff no newer ok) —
  reset-hint parsing, the error→class map, and live transitions stay U2.
- Deliberate U1 gaps: the claude EXECUTOR is not metered (not a named choke point — its
  spend stays on `dispatches.cost_usd`, §3); executor rows carry model=NULL (the poller
  has no model source); kimi-log rows are role=seat/dispatch NULL (log lines carry
  neither); `raw_error` stays the DEGRADED `kimi exit 1`-style string until U2's :877 fix.

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

## 6. Open (⧗ recap) — U0 dispositions, 2026-07-23

**RESOLVED by U0** (evidence in §3):
- codex sidecar-vs-rollout: ONLY codex's own rollout files carry `rate_limits` (5 hits in the
  newest rollout, all on `event_msg`/`token_count` lines; zero matches anywhere under
  `~/.orchestrator/` — our exec sidecars carry the `codex exec --json` surface events only).
  U2's `used_percent` refresh must read rollouts, not sidecars.
- claude scriptable `/usage`: NONE — no usage subcommand in `claude --help` (2.1.218);
  `/usage` is interactive-REPL-only.
- `kimi server` REST quota: NO usage/quota route (v0.27.0 binary route census, §3).
- glm 429: pinned verbatim in §3.

**NOT OBSERVED** (honest gaps — pin verbatim from the FIRST real event, never guess; each
would land in an artifact we already parse: claude transcript/brain sidecar, codex exec
sidecar + rollout, glm `panel_breakdown` seat error):
- claude limit-warning text (no usage-limit event exists in any local transcript/sidecar).
- codex 429/limit text (zero error events in 391 rollouts; the surviving sidecar's 400 shows
  verbatim errors do reach sidecars, so the first real one is capturable).
- glm coding-plan QUOTA-exhausted text (the pinned 429 is transient overload, code 1305).

**OPEN — operator / vendor-side:**
- z.ai console usage API — vendor-docs question; no scraping (§5).
- gemini seat — **DECISION FOR OPERATOR**: set `GEMINI_API_KEY` or drop the seat from the
  default panel. State 2026-07-23: env unset AND no config.json override (seed-only, keyless)
  → every panel logs a failed seat `GEMINI_API_KEY not set (env or config.json)` (latest:
  dispatch #364, 2026-07-23 15:22; no gemini call has ever succeeded locally, so no real
  gemini error string exists either).
