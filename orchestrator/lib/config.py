"""Fusion configuration: the provider registry, per-provider key resolution,
and presets — all read from ~/.orchestrator/config.json (never the repo).

This is the F0 foundation of the optional, default-off multi-model "Fusion"
brain layer (see FUSION_PLAN.md). It touches no network and makes no model
call; it only answers "which providers are configured and usable right now?"
so later phases (and the dispatch UI) can gate the Fusion toggle.

Design contracts (relied on by callers AND the standalone provider scripts):
  - load_config() NEVER raises — returns {} if the file is absent or malformed.
    Fusion must degrade to the local `claude` path, not crash a dispatch, when
    config is missing or broken.
  - Key resolution precedence, per provider: the provider's `key_env`
    environment variable  →  config.json's per-provider `api_key`  →  None.
    (Each provider/<name>.py applies the SAME precedence independently, so the
    key is read inside the script — never passed via AppleScript.)
  - A provider is ACTIVE when its key resolves AND it is not explicitly disabled
    (`enabled: false`). Fusion is "available" only at >= 2 active providers.

The registry/preset SEEDS below are fallbacks, not an allowlist: real values
live in config.json and are merged over these (see fusion_config()). Keeping the
seeds here means Fusion has sane defaults before the user edits anything, and a
later phase's claude_runner can import them rather than redefine them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Optional

from orchestrator.lib.db import DATA_DIR

# Registry + secrets live in the data dir, never the repo. install.sh writes it
# chmod 600 (it holds the per-provider api_key fallbacks).
CONFIG_PATH = DATA_DIR / "config.json"

# ── Registry / preset SEEDS (fallback defaults; real values come from config.json) ──
# Each entry names a provider SCRIPT (which owns the lab's base URL + native
# request/response format), the env var holding its key, the native model id,
# and list prices ($/M, in→out) used only for cost accounting. Prices are a
# 2026-06-17 snapshot — re-verify; they live in config so a swap is a file edit.
FUSION_PROVIDERS_SEED = {
    "deepseek": {"script": "providers/deepseek.py", "key_env": "DEEPSEEK_API_KEY",
                 "model": "deepseek-chat",    "price_in": 0.44, "price_out": 0.87},
    "xai":      {"script": "providers/xai.py",      "key_env": "XAI_API_KEY",
                 "model": "grok-4",           "price_in": 1.25, "price_out": 2.50},
    "gemini":   {"script": "providers/gemini.py",   "key_env": "GEMINI_API_KEY",
                 "model": "gemini-2.5-flash", "price_in": 0.30, "price_out": 1.50},
    "minimax":  {"script": "providers/minimax.py",  "key_env": "MINIMAX_API_KEY",
                 "model": "MiniMax-Text-01",  "price_in": 0.30, "price_out": 1.20},
    "glm":      {"script": "providers/glm.py",      "key_env": "ZAI_API_KEY",
                 "model": "glm-4.6",          "price_in": 1.40, "price_out": 4.40},
    "qwen":     {"script": "providers/qwen.py",     "key_env": "DASHSCOPE_API_KEY",
                 "model": "qwen-max",         "price_in": 1.25, "price_out": 3.75},
}
FUSION_PRESETS_SEED = {
    "budget":   ["deepseek", "minimax", "gemini"],
    "balanced": ["deepseek", "xai", "qwen"],
    "max":      ["deepseek", "xai", "gemini", "minimax", "glm", "qwen"],  # high-stakes only
}
DEFAULT_FUSION_PRESET = "budget"
DEFAULT_FUSION_TIMEOUT_S = 300

# ── F8.4: per-seat LENS prompts (the §5 decorrelation refinement) ────────────
# A lens is a short perspective a panel seat answers THROUGH ("find the risks",
# "find the simplest path", "find what's ambiguous"), so the seats make less
# correlated errors and the judge has genuinely different angles to synthesize.
# A seat opts into a lens by NAME (resolved against this seed merged with
# config.json's fusion.lenses) or by literal text; no lens ⇒ the seat gets the
# shared prompt verbatim, so lenses are opt-in and a lens-free panel is unchanged.
#
# Decorrelation discipline (the WHOLE point of lenses): each lens must attack a
# DISTINCT failure axis, not be a synonym of another. The original three accept
# the task's framing and reason about the PRESENT artifact: risks = downside
# enumeration, simplest = minimal path / what to cut, ambiguity = what's unclear
# in the QUESTION. The seven added below open new axes (the §11.c.3 backlog,
# 2026-06-22): first-principles rejects the framing itself; user-intent serves
# the goal behind the literal request; long-horizon weighs future-change cost
# (NOT present minimalism — that's simplest's axis); concrete forces the runnable
# artifact; adversary red-teams a committed answer (sharpest-edge vs. risks);
# precedent reuses prior art (the literal inverse of first-principles); evidence
# distrusts the FACTS (vs. adversary's distrust of the DESIGN).
FUSION_LENSES_SEED = {
    "risks":            "Approach this through a RISK lens: surface failure modes, edge "
                        "cases, security and correctness hazards, and what could go wrong "
                        "— even where the obvious approach looks fine.",
    "simplest":         "Approach this through a SIMPLICITY lens: favour the most direct, "
                        "minimal path that still solves the task, and call out needless "
                        "complexity or anything that could be cut.",
    "ambiguity":        "Approach this through an AMBIGUITY lens: surface what is "
                        "underspecified, the assumptions a confident answer would smuggle "
                        "in, and the questions worth resolving before acting.",
    "first-principles": "Approach this through a FIRST-PRINCIPLES lens: ignore "
                        "convention, precedent, and the way the task is framed; "
                        "re-derive the right answer from the actual goal and "
                        "constraints, and call out any premise in the task that "
                        "doesn't hold.",
    "user-intent":      "Approach this through a USER-INTENT lens: answer what "
                        "the asker actually needs — the underlying goal behind "
                        "the literal request — not just the words as written; "
                        "where the literal reading and the real intent diverge, "
                        "serve the intent and say so.",
    "long-horizon":     "Approach this through a LONG-HORIZON lens: weigh what "
                        "this choice costs later, not just now — how it ages, "
                        "scales, and constrains future change; favour what stays "
                        "cheap to reverse and flag anything that quietly locks "
                        "the project in.",
    "concrete":         "Approach this through a CONCRETE lens: prefer the exact, "
                        "runnable artifact over description — the specific code, "
                        "command, value, or worked example — and make every claim "
                        "something the reader could check or execute directly.",
    "adversary":        "Approach this through an ADVERSARY lens: assume the "
                        "obvious answer is wrong and try to defeat it — find the "
                        "counterexample, the input that breaks it, the case where "
                        "it backfires — and report what survives the attack.",
    "precedent":        "Approach this through a PRECEDENT lens: look first for "
                        "how this is already solved — the existing pattern, "
                        "library, prior art, or in-repo convention — and prefer "
                        "adapting a proven solution over inventing a new one; "
                        "name what you'd reuse.",
    "evidence":         "Approach this through an EVIDENCE lens: treat every "
                        "factual claim as unproven until supported; demand the "
                        "source or the verification step, actively seek "
                        "disconfirming evidence, and separate what is established "
                        "from what is assumed.",
}

# ── Codex ENGINE SEED (C4): the codex CLI's model id + flag set, merged from ───
# config.json's `fusion.codex` exactly like the provider/preset/lens seeds.
# claude_runner IMPORTS these (the run_codex_* flag set + the selectable judge's
# model resolution) instead of redefining the literals, so the codex `-m` id and
# flags have ONE source of truth, swappable by a config.json edit. Flags + event
# schema are version-pinned to codex-cli 0.144.4 (originally 0.141.0, CODEX_PLAN.md
# §3; flags + exec JSONL schema re-verified live on 0.144.4, 2026-07-14); codex
# churns them, so re-verify on upgrade.
#
# `model` is a codex id, NEVER a Claude id: the Fusion judge/verify defaults are
# Claude ids (opus), and feeding one to `codex -m` is a silent downgrade (dispatch
# #3) — so the codex judge path resolves its model from HERE, not the Claude default.
CODEX_ENGINE_SEED = {
    # `-m` value codex passes. On a ChatGPT-subscription account (Branch A) the valid
    # ids are the plain GPT models codex routes to — the GPT-5.6 family `gpt-5.6-sol`
    # (flagship, best coding), `gpt-5.6-terra` (balanced), `gpt-5.6-luna` (fastest,
    # most runway), plus the prior gen `gpt-5.5`/`gpt-5.4`/`gpt-5.4-mini` — NEVER a
    # Claude id, and NOT the bare family alias: `gpt-5.6` (the docs shorthand) is
    # REJECTED ("model is not supported when using Codex with a ChatGPT account" —
    # verified live 2026-07-14 on BOTH codex-cli 0.141.0 and 0.144.4), exactly like the
    # `-codex`-suffixed `gpt-5-codex` (API-only/retired, verified 2026-06-23). The 5.6
    # family needs codex-cli >= 0.143.0 — on 0.141.0 it 400s "requires a newer version
    # of Codex" (this machine upgraded to 0.144.4, 2026-07-14). Codex churns these, so
    # re-verify on upgrade; override per-machine via config.json `fusion.codex.model`
    # (e.g. to gpt-5.6-luna for a tight Plus cap) with no code change.
    "model": "gpt-5.6-sol",
    # The full set of valid ChatGPT-account codex `-m` ids — the dispatch picker's codex
    # model options AND the validation whitelist (app._codex_seat_models unions these).
    # gpt-5.6-sol / -terra / -luna live-verified ACCEPTED 2026-07-14 (codex-cli 0.144.4;
    # each returned a real answer, and the bogus-id negative control still 400s).
    # gpt-5.5 / gpt-5.4 / gpt-5.4-mini verified 2026-06-23 and KEPT VALID so saved
    # dispatches/profiles still validate; gpt-5-codex and gpt-5.5-mini REJECTED ("not
    # supported when using Codex with a ChatGPT account"). Per-window Plus runway grows
    # DOWN the list (5.6-era runway unmeasured; 5.5-era was ≈15-80 / 20-100 / 60-350
    # msgs per 5h). `model` above must be one of these; override the whole list via
    # `fusion.codex.models`.
    "models": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
               "gpt-5.5", "gpt-5.4", "gpt-5.4-mini"],
    "effort": "",                    # default reasoning effort; "" ⇒ codex's own model default (no -c override)
    # The selectable reasoning-effort ladder offered by the Fusion codex-seat picker
    # AND the /send validation whitelist (app._codex_seat_efforts unions these). These
    # are codex's OWN values, NOT claude's (claude = low/medium/high/xhigh/max): the
    # live API rejects an unknown value with a 400 listing the supported set —
    # verified 2026-06-24 (codex-cli 0.141.0), re-verified IDENTICAL 2026-07-14
    # (0.144.4) for BOTH gpt-5.6-sol and gpt-5.5: 'none','minimal','low','medium',
    # 'high','xhigh' (ChatGPT's "5.6 max reasoning" is NOT a wire-level enum value).
    # We offer the meaningful ladder and OMIT 'none' (it disables reasoning — a footgun
    # for a perspective seat; add it back per-machine via `fusion.codex.efforts` if
    # wanted). The picker also offers a "default" option that maps to the empty string
    # (effort=""), which means "no -c override → the model's own default" and is ALWAYS
    # valid (it is deliberately NOT in this list — an empty effort is omitted, not
    # validated against it). Codex churns these, so re-verify on upgrade; override the
    # whole ladder via config.json `fusion.codex.efforts`.
    "efforts": ["minimal", "low", "medium", "high", "xhigh"],
    "exec_subcmd": "exec",           # non-interactive subcommand — the `claude -p` analogue (§3)
    # C6 HYBRID executor (the #246 fix — mirrors the claude REPL's stay-open, continuable
    # tab): after the CAPTURED one-shot `exec` turn the orchestrator finalizes from the
    # sidecar, the SAME iTerm tab hands off to an INTERACTIVE `codex resume <thread_id>` so
    # the user can read the answer, keep the conversation going (full context — verified),
    # and close the tab manually. The dispatch is already 'completed' by then (PID file +
    # wall-clock cap + poller all cleared at finalize), so this interactive phase is
    # UNTRACKED — it holds no concurrency slot, can't be cap-killed, and its follow-up turns
    # are NOT recorded (exactly a claude dispatch's post-Stop-hook state).
    #
    # resume_flags carries the resume-ONLY flags (the shared `-s <sandbox>` comes from
    # executor_sandbox below, so turn 1 and the resume use ONE sandbox value):
    #   --include-non-interactive  lets the interactive resume adopt the exec-CREATED
    #                              (non-interactive) session by its explicit thread id.
    #   -a never                   never ask for approval — the resume session acts WITHOUT
    #                              prompts, the codex twin of `claude --dangerously-skip-
    #                              permissions`, so a continued codex dispatch feels identical
    #                              to a continued claude one (operator, 2026-06-25: "no
    #                              noticeable difference between picking codex vs claude").
    # (`-a` is interactive-only — NOT valid on `exec` — so it lives here, not on turn 1.)
    # 0.144.4-pinned (both flags re-verified in `codex resume --help`, 2026-07-14);
    # interpolated into spawn.codex_dispatch_run.sh + pinned by tests.
    "resume_subcmd": "resume",
    "resume_flags": "--include-non-interactive -a never",
    "sandbox": "read-only",          # `-s <mode>` for a $0 SEAT/judge (read-only — it only READS to answer)
    "executor_sandbox": "danger-full-access",  # `-s <mode>` for the C6 EXECUTOR — used on BOTH turn 1 and the resume
                                     # hand-off (the single source of truth for the dispatch's sandbox). danger-full-access
                                     # = full machine access, no sandbox: the codex twin of `claude --dangerously-skip-
                                     # permissions`, so a codex dispatch is INDISTINGUISHABLE from a claude one
                                     # (operator-chosen 2026-06-25: "no noticeable difference between picking codex vs
                                     # claude"). REVERSES C6.0's confined `workspace-write` default — C6.0 had verified
                                     # workspace-write is write-capable + non-hanging + project-confined, but the operator
                                     # prefers claude parity over confinement. Full access via the `-s` sandbox MODE (clean,
                                     # version-stable), NOT the auto_bypass_flag (which OVERRIDES -s and is the
                                     # EXTREMELY-DANGEROUS-flagged path). Re-confine per-machine via a config.json
                                     # `fusion.codex.executor_sandbox` override (e.g. "workspace-write") — turn 1 + resume
                                     # both follow it; reversible, no code change.
    "json_flag": "--json",           # structured-JSONL flag the parser reads (§3)
    "auth_probe": ["codex", "login", "status"],   # cheap, NON-BILLING auth-state probe (§3; not just `which`)
    "auto_bypass_flag": "--dangerously-bypass-approvals-and-sandbox",  # full-access no-sandbox flag; C6.0 found it
                                     # OVERRIDES -s entirely (NOT additive). UNUSED by the executor — full access now
                                     # comes from `-s danger-full-access` above (the clean sandbox MODE), not this flag;
                                     # kept for the C0/§3 record + an opt-in override.
    "max_concurrent_dispatches": 2,  # §2 Q7 / Plus cap GUARD: max codex EXECUTOR dispatches running at once.
                                     # Each codex dispatch is a full agentic run sharing ONE 5-hour subscription
                                     # window (esp. tight on Plus), so a burst of concurrent codex dispatches can
                                     # silently exhaust it. _run_dispatch rejects a codex spawn (VISIBLE failed row,
                                     # never a claude fallback) once this many codex dispatches are already running.
                                     # A best-effort SOFT cap (a near-simultaneous pair may overshoot by 1). 0/None ⇒
                                     # unlimited. Tune per-tier via config.json `fusion.codex.max_concurrent_dispatches`
                                     # (raise on Pro/Business; this does NOT bound a single Fusion codex PANEL — that's
                                     # bounded per-call by its seat count).
    # A default codex panel for the C5 dispatch picker (>=2 seats, lens-decorrelated
    # so the judge sees genuinely different angles). Unused until C5 — here so the
    # picker's default lives in config, not code, like FUSION_PRESETS_SEED.
    "seats": [
        {"kind": "codex_cli", "model": "gpt-5.6-sol", "lens": "risks"},
        {"kind": "codex_cli", "model": "gpt-5.6-sol", "lens": "simplest"},
    ],
}


# ── Kimi ENGINE SEED (K3): the kimi-code CLI's model alias + flag set, merged from
# config.json's `fusion.kimi` exactly like the codex/provider/preset seeds. claude_runner
# and spawn IMPORT these so the `-m` alias + flags have ONE source of truth. Pinned to
# kimi-code 0.27.0 (KIMI_PLAN.md §4; flags + stream-json schema verified live 2026-07-17);
# the CLI churns them, so re-verify on `kimi upgrade`.
#
# ⚠ This is kimi-code, NOT the legacy `kimi-cli` in the online docs: the headless flag is
# `-p` (NOT `--print`), there are NO `-s` sandbox modes and NO per-call effort flag, and the
# binary lives at ~/.kimi-code/bin (interactive-PATH only — resolve via bin_fallback).
KIMI_ENGINE_SEED = {
    # `-m` model ALIAS (resolved from ~/.kimi-code/config.toml). kimi-code/k3 = K3 (the config
    # default). NOT a Claude/codex id. Override per-machine via config.json fusion.kimi.model.
    "model": "kimi-code/k3",
    # Selectable aliases — the picker's model options AND the validation whitelist.
    "models": ["kimi-code/k3", "kimi-code/kimi-for-coding", "kimi-code/kimi-for-coding-highspeed"],
    "prompt_flag": "-p",                        # non-interactive one-shot (the `codex exec` analog; NOT --print)
    "output_format_flag": "--output-format",
    "output_format": "stream-json",             # JSONL the parser reads
    "resume_flag": "-r",                         # resume-by-id (== -S/--session); id from the session.resume_hint line
    "continue_flag": "-c",                       # continue the cwd's last session (alt resume)
    # K5 EXECUTOR: turn-1 `-p` auto-approves tool use (verified 2026-07-17) and CANNOT take
    # -y/--auto; the INTERACTIVE resume hand-off (`kimi -r <id>`) adds this flag for the
    # never-prompt, claude-parity continuation ([[codex-claude-dispatch-parity]]). Re-verify
    # `-r <id> -y` on `kimi upgrade`; if it ever errors, the runner falls back to a kept-open
    # tab (no data loss) — drop this to "" via config.json fusion.kimi.resume_approve_flag.
    "resume_approve_flag": "-y",
    "auth_probe": ["kimi", "provider", "list"],  # cheap NON-BILLING login probe (exit 0 + source=oauth)
    "bin_fallback": "~/.kimi-code/bin/kimi",     # PATH has ~/.kimi-code/bin only in interactive .zshrc; resolve here if which() misses
    "creds_path": "~/.kimi-code/credentials/kimi-code.json",  # OAuth credential; absent ⇒ never logged in
    "log_path": "~/.kimi-code/logs/kimi-code.log",  # U1: the CLI's own log — the usage backfill reads pinned 403s from here (UTC timestamps)
    "max_concurrent_dispatches": 2,             # Plus-cap guard (mirror codex): max kimi EXECUTOR dispatches at once
    # A default kimi panel for the K4 dispatch picker (>=2 lens-decorrelated seats). Unused until K4.
    "seats": [
        {"kind": "kimi_cli", "model": "kimi-code/k3", "lens": "risks"},
        {"kind": "kimi_cli", "model": "kimi-code/k3", "lens": "simplest"},
    ],
}


# ── U1: usage-dashboard engine enumeration (USAGE_PLAN.md) ───────────────────
# The engines the usage layer (`usage_events` / `engine_limit_state`) tracks.
# Derived from the config SEEDS — never a literal list in app.py (drift-guard
# convention). The three CLI/subscription engines have no provider-registry
# entry (claude is gated by claude_cli_available(), codex/kimi by their engine
# seeds), so they are named here, in config — the seeds' home — and every
# registry provider (seeds + config.json customs) is appended after them.
USAGE_CLI_ENGINES = ("claude", "codex", "kimi")

# U1/§3: the PINNED kimi cycle-quota limit signal (verified in
# ~/.kimi-code/logs/kimi-code.log on 2026-07-20 + 2026-07-23). The ONLY limit
# string U1 recognizes — the backfill matches it verbatim; U2's per-engine
# error→class map builds on top of this constant, never a re-typed copy.
KIMI_LIMIT_SIGNAL = "usage limit for this billing cycle"


def usage_engines() -> list[str]:
    """Every engine the usage layer tracks, in stable order: the three CLI
    engines, then the MERGED provider registry (seeds + config.json) in
    registry order. Includes keyless/inactive providers deliberately — the
    usage page reports on engines whether or not they are usable right now.
    db.py cannot import this module (config imports db), so callers pass the
    result to db.ensure_engine_limit_rows()."""
    out = list(USAGE_CLI_ENGINES)
    for name in fusion_config()["providers"]:
        if name not in out:
            out.append(name)
    return out


def load_config() -> dict:
    """Read ~/.orchestrator/config.json and return it as a dict. Returns {} if
    the file is absent, unreadable, malformed, or not a JSON object. NEVER
    raises — see the module contract."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        # Absent / unreadable / bad JSON — all degrade to "no config".
        return {}
    return data if isinstance(data, dict) else {}


