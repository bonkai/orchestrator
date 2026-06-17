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

    return {
        "preset": fcfg.get("preset") or DEFAULT_FUSION_PRESET,
        "timeout_s": fcfg.get("timeout_s") or DEFAULT_FUSION_TIMEOUT_S,
        "providers": providers,
        "presets": presets,
    }


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


def is_fusion_available() -> bool:
    """True once >= 2 providers are active — the minimum for a real panel.
    Below that the Fusion toggle is disabled and brain calls stay local."""
    return len(active_providers()) >= 2
