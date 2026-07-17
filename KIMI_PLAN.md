# KIMI_PLAN.md — Kimi Code CLI as a native Fusion engine (seat + executor)

Status: **K0 PASSED (2026-07-17)** — CLI installed, OAuth-logged-in, schema pinned live
(§4). Paid `kimi` provider RETIRED. K1–K6 pending. Mirrors `CODEX_PLAN.md`. Judge stays
on the local `claude` CLI. Operator chose: native CLI engine, full = **seat + executor**.

## 1. Goal

Add Moonshot's **Kimi Code CLI** as a `$0`, subscription-backed engine — a Fusion **panel
seat** AND a watchable **dispatch executor** — by porting the codex integration. Membership
powers it via OAuth; no per-token API key. codex twin: `codex exec --json` → `kimi -p …
--output-format stream-json`.

## 2. Why the CLI engine, not the paid provider

Membership ≠ API access (Moonshot never bundles them). The subscription authenticates the
CLI via OAuth (device flow); the CLI hits the coding endpoint `api.kimi.com/coding/v1`. The
earlier paid `kimi` provider (per-token `api.moonshot.ai/v1`) was **removed** (operator: one
Kimi).

## 3. Naming — the engine OWNS `kimi` (paid provider retired)

`providers/kimi.py` + the `FUSION_PROVIDERS_SEED["kimi"]` entry + install.sh line are gone.
The engine takes the name, parallel to codex:

| token | value (cf. codex) |
|---|---|
| picker `type` | `kimi` (cf. `codex`) |
| seat `kind` | `kimi_cli` (cf. `codex_cli`) |
| config seed key | `fusion.kimi` (cf. `fusion.codex`) |
| profile seat list | `kimi_seats` (cf. `codex_seats`) |
| model alias | `kimi-code/k3` |

## 4. CLI facts — PINNED LIVE vs kimi-code v0.27.0 (2026-07-17; re-verify on `kimi upgrade`)