def _normalize_profile(prof: dict) -> dict:
    """Coerce a saved Fusion PROFILE to the canonical shape
    {"claude_seats": [{model, effort, lens}], "codex_seats": [{model, effort, lens}],
    "kimi_seats": [{model, lens}], "provider_seats": [{name, lens}]}, dropping malformed
    seats and unknown keys.
    Pure / no IO — used on BOTH the read path (fusion_config, so a hand-edited
    config.json can't break the picker) and the write path (save_profile, so what
    lands on disk is always clean). A profile saved before codex seats existed simply
    has no `codex_seats` key → an empty list (backward-compatible)."""
    def s(v) -> str:
        return str(v).strip() if v is not None else ""
    prof = prof if isinstance(prof, dict) else {}
    claude = []
    for seat in prof.get("claude_seats") or []:
        if isinstance(seat, dict) and s(seat.get("model")):
            claude.append({"model": s(seat.get("model")),
                           "effort": s(seat.get("effort")) or "high",
                           "lens": s(seat.get("lens"))})
    codex = []
    for seat in prof.get("codex_seats") or []:
        if isinstance(seat, dict) and s(seat.get("model")):
            # Codex effort is OPTIONAL — "" means the model's own reasoning default
            # (NOT defaulted to "high" the way a Claude seat is). We only coerce shape
            # here; the picker/_parse_fusion_panel validate the id + effort whitelist.
            codex.append({"model": s(seat.get("model")),
                          "effort": s(seat.get("effort")),
                          "lens": s(seat.get("lens"))})
    kimi = []
    for seat in prof.get("kimi_seats") or []:
        # K4: kimi seats persist alongside codex seats. kimi-code has NO reasoning
        # effort, so a kimi seat is just {model, lens} (no effort field).
        if isinstance(seat, dict) and s(seat.get("model")):
            kimi.append({"model": s(seat.get("model")), "lens": s(seat.get("lens"))})
    providers = []
    for seat in prof.get("provider_seats") or []:
        if isinstance(seat, dict) and s(seat.get("name")):
            providers.append({"name": s(seat.get("name")), "lens": s(seat.get("lens"))})
    return {"claude_seats": claude, "codex_seats": codex, "kimi_seats": kimi,
            "provider_seats": providers}


