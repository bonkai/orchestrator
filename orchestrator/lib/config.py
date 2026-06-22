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

    return {
        "preset": fcfg.get("preset") or DEFAULT_FUSION_PRESET,
        "timeout_s": fcfg.get("timeout_s") or DEFAULT_FUSION_TIMEOUT_S,
        "verify": bool(fcfg.get("verify", False)),   # F11.c.1: opt-in verifier seat (default off)
        "providers": providers,
        "presets": presets,
        "lenses": lenses,
    }


def fusion_lenses() -> dict:
    """The effective named lenses: FUSION_LENSES_SEED with config.json's
    fusion.lenses merged over it (per-name override/extend, like presets). Each
    value is a per-seat prompt prefix used for §5 decorrelation (F8.4)."""
    return fusion_config()["lenses"]


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


def is_fusion_available() -> bool:
    """True when a >=2-seat panel is buildable: either the local `claude` CLI is
    present (you can always add >=2 free Claude Code seats — no key needed), OR
    >=2 external providers are active. Below that the Fusion toggle is disabled."""
    return claude_cli_available() or len(active_providers()) >= 2


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