⚠ The installed CLI is **`kimi-code`** (`~/.kimi-code/bin/kimi`, v0.27.0) — NOT the legacy
`kimi-cli` the online docs (moonshotai.github.io/**kimi-cli**) describe; their flags differ
(`kimi migrate` exists to move off legacy). Real docs: moonshotai.github.io/**kimi-code**.
Verified from `kimi --help` + a live subscription smoke (`SMOKE_EXIT=0`):

- **Non-interactive**: `kimi -p "<prompt>" --output-format stream-json` — the `-p/--prompt`
  flag IS headless mode; there is **NO `--print`**. Subscription OAuth powers it. ✓
- **stream-json schema** (JSONL): `{"role":"assistant","content":"..."}` — final answer =
  **LAST `role=="assistant"` line's `content`**. A trailing meta line carries the resume id:
  `{"role":"meta","type":"session.resume_hint","session_id":"session_...","command":"kimi -r <id>"}`.
  tool lines `{"role":"tool",...}`. **No usage field** → tokens/cost = 0 (subscription; mirrors codex).
- **Model**: `-m kimi-code/k3` (already the config default). Aliases in `~/.kimi-code/config.toml`:
  `kimi-code/k3`=K3, `…/kimi-for-coding`=K2.7, `…/-highspeed`.
- **Effort**: K3 supports low/high/max (default **max**) but **ONLY via config.toml**
  (`[thinking] effort`, model `default_effort`) — **no per-invocation CLI flag**. So: no effort
  ladder; run at config default. Simpler than codex.
- **Resume/continue** (executor tab): parse `session.resume_hint.session_id`, then
  `kimi -r <id>` (== `-S/--session <id>`); or `-c/--continue` (cwd's last session).
- **Approvals**: `-p` handles approvals itself and **cannot combine with `-y`** ("Cannot
  combine --prompt with --yolo"). Seat = `-p` alone (smoke answered without touching files).
  Interactive executor hand-off tab = `-y`/`--auto`. **No `-s` sandbox modes.**
- **Working dir**: no `-w`. Runs in cwd; kimi resets cwd to the git/workspace root (prints
  "Shell cwd was reset to …"); `--add-dir` adds workspace dirs.
- **Auth probe** (non-billing): `kimi provider list` → exit 0 + `source=oauth`. `kimi doctor`
  only validates config files (NOT login). Creds: `~/.kimi-code/credentials/kimi-code.json`.
- **PATH**: binary at `~/.kimi-code/bin`, exported only in interactive `.zshrc`. Invoker/probe
  MUST resolve it (`shutil.which("kimi")` OR a `~/.kimi-code/bin/kimi` fallback), since the
  server subprocess PATH may lack it.

**⧗ Still to pin (K5, executor only):** confirm `-p` (or `-p --auto`) **auto-approves TOOL USE**
for an agentic run that edits files — the smoke needed no tools, and `-p` forbids `-y`. If `-p`
auto-denies tools, the executor needs `--auto` (verify it's allowed with `-p`).

## 5. Phases (mirror codex C0–C6)

### K0 — gate ✓ DONE (§4)

### K1 — invoker + parser (`claude_runner.py`) — cf. C1
- `_kimi_envelope_from_lines(lines)` ← `_codex_envelope_from_lines` (L369): text = last
  `role=="assistant"` `content`; `session_id` from the `role=="meta"` `session.resume_hint`;
  tolerate garbage, never raise; return None if no assistant line.
- `_envelope_from_kimi_stream(path)` ← L412; `_build_kimi_run(env, model)` ← `_build_codex_run`
  (L423): `ClaudeRun(ok,text,…,cost_usd=0.0,model=requested_model,raw=env)`.
- `run_kimi_headless(...)` ← `run_codex_headless` (L506): cmd `[<kimi>, "-p", prompt,
  "--output-format","stream-json","-m",model]` (resolve `<kimi>` via which/`~/.kimi-code/bin`;
  no `--print`, no `-s`, no effort). Scrub `MOONSHOT_API_KEY`+`OPENAI_API_KEY`+`ORCHESTRATOR_RUN_ID`;
  `stdin=DEVNULL`.
- `run_kimi_json(...)` ← `run_codex_json` (L563): tab + poll `<id>.done`/`.pid` in `spawn.KIMI_DIR`;
  reuse engine-neutral grace/poll/`_read_pid`/`_tail`; `finally: spawn.finish_kimi_tab`.
- `DEFAULT_KIMI_MODEL = config.KIMI_ENGINE_SEED["model"]`.

### K2 — Fusion SEAT + availability — cf. C2
- `_kimi_seat_answer(seat, prompt, cwd)` ← `_codex_seat_answer` (L934): `{name,model,text,
  cost:0.0,prompt_tokens:0,completion_tokens:0,subscription:True,lens,ok}`; fail-soft
  `{name,ok:False,error,lens}`. (No effort.)
- `config.kimi_cli_available()` ← `codex_cli_available` (L469): binary resolvable AND probe
  `kimi provider list` exit 0 (+ output has `oauth`/`kimi-code`); timeout, stdin closed, key
  scrub, fail-safe False.
- `config.is_fusion_available()` (L515): add `or kimi_cli_available()` (cost-order LAST).
- `run_fusion_json` seat gathering (L1084-1151): add `kind=="kimi_cli"` branch + `kimi_ok` gate.

### K3 — config seed centralization (`config.py`) — cf. C4
- `KIMI_ENGINE_SEED` (model `kimi-code/k3`, `models`, flag tokens `-p`/`--output-format`/
  `stream-json`/resume `-r`/continue `-c`, `auth_probe = ["kimi","provider","list"]`,
  `bin_fallback = "~/.kimi-code/bin/kimi"`, `max_concurrent_dispatches`, default `seats`; NO
  effort, NO sandbox) + `kimi_engine()` + `fusion.kimi` merge in `fusion_config()`.
- claude_runner + spawn IMPORT the seed (one source of truth); merge/drift test.
- `_normalize_profile` gains `kimi_seats` (L260); `save_profile` accepts them.

### K4 — dispatch-form picker + routing (`app.py` + `templates/index.html`) — cf. C5
- `_kimi_seat_models()` ← `_codex_seat_models` (L46); NO `_kimi_seat_efforts`.
- `_parse_fusion_panel` kimi branch (L1306 analog): `type=="kimi"` → `{"kind":"kimi_cli","model"[,"lens"]}`.
- `_derive_executor`/`_validate_executor_engine` (L1333/1368): route the `kimi-code/k3` id →
  `("kimi","",model)`; add `kimi_available` param.
- `_view_ctx` (L179): add `kimi_cli_available`, `kimi_seat_models`; `_run_dispatch` kimi branch
  (L370) + `max_concurrent_dispatches` cap (visible failed row, NEVER a claude fallback).
- `index.html`: executor picker + kimi-seat block + JS `KIMI_MODELS`/`KIMI_AVAILABLE` + seat-row
  builder + profile restore + 2-seat gate.

### K5 — executor (`spawn.py` + `app.py` + `watchdog.py`) — cf. C6
- `KIMI_DIR`/`KIMI_RUN_SH`, `_KIMI_RUN_SH_TEMPLATE` (seat tab), `spawn_kimi_tab`/`finish_kimi_tab`,
  `cleanup_kimi_files`.
- `_build_kimi_dispatch_run_sh(eng)` ← `_build_codex_dispatch_run_sh` (L1213): FIFO + backgrounded
  `kimi -p … --output-format stream-json [--auto?]` → real PID to `PIDS_DIR/<id>.pid` (claude path,
  so kill/cap/reaper find it); wall-clock via `watchdog`; capture `session_id` from the resume_hint
  BEFORE `.done`; interactive hand-off `exec kimi -r <session_id> [-y|--auto]` (NO `</dev/null` —
  needs TTY), keep-open fallback. Env `ORCHESTRATOR_KIMI_RUN_ID`, tab tag `user.orch_id`. Resolve
  `<kimi>` binary. **Resolve the ⧗ tool-approval question (§4) HERE first.**
- `is_kimi_dispatch(id)` ← `is_codex_dispatch` (L1427, sidecar detection — no DB column).
- `spawn_kimi_dispatch(...)` ← `spawn_codex_dispatch` (L1437).
- `app._kimi_dispatch_poller`/`_kimi_timeline_step` ← L1084; `watchdog` poller registry +
  `engine="kimi"` cap; `summarizer.distill_transcript` kimi branch (L142); `app._resolve_refine_transcript`
  (L610); `spawn.cleanup_dispatch_files` kimi sidecar list (L781).
- **Restart required** to deploy the runner (uvicorn reload=False; "green tests ≠ deployed runner").

### K6 — tests (mirror the codex suite)
Analogs of `test_codex_parser.py`, `test_codex_seat.py`, `test_codex_executor.py`,
`test_codex_config.py` (incl. `TestSpawn*RunShPinnedToSeed` heredoc/seed-drift + no unresolved
`@@…@@` + resume-needs-a-TTY + PID-to-claude-path guards).

## 6. Non-goals / deferred
- Kimi as a selectable **judge** engine (parity with codex — unwired knob).
- **Effort ladder** — kimi-code has no per-call effort flag (config-only). N/A.
- The paid provider — retired (§3).