def fusion_config() -> dict:
    """The effective Fusion config: the SEEDS above with config.json merged over
    them. Always returns {preset, timeout_s, providers, presets}.

    Merge rules:
      - providers: per-provider shallow merge, so a partial override in
        config.json (e.g. just a new `model` or an `api_key`) keeps the seed's
        `script`/`key_env`/prices. A provider present only in config.json is
        added — a user can register a brand-new lab without touching code.
      - presets: per-name override; config.json presets replace/extend the seeds.
      - lenses: per-name override; config.json lenses replace/extend the seeds.
      - preset / timeout_s: the config.json value when truthy, else the default.
    """
    fcfg = load_config().get("fusion")
    if not isinstance(fcfg, dict):
        fcfg = {}

    providers = {name: dict(entry) for name, entry in FUSION_PROVIDERS_SEED.items()}
    file_providers = fcfg.get("providers")
    if isinstance(file_providers, dict):
        for name, entry in file_providers.items():
            if isinstance(entry, dict):
                providers[name] = {**providers.get(name, {}), **entry}

    presets = {name: list(seats) for name, seats in FUSION_PRESETS_SEED.items()}
    file_presets = fcfg.get("presets")
    if isinstance(file_presets, dict):
        for name, seats in file_presets.items():
            if isinstance(seats, list):
                presets[name] = list(seats)

    lenses = dict(FUSION_LENSES_SEED)
    file_lenses = fcfg.get("lenses")
    if isinstance(file_lenses, dict):
        for name, text in file_lenses.items():
            if isinstance(text, str) and text.strip():
                lenses[name] = text

    # C4: codex ENGINE config — CODEX_ENGINE_SEED with config.json's fusion.codex
    # merged over it (per-key override, like the lens/preset merges above). The
    # mutable seed values (the probe + the seat list) are re-copied so a caller
    # mutating the returned config can't corrupt the module seed.
    codex = {**CODEX_ENGINE_SEED,
             "auth_probe": list(CODEX_ENGINE_SEED["auth_probe"]),
             "seats": [dict(s) for s in CODEX_ENGINE_SEED["seats"]]}
    file_codex = fcfg.get("codex")
    if isinstance(file_codex, dict):
        codex.update(file_codex)

    # K3: kimi ENGINE config — KIMI_ENGINE_SEED with config.json's fusion.kimi merged over it
    # (per-key override, mirror of the codex merge above). Mutable seed values are re-copied so
    # a caller mutating the returned config can't corrupt the module seed.
    kimi = {**KIMI_ENGINE_SEED,
            "models": list(KIMI_ENGINE_SEED["models"]),
            "auth_probe": list(KIMI_ENGINE_SEED["auth_probe"]),
            "seats": [dict(s) for s in KIMI_ENGINE_SEED["seats"]]}
    file_kimi = fcfg.get("kimi")
    if isinstance(file_kimi, dict):
        kimi.update(file_kimi)

    # profiles: named, full panel configs (Claude + provider seats with lenses)
    # the dispatch picker saves and re-applies. Pure user data — NO seeds, so no
    # merge; each is normalized so a hand-edited file can't break the picker.
    profiles: dict = {}
    file_profiles = fcfg.get("profiles")
    if isinstance(file_profiles, dict):
        for name, prof in file_profiles.items():
            if isinstance(name, str) and name.strip() and isinstance(prof, dict):
                profiles[name] = _normalize_profile(prof)

    return {
        "preset": fcfg.get("preset") or DEFAULT_FUSION_PRESET,
        "timeout_s": fcfg.get("timeout_s") or DEFAULT_FUSION_TIMEOUT_S,
        "verify": bool(fcfg.get("verify", False)),   # F11.c.1: opt-in verifier seat (default off)
        "providers": providers,
        "presets": presets,
        "lenses": lenses,
        "profiles": profiles,
        "codex": codex,
        "kimi": kimi,
    }


