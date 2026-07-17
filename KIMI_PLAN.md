# KIMI_PLAN.md — Kimi Code CLI as a native Fusion engine (seat + executor)

Status: **DRAFT / K0 not yet passed.** Mirrors `CODEX_PLAN.md`. Judge stays on the
local `claude` CLI (unchanged). Operator chose: native CLI engine (not the paid
provider), full engine = **seat + executor** (2026-07-17).

## 1. Goal

Add Moonshot's **Kimi Code CLI** as a `$0`, subscription-backed engine — usable as a
Fusion **panel seat** AND a watchable **dispatch executor** — by porting the codex
integration. The membership powers it via OAuth `/login`; no per-token API key. This
is the codex twin: `codex exec --json` → `kimi --print --output-format stream-json`.

## 2. Why the CLI engine, not the paid provider

Membership ≠ API access (Moonshot never bundles them). The `kimi` **provider**
(`orchestrator/providers/kimi.py`, seed `FUSION_PROVIDERS_SEED["kimi"]`, model
`kimi-k3`, `$3/$15` per-token) built earlier this session is the **paid fallback** —
it stays inert while keyless. This plan is the primary, subscription path.

## 3. ⚠ Naming — collision with the existing paid `kimi` provider

`kimi` is already a Fusion cross-lab **provider name**, and `app._parse_fusion_panel`
routes provider seats by name (`type:"provider"`). The new **engine** must use
DISTINCT tokens or it clashes:

| Concept | Paid provider (exists) | New CLI engine (this plan) |
|---|---|---|
| seat `kind` | — (routed by name) | **`kimi_cli`** (cf. `codex_cli`) |
| picker `type` | `provider` (name=`kimi`) | **`kimicode`** (cf. `codex`) — NOT `kimi` |
| config seed key | `fusion.providers.kimi` | **`fusion.kimi`** (cf. `fusion.codex`) |
| profile seat list | `provider_seats` | **`kimi_seats`** (cf. `codex_seats`) |
| model id | `kimi-k3` | `kimi-k3` (same model, diff. transport) |

**DECISION PENDING (operator):** keep BOTH (paid provider `kimi` + subscription engine
`kimicode`) — default, no rework — OR retire the paid provider and let the engine own
the `kimi` name (removes tested code). Building assumes **keep both** until told otherwise.

## 4. CLI facts (✓ = confirmed from docs · ⧗ = pin live at K0)

- ✓ Install: `curl -LsSf https://code.kimi.com/install.sh | bash`
- ✓ Login: `kimi` → `/login` → Kimi Code → browser OAuth (subscription). Also `kimi login`/`kimi logout`.
- ✓ Headless: `kimi --print --output-format stream-json -p "<prompt>" -m kimi-k3` (JSONL). `--print` implies `--afk` (auto-approve + auto-dismiss questions). `--quiet` = `--print --output-format text --final-message-only`.
- ✓ stream-json lines: `{"role":"user"|"assistant"|"tool","content":"...","tool_calls":[...],"tool_call_id":"..."}`. **Final answer = last line where `role=="assistant"`** (with content).
- ✓ Full-auto (executor): `--yolo`/`-y` / `--yes` / `--auto-approve` (kimi has NO `-s` sandbox modes — approval-based, unlike codex).
- ✓ Continue tab: `kimi --continue`/`-C` (cwd) · `kimi --resume [ID]`/`-r` / `--session [ID]`/`-S` ("creates new if not found").
- ✓ Model: `-m`/`--model`. **No reasoning-effort flag** (simpler than codex — drop the effort ladder/whitelist entirely).
- ✓ Working dir: `-w`/`--work-dir`, `--add-dir`.
- ✓ Token/cost policy: **subscription → tokens=0, cost=$0** (identical to `_codex_seat_answer`; no usage field in JSON is a NON-issue).
- ⧗ **Auth probe** (the one real unknown): kimi has `login`/`logout` but no documented `status`. Find a non-billing check for `kimi_cli_available()`: try `kimi login status`/`kimi status`/`kimi --help`, else stat the auth-token file (`~/.kimi*`). MUST be non-hanging (timeout + closed stdin) + fail-safe False.
- ⧗ **Session id for resume**: kimi stream-json shows no session-id field. Prefer FORCING it: pass `--session <run_id>` into turn 1, resume with the same id (sidesteps parsing). Verify `--session`/`--resume` accept a caller-chosen id.
- ⧗ Confirm headless print-mode authenticates off the OAuth **subscription** (not an API key) — the whole premise.
- ⧗ Confirm a thinking/reasoning line (if any) doesn't shadow the final assistant content.

## 5. Phases (mirror codex C0–C6)

### K0 — gate (BLOCKED on operator) → task #5
Install + `/login`. Then I capture live: `kimi --version`, `kimi --help`, `kimi login --help`,
`kimi --print --output-format stream-json -p "reply one word: pong"`, and the resume/session
behavior. Pin the ⧗ items above into §4. No build until green.

