# Orchestrator — OpenAI `codex` CLI Integration Plan *(C0–C6 BUILT + TESTED)*

> 🟢 **BUILD STATUS (2026-06-23): C0–C6 are ALL BUILT + TESTED — the codex integration is complete.**
> C0 (gate, Branch A), C1 (codex CLI invoker + parsers), C2 (Fusion codex seat),
> C3 (selectable judge), C4 (config SEEDS — `CODEX_ENGINE_SEED` in `config.py`,
> IMPORTED by `claude_runner`), C5 (dispatch-form engine+model picker — the codex
> SEAT parse + the executor engine picker + codex availability gating), and **C6 (the $0
> codex EXECUTOR — `spawn.spawn_codex_dispatch` + a write-capable run.sh + the §5 in-band
> completion poller)** all shipped with offline tests (suite 451 / skipped=4).
> **engine=codex now SPAWNS a watchable codex executor** (writes confined to the project
> via `-s workspace-write`; killable / capped / loop-watched like a claude dispatch) — and
> NEVER silently falls back to a `claude` executor, on success OR spawn failure (the
> dispatch #3 downgrade). The §5 hook gap (codex has no Stop/PreToolUse hooks) is closed
> in-band: a lifetime poller (`app._codex_dispatch_poller`, modeled on the watchdog) tails
> the sidecar JSONL → timeline + loop-watchdog fingerprint, and on `.done` calls the SAME
> extracted completion core (`app._finalize_dispatch`) `/api/complete` uses — no Claude
> hooks, no self-POST. Per-phase detail lives in the `codex-c*-built` session memories;
> this banner + the §6 table are the source of truth. (Lesson baked in: keep this status
> honest the moment a phase lands.)
>
> **⚠ Two C6 deviations from this doc's earlier text, both deliberate + recorded:**
> (1) The executor runs `-s workspace-write` ALONE (no `--dangerously-bypass-...`). C6.0
> verified that bypass flag OVERRIDES `-s` to full-access (codex escaped to `/tmp`), and
> that `workspace-write` alone is non-hanging (note 1's "else it HANGS" is **false** for
> codex-cli 0.141.0 — an out-of-sandbox action is rejected + the run continues). The
> operator chose the confined default; loosen via a `fusion.codex.executor_sandbox`
> override (reversible, no code change). (2) The summarizer fork (note 4) = an additive
> codex branch in `summarizer.distill_transcript` (most reversible; yields a real,
> non-empty summary; the claude path is byte-for-byte unchanged).

Adding the OpenAI **`codex` CLI** to the orchestrator as **three near-independent
deliverables that share one verification gate**:

- **(a)** a **$0, subscription-backed task EXECUTOR** in a watchable iTerm2 tab — the
  codex analogue of the dispatched `claude` session (`spawn.spawn_iterm2` → `run.sh`).
- **(b)** a **Fusion seat** (`kind:"codex_cli"`) — the codex analogue of the local
  Claude-Code seat (`_anthropic_seat_answer` / `kind:"claude_cli"`), $0, visible tab,
  no OpenAI API.
- **(c)** a **selectable judge/executor engine** — so the Fusion *judge* (and the panel
  seats, and a dispatch's executor) can each be **Claude or Codex**.

This is a phased *plan*, not a built feature. When nothing opts into codex, behavior is
**byte-for-byte identical to today**. It mirrors the proven Fusion seams (see
`FUSION_PLAN.md`) rather than inventing new plumbing.

> ✅ **THE LOAD-BEARING PREMISE — VERIFIED 2026-06-22 (codex-cli 0.141.0): BRANCH A holds.**
> The brief framed this as "*$0, exactly like Claude Code.*" It is **confirmed**: `codex exec
> --json` runs **non-interactively at $0 on a ChatGPT subscription** (`codex login status` →
> "Logged in using ChatGPT"; a real `codex exec` round-tripped with **no `OPENAI_API_KEY`**).
> C0 was executed live on this laptop — binary installed, ChatGPT login completed, event
> schema captured (§0/§3). The design below is therefore **un-gated on its central premise**;
> what remains (C1–C6) is engineering, not viability. Flags + event schema are **version-pinned
> to 0.141.0** — codex churns them, so re-verify on upgrade.

> ⚠️ **Hard rules, extended to codex (mirror of CLAUDE.md's Claude rules):**
> - **NO OpenAI API calls / no hidden HTTP.** Codex runs on the **subscription via the
>   `codex` CLI**, exactly as the brain calls run on the `claude` CLI. A paid
>   `OPENAI_API_KEY` path is the *opposite* of what's wanted; if C0 finds it's the
>   *only* path, see Branch B (§2) — the plan recommends **not shipping the executor**,
>   not silently adding an API path.
> - **Visible, never headless.** Every codex invocation — seat, judge, *and* dispatched
>   executor — runs in a **watchable iTerm2 tab**, streamed + tee'd to a sidecar, like
>   `brain_run.sh` / `run.sh`.
> - **Local only.** No remote workers. Data in `~/.orchestrator/` (and codex's own
>   `~/.codex/`), never the repo.
> - **Env-gated.** No hook or notifier may affect the user's **manual** `codex` sessions
>   — the codex analogue of "Stop hook is a no-op unless `ORCHESTRATOR_RUN_ID` is set."

---

## 0. Verification status — READ FIRST *(C0 EXECUTED live 2026-06-22, codex-cli 0.141.0)*

C0 was **run end-to-end on this laptop**: `npm install -g @openai/codex` → `codex login`
(ChatGPT browser flow, done by the operator) → a real `env -u OPENAI_API_KEY codex exec
--json` round-trip. **Verdict: BRANCH A — the $0 premise holds.** Facts are version-pinned to
**codex-cli 0.141.0**; codex churns flags, so re-verify on upgrade.

| Claim | How checked | Verdict (2026-06-22) |
|-------|-------------|----------------------|
| `codex` CLI installed | `npm install -g @openai/codex` → `codex --version` | ✅ **0.141.0** (`~/.nvm/.../bin/codex`) |
| Non-interactive mode | `codex exec --help` | ✅ `codex exec [PROMPT]` ("Run Codex non-interactively", alias `e`) — the `claude -p` analogue |
| **$0 ChatGPT-sub auth, headless** | `codex login status` → `env -u OPENAI_API_KEY codex exec --json …` round-tripped | ✅ **"Logged in using ChatGPT"; returned with NO `OPENAI_API_KEY` → BRANCH A** |
| Structured output | `codex exec --json` (JSONL) + `-o/--output-last-message` + `--output-schema` | ✅ all present; final text in `item.completed`/`agent_message`, usage in `turn.completed` |
| Token usage present | the `turn.completed` event | ✅ `input_tokens`/`cached_input_tokens`/`output_tokens`/`reasoning_output_tokens` (a hypothetical paid seat is priceable — no dead end) |
| No-hang dispatch flag | `codex exec --help` | ✅ `--dangerously-bypass-approvals-and-sandbox` + `-s read-only\|workspace-write\|danger-full-access` (NB: `-a/--ask-for-approval` is **interactive-only**, NOT on `exec`) |
| stdin gotcha | the probe hung until fixed | ⚠ `codex exec` **blocks "Reading additional input from stdin…"** in a non-TTY → **must run `< /dev/null`** (exactly like `brain_run.sh` does for `claude -p`) |
| Auth-state probe (not PATH) | `codex login status` / `codex doctor` | ✅ both report auth; `codex_cli_available()` must call one of these (a ChatGPT token can expire) |
| Per-seat state isolation | `codex exec --help` | ✅ `--ephemeral` (no session files) + `CODEX_HOME` (isolate `~/.codex/` per seat) |
| Claude parity flags (mirror) | `claude --help` | ✅ `--effort`, `--model`, `--output-format`, `--dangerously-skip-permissions` |
| Loop watchdog is live | `app.py:406` fed by `bin/notify_tool_use.sh` | ✅ **LIVE — CLAUDE.md STALE** ("planned; not in MVP"); the PreToolUse half of the hook gap (§5) is real |
| Hooks are Claude-bound | `bin/install.sh` merges `notify_*.sh` into `~/.claude/settings.json`, gated on `ORCHESTRATOR_RUN_ID` | ✅ fire for `claude`, **never** `codex` (§5) |

**Real terminal-event schema captured (codex-cli 0.141.0 — what `_build_codex_run` parses):**

```jsonl
{"type":"thread.started","thread_id":"019ef12d-…"}          // thread_id = the resume/session handle
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"…final answer…"}}
{"type":"turn.completed","usage":{"input_tokens":15026,"cached_input_tokens":12032,"output_tokens":9,"reasoning_output_tokens":0}}
```
- **Discriminator is `type`** (`thread.started`/`turn.started`/`item.completed`/`turn.completed`) — **NOT** claude's `system/init`/`assistant`/`result`. The two parsers (§4) key off these.
- **Final text** = the **last** `item.completed` whose `item.type=="agent_message"` → `item.text`. **Terminal event** = `turn.completed`.
- **No `model` field** in `--json` output → `_build_codex_run` falls back to the model we pass via `-m` (pass it EXPLICITLY anyway — dispatch #3).
- **~15k input-token base overhead per call** (codex's system prompt; mostly cached). $0 on the subscription, but relevant to the per-model **cap/quota** math in §2.

**Consequence:** the central viability question is **answered — Branch A.** §2's Branch B is
retained for reference only; C6 (the executor) is **un-gated.** What remains (C1–C6) is
engineering. §3 records the per-fact C0 results the design keys off.

---

## 1. What this adds, and the seam it rides on

Three deliverables, **one gate** (C0). They are *near-independent*: the **seat (b)** ships
first and standalone — it rides the existing sidecar-parse path and is **untouched by the
hook gap** — while the **executor (a)** is the highest-risk piece because it inherits the
Claude-only hooks (§5).

> 📐 **Recommended build order inverts the brief's (a)/(b) labeling.** The brief lists the
> executor first, but the **Fusion seat is the natural first ship**: lowest risk, no hooks,
> reuses the brain-tab sidecar path verbatim. Suggested order: **C0 (gate) → C1 (the codex
> CLI invoker) → C2 (seat) → C3 (selectable judge) → [C4 config, C5 picker] → C6 (executor,
> last)**. The executor is gated behind everything else *and* behind Branch A of C0.

**The seam (anchored to symbols, not line numbers — those drift).** Everything below is a
**mirror** of an existing, working Claude/Fusion symbol:

| New codex symbol *(proposed)* | Mirrors existing | Role |
|---|---|---|
| `claude_runner.run_codex_json` | `run_claude_json` | PRIMARY: run one codex call in a watchable tab, parse sidecar → `ClaudeRun` |
| `claude_runner.run_codex_headless` | `run_claude_headless` | FALLBACK when iTerm2 absent (captured subprocess) |
| `spawn.spawn_codex_tab` + `CODEX_RUN_SH_CONTENT` (`codex_run.sh`) | `spawn_brain_tab` + `BRAIN_RUN_SH_CONTENT` | open the watchable tab; tee codex's stream to a sidecar |
| `spawn.ensure_codex_runner` / `finish_codex_tab` / `cleanup_codex_files` / `CODEX_DIR` | `ensure_brain_runner` / `finish_brain_tab` / `cleanup_brain_files` / `BRAIN_DIR` | lazy runner + teardown + sidecar dir |
| `_envelope_from_codex_stream` + `_build_codex_run` | `_envelope_from_stream_jsonl` + `_build_claude_run` | **codex's terminal-event schema differs** from claude's `{"type":"result"}` — needs its own parser (§4) |
| codex in-tab pretty-printer (inside `codex_run.sh`) | the `python3 -u -c "…"` block in `BRAIN_RUN_SH_CONTENT` | **second parser** — cosmetic, keyed off codex event types, not claude's `assistant`/`result` |
| `_codex_seat_answer` + `kind:"codex_cli"` | `_anthropic_seat_answer` + `kind:"claude_cli"` | one Fusion codex seat, $0, visible tab, **model passed EXPLICITLY** |
| `config.codex_cli_available()` | `config.claude_cli_available()` | availability gate — **but probes AUTH STATE, not just PATH** (§2) |
| codex SEEDS (`CODEX_ENGINE_SEED` / seat presets) | `FUSION_PROVIDERS_SEED` / `FUSION_PRESETS_SEED` | config defaults (**design only — do NOT add in this task**; import, don't redefine) |
| `judge_engine` param + `_JUDGE_ENGINES` map | the **hard-wired** `run_claude_json` judge inside `run_fusion_json` | (c) selectable judge/verifier/re-judge engine |
| `spawn.spawn_codex_dispatch` + a codex `run.sh` branch | `spawn.spawn_iterm2` + `RUN_SH_CONTENT` | (a) the $0 executor dispatch tab + PID file |

**Engine-agnostic machinery REUSED as-is (no codex fork needed):** `spawn._spawn_tab_script`,
`_TAB_SPAWN_LOCK` + `_spawn_osascript` (tab-creation serialization already covers a codex
burst), `_setuservar_printf` + `close_iterm2_session_by_var` (tab tagging/closing by
`user.orch_*`), `pid_alive` / `kill_pid` / `kill_pid_async` / `read_claude_pid`, and the
`ClaudeRun` dataclass itself (engine-neutral; reused as the codex return type so
`run_brain_json`'s contract is unchanged). **`spawn_codex_tab` is mostly a new `codex_run.sh`
+ a new sidecar dir, not new spawn plumbing.**

**Naming discipline (reversibility):** `kind:"codex_cli"` is **additive** — it never touches
`kind:"claude_cli"` panels. Do **not** "generalize" `claude_cli` → a bare `"cli"` kind; that
would break existing `config.json` panels. Favor additive, default-off, reversible.

---

## 2. The $0 auth fork — Phase C0 decides the whole shape

The premise "$0 like Claude Code" has **two distinct failure modes**, and **both** must be
designed for:

1. **Billing failure:** codex authenticates only via a paid `OPENAI_API_KEY` (per-token).
2. **Mode-specific failure (the subtle one, and exactly the hazard):** the ChatGPT
   subscription works **interactively** but `codex exec` / `--json` / the non-interactive
   path needs an API key. "$0 interactively" does **not** imply "$0 headless."

So C0 must verify subscription auth in the **specific non-interactive mode** the
orchestrator will use — not just that `codex login` works in a TUI.

### Branch A — subscription auth works non-interactively ($0) ✅ the target
- The codex seat is **`kind:"codex_cli"`** — the **direct analogue of `kind:"claude_cli"`**
  (`_anthropic_seat_answer`): visible tab, subscription, `cost = 0.0`, **no OpenAI API**.
  It is **NOT** a paid provider-script seat (`orchestrator/providers/*.py`) — keep the two
  patterns distinct (the §-from-Fusion rule: a $0 CLI seat ≠ a per-token provider script).
- The executor (a) is buildable and compliant.
- `config.codex_cli_available()` gates it — **but unlike `claude_cli_available()` (a bare
  `shutil.which("claude")`), codex's `~/.codex/auth.json` login can EXPIRE.** A PATH-only
  check would mis-gate: it would report "available," then every seat/dispatch would fail at
  run time. C0 must find a cheap, **non-billing** auth-state probe (a `codex` whoami/status
  equivalent, or a parse of `~/.codex/auth.json`) — *not* a real model call.

### Branch B — API-key-only / per-token (the premise fails)
- The "$0 executor like Claude Code" goal **collapses.** A codex executor would bill per
  token — **the opposite of the hard rule.** The plan's recommendation in this branch:
  **do NOT ship the executor (a).** Do not silently add an API path to honor "completeness."
- A codex *Fusion seat* could still exist, but only as a **paid provider-script seat**
  (`providers/codex.py` + `price_in`/`price_out` in the registry, like the cross-lab labs in
  `FUSION_PROVIDERS_SEED`) — i.e. it reuses the *Fusion egress deviation* (§9 of
  `FUSION_PLAN.md`), not the $0 CLI-seat pattern. This is a **different deliverable** and
  should be labeled as such.
- **Double-conditional dead end:** if codex is paid **AND** its non-interactive mode omits
  token counts, the paid-seat branch is **unbuildable** — provider scripts price from
  `prompt_tokens`/`completion_tokens`, and there'd be nothing to price from. C0 must capture
  whether the terminal event carries usage counts; if not, Branch B has no seat at all.

### Auth precedence & env isolation *(applies to BOTH branches)*
- A paid `OPENAI_API_KEY` present in the env could **silently route codex through the billed
  path even when subscription auth exists.** The runner must define explicit precedence and,
  for the $0 path, **scrub `OPENAI_API_KEY` from the child env** — the mirror of how
  `run_claude_headless` scrubs `ORCHESTRATOR_RUN_ID` and how the brain tabs set
  `ORCHESTRATOR_BRAIN_ID` instead of `ORCHESTRATOR_RUN_ID`. (C0 confirms the exact precedence
  codex applies; the design enforces "$0 means no key in the child env.")

### "$0" ≠ "unlimited" *(true even in Branch A — fold into §7 / open questions)*
- **Subscription message caps.** ChatGPT plans meter per-model usage. A Fusion fan-out of N
  codex seats + a codex judge, or several concurrent codex dispatches, can **blow the cap
  mid-run** — a failure mode the $0-and-done Claude subscription rarely hits.
- **Shared session state.** Concurrent codex seats/executors share **one** subscription **and
  one** `~/.codex/` session/history/auth state. A fan-out can **race on that on-disk state**
  (distinct from cap exhaustion). The design should isolate per-seat state (e.g. a per-run
  `CODEX_HOME` / `--cd` / config override — exact mechanism = C0) so seats don't corrupt each
  other's history.
- **ToS / longevity.** Programmatic orchestration of a ChatGPT subscription via `codex` may
  conflict with OpenAI's terms — "$0 today, account action tomorrow." Weigh **viability and
  terms**, not just cost, before committing the executor. (Open question.)

---

## 3. C0 results — VERIFIED *(2026-06-22, codex-cli 0.141.0; re-verify on upgrade)*

> ✅ Run live on this laptop. Each row is a confirmed fact the design keys off, **not** a
> hypothesis. Flags churn — **pinned to 0.141.0.**

| Fact (verified) | Result | Design consequence |
|---|---|---|
| Non-interactive subcommand | `codex exec [PROMPT]` (alias `e`); also `codex review` | the `run_codex_json` core; `claude -p` analogue |
| **$0 subscription auth, headless** | `codex login status` → "Logged in using ChatGPT"; `env -u OPENAI_API_KEY codex exec` returned OK | **BRANCH A** — seat + judge + executor all buildable, zero hard-rule breach |
| stdin must be closed | hangs "Reading additional input from stdin…" on a non-TTY | `codex_run.sh` MUST use `< /dev/null` (like `brain_run.sh`) |
| JSONL events | `codex exec --json` | parser #1 reads it; `-o <file>` gives just the final message; `--output-schema <file>` pins the judge's JSON shape |
| Event schema | `type` ∈ {`thread.started`,`turn.started`,`item.completed`,`turn.completed`} | §0 block; final text = last `agent_message` item's `.text`, usage on `turn.completed` |
| Token usage | present on `turn.completed` (`input/cached_input/output/reasoning_output`) | $0 (don't bill); a Branch B seat would still be priceable — no dead end |
| No-hang flag | `--dangerously-bypass-approvals-and-sandbox` + `-s <mode>` (exec has **no** `-a`) | dispatched executor won't hang (§6) |
| Model flag | `-m/--model` (no `--effort` like claude; reasoning via `-c model_reasoning_effort=…`) | pass `-m` EXPLICITLY (dispatch #3) |
| Auth-state probe | `codex login status` / `codex doctor` (✗/✓ auth) | `codex_cli_available()` calls this, not just `which` |
| State isolation | `--ephemeral` + `CODEX_HOME` | per-seat isolation for a fan-out (§2 concurrency) |
| Resume/session | `thread_id` on `thread.started`; `codex resume` / `codex exec resume` | a `spawn_iterm2_resume` analogue is feasible (deferred) |
| Config + hooks | `~/.codex/config.toml`; codex has its OWN hook system w/ a trust model (`--dangerously-bypass-hook-trust`) | §5 fix (ii) "codex notify" would be global — prefer fix (iii) |

**Deferred (not blocking):** exact `~/.codex/config.toml` `notify` mechanics (moot — §5 chose
the in-band fix iii); any reasoning/effort knob beyond `-c model_reasoning_effort=…` (the `-c`
override suffices). Environment note: `codex doctor` warned `websocket … HTTPS fallback may
still work` — `exec` worked regardless; watch for proxy/VPN flakiness.

---

## 4. The two codex parsers *(why the seam isn't a one-liner)*

Codex's stream schema is **not** claude's. Two *separate* parsers are required — easy to
build one and forget the other:

1. **Sidecar → envelope parser** (`_envelope_from_codex_stream` + `_build_codex_run`).
   `_envelope_from_stream_jsonl` keys off claude's terminal `{"type":"result", …}` carrying
   `result` / `total_cost_usd` / `duration_ms`, and the model from `system/init`. **Codex's
   terminal event has different field names** (captured in C0). This parser reconstructs the
   same `ClaudeRun` shape (`ok/text/parsed_json/cost_usd/duration_s/model/raw`) so every
   existing brain caller and `run_fusion_json` treat a codex result identically. Reuse
   `_strip_fences` for JSON extraction. **Under Branch A, `cost_usd = 0.0`** (subscription).

2. **In-tab pretty-printer** (the `python3 -u -c "…"` block inside `codex_run.sh`). The
   brain runner's printer hard-codes claude's `assistant` / `tool_use` / `result` /
   `system/init` event types to render readable `[assistant]` / `[tool]` / `[done]` lines.
   Codex emits different event types, so this cosmetic formatter must be **rewritten for
   codex's schema** (it only affects the terminal copy; `tee` still writes raw JSONL to the
   sidecar, which parser #1 reads unchanged). Keep `PIPESTATUS[0]` capturing codex's exit
   code, exactly as `brain_run.sh` does.

**Result handling must cover the unhappy paths**, not just success: codex auth-expired,
rate-limit / quota-exceeded, sandbox-denied, timeout, and a closed tab. Mirror
`run_claude_json`'s loop (`.done` exit-code file, `.pid` liveness via `pid_alive`,
`_STARTUP_GRACE_S` for a tab that never started) and return `ClaudeRun(ok=False, error=…)`
on each — never raise.

---

## 5. The executor-side hook gap *(the hazard) — and the fix*

**The problem, stated precisely.** `bin/install.sh` merges `notify_complete.sh` (Stop),
`notify_tool_use.sh` (PreToolUse), and `notify_tool_result.sh` (PostToolUse) into
**`~/.claude/settings.json`**, each a no-op unless `ORCHESTRATOR_RUN_ID` is set. They fire
for **`claude`**, never `codex`. A dispatched **codex executor** therefore loses **all** of
this — note it has **more than two halves**:

| Lost for a codex executor | Source today | Impact |
|---|---|---|
| Completion logging → `/api/complete` | `notify_complete.sh` Stop hook | dispatch never marked complete; **no outcome row**, **no summarizer**, no memory update |
| **Loop watchdog** (kill on N identical tool calls) | `notify_tool_use.sh` → `app.py:406` `loop_watchdog` | **LIVE today** (CLAUDE.md stale) — a looping codex run can't be auto-killed |
| Live tool timeline + idle detection | `/api/tool_use` + `/api/tool_result` + `idle_notifier.reset_idle` | the dispatch UI shows no per-tool activity |

> **A Fusion codex SEAT is UNAFFECTED** by this — it uses the sidecar-parse path
> (`run_codex_json`, like a brain call), **not** the executor hooks. The gap is **only** the
> executor (a). This is the core reason to ship the seat first (§1).

**Three fixes (the fork):**

- **(i) Bend codex into Claude's hooks** — *rejected.* The hooks live in
  `~/.claude/settings.json` and are dispatched by the `claude` process; `codex` doesn't read
  them. Not feasible.
- **(ii) Give codex its own notify** (e.g. `~/.codex/config.toml`'s `notify`) — *risky.* That
  config is **GLOBAL** (C0 to confirm), so it would fire for the user's **manual** codex
  sessions too. It could only be used if it can be **env-gated** the way the Stop hook is
  gated by `ORCHESTRATOR_RUN_ID` — and a global config can't be conditioned per-invocation as
  cleanly. Treat as a fallback at best.
- **(iii) Converge the executor onto the brain-style in-band signal** — ✅ **recommended.**
  The completion/activity signal becomes something the **orchestrator already controls**: the
  codex dispatch runs through the **same sidecar + PID-poll mechanism as `run_codex_json`**
  (`.done` exit code, `.pid` liveness, the streamed JSONL). The orchestrator reads completion
  from the `.done` file (→ calls the same internal completion logic `/api/complete` triggers),
  derives a tool-call fingerprint from the **streamed JSONL** to feed `loop_watchdog.record`
  in-process, and records timeline events from the same stream. **No `~/.claude/settings.json`
  dependency, no touching the user's global codex config, and it reuses the seat machinery.**

> **Honest framing of (iii):** this isn't "replacing missing hooks" — it makes codex
> completion an **in-band signal the orchestrator owns**, converging the executor onto the
> *same watchable-tab/sidecar mechanism the seat uses*. Arguably **cleaner** than the
> out-of-band claude hook path it replaces. The cost is **divergent executor logging between
> engines** (claude via hooks, codex via sidecar) until/unless claude is later converged too —
> a deliberate, documented trade-off, not an oversight.

**Watchdog caveat — corrected:** CLAUDE.md says the loop watchdog is "planned; not in MVP."
**That is stale** — `app.py:406` calls `loop_watchdog.record` / `trigger_kill` today, fed by
the PreToolUse hook. So fix (iii) **must** reproduce the fingerprint feed for codex; it is
**not** safe to skip the watchdog half as "not built yet."

---

## 6. Phased rollout

| Phase | Scope | Deliverable | Status |
|-------|-------|-------------|--------|
| **C0** | **Verification gate** | live `codex --help`/`codex exec --help` + ChatGPT-login auth probe + captured event JSONL + the §2 verdict | ✅ **DONE 2026-06-22 — BRANCH A (0.141.0)** |
| **C1** | The codex CLI invoker | `run_codex_json` (+ headless fallback) + `spawn_codex_tab`/`codex_run.sh`/`ensure_codex_runner` + the two parsers (§4) | ✅ built + tested |
| **C2** | Fusion **codex seat** *(ships first after C1)* | `_codex_seat_answer` + `kind:"codex_cli"`; `codex_cli_available()` (auth-probing); panel splits 3 ways (provider / claude_cli / codex_cli) | ✅ built + tested |
| **C3** | **Selectable judge** | `judge_engine` param + in-function engine map; routes judge **and** verifier **and** re-judge; default `"claude"` | ✅ built + tested |
| **C4** | Config SEEDS | `CODEX_ENGINE_SEED` + `codex_engine()` in `config.py`, merged from `config.json`; IMPORTED by `claude_runner` (no redefinition). Residual: spawn's bash heredoc still dup'd (guard-tested; bash→seed interp deferred to C6) | ✅ built + tested (2026-06-23) |
| **C5** | Dispatch-form engine+model picker | engine selector (claude\|codex) + **per-engine model** id threaded `/send` → `_send_in_background`; codex SEAT parse (`{type:"codex"}`→`kind:"codex_cli"`); codex availability in `_view_ctx` + UI gating. Executor SPAWN deferred to C6 (validated, INERT seam) | ✅ **built + tested (2026-06-23)** |
| **C6** | **$0 executor** *(Branch A only; build last)* | `spawn_codex_dispatch` + codex `run.sh` branch + the §5 hook-gap convergence (iii) + PID file + auto-bypass flag — **see the C6 PRE-FLIGHT + IMPLEMENTATION NOTES block** (grounded, added 2026-06-23) | ◻ design only — **un-gated: C0=Branch A ✅** |

Build strictly in order; the seat (C2) is the first shippable thing and is hook-gap-free.
**C0–C5 are BUILT + TESTED (2026-06-23); C6 is next (un-gated — C0=Branch A ✅).**

### Phase C0 — Verification gate ✅ *(DONE 2026-06-22 — BRANCH A, codex-cli 0.141.0)*
*Goal: turn every §3 hypothesis into a verified fact, and return the §2 branch verdict. **Result: all of C0.1–C0.5 confirmed; details in §0/§3.***
- [x] **C0.1** Installed `@openai/codex` (0.141.0); captured `codex --help` + `codex exec --help` verbatim. ✅
- [x] **C0.2** ChatGPT login done; `env -u OPENAI_API_KEY codex exec --json` round-tripped at **$0** → **Branch A**. ✅
- [x] **C0.3** Real event JSONL captured; schema in §0 (`type` discriminator; `agent_message.text`; `turn.completed.usage` with token counts). ✅
- [x] **C0.4** `--dangerously-bypass-approvals-and-sandbox` (no-hang) + `codex login status`/`codex doctor` (auth probe) confirmed. ✅ Also found: `codex exec` needs `< /dev/null`.
- [x] **C0.5** `~/.codex/config.toml` + codex's own trust-gated hook system noted; §5 picks the in-band fix (iii), so a global `notify` is moot. ✅

### Phase C1 — The codex CLI invoker
*Goal: `run_codex_json(prompt, cwd, model, …)` returning a `ClaudeRun`, in a watchable tab — the codex twin of `run_claude_json`.*
- [ ] **C1.1** `spawn.spawn_codex_tab` + `CODEX_RUN_SH_CONTENT` (`codex_run.sh`) + `ensure_codex_runner` + `CODEX_DIR` + `finish_codex_tab`/`cleanup_codex_files`, mirroring the brain-tab block; sets a codex-specific env id (`ORCHESTRATOR_CODEX_ID`), **never** `ORCHESTRATOR_RUN_ID`, so no hook fires. · *verify:* a test id opens a visible tab writing `.pid`/`.jsonl`/`.done`.
- [ ] **C1.2** `_envelope_from_codex_stream` + `_build_codex_run` (parser #1, §4) keyed off C0's captured schema; reuse `_strip_fences`. · *verify:* a captured codex JSONL → a `ClaudeRun` with `text`/`model` populated, `cost_usd=0` (Branch A).
- [ ] **C1.3** the in-tab pretty-printer (parser #2, §4) in `codex_run.sh`, keyed off codex event types; `PIPESTATUS[0]` keeps codex's exit code. · *verify:* the live tab shows readable lines; the sidecar JSONL is raw.
- [ ] **C1.4** `run_codex_json` poll loop (mirror `run_claude_json`: `.done`/`.pid`/`_STARTUP_GRACE_S`) + `run_codex_headless` fallback that scrubs `OPENAI_API_KEY`. · *verify:* a closed tab → `ok=False`; auth-expired/rate-limit → `ok=False` with a useful `error`, never a raise.

### Phase C2 — Fusion codex seat *(`kind:"codex_cli"`)*
*Goal: a codex seat in the panel, $0, visible tab, no OpenAI API — the twin of `_anthropic_seat_answer`.*
- [ ] **C2.1** `config.codex_cli_available()` — **auth-probing**, not PATH-only (§2). · *verify:* returns False when logged out/expired even if the binary is present.
- [ ] **C2.2** `_codex_seat_answer(seat, prompt, cwd)` → `run_codex_json` with **model passed EXPLICITLY** (dispatch #3 lesson); returns the normalized seat dict (`cost:0.0`, `subscription:True`, lens-aware via `_apply_lens`). · *verify:* a codex seat answers; `ok=False` on failure, no raise.
- [ ] **C2.3** `run_fusion_json` panel-normalization learns a **third** seat kind: `kind:"codex_cli"` alongside `kind:"claude_cli"` and external-provider strings; codex seats fan out in parallel like Claude seats. · *verify:* a mixed panel (codex + claude + provider) returns ≥2 answers; a pure-codex pair satisfies the ≥2 gate; `is_fusion_available()` accounts for codex.

### Phase C3 — Selectable judge engine
*Goal: the hard-wired `run_claude_json` judge becomes `claude` OR `codex`.*
- [ ] **C3.1** `_JUDGE_ENGINES = {"claude": run_claude_json, "codex": run_codex_json}` + a `judge_engine: str = "claude"` param on `run_fusion_json`; route the judge through it. Default `"claude"` keeps today's behavior byte-for-byte (opt-in, reversible). · *verify:* `judge_engine="codex"` runs the synthesis in a codex tab; default still runs claude.
- [ ] **C3.2** Route the **verifier** and **re-judge** through the same engine selection (they are also hard-wired to `run_claude_json` today). · *verify:* with `judge_engine="codex"`, no `run_claude_json` call remains in the judge/verify/rejudge path.
> 🔭 An **engine-keyed map** is chosen over a `claude|codex` boolean deliberately: it scales to a 3rd CLI without a rewrite, while staying a one-line default. **Note:** `_strip_fences`/the strict-JSON verdict prompts (`_verify_prompt`) are tuned to Claude's output habits — a **codex judge's JSON-format fidelity may differ** (open question), and claude-judging-codex vs codex-judging-claude are **not** interchangeably calibrated (inter-rater bias).

### Phase C4 — Config SEEDS ✅ *(BUILT + TESTED 2026-06-23)*
*Goal: codex engine defaults live in `config.py` SEEDS, merged from `config.json` like Fusion's.*
- [x] **C4.1** `CODEX_ENGINE_SEED` in `config.py` (model id, the exec/`-s`/`--json` flag set, the auto-bypass flag, the auth-probe command, default effort, a default seat panel), merged in `fusion_config()` under a `codex` key with a `codex_engine()` accessor — the way `FUSION_PROVIDERS_SEED`/`FUSION_PRESETS_SEED`/lenses are. `claude_runner` IMPORTS the model/flags, NO redefinition: `DEFAULT_CODEX_MODEL` = the seed model; `run_codex_headless`'s flag set + (in `config.py`) `codex_cli_available()`'s probe read the seed; the selectable judge resolves its model from the MERGED `cfg["codex"]["model"]` so a `config.json` `fusion.codex.model` override wins (closes the dispatch #3 silent-downgrade). · *verified (`tests/test_codex_config.py`):* `config.json` overrides merge over the seed; no duplicate definition in `claude_runner`; a codex judge resolves a codex id, not a Claude one. **Residual (deferred to C6):** `spawn.py`'s `CODEX_RUN_SH_CONTENT` bash heredoc still duplicates the flag set + model fallback (bash can't import the Python seed); a guard test pins it to the seed so a drift fails loudly, and C6's codex run.sh work will source it via seed→bash interpolation.

### Phase C5 — Dispatch-form engine + model picker ✅ *(BUILT + TESTED 2026-06-23)*
*Goal: a task executor can be claude **or** codex; a Fusion seat list can include codex seats.*
- [x] **C5.1** The picker is **engine + per-engine model id**, not a flat toggle. `/send` gained an executor `engine` (+ its model) and accepts `codex` seats in the seat JSON — the F9 `fusion_seats` shape extended with `{type:"codex",model}` → `{kind:"codex_cli",model}` (`app._parse_fusion_panel`), which `run_fusion_json` already consumes (C2.3); both the seat model and the executor model validate against a codex whitelist sourced from `CODEX_ENGINE_SEED` (a codex id, NEVER a Claude id — C4 import-don't-redefine; `app._codex_seat_models`). · *verified (`tests/test_fusion_send.py`):* a `{type:"codex",model}` seat → a `kind:"codex_cli"` panel seat; a model-less or Claude-id codex seat is dropped; `_validate_executor_engine` rejects a blank OR an unknown codex executor model (the no-downgrade guard and the bad-id guard are DISTINCT branches, both reject). **Executor SEAM:** `_run_dispatch` validates engine+model, but the codex executor SPAWN is **C6** — engine=codex returns a VISIBLE failed row ("codex executor not yet available (C6)") and NEVER silently spawns a `claude` executor (the dispatch #3 downgrade); `spawn.py` is untouched. **Per-call codex JUDGE model: reviewed + DECLINED for C5** — no dispatch surface selects one (C5 ships the executor engine + codex seats, not a per-dispatch judge), so the merged-config codex model (C4: `config.codex_engine()["model"]`, overridable via `fusion.codex.model`) stays the single source of truth; the stale `claude_runner` note ("a per-CALL explicit codex judge model is C5") was corrected to record this.
- [x] **C5.2** `_view_ctx` surfaces codex availability (`codex_cli_available()`, computed once per render) + the codex model list (`codex_seat_models`), so the dispatch form greys the codex engine `<option>` when codex is absent/logged-out, mirroring the `is_fusion_available()` gating. · *verified (`tests/test_fusion_view_ctx.py`):* `_view_ctx` exposes `codex_cli_available` + `codex_seat_models`; the option-disable is server-backed — a crafted POST with engine=codex is still rejected by `_validate_executor_engine` in `/send`, not just hidden in the UI.

### Phase C6 — $0 visible-tab executor *(Branch A only — build LAST)*
*Goal: a dispatched codex executor, watchable, killable, completion-logged — without the Claude hooks.*

> 🛠️ **C6 PRE-FLIGHT + IMPLEMENTATION NOTES — added 2026-06-23, grounded in the code at C5-complete. READ FIRST: these turn several "design" bullets into near-mechanical work and flag the ONE real unknown.** (Session memory: [[codex-c5-built]].)
>
> **C5 plumbing is DONE — do NOT rebuild it.** Form → `/send` → `_send_in_background` → `_run_dispatch` already carries `executor_engine`/`executor_model`, validated by `app._validate_executor_engine` against `app._codex_seat_models()`. **C6 is essentially: (i) build `spawn_codex_dispatch` + a write-capable codex run.sh + the completion poller, then (ii) replace the ONE seam block in `_run_dispatch`** (`if executor_engine == "codex":` — today it marks a failed row "codex executor not yet available (C6)") **with the real spawn + poller-launch + `watchdog.schedule`.** Leave the form/validation untouched.
>
> **(0) ⚠ PRE-FLIGHT VERIFY (do before coding — C0 was a trivial round-trip and did NOT capture this):**
>   - **Re-capture the codex TOOL-CALL event schema.** §0 only shows `agent_message`/`turn.completed` — NO tool/command events. The loop-watchdog feed (C6.2) needs `(tool_name, input_hash)` per tool call. Run `env -u OPENAI_API_KEY codex exec --json "<task that reads a file, writes a file, and runs a shell command>"` and record the event types codex emits for tool use (likely `item.completed` with `item.type` ∈ command-exec / file-change / tool-call — CONFIRM the real names + where the command/args live). Without this, C6.2's fingerprint is a guess.
>   - **Re-confirm codex-cli is still 0.141.0** (`codex --version`) — flags + schema are pinned to it.
>   - **Confirm the no-hang flag semantics:** does `--dangerously-bypass-approvals-and-sandbox` REPLACE `-s <mode>` or COMBINE with it (`codex exec --help`)? The executor must be write-capable AND non-hanging.
>
> **(1) The SEAT runner is NOT the executor — build a new write-capable run.sh.** `CODEX_RUN_SH_CONTENT` (C1) is `-s read-only` (a seat only READS to answer). An executor WRITES the project → needs `-s workspace-write`/`danger-full-access` + the seed's `auto_bypass_flag`. Add the executor sandbox mode to `CODEX_ENGINE_SEED` (the existing `sandbox` is the seat's read-only), finish the deferred C4 seed→bash interpolation here, and extend `tests/test_codex_config.py::TestSpawnCodexRunShPinnedToSeed` to pin the executor runner too.
>
> **(2) 🎯 BIG WIN — write the PID to the CLAUDE pid path → the whole watchdog works FREE.** `watchdog.schedule/manual_kill/kill_all/reap_orphans/resume_watchers_on_boot` + the wall-clock cap ALL locate the process via `spawn.read_pid_now(dispatch_id)` → `PIDS_DIR/<dispatch_id>.pid` (+ `pid_alive`/`kill_pid_async`). If the executor run.sh does `echo $$ > "$HOME/.orchestrator/pids/<dispatch_id>.pid"` (like claude's `RUN_SH_CONTENT` — NOT the seat's `$CODEX_DIR/<id>.pid`), then manual kill + kill-all + the cap + the orphan reaper + boot re-attach reach a codex dispatch with **ZERO watchdog changes** — that's most of C6.3 for free. Tag the tab `user.orch_id` (the dispatch tag, so `select_iterm2_tab`/auto-close work), NOT `user.orch_codex`. Do NOT set `ORCHESTRATOR_RUN_ID` (Stop hook stays a no-op); key the run.sh off a distinct id (e.g. `ORCHESTRATOR_CODEX_RUN_ID=<dispatch_id>`). Make `cleanup_dispatch_files` also clear the codex sidecars.
>
> **(3) The completion poller is NET-NEW infra — model it on `watchdog`, NOT `run_codex_json`.** `run_codex_json` is a synchronous one-shot poll for one brain call; the executor poller runs for the dispatch's LIFETIME (hours) as an async `_background_tasks` task: tail the sidecar JSONL live (→ timeline events + the loop-watchdog fingerprint), and on `.done` finalize. It is the SOLE finalizer for a codex dispatch (no Stop hook): if it dies, the dispatch sits 'running' until the orphan reaper marks it `orphaned` (NO summary). So (a) EXTRACT the `/api/complete` completion core (`watchdog.cancel` → `db.complete_dispatch` atomic-winner → transcript/artifact → `_run_summarizer`; SKIP the claude-only `is_pausing` branch) into a function the poller calls IN-PROCESS (don't self-POST), and (b) extend `resume_watchers_on_boot` to re-attach the codex poller, not just the cap watchdog.
>
> **(4) The summarizer reads a CLAUDE transcript — codex's sidecar is a different schema.** `summarizer.distill_transcript(path)` parses claude's Stop-hook message JSONL; pointed at the codex sidecar (codex events) it returns "[no conversational content found]". DECIDE up front: (a) add a codex branch to `distill_transcript`, (b) translate the codex sidecar → a claude-shaped transcript on `.done`, or (c) v1: skip the summary for codex and record the degradation honestly. The completion core's transcript-copy/artifact step should point at the codex sidecar (or the translation).
>
> **(5) No codex timeout-RESUME in v1.** `watchdog._run`'s pause-and-resume waits for a Stop-hook `session_id`; codex has none (it resumes by `thread_id`, §3/Q11 — deferred). A codex dispatch that hits the cap HARD-KILLS (not resumable) — surface that in the outcome reason; don't wire codex resume in C6.
>
> **(6) Effort vocabulary differs:** the dispatch `effort` form param is claude's (medium/high/xhigh/max); codex uses `-c model_reasoning_effort=<e>` (§3). For the executor, IGNORE claude `effort` (use the codex model default) unless you add an explicit translation. `executor_model` is already validated/threaded — use it for `-m`.
>
> **(7) Reuse engine-neutral spawn machinery as-is (§1):** `_spawn_tab_script`, `_TAB_SPAWN_LOCK`+`_spawn_osascript`, `_setuservar_printf`+`close_iterm2_session_by_var`, `pid_alive`/`kill_pid`/`kill_pid_async`, the `ClaudeRun` dataclass. `spawn_codex_dispatch` ≈ a new run.sh + these helpers, mirroring `spawn_iterm2` (which writes `tasks/<id>.txt`/`.effort`/`.model` + the pid to `pids/<id>.pid`).
>
> **(8) Tests stay offline, skipped=4:** mock `spawn.spawn_codex_dispatch`; drive the poller with a synthetic codex sidecar JSONL fixture (agent_message + tool-call events + `.done` exit 0 / nonzero / closed-tab). Reuse the `tests/test_fusion_send.py::TestRunDispatchCodexSeam` mock shape. Suite is 420 / skipped=4 — new tests in non-skipped classes ([[test-suite-runner]]).

- [x] **C6.0 (pre-flight)** ✅ **DONE 2026-06-23 — codex-cli 0.141.0 re-confirmed; tool-call schema captured live; bypass/`-s` semantics resolved (a SAFETY finding that revised note 1).** Probe = `env -u OPENAI_API_KEY codex exec --json … "read a file, write a file, run a shell command"` in a throwaway git repo.

  **§C6.0 — captured codex TOOL-CALL event schema (the loop-watchdog fingerprint + timeline feed):**

  ```jsonl
  {"type":"item.started",  "item":{"id":"item_1","type":"command_execution","command":"/bin/zsh -lc \"sed -n '1p' seed.txt\"","aggregated_output":"","exit_code":null,"status":"in_progress"}}
  {"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":"/bin/zsh -lc \"sed -n '1p' seed.txt\"","aggregated_output":"hello…\n","exit_code":0,"status":"completed"}}
  {"type":"item.started",  "item":{"id":"item_2","type":"file_change","changes":[{"path":"/abs/out.txt","kind":"add"}],"status":"in_progress"}}
  {"type":"item.completed","item":{"id":"item_2","type":"file_change","changes":[{"path":"/abs/out.txt","kind":"add"}],"status":"completed"}}
  {"type":"item.completed","item":{"id":"item_4","type":"agent_message","text":"…"}}
  ```
  - **Tool item.type values = `command_execution` and `file_change`** (note 0 predicted "command-exec / file-change" — CONFIRMED, no material divergence → C6.2 proceeds). Plus `agent_message` (final text).
  - **Lifecycle: `item.started` → `item.completed`** for `command_execution`/`file_change`; `agent_message` emits ONLY `item.completed`. New top-level type vs §0: **`item.started`**.
  - **Fingerprint source** (`claude_runner._codex_tool_fingerprint`): `command_execution` → `(tool_name="command_execution", input=item.command)`; `file_change` → `(tool_name="file_change", input=sorted item.changes paths+kinds)`. Hash the input → the `(tool_name, input_hash)` `loop_watchdog.record` wants. Fire ONCE per `item.id` (dedup), on first sighting (started). Timeline `tool_use` on first-sight, `tool_result` on completed (carries exit_code / aggregated_output preview / changed paths).
  - **Terminal event = `turn.completed`** (usage), exactly as §0. The existing `_codex_envelope_from_lines` (C1) already parses final text + usage — REUSED for the executor's transcript envelope.

  **§C6.0 — bypass/`-s` semantics (REVISES note 1 — verified, NOT assumed):**
  - `--dangerously-bypass-approvals-and-sandbox` **OVERRIDES** `-s <mode>` → effectively **danger-full-access**: with `-s workspace-write --dangerously-bypass-approvals-and-sandbox`, codex **wrote OUTSIDE the project** (escaped to `/tmp`). The two do NOT "combine" into confinement; the bypass flag removes the sandbox.
  - **`-s workspace-write` ALONE (no bypass) does NOT hang** (16s round-trip, exit 0). When an action needs approval that's unavailable in non-interactive `exec`, codex **REJECTS it and continues** ("…outside the project and approval is unavailable") — it does **not** block on a prompt. It is **write-capable within the project AND confined**. So note 1's "else it HANGS on an approval prompt" is **false for 0.141.0**.
  - **Consequence for C6.1:** the executor sandbox flag is a genuine safety choice (confined `workspace-write` vs claude-parity full-access). Recorded in `CODEX_ENGINE_SEED["executor_sandbox"]`; `auto_bypass_flag` is applied ONLY if the chosen mode is the full-access one. (See the C6.1 row for the decision taken.)
- [ ] **C6.1** `spawn.spawn_codex_dispatch` (mirror `spawn_iterm2`): a NEW write-capable codex run.sh (`-s workspace-write`/`danger-full-access` + the seed `auto_bypass_flag` — else it HANGS on an approval prompt) that writes its **PID to `PIDS_DIR/<dispatch_id>.pid`** (note 2 — so the watchdog/kill/cap/reaper all work unchanged), tags the tab `user.orch_id`, runs the model **explicitly** (`executor_model`), and does NOT set `ORCHESTRATOR_RUN_ID`. Then replace the `_run_dispatch` C5 seam with this spawn + `watchdog.schedule`. · *verify:* a dispatch opens a watchable tab; manual kill + global kill-all + the cap all terminate it.
- [ ] **C6.2** The §5 fix (iii): a lifetime async poller (note 3) tails the sidecar JSONL → records `tool_use`/`tool_result` timeline events + feeds `loop_watchdog.record` from the codex tool-call fingerprint (note 0), and on `.done` calls the EXTRACTED completion core (note 3) — outcome row + summary (note 4) — all without `~/.claude/settings.json`. · *verify:* a codex dispatch produces an outcome row + summary; a looping codex run is auto-killed; the timeline shows tool activity.
- [ ] **C6.3** Every kill/timeout writes an `outcomes` row with reason (safety parity). Mostly FREE via note 2 (the watchdog's kill/cap/reap writers already produce outcome rows once the PID is at the claude path). · *verify:* a killed codex dispatch leaves an outcome row the learning loop can see.

---

## 7. Deviation acknowledgment *(the honest version, mirror of FUSION_PLAN §9)*

- **Branch A (subscription $0):** a codex seat/judge/executor breaks **zero** hard rules —
  identical to the Claude-Code seat's clean bill (FUSION_PLAN §F9.d): **no OpenAI API**
  (CLI on the subscription), **local only** (no egress — the prompt/bundle never leaves the
  laptop for a pure-codex/claude panel), **visible** (every call a watchable tab), and the
  **Stop hook stays a no-op** (codex sets `ORCHESTRATOR_CODEX_ID`, not `ORCHESTRATOR_RUN_ID`;
  the executor uses in-band completion, §5). It is **additive and default-off** — nothing
  fires unless the user picks codex.
- **Branch B (API-key / per-token):** shipping a codex *executor* would **break "No OpenAI
  API calls."** The plan's stance: **do not ship the executor in Branch B.** A paid codex
  *Fusion seat* is possible only as a provider-script seat under Fusion's **already-relaxed**
  "Local only" deviation (egress to OpenAI) — a separate, explicitly-labeled deliverable,
  **not** the "$0 like Claude Code" thing the brief asked for.
- **Cross-cutting (even Branch A):** "$0" ≠ "unlimited." Codex draws on a **metered ChatGPT
  subscription** with per-model caps and **shared `~/.codex/` state**; a fan-out can exhaust
  caps or race on disk state, and programmatic orchestration may **conflict with OpenAI's
  ToS** (§2). Treat the codex engine as opt-in per send, like Fusion.

---

## 8. OPEN QUESTIONS *(resolve before any implementation)*

> **C0 resolved Q1–Q5 + Q8** (2026-06-22, §0/§3): the $0 premise, token-usage presence, the
> auth-state probe, the no-hang flag, the event schema, and per-seat state isolation are all
> confirmed. The genuinely-open ones are **Q6, Q7, Q9, Q10, Q11** below.

1. ✅ **RESOLVED — Branch A.** `codex exec --json` runs **$0 on the ChatGPT subscription** with
   no `OPENAI_API_KEY` (§0/§3). The viability gate is cleared.
2. **Token/cost usage in the terminal event?** Branch B's paid seat **can't be priced**
   without it — a double-conditional dead end (§2). What are the exact field names (§3)?
3. **Auth-state probe.** Is there a cheap, **non-billing** way to detect login/expiry so
   `codex_cli_available()` doesn't mis-gate on PATH alone (§2)? Codex logins expire; Claude's
   `shutil.which` check has no analogue need.
4. **Auto-bypass flag.** What is the exact `--dangerously-skip-permissions` analogue, and does
   it fully prevent a mid-run approval/sandbox **hang** in an unwatched tab (§3, §6)?
5. **Event schema for two parsers.** Confirm codex's terminal-event and per-event types so
   both `_build_codex_run` and the in-tab pretty-printer key off real names, not claude's
   `type:result`/`assistant` (§4).
6. **Hook-gap fix choice.** Is the in-band sidecar/PID convergence (iii) accepted, given it
   **diverges** codex executor logging from claude's hook path (§5)? Or is a **global**
   `~/.codex/config.toml` `notify` (ii) wanted despite the manual-session risk?
7. **Subscription caps & concurrency.** How many concurrent codex seats/dispatches before the
   ChatGPT plan throttles, and should the orchestrator cap codex fan-out below the Fusion
   default (§2)?
8. **`~/.codex/` state isolation.** Do concurrent codex seats need per-run state isolation
   (`CODEX_HOME`/config override) to avoid racing on shared history/auth (§2)?
9. **Judge calibration.** Is a **codex judge** as reliable as the Claude judge, given
   `_strip_fences`/the JSON-verdict prompts are tuned to Claude, and that claude-judging-codex
   vs codex-judging-claude aren't interchangeably calibrated (§C3)? Needs an A/B before making
   codex a default judge.
10. **ToS / longevity.** Does orchestrating a ChatGPT subscription via `codex` comply with
    OpenAI's terms — is the "$0 executor" durable, or "$0 today, banned tomorrow" (§2, §7)?
11. **Resume.** Is a codex analogue of `spawn_iterm2_resume` (tracked `claude --resume`)
    wanted, and does codex expose a session/resume model (§3)? Left out of the MVP above.

**STATUS — C0–C5 are BUILT + TESTED (2026-06-23): C5 shipped the codex SEAT parse, the
executor engine+model picker (validated + threaded; the codex executor SPAWN is the C6
seam, never a silent claude fallback), and codex availability gating in `_view_ctx` + the
dispatch form. Only C6 (the $0 codex executor — `spawn_codex_dispatch` + the §5 hook-gap
convergence) remains design-only. See the top-of-file build-status banner + the §6 table.**