def fusion_lenses() -> dict:
    """The effective named lenses: FUSION_LENSES_SEED with config.json's
    fusion.lenses merged over it (per-name override/extend, like presets). Each
    value is a per-seat prompt prefix used for §5 decorrelation (F8.4)."""
    return fusion_config()["lenses"]


def codex_engine() -> dict:
    """The effective codex ENGINE config: CODEX_ENGINE_SEED with config.json's
    `fusion.codex` merged over it (per-key override, mirror of fusion_lenses()).
    The codex CLI's single source of truth — the `-m` model id, the exec/-s/--json
    flag set, the auth-probe command, the C6 auto-bypass flag, and a default seat
    panel. claude_runner imports the model/flags from here rather than redefining
    them; a config.json `fusion.codex.<key>` override wins (proven in tests)."""
    return fusion_config()["codex"]


def kimi_engine() -> dict:
    """The effective kimi ENGINE config: KIMI_ENGINE_SEED with config.json's `fusion.kimi`
    merged over it (per-key override, mirror of codex_engine()). The kimi-code CLI's single
    source of truth — the `-m` alias, the flag set, the auth probe, and a default seat panel.
    claude_runner/spawn import from here rather than redefining the literals."""
    return fusion_config()["kimi"]


def fusion_profiles() -> dict:
    """The saved Fusion PROFILES (name → {claude_seats, codex_seats, provider_seats})
    — named, full panel configs the dispatch picker saves and re-applies. Pure user data
    read from config.json's fusion.profiles (→ {} when absent/garbage); unlike
    presets/lenses there are no built-in defaults, so this is a plain read."""
    return fusion_config()["profiles"]