### K1 — invoker + parser (claude_runner.py) — cf. C1
Port, with `kimi_` names:
- `_kimi_envelope_from_lines(lines)` ← `_codex_envelope_from_lines` (L369): last `role=="assistant"` line → text; session_id if surfaced; tolerate garbage, never raise.
- `_envelope_from_kimi_stream(path)` ← `_envelope_from_codex_stream` (L412).
- `_build_kimi_run(env, requested_model)` ← `_build_codex_run` (L423): `ClaudeRun(ok,text,...,cost_usd=0.0,model=requested_model,raw=env)`.
- `run_kimi_headless(...)` ← `run_codex_headless` (L506): cmd `["kimi","--print","--output-format","stream-json","-p",prompt,"-m",model]`; scrub `MOONSHOT_API_KEY`+`ORCHESTRATOR_RUN_ID`; `stdin=DEVNULL`. (No `-s`, no effort.)
- `run_kimi_json(...)` ← `run_codex_json` (L563): tab + poll `<id>.done`/`.pid` in `spawn.KIMI_DIR`; reuse engine-neutral grace/poll/`_read_pid`/`_tail`; `finally: spawn.finish_kimi_tab`.
- `DEFAULT_KIMI_MODEL = config.KIMI_ENGINE_SEED["model"]` (L366 analog).

### K2 — Fusion SEAT + availability — cf. C2
- `_kimi_seat_answer(seat, prompt, cwd)` ← `_codex_seat_answer` (L934): returns `{name,model,text,cost:0.0,prompt_tokens:0,completion_tokens:0,subscription:True,lens,ok}`; fail-soft `{name,ok:False,error,lens}`. (No `effort`.)
- `config.kimi_cli_available()` ← `codex_cli_available` (config.py L469): seeded `auth_probe`, `MOONSHOT_API_KEY` scrub, timeout, stdin closed, `returncode==0`, fail-safe False.
- `config.is_fusion_available()` (L515): add `or kimi_cli_available()` (cost-order LAST).
- `run_fusion_json` seat gathering (L1084-1151): add `kind=="kimi_cli"` branch + `kimi_ok = config.kimi_cli_available()` gate.

### K3 — config seed centralization (config.py) — cf. C4
- Add `KIMI_ENGINE_SEED` (model `kimi-k3`, `models` list, `print`/`output-format`/`resume`/`session` flags, `auth_probe`, `max_concurrent_dispatches`, default `seats`; NO effort, NO sandbox modes) + `kimi_engine()` reader + `fusion.kimi` merge in `fusion_config()`.
- claude_runner + spawn IMPORT the seed (one source of truth); guard with a merge/drift test.
- Profiles: `_normalize_profile` gains `kimi_seats` (L260); `save_profile` accepts them.

### K4 — dispatch-form picker + routing (app.py + templates) — cf. C5
- `_kimi_seat_models()` ← `_codex_seat_models` (L46); NO `_kimi_seat_efforts`.
- `_parse_fusion_panel` kimi branch (L1306 analog): `type=="kimicode"` → `{"kind":"kimi_cli","model"[,"lens"]}`.
- `_derive_executor`/`_validate_executor_engine` (L1333/1368): route `kimi-k3` → `("kimi","",model)`; add `kimi_available` param.
- `_view_ctx` (L179): add `kimi_cli_available`, `kimi_seat_models`; wire `_run_dispatch` (L370) kimi branch + `max_concurrent_dispatches` cap (visible failed row, never a claude fallback).
- `templates/index.html`: executor picker + kimi-seat block + JS `KIMI_MODELS`/`KIMI_AVAILABLE` + seat-row builder + profile restore + 2-seat gate.

### K5 — executor (spawn.py + app.py + watchdog.py) — cf. C6
- `KIMI_DIR`/`KIMI_RUN_SH`, `_KIMI_RUN_SH_TEMPLATE` (seat tab), `spawn_kimi_tab`/`finish_kimi_tab`, `cleanup_kimi_files`.
- `_build_kimi_dispatch_run_sh(eng)` ← `_build_codex_dispatch_run_sh` (L1213): FIFO + backgrounded `kimi --print ... --yolo` → real PID to `PIDS_DIR/<id>.pid` (claude path, so kill/cap/reaper find it); wall-clock via `watchdog`; capture session id before `.done`; interactive resume hand-off `exec kimi --resume <id>` (NO `</dev/null` — needs TTY), keep-open fallback. Env `ORCHESTRATOR_KIMI_RUN_ID`, tab tag `user.orch_id`.
- `is_kimi_dispatch(id)` ← `is_codex_dispatch` (L1427, sidecar detection — no DB column).
- `spawn_kimi_dispatch(...)` ← `spawn_codex_dispatch` (L1437).
- `app._kimi_dispatch_poller`/`_kimi_timeline_step` ← `_codex_dispatch_poller` (L1084); `watchdog.py` poller registry + `engine="kimi"` cap branch; `summarizer.distill_transcript` kimi branch (L142); `app._resolve_refine_transcript` (L610); `spawn.cleanup_dispatch_files` kimi sidecar list (L781).
- **Restart required** to deploy the runner (uvicorn reload=False; "green tests ≠ deployed runner").

### K6 — tests (mirror codex suite)
Analogs of `test_codex_parser.py`, `test_codex_seat.py`, `test_codex_executor.py`,
`test_codex_config.py` (incl. the heredoc/seed-drift `TestSpawn*RunShPinnedToSeed` + no
unresolved `@@...@@` + resume-needs-a-TTY + PID-to-claude-path guards). Judge tests
(`test_codex_judge.py`) only if a kimi judge is wired (default: not — unwired knob, like codex).

## 6. Non-goals / deferred
- Kimi as a selectable **judge** engine (codex declined a per-call judge; keep parity — unwired).
- Reasoning-effort ladder (kimi has no effort flag).
- Retiring the paid `kimi` provider (pending operator decision, §3).