def resolve_lens(value: Optional[str], lenses: Optional[dict] = None) -> str:
    """Resolve a seat's lens spec to its prompt text. A configured lens NAME
    resolves to its text; any other non-empty string is treated as LITERAL lens
    text; empty/None → "" (no lens — the seat gets the shared prompt verbatim, so
    lenses stay opt-in). `lenses` may be passed in to avoid re-reading config.json
    when resolving many seats in one call (run_fusion_json does this)."""
    if value is None:
        return ""
    value = str(value).strip()
    if not value:
        return ""
    if lenses is None:
        lenses = fusion_lenses()
    resolved = lenses.get(value)
    return resolved if isinstance(resolved, str) and resolved.strip() else value


def _resolve_key(prov: dict) -> Optional[str]:
    """Resolve ONE merged provider entry's key: env var (`key_env`) →
    file `api_key` → None. Whitespace-only values count as unset. This mirrors
    the precedence each provider script applies on its own."""
    key_env = prov.get("key_env")
    if key_env:
        env_val = os.environ.get(key_env)
        if env_val and env_val.strip():
            return env_val.strip()
    api_key = prov.get("api_key")
    if isinstance(api_key, str) and api_key.strip():
        return api_key.strip()
    return None


def get_provider_key(name: str) -> Optional[str]:
    """Resolved key for a provider by name: env `key_env` → file `api_key` →
    None. Uses the merged registry, so config-only providers resolve too."""
    prov = fusion_config()["providers"].get(name)
    return _resolve_key(prov) if isinstance(prov, dict) else None


def active_providers() -> dict:
    """Providers usable RIGHT NOW, in registry order: key resolves AND not
    explicitly disabled (`enabled: false` — defaults to enabled). Maps
    name → its merged registry entry (carrying `model`, prices, …) but with
    `api_key` stripped, since this feeds the browser UI and keys must never
    reach it.

    Returned as a dict so both call patterns work: membership by name
    (`"deepseek" in active_providers()`, used to validate a dispatch panel —
    F3.1) and per-provider detail (`active_providers()["deepseek"]["model"]`,
    used to render the picker — F4.2)."""
    out: dict = {}
    for name, prov in fusion_config()["providers"].items():
        if prov.get("enabled") is not False and _resolve_key(prov):
            out[name] = {k: v for k, v in prov.items() if k != "api_key"}
    return out


def claude_cli_available() -> bool:
    """True if the `claude` CLI is on PATH. Claude Code panel seats (Fusion's
    effort-differentiated LOCAL seats) need no API key — only the CLI — so this
    is their availability gate, the way _resolve_key gates external providers.
    Running a seat through the CLI keeps the 'No Anthropic API calls' rule intact."""
    return shutil.which("claude") is not None


# How long the codex auth probe may run before we treat codex as unavailable.
# `codex login status` reads local auth state and returns near-instantly; this
# finite cap exists only so a wedged probe can't hang a UI render / dispatch
# (we also close its stdin — codex blocks reading stdin in a non-TTY otherwise).
_CODEX_PROBE_TIMEOUT_S = 10


def codex_cli_available() -> bool:
    """True only if the `codex` CLI is on PATH AND a current ChatGPT login is
    present — the codex twin of claude_cli_available(), but it CANNOT be a bare
    `shutil.which`. Unlike the `claude` CLI, a codex login EXPIRES: its ChatGPT
    token in ~/.codex/auth.json is not permanent, so a PATH-only check would
    mis-gate — it would report "available", then every seat/dispatch would fail at
    run time (CODEX_PLAN.md §2). So this ALSO runs a cheap, NON-BILLING auth probe
    (`codex login status`) and returns False when logged out/expired even though
    the binary is present.

    Hard guarantees (it gates the Fusion toggle and every codex seat, and runs on
    UI-render paths):
      - NEVER raises — fail-safe to False on anything unexpected (missing binary,
        non-zero/odd exit, timeout, OSError).
      - NEVER escalates to a real `codex exec`/model call — only the local status
        probe, and with OPENAI_API_KEY scrubbed from the child env, so there is no
        OpenAI API egress AND the probe reflects the $0 SUBSCRIPTION login rather
        than a billed API key (CLAUDE.md hard rule, extended to codex: a key in the
        env must not make codex look "available" — using it would be the billed
        path the rule forbids).
      - CANNOT hang — a finite timeout + closed stdin.

    Interprets the EXIT CODE (more version-robust than parsing "Logged in using
    ChatGPT"); pinned to codex-cli 0.144.4's `login status` (exit-0 probe
    re-verified 2026-07-14; originally 0.141.0), and — like the C1 parsers — a
    re-verify-on-upgrade surface."""
    if shutil.which("codex") is None:
        return False
    # Scrub OPENAI_API_KEY so the probe checks the SUBSCRIPTION login only (mirror
    # of run_codex_headless's scrub). subprocess.run(env=...) replaces the whole
    # environment, so this is the full env minus the one key (PATH etc. preserved).
    env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
    try:
        proc = subprocess.run(
            list(CODEX_ENGINE_SEED["auth_probe"]),   # C4: the seeded auth-probe command
            capture_output=True,
            text=True,
            timeout=_CODEX_PROBE_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except Exception:
        return False
    return proc.returncode == 0


_KIMI_PROBE_TIMEOUT_S = 10


def _resolve_kimi_bin() -> Optional[str]:
    """Path to the kimi-code binary: PATH (shutil.which) → the seeded ~/.kimi-code/bin
    fallback → None. which() can MISS it because the installer exports ~/.kimi-code/bin only
    in the interactive .zshrc, which a server subprocess may not source."""
    found = shutil.which("kimi")
    if found:
        return found
    fallback = os.path.expanduser(KIMI_ENGINE_SEED["bin_fallback"])
    return fallback if os.path.exists(fallback) else None


def kimi_cli_available() -> bool:
    """True only if the kimi-code CLI is resolvable AND an OAuth login is present — the codex
    twin (kimi is subscription-backed, so seats need no key, only a current login). kimi-code
    has NO `login status` subcommand, so this checks: (a) the binary resolves, (b) the OAuth
    credential file exists (absent ⇒ never logged in), and (c) a cheap NON-BILLING
    `kimi provider list` exits 0. Like codex it can't perfectly detect an EXPIRED token — that
    surfaces fail-soft at run time — but it never falsely reports available when logged out.

    Hard guarantees (it gates the Fusion toggle + every kimi seat, and runs on UI-render paths):
      - NEVER raises (fail-safe False on anything unexpected).
      - NEVER escalates to a billed model call — only the local `provider list`, with
        MOONSHOT_API_KEY/OPENAI_API_KEY scrubbed so it reflects the $0 SUBSCRIPTION login.
      - CANNOT hang — finite timeout + closed stdin.
    Pinned to kimi-code 0.27.0; re-verify on `kimi upgrade`."""
    kbin = _resolve_kimi_bin()
    if kbin is None:
        return False
    if not os.path.exists(os.path.expanduser(KIMI_ENGINE_SEED["creds_path"])):
        return False
    # Scrub billed-key envs (mirror codex) so the probe reflects the SUBSCRIPTION login only.
    env = {k: v for k, v in os.environ.items() if k not in ("MOONSHOT_API_KEY", "OPENAI_API_KEY")}
    env["PATH"] = os.path.dirname(kbin) + os.pathsep + env.get("PATH", "")
    try:
        proc = subprocess.run(
            [kbin] + list(KIMI_ENGINE_SEED["auth_probe"])[1:],   # resolved bin + `provider list`
            capture_output=True,
            text=True,
            timeout=_KIMI_PROBE_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except Exception:
        return False
    return proc.returncode == 0


def is_fusion_available() -> bool:
    """True when a >=2-seat panel is buildable: the local `claude` CLI is present
    (you can always add >=2 free Claude Code seats — no key needed), OR the `codex`
    CLI is present AND logged in (same — >=2 free codex seats), OR >=2 external
    providers are active. Below that the Fusion toggle is disabled.

    Order is by cost: the `which`-cheap claude check and the file-read
    active_providers() short-circuit BEFORE codex_cli_available(), so codex's
    auth-probe SUBPROCESS only runs when neither claude nor >=2 providers are
    present."""
    return (claude_cli_available()
            or len(active_providers()) >= 2
            or codex_cli_available()
            or kimi_cli_available())


# ── F8: registry/preset writes (the browser Settings UI) ────────────────────
# These MUTATE config.json. Two invariants the whole settings surface depends on:
#   1. api_keys are FILE-ONLY — never read from a browser request, never returned
#      to one, and ALWAYS preserved across a save (a save merges into the on-disk
#      object, which still carries the keys).
#   2. a MALFORMED config.json is never overwritten — that would silently destroy
#      the user's pasted keys. _read_config_for_write() raises on a corrupt file
#      so the save aborts and the UI shows an error instead.

class ConfigWriteError(Exception):
    """Raised when config.json can't be safely written (e.g. it exists but is
    malformed, so overwriting would clobber the user's keys)."""


def _read_config_for_write() -> dict:
    """Like load_config() but DISTINGUISHES absent (→ {}) from malformed (→
    raise). Used only by the write helpers: a write must never clobber a file it
    couldn't parse, because that file may hold api_keys."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise ConfigWriteError(f"config.json is unreadable/malformed ({e}); "
                               "refusing to overwrite (it may hold your keys)")
    if not isinstance(data, dict):
        raise ConfigWriteError("config.json is not a JSON object; refusing to overwrite")
    return data


def save_config(cfg: dict) -> None:
    """Atomically write the FULL config dict to config.json (chmod 600). The
    caller MUST have merged over the on-disk object so api_keys are preserved.
    Atomic via write-tmp-then-rename so a crash can't leave a half-written file."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, CONFIG_PATH)


def set_preset(preset: str) -> dict:
    """Set fusion.preset (merge-preserving everything else, incl. api_keys).
    Returns the new fusion_config(). Raises ConfigWriteError on a corrupt file."""
    cfg = _read_config_for_write()
    cfg.setdefault("fusion", {})["preset"] = str(preset)
    save_config(cfg)
    return fusion_config()


def set_verify(enabled: bool) -> dict:
    """Set fusion.verify — the opt-in, default-off verifier seat (FUSION_PLAN §11.c.1):
    after the fusion judge synthesizes, a $0 local-CLI critic checks it and, on a
    found defect, triggers ONE re-judge. Merge-preserving (everything else, incl.
    api_keys). Returns the new fusion_config(). Raises ConfigWriteError on a corrupt
    file."""
    cfg = _read_config_for_write()
    cfg.setdefault("fusion", {})["verify"] = bool(enabled)
    save_config(cfg)
    return fusion_config()


def upsert_provider(name: str, *, script: str, key_env: str, model: str,
                    price_in: float, price_out: float, enabled: bool = True) -> dict:
    """Add or edit one registry provider in config.json. The api_key is NEVER
    set from here — an existing key is preserved, a new provider gets an empty
    one (the user pastes keys into the file directly). Raises ConfigWriteError on
    a corrupt file or a blank name."""
    name = (name or "").strip()
    if not name:
        raise ConfigWriteError("provider name is required")
    cfg = _read_config_for_write()
    provs = cfg.setdefault("fusion", {}).setdefault("providers", {})
    entry = dict(provs.get(name) or {})
    existing_key = entry.get("api_key", "")          # file-only — preserved verbatim
    entry.update({"script": str(script), "key_env": str(key_env), "model": str(model),
                  "price_in": float(price_in), "price_out": float(price_out),
                  "enabled": bool(enabled), "api_key": existing_key})
    provs[name] = entry
    save_config(cfg)
    return fusion_config()


def set_provider_enabled(name: str, enabled: bool) -> dict:
    """Flip one provider's `enabled` flag without touching anything else.
    A provider present only as a SEED (not yet in config.json) is materialized
    from its merged entry first (sans api_key), so toggling it persists."""
    cfg = _read_config_for_write()
    provs = cfg.setdefault("fusion", {}).setdefault("providers", {})
    if name not in provs:
        merged = fusion_config()["providers"].get(name)
        if not isinstance(merged, dict):
            raise ConfigWriteError(f"unknown provider: {name}")
        provs[name] = {k: v for k, v in merged.items() if k != "api_key"}
    provs[name]["enabled"] = bool(enabled)
    save_config(cfg)
    return fusion_config()


def remove_provider(name: str) -> dict:
    """Remove a provider's config.json override. (A canonical SEED name still
    reappears from the seeds, but keyless → inactive; a custom name disappears
    entirely.) Raises ConfigWriteError on a corrupt file."""
    cfg = _read_config_for_write()
    provs = cfg.setdefault("fusion", {}).setdefault("providers", {})
    provs.pop(name, None)
    save_config(cfg)
    return fusion_config()


def set_lens(name: str, text: str) -> dict:
    """Add or edit one named lens (fusion.lenses) — F8.4. Merge-preserving like
    the other write helpers (everything else, incl. api_keys, is kept). A blank
    name or blank text raises ConfigWriteError. Returns the new fusion_config()."""
    name = (name or "").strip()
    if not name:
        raise ConfigWriteError("lens name is required")
    text = (text or "").strip()
    if not text:
        raise ConfigWriteError("lens text is required")
    cfg = _read_config_for_write()
    cfg.setdefault("fusion", {}).setdefault("lenses", {})[name] = text
    save_config(cfg)
    return fusion_config()


def remove_lens(name: str) -> dict:
    """Remove a lens's config.json override. (A canonical SEED lens reappears from
    the seeds; a custom lens disappears entirely.) Raises ConfigWriteError on a
    corrupt file."""
    cfg = _read_config_for_write()
    cfg.setdefault("fusion", {}).setdefault("lenses", {}).pop(name, None)
    save_config(cfg)
    return fusion_config()


# ── Fusion PROFILES: named, saveable panel configs (the dispatch quick-switch) ──
# A profile bundles the EXACT panel a user wants for a kind of task — Claude seats
# (model+effort+lens), codex seats (model+effort+lens), and provider seats (name+lens)
# — under a chosen name, so the picker can re-populate itself in one click. The codex
# seats persist alongside the others so a saved profile never silently drops them.
# Merge-preserving and corruption-
# guarded like the lens/preset helpers (api_keys, presets, lenses all survive).

def save_profile(name: str, profile: dict) -> dict:
    """Add or edit one saved Fusion profile (fusion.profiles). The profile is
    normalized to {claude_seats:[{model,effort,lens}], codex_seats:[{model,effort,lens}],
    provider_seats:[{name,lens}]} before storing, so junk can't land on disk. A blank
    name, a non-dict profile, or a profile with NO valid seats (of any kind) raises
    ConfigWriteError. Returns the new fusion_config(). Raises ConfigWriteError on a
    corrupt file (never clobbers it)."""
    name = (name or "").strip()
    if not name:
        raise ConfigWriteError("profile name is required")
    if not isinstance(profile, dict):
        raise ConfigWriteError("profile must be an object")
    clean = _normalize_profile(profile)
    if not (clean["claude_seats"] or clean["codex_seats"] or clean["kimi_seats"]
            or clean["provider_seats"]):
        raise ConfigWriteError("profile has no seats")
    cfg = _read_config_for_write()
    cfg.setdefault("fusion", {}).setdefault("profiles", {})[name] = clean
    save_config(cfg)
    return fusion_config()


def remove_profile(name: str) -> dict:
    """Remove a saved profile from config.json (idempotent — unknown name is a
    no-op). Raises ConfigWriteError on a corrupt file."""
    cfg = _read_config_for_write()
    cfg.setdefault("fusion", {}).setdefault("profiles", {}).pop((name or "").strip(), None)
    save_config(cfg)
    return fusion_config()
