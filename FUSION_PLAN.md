# Orchestrator — Multi-Model Fusion Plan (direct providers)

Adding **Fusion** as an *optional, opt-in* multi-model "brain" layer that calls
model providers **directly** — no OpenRouter, no aggregator, no router margin. This
is a phased implementation plan, not a built feature. When the Fusion toggle is
**off**, behavior is byte-for-byte identical to today.

> 🔁 **This supersedes the original OpenRouter design.** Earlier drafts routed the
> panel through OpenRouter's hosted "Fusion" endpoint (one API call, OpenRouter
> orchestrates the panel + judge). We dropped that: the only cost should be
> **per-token API charges paid directly to each model provider**, with nothing on
> top to a middleman. OpenRouter adds no per-token markup but does take a ~5% fee on
> credit top-ups — so we orchestrate the panel ourselves and pay each lab directly.
> A side benefit: owning the orchestration lets us tune the judge prompt and give
> each panel seat a different lens (things OpenRouter's black-box Fusion can't do).

> ⚠️ **Hard-rule deviation, stated up front.** CLAUDE.md says *"Local only. No
> remote workers, no hosted services."* Fusion makes outbound HTTPS calls to
> **multiple model-provider APIs**, and the prompt (which includes the project
> bundle) leaves the laptop. This plan **intentionally relaxes that rule**, but only
> behind a default-off toggle that falls back to the local path automatically.
>
> **What is NOT broken anymore:** the *"No Anthropic API calls"* rule. The **judge
> runs on the local `claude` CLI** (subscription, in a visible tab), and no panelist
> is Anthropic-via-API — so unlike the OpenRouter design, Fusion never calls the
> Anthropic API. The remaining deviation is purely *panel egress to non-Anthropic
> providers*. See §9 before implementing F1.
>
> **On "headless":** brain calls run in **visible iTerm2 tabs**, and Fusion does
> too — the panel fan-out runs in a watchable `fusion` tab and the judge runs in the
> normal watchable `brain` tab. No hidden in-process HTTP.

---

## 1. What Fusion is

Fusion is a **one-shot, multi-model ensemble** we orchestrate locally:

1. **Panel** — the same prompt fans out to N "analysis" models at **different labs**,
   in **parallel**, each called **directly** at that lab's own API.
2. **Judge** — our local **`claude` CLI** (Opus) reads all N answers and synthesizes
   one. The judge is free on the existing subscription and runs in a visible tab.
3. We get back **one synthesized completion**, returned as the same `ClaudeRun`
   dataclass every existing brain caller already expects.

Key properties:

- **NOT iterative.** No agent loop, no tool-use turns. Fan out once, judge once,
  done. (Claude Code's iTerm2 executor sessions remain the only agentic part.)
- **We orchestrate, not a gateway.** Our side makes **N panel calls + 1 judge call**.
  The panel calls are parallel direct HTTPS POSTs; the judge is `run_claude_json`.
- **Only cost = per-token charges to the panel providers.** The judge adds no
  out-of-pocket cost (subscription). No router/aggregator fee of any kind.

**Good for:** architecture decisions, research questions, complex/ambiguous
prompt-rewriting, "what's the best way to approach this" — anywhere multiple
perspectives beat one.

**Overkill for:** routine coding, short rewrites, classification/tagging — anything
a single Sonnet/Opus call already nails (see §7).

---

## 2. Cost model & the one rule that shapes everything

**The rule:** the only money spent is **per-token API cost paid directly to each
panel provider.** No OpenRouter, no Groq/Together-style host, no markup. The judge is
the local `claude` CLI — flat subscription, **$0 marginal per call**.

Consequences for the design:

- **Cost = Σ(panel provider token costs).** We compute it ourselves from each
  provider's returned `usage` (`prompt_tokens`/`completion_tokens`) × the per-provider
  `$/M` price stored in the registry (§4). There is no unified `usage.cost` like
  OpenRouter returned — we sum it.
- **Register many, fire a subset.** Signing up for 6+ labs only fills the *registry*.
  A **preset** selects which ~3 actually run per call, so you never pay 6× or wait on
  the slowest of six unless you explicitly pick the `max` preset.
- **The judge being free makes this cheaper than the OpenRouter design**, which paid
  for an Opus judge on every call.

| Preset | Panel that fires (direct) | Judge | Rough out-of-pocket |
|--------|---------------------------|-------|---------------------|
| **budget** ✅ default | DeepSeek + MiniMax + Gemini Flash | `claude` CLI (free) | pennies — 3 cheap cross-vendor seats |
| **balanced** | DeepSeek + Grok + Qwen | `claude` CLI (free) | low — 3 strong diverse seats |
| **max** | all 6 (DeepSeek+Grok+Gemini+MiniMax+GLM+Qwen) | `claude` CLI (free) | 6 seats — high-stakes only, slowest-bound |

Surface `run.cost_usd` (the summed panel cost) on the `rewrite_ok` stage event so the
premium is visible. Reserve `max` for high-stakes rewrites (architecture, irreversible
migrations).

---

## 3. Architecture decision

**Claude Code stays the EXECUTOR. Fusion only touches the BRAIN CALLS.** The
dispatched iTerm2 `claude` session — the thing that edits files and runs commands — is
**completely untouched**. `spawn.spawn_iterm2(...)` does not change.

There are **two distinct ways** a panel can help; the plan supports both:

### 3a. Primary mode — drop-in brain-call replacement *(Phases F1–F6)*

When the toggle is on, the **internal LLM call** inside the rewriter (and optionally
the summarizer / onboarding analyzer) routes through Fusion instead of the single
visible-tab `claude` call. It produces the *same kind of artifact* (a rewritten
prompt, a summary) — just authored by a panel + judge.

`claude_runner.py` stays *"the single entry point for all internal LLM calls."* We add
a sibling `run_fusion_json()` returning the **same `ClaudeRun` dataclass**, and route
through one `run_brain_json()` dispatcher so the single-entry invariant holds.

```
                  ┌────── BRAIN CALL (swappable — ALWAYS in watchable tabs) ─────────┐
 task ─▶ bundle ─▶│ run_brain_json(fusion?)                                          │─▶ rewritten ─▶ spawn iTerm2 ─▶ claude
                  │   fusion OFF → run_claude_json → 1 visible brain tab             │                 (EXECUTOR — unchanged)
                  │   fusion ON  → run_fusion_json →                                 │
                  │        panel fan-out (visible fusion tab, N direct providers)    │
                  │        + judge (visible brain tab, local claude CLI)             │
                  └──────────────────────────────────────────────────────────────────┘
  Panel, judge, and executor each run in iTerm2 tabs you can watch live.
```

Bonus: fusion cost rides through the **existing** `rewrite_ok` cost plumbing for free,
because `run_fusion_json` populates `ClaudeRun.cost_usd` with the summed panel cost.

### 3b. Optional mode — multi-model enrichment block *(Phase F7)*

Instead of *replacing* the rewrite, run a panel purely to **reason about the task** and
append its synthesis to the prompt the executor sees, as a fenced "Multi-model
analysis" block (`{consensus, contradictions, partial_coverage, unique_insights,
blind_spots}`). The executor weighs it as context, not gospel. With a panel of strong
but non-frontier models, this "inject disagreement as context" mode is often *safer*
than the drop-in (you don't trust the panel to author the final artifact).

---

## 4. Provider registry & secrets

There is **one secret per provider** (not one unified key). Each lives in `config.json`
or an env var. Resolution precedence, **per provider** (in `config.py`):
**`<PROVIDER>_API_KEY` env var → `~/.orchestrator/config.json["providers"][name]["api_key"]` → `None`.**

The **registry** lives in `~/.orchestrator/config.json` — matching *"Data lives in
`~/.orchestrator/`, not in the repo."* Each provider entry:

```jsonc
// ~/.orchestrator/config.json  (chmod 600 — holds secrets; NEVER in the repo)
{
  "fusion": {
    "preset": "budget",
    "timeout_s": 300,
    "providers": {
      "deepseek": { "base_url": "https://api.deepseek.com",
                    "key_env": "DEEPSEEK_API_KEY", "api_key": "",
                    "model": "deepseek-chat",        "price_in": 0.44, "price_out": 0.87 },
      "xai":      { "base_url": "https://api.x.ai/v1",
                    "key_env": "XAI_API_KEY",      "api_key": "",
                    "model": "grok-4",               "price_in": 1.25, "price_out": 2.50 },
      "gemini":   { "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                    "key_env": "GEMINI_API_KEY",   "api_key": "",
                    "model": "gemini-2.5-flash",     "price_in": 0.30, "price_out": 1.50 },
      "minimax":  { "base_url": "https://api.minimax.io/v1",
                    "key_env": "MINIMAX_API_KEY",  "api_key": "",
                    "model": "MiniMax-Text-01",      "price_in": 0.30, "price_out": 1.20,
                    "adapter": "minimax" },          // ⚠ may not be drop-in OpenAI-shaped
      "glm":      { "base_url": "https://api.z.ai/api/paas/v4",
                    "key_env": "ZAI_API_KEY",      "api_key": "",
                    "model": "glm-4.6",              "price_in": 1.40, "price_out": 4.40 },
      "qwen":     { "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                    "key_env": "DASHSCOPE_API_KEY","api_key": "",
                    "model": "qwen-max",             "price_in": 1.25, "price_out": 3.75 }
    },
    "presets": {
      "budget":   ["deepseek", "minimax", "gemini"],
      "balanced": ["deepseek", "xai", "qwen"],
      "max":      ["deepseek", "xai", "gemini", "minimax", "glm", "qwen"]
    }
  }
}
```

- **Env-var-first** lets you keep secrets out of any file; the `api_key` field is the
  fallback so `install.sh` can scaffold it. Either way the key is read **inside the
  fusion tab** (env → config.json) — never passed through AppleScript.
- **Never committed, never browser-editable** (edit the file directly).
- **Startup probe** (mirror the embeddings probe in `app.py` lifespan): log one WARNING
  if fewer than 2 panel providers have a resolvable key, so the user knows the toggle
  will fall back.
- The judge needs **no key** — it's the local `claude` CLI.

---

## 5. Panel composition & presets

**An ensemble only beats a solo call when panelists make *uncorrelated* errors.** Build
the panel from **different labs**, and let the judge (a different family again — Claude)
synthesize. The six registered labs span DeepSeek (CN), xAI (US), Google (US), MiniMax
(CN), Z.ai/GLM (CN), Qwen/Alibaba (CN) — maximally diverse.

- **`budget`** (default): 3 cheap, cross-vendor seats. Pennies per call.
- **`balanced`**: 3 strong, diverse seats.
- **`max`**: all 6 — only for high-stakes rewrites; cost and latency scale with seat
  count (latency is slowest-seat-bound, so the fan-out **must** run in parallel).

Presets are config — edit freely, add your own (e.g. a `coding` preset). Adding a lab
is a **config edit, not a code change**; nothing validates a slug, so a brand-new model
works the moment you point a registry entry at it.

**Optional refinement (owning the orchestration buys this):** give each seat a different
*lens* instead of the identical prompt — e.g. "find the risks," "find the simplest
path," "find what's ambiguous." Store per-seat prompt prefixes in the registry. Defer to
after F5.

---

## 6. Provider catalog (direct APIs)

> **Verify before wiring.** Each lab's **base URL**, **native model id**, and
> **OpenAI-compatibility** drift and must be confirmed against the provider's own docs
> before F1 ships. ⚠ Native model ids are **NOT** OpenRouter's `vendor/model` slugs —
> each lab uses its own (e.g. DeepSeek's API wants `deepseek-chat`, not
> `deepseek/deepseek-v4-pro`). Prices are list rates ($/M tokens, input→output),
> snapshot **2026-06-17** — re-verify; keep them in the config registry, not in code.

| Lab | Get the key | Base URL (OpenAI-compatible) | Native model id (verify) | Key env | $/M (in→out) |
|-----|-------------|------------------------------|--------------------------|---------|--------------|
| **DeepSeek** | https://platform.deepseek.com/api_keys | `https://api.deepseek.com` | `deepseek-chat` / `deepseek-reasoner` | `DEEPSEEK_API_KEY` | $0.44 → $0.87 |
| **xAI (Grok)** | https://console.x.ai | `https://api.x.ai/v1` | `grok-4` / `grok-4-fast` | `XAI_API_KEY` | $1.25 → $2.50 |
| **Google (Gemini)** | https://aistudio.google.com/apikey | `https://generativelanguage.googleapis.com/v1beta/openai` | `gemini-2.5-flash` / `gemini-2.5-pro` | `GEMINI_API_KEY` | ~$0.30 → $1.50 (free tier exists) |
| **MiniMax** ⚠ | https://www.minimax.io/platform | `https://api.minimax.io/v1` | `MiniMax-Text-01` (current M-series) | `MINIMAX_API_KEY` | $0.30 → $1.20 |
| **Z.ai (GLM)** | https://z.ai → API keys | `https://api.z.ai/api/paas/v4` | `glm-4.6` / `glm-4.5` | `ZAI_API_KEY` | $1.40 → $4.40 |
| **Qwen (Alibaba)** | https://modelstudio.console.alibabacloud.com | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | `qwen-max` / `qwen-plus` / `qwen3-max` | `DASHSCOPE_API_KEY` | $1.25 → $3.75 |
| *(optional)* **OpenAI** | https://platform.openai.com/api-keys | `https://api.openai.com/v1` | `gpt-5...` | `OPENAI_API_KEY` | varies |
| *(optional)* **Moonshot (Kimi)** | https://platform.moonshot.ai | `https://api.moonshot.ai/v1` | `kimi-k2-...` | `MOONSHOT_API_KEY` | $0.60 → $2.50 |
| **Judge → Anthropic (Opus)** | — uses your `claude` CLI | — (local subprocess, visible tab) | (executor model) | — none | **$0 marginal** |

### Per-provider caveats the implementer must know
- **MiniMax ⚠** — its chat endpoint may be `/v1/text/chatcompletion_v2` with a slightly
  non-OpenAI body. Use the `adapter: "minimax"` hook to translate request/response if
  the drop-in OpenAI client 4xxs. Verify the international host (`api.minimax.io`), not
  the China host.
- **Z.ai / GLM** — international platform is **`z.ai`** (`api.z.ai/api/paas/v4`,
  OpenAI-compatible `/chat/completions`); the China platform is Zhipu
  (`open.bigmodel.cn`). Prefix gotcha carried over from the OpenRouter days: the lab is
  **Z.ai**, model ids are `glm-*`.
- **Qwen / Alibaba** — use the **`-intl`** DashScope host + `compatible-mode/v1` for the
  OpenAI-compatible interface; the bare `dashscope.aliyuncs.com` is the China region.
- **Gemini** — the OpenAI-compat path is `.../v1beta/openai`; it has a **free tier**
  (rate-limited), then paid via a linked Cloud billing project.
- **DeepSeek / xAI** — straightforward OpenAI-compatible; cheapest to start with for
  wiring the plumbing (see F1 — start with one seat).

---

## 7. What NOT to run through Fusion

Keep these on the existing single-`claude` path (or no LLM at all):

- **Verbatim dispatch** — "skip rewrite & send" makes **no brain call**; nothing to route.
- **Prompt/bundle construction** — pure string work, no model.
- **Short-session transcript distillation** — one Sonnet call is plenty.
- **Onboarding scans of small projects** — a handful of files doesn't warrant a panel.
- **Classification / tagging** — single-label outputs; a panel adds cost, not accuracy.
- **The rewriter's auto-retry** — retry on a *single* model (the `claude` judge);
  never re-fan-out the panel.
- **Latency-sensitive paths** — the interactive "preview rewrite" where the user watches
  a spinner. A panel is slower (slowest-seat-bound); prefer it for fire-and-forget
  `/send`, not live preview.

Rule of thumb: **Fusion is for hard, ambiguous, one-shot reasoning that benefits from
disagreement among models.** Everything routine stays solo.

---

## 8. Phased rollout

| Phase | Scope | Deliverable | Status |
|-------|-------|-------------|--------|
| **F0** | Config & key mgmt | `config.py` (registry + per-provider keys + presets) + idempotent `install.sh` template | ☐ |
| **F1** | `claude_runner` extension | `run_fusion_json()` (parallel panel + `claude` judge) + `run_brain_json()` dispatcher | ☐ |
| **F2** | Rewriter integration | rewriter routes through fusion when toggled | ☐ |
| **F3** | Pipeline wiring | thread `fusion` flag `/send` → `_send_in_background` | ☐ |
| **F4** | Dispatch-form toggle | checkbox, localStorage, disabled-when-<2-providers, cost hint | ☐ |
| **F5** | Surface + cost | show panel breakdown + summed cost; cost in outcomes | ☐ |
| **F6** (opt) | Summarizer + onboarding | same drop-in for the other two brain calls | ☐ |
| **F7** (opt) | Enrichment-block mode | panel → analysis block appended to executor prompt | ☐ |
| **F8** (opt) | Model-selection UI | edit the registry + pick each seat's model/version from the browser | ☐ |

**F0–F5 deliver a working, shippable on/off Fusion toggle.** Build strictly in order;
don't start a task until the previous one's verify passes. **⟂** marks order-independent
tasks.

### Phase F0 — Config & key management
*Goal: a `config.py` that resolves per-provider keys + the registry/presets, and an installer that scaffolds the config file.*
- [ ] **F0.1** `config.py`: `load_config()` (reads `~/.orchestrator/config.json`; `{}` if absent/malformed; never raises) + `get_provider_key(name)` (env `key_env` → file `api_key` → None) + `is_fusion_available()` (≥2 panel providers resolve a key). · *verify:* `is_fusion_available()` → `False` clean, `True` once two providers' keys are set via env **or** file.
- [ ] **F0.2** `config.py`: `fusion_config()` → `{preset, timeout_s, providers, presets}` merged over the §4/§6 seed defaults. · *verify:* returns seeds with no file; returns your values when `config.json` sets them.
- [ ] **F0.3** `install.sh`: write the `config.json` template **only if absent** (idempotent) — full registry with empty `api_key`s + presets — and print where to paste each key. · *verify:* run twice; the 2nd run is a no-op and never clobbers existing keys.

### Phase F1 — `claude_runner.py` extension *(the core)*
Add `run_fusion_json()`: same `ClaudeRun` return, same never-raises contract, same
**visible-tab** behavior. The panel fan-out runs in a watchable `fusion` tab
(`spawn_fusion_tab` + `fusion_run.sh`); the judge reuses `run_claude_json` (already a
watchable `brain` tab). An in-process `urllib` fan-out is the fallback when iTerm2 is
absent. Reuse `_strip_fences` for JSON extraction. **No new Python deps** (stdlib
`urllib` + `concurrent.futures`).

- [ ] **F1.1** `_build_body(prompt, model)` + `_post_openai_compat(base_url, key, body, timeout) -> envelope` (stdlib `urllib` POST to `<base_url>/chat/completions`). · *verify:* against DeepSeek, returns an OpenAI-shaped envelope with `choices` + `usage`.
- [ ] **F1.2** `_panel_answer(name, prov, prompt, timeout) -> {name, model, text, cost, ok, error}` (resolves the provider key, POSTs, computes cost from `usage`×price; never raises) + the `minimax` adapter shim. · *verify:* one provider → `ok=True` with `cost>0`; missing key → `ok=False`, no raise.
- [ ] **F1.3** `_run_panel(prompt, panel, providers, timeout)` — fan out **in parallel** (`ThreadPoolExecutor`) over the preset's subset; return all answers. · *verify:* a 3-seat preset returns 3 answers; wall-clock ≈ slowest seat, not the sum.
- [ ] **F1.4** `_judge_prompt(orig, answers)` (embeds the N panel answers + asks `claude` to synthesize the required artifact) **+** `run_fusion_json(...)` that resolves preset/panel/timeout (arg → `config.fusion_config()` → seed), runs the panel, then calls `run_claude_json(synthesis, cwd)` as the judge, sets `cost_usd = Σ panel cost`. · *verify:* `run_fusion_json("Should a single-writer app use SQLite WAL?")` → `ok=True`, `cost>0`; editing a registry `model` changes which models are billed. *(In-process panel for now; F1.6 puts the visible tab in front.)*
- [ ] **F1.5** ⟂ `fusion_call.py` (stdlib `urllib` + `concurrent.futures`, **standalone** — must NOT import the orchestrator package): read the request sidecar (`{prompt, panel, providers, timeout}`), resolve each key (env → config.json), POST to each provider **in parallel**, echo each answer to **stderr** (watchable), print the collected answers JSON to **stdout** (captured). · *verify:* run by hand with a request file → you SEE each provider answer; `<id>.json` holds the collected answers.
- [ ] **F1.6** `spawn.spawn_fusion_tab(fusion_id, body, cwd)` (mirror `spawn_brain_tab`) + `ensure_fusion_runner()` (writes `fusion_run.sh` + `fusion_call.py`) **+** rewire `run_fusion_json` to run the panel in the tab (`<id>.done`/`.pid` poll, copied from `run_claude_json`), falling back to the in-process fan-out only when `spawn.iterm2_installed()` is false. The tab sets `ORCHESTRATOR_FUSION_ID` (never `ORCHESTRATOR_RUN_ID`), so the Stop hook stays a no-op. · *verify:* `run_fusion_json(...)` opens a visible fusion tab for the panel + a brain tab for the judge; faked-no-iTerm2 still returns a valid `ClaudeRun`.
- [ ] **F1.7** `run_brain_json(prompt, cwd, fusion=False, **kw)` dispatcher (fusion→`run_fusion_json`; else, or on failure→`run_claude_json`). · *verify:* `fusion=True` with <2 keys falls back to the normal visible-tab claude call, no hard error.
- [ ] **F1.8** ⟂ `list_provider_models(name)` (GET the provider's own model-list endpoint where one exists, e.g. `/models`; else return the registry entry), used later by F8. · *verify:* returns a non-empty list for a provider that exposes `/models`; `[]` otherwise, never raises.

*Code reference for F1 — target shapes (not extra work):*

```python
# ── additions to orchestrator/lib/claude_runner.py ───────────────────────────
import urllib.request, urllib.error, concurrent.futures   # (json, os already imported)

# Registry SEED — fallback only, NOT an allowlist. Real config:
#   ~/.orchestrator/config.json["fusion"]["providers"]  (§4)
# Each provider: base_url (OpenAI-compatible), key_env, native model slug (§6 —
# NOT an OpenRouter vendor/model slug), price_in/out ($/M), optional adapter.
FUSION_PROVIDERS_SEED = {
  "deepseek": {"base_url": "https://api.deepseek.com",     "key_env": "DEEPSEEK_API_KEY",
               "model": "deepseek-chat",      "price_in": 0.44, "price_out": 0.87},
  "xai":      {"base_url": "https://api.x.ai/v1",          "key_env": "XAI_API_KEY",
               "model": "grok-4",             "price_in": 1.25, "price_out": 2.50},
  "gemini":   {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "key_env": "GEMINI_API_KEY",   "model": "gemini-2.5-flash",
               "price_in": 0.30, "price_out": 1.50},
  "minimax":  {"base_url": "https://api.minimax.io/v1",    "key_env": "MINIMAX_API_KEY",
               "model": "MiniMax-Text-01",    "price_in": 0.30, "price_out": 1.20,
               "adapter": "minimax"},          # ⚠ verify OpenAI-compat; shim if not
  "glm":      {"base_url": "https://api.z.ai/api/paas/v4", "key_env": "ZAI_API_KEY",
               "model": "glm-4.6",            "price_in": 1.40, "price_out": 4.40},
  "qwen":     {"base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
               "key_env": "DASHSCOPE_API_KEY","model": "qwen-max",
               "price_in": 1.25, "price_out": 3.75},
}
FUSION_PRESETS_SEED = {
  "budget":   ["deepseek", "minimax", "gemini"],
  "balanced": ["deepseek", "xai", "qwen"],
  "max":      ["deepseek", "xai", "gemini", "minimax", "glm", "qwen"],  # high-stakes only
}
DEFAULT_FUSION_PRESET    = "budget"
DEFAULT_FUSION_TIMEOUT_S = 300
# JUDGE is ALWAYS the local claude CLI (run_claude_json): free on the subscription,
# keeps "No Anthropic API calls" intact, runs in a visible brain tab.


def _build_body(prompt: str, model: str) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False}


def _post_openai_compat(base_url: str, key: str, body: dict, timeout_s: int) -> dict:
    """POST to an OpenAI-compatible /chat/completions; return the parsed envelope.
    Raises on HTTP/URL/parse error (caller converts). fusion_call.py duplicates this —
    it runs standalone in the tab and can't import this module."""
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        return json.loads(r.read().decode())


def _panel_answer(name: str, prov: dict, prompt: str, timeout_s: int) -> dict:
    """One panelist → {name, model, text, cost, ok, error}. Never raises."""
    key = os.environ.get(prov["key_env"]) or config.get_provider_key(name)
    if not key:
        return {"name": name, "ok": False, "error": f"{prov['key_env']} not set"}
    body = _build_body(prompt, prov["model"])
    if prov.get("adapter") == "minimax":
        body = _minimax_adapt(body)                      # ⚠ shim if not OpenAI-shaped
    try:
        env = _post_openai_compat(prov["base_url"], key, body, timeout_s)
    except Exception as e:                               # HTTPError/URLError/Timeout/parse
        return {"name": name, "ok": False, "error": str(e)}
    msg  = (env.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    u    = env.get("usage") or {}
    cost = (u.get("prompt_tokens", 0) * prov["price_in"]
            + u.get("completion_tokens", 0) * prov["price_out"]) / 1e6
    return {"name": name, "model": prov["model"], "text": msg, "cost": cost, "ok": True}


def _run_panel(prompt: str, panel: list, providers: dict, timeout_s: int) -> list:
    """Fan out to the preset's subset IN PARALLEL (slowest-seat-bound, not the sum)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(panel))) as ex:
        return list(ex.map(lambda n: _panel_answer(n, providers[n], prompt, timeout_s), panel))


def run_fusion_json(prompt: str, cwd: str = "", preset: Optional[str] = None,
                    panel: Optional[list] = None, timeout_s: Optional[int] = None) -> ClaudeRun:
    """Fusion sibling of run_claude_json. Fans out to a PANEL of direct providers
    (parallel, visible fusion tab), then synthesizes via the local claude CLI judge
    (run_claude_json, visible brain tab — free on the subscription). cost_usd = Σ panel
    provider costs; the judge adds no out-of-pocket cost. Any slug works (forwarded
    verbatim, no allowlist). Never raises. See §9."""
    cfg       = config.fusion_config()                  # {} → pure seed fallback
    providers = {**FUSION_PROVIDERS_SEED, **cfg.get("providers", {})}
    presets   = {**FUSION_PRESETS_SEED,   **cfg.get("presets", {})}
    preset    = preset or cfg.get("preset") or DEFAULT_FUSION_PRESET
    panel     = panel  or presets.get(preset) or FUSION_PRESETS_SEED["budget"]
    timeout_s = timeout_s or cfg.get("timeout_s") or DEFAULT_FUSION_TIMEOUT_S

    answers = _run_panel(prompt, panel, providers, timeout_s)   # F1.6: in the visible tab
    ok = [a for a in answers if a.get("ok")]
    if len(ok) < 2:                                     # not enough perspectives to fuse
        errs = "; ".join(f"{a['name']}: {a.get('error')}" for a in answers if not a.get("ok"))
        return ClaudeRun(ok=False, error=f"fusion panel: only {len(ok)} provider(s) answered ({errs})")

    panel_cost = sum(a["cost"] for a in ok)
    judge = run_claude_json(prompt=_judge_prompt(prompt, ok), cwd=cwd)   # local claude judge
    judge.cost_usd = panel_cost                         # real out-of-pocket = panel only
    judge.raw = {"panel": answers, "preset": preset}
    return judge


def run_brain_json(prompt: str, cwd: str, fusion: bool = False, **kw) -> ClaudeRun:
    """Single entry point for brain calls. Routes to Fusion when requested AND
    available; falls back to the standard visible-tab claude call when fusion is on but
    unavailable — a flaky panel never hard-fails a run."""
    if fusion:
        run = run_fusion_json(prompt=prompt, cwd=cwd, **kw)
        if run.ok:
            return run
        print(f"[claude_runner] fusion unavailable ({run.error}); falling back to claude")
    return run_claude_json(prompt=prompt, cwd=cwd)
```

**Visible-tab plumbing (mirror the brain-tab path).** `_run_fusion_in_tab` generates a
`fusion_id`, calls `spawn.spawn_fusion_tab(fusion_id, body, cwd)` (writes
`~/.orchestrator/fusion/<id>.request.json` with `{prompt, panel, providers, timeout}`,
opens the tab via `fusion_run.sh`), then polls `<id>.done`/`.pid` like the brain loop.
Keys are read **inside the tab** (env → `config.json`) — never via AppleScript.

```bash
#!/bin/bash
# ~/.orchestrator/bin/fusion_run.sh — execed in an iTerm2 tab so the panel fan-out is
# WATCHABLE (same principle as brain_run.sh). A stdlib-python runner POSTs to each
# provider in parallel, echoes each lab's answer to the SCREEN (stderr), and prints
# ONLY the collected-answers JSON to stdout — which `tee` captures to <id>.json.
ID="$ORCHESTRATOR_FUSION_ID"; DIR="$HOME/.orchestrator/fusion"
echo $$ > "$DIR/$ID.pid"
echo "---- orchestrator fusion panel: $ID (watching live) ----"
python3 "$HOME/.orchestrator/bin/fusion_call.py" "$DIR/$ID.request.json" | tee "$DIR/$ID.json"
echo "${PIPESTATUS[0]}" > "$DIR/$ID.done"
echo "---- fusion panel finished (judge runs next in a brain tab) ----"
```

`fusion_call.py` is standalone stdlib `urllib` + `concurrent.futures` — it runs in the
tab's own process, so it must NOT import the `orchestrator` package; it duplicates the
small per-provider POST. It reads `<id>.request.json`, resolves each key itself
(env → `config.json`), fans out in parallel, echoes each answer to stderr for watching,
and prints the collected answers to stdout for capture. The **judge** then runs as a
normal `run_claude_json` brain tab — so both halves stay visible.

*Acceptance:* with ≥2 keys set, `run_fusion_json("Should a single-writer local app use
SQLite WAL mode?")` opens a visible fusion tab (panel) + a brain tab (judge) and returns
`ok=True` with `cost>0` (panel sum); with <2 keys, `ok=False`; with iTerm2 uninstalled,
it still returns a valid `ClaudeRun` via the in-process fan-out.

### Phase F2 — Rewriter integration
*Goal: the rewriter can route its one brain call through Fusion.*
- [ ] **F2.1** Add `fusion: bool = False` to `rewriter.rewrite(...)` and swap the brain call to `run = claude_runner.run_brain_json(prompt=prompt, cwd=str(project), fusion=fusion)`. Downstream (`run.ok`/`run.parsed_json`/`run.cost_usd`) unchanged. · *verify:* `fusion=False` behaves exactly as today; `fusion=True` opens a fusion panel + judge and returns a rewrite.
- [ ] **F2.2** Make the existing auto-retry force `run_claude_json` directly (a strict-JSON reminder to one model) so a flaky panel never re-fans-out. · *verify:* trigger a retry → it does **not** open a second fusion panel.

### Phase F3 — Pipeline wiring *(the on/off toggle, server side)*
- [ ] **F3.1** `app.py` `/send`: add `fusion: str = Form("false")`, parse `do_fusion = fusion.lower() in ("1","true","yes","on")`, thread into `_send_in_background(... do_fusion=...)`. `_run_dispatch` needs **no change**. · *verify:* POST `fusion=true` → a temporary log shows `do_fusion=True`.
- [ ] **F3.2** `_send_in_background`: pass `fusion=do_fusion` into `rewriter.rewrite(...)` and record `do_fusion` on the `rewrite_ok` stage event. · *verify:* a `fusion=true` send produces a panel-authored rewrite whose summed cost shows on the timeline; `fusion=true` + <2 keys still dispatches via fallback.

### Phase F4 — Dispatch-form toggle *(the on/off toggle, UI side)*
- [ ] **F4.1** Add the **Fusion checkbox** to `index.html` next to the effort/model selects. Label `fusion (multi-model) ⚡` + muted hint `multi-model panel — costs API tokens at each provider; best for architecture / research / high-stakes`. Persist in `localStorage`; **default OFF**. · *verify:* toggling + reloading keeps state.
- [ ] **F4.2** In `send(rewrite)` append `fd.append('fusion', chkFusion.checked ? 'true':'false')` next to `effort`/`model`/`rewrite`; works with **both** buttons. · *verify:* the `/send` payload includes `fusion`.
- [ ] **F4.3** Disabled state: extend `_view_ctx()` to pass `fusion_available = config.is_fusion_available()`; when false, render the checkbox **disabled** with *"Configure ≥2 providers' keys in ~/.orchestrator/config.json to enable."* When on, the in-flight banner reads `rewriting (multi-model) then dispatching (~15–40s).` · *verify:* <2 keys → disabled w/ note; ≥2 keys → enabled. **End-to-end toggle now works (F3 + F4).**

### Phase F5 — Surface + cost accounting
- [ ] **F5.1** `/dispatch/{id}` (`templates/dispatch.html`): render the `rewrite_ok`/`rewrite_skipped` stage events, including the **per-seat panel breakdown** (`run.raw["panel"]`) + summed cost. · *verify:* a fused dispatch's detail page shows each model + the total.
- [ ] **F5.2** Ensure the fused rewrite's `cost_usd` (panel sum) flows into the `outcomes` row (`cost_usd` at db.py:113). · *verify:* the outcomes row reflects the panel spend.
- [ ] **F5.3** ⟂ *(optional)* a ⚡ badge on fused rows in `templates/_runs.html`. · *verify:* fused runs are visually distinguishable.

### Phase F6 — Summarizer + onboarding *(optional)*
- [ ] **F6.1** ⟂ `summarizer.summarize(..., fusion=False)` → `run_brain_json(..., fusion=fusion)`. · *verify:* a summary can be produced via a fusion panel + judge.
- [ ] **F6.2** ⟂ `onboarding.analyze(..., fusion=False)` → `run_brain_json(..., fusion=fusion)`. · *verify:* an onboarding run can use fusion.

*(Lower priority — short sessions rarely justify panel cost, §7.)*

### Phase F7 — Enrichment-block mode *(optional, advanced)*
*Goal: optionally append a "multi-model analysis" block to the executor's prompt instead of replacing the rewrite. Build only once F1–F5 are solid.*
- [ ] **F7.1** New `orchestrator/lib/fusion.py`: `enrich(prompt, project_path) -> FusionResult` — calls `run_fusion_json` asking the judge for the analysis JSON, renders the `## Multi-model analysis` block. Cap panel input ~12K chars (à la `embeddings.MAX_INPUT_CHARS`). · *verify:* returns an `enrichment_md` block; on any failure returns the prompt unchanged (never raises).
- [ ] **F7.2** Wire an enrich path into `_send_in_background` (separate from rewrite): `fused_prompt = prompt + "\n\n" + enrichment_md`; **a failure here must NOT abort the dispatch**; record a distinct `fusion_ok`/`fusion_skipped` event. · *verify:* an enriched dispatch's prompt contains the block; a forced failure still dispatches.
- [ ] **F7.3** ⟂ Surface the collapsible analysis on the dispatch detail page. · *verify:* the block renders, collapsed by default.

```python
@dataclass
class FusionResult:                   # in fusion.py — distinct from ClaudeRun
    ok: bool
    analysis: Optional[dict] = None   # {consensus, contradictions, partial_coverage,
                                      #  unique_insights, blind_spots}
    enrichment_md: str = ""           # rendered "## Multi-model analysis" block
    panel_models: list = field(default_factory=list)
    cost_usd: float = 0.0
    error: str = ""
```

### Phase F8 — Model-selection UI *(edit the registry from the browser)*
*Goal: a UI to register providers + pick each panel seat's model/version. You can already do this by editing `config.json`; F8 makes it clickable.*
- [ ] **F8.1** `/settings` read view: show fusion availability + the current `preset`/`presets`/`providers` from `config.fusion_config()`. **Keys are never shown or editable in the browser.** · *verify:* the page reflects `config.json`.
- [ ] **F8.2** Preset switch — pick `budget`/`balanced`/`max`/`custom`; write to `config.json`. · *verify:* switching presets changes which providers the next fusion call bills.
- [ ] **F8.3** Registry editor — add/remove a provider (base_url, key_env, model, prices) and hand-pick each seat's model via `list_provider_models(name)` where available; save to `config.json`. **Key fields stay file-only.** · *verify:* a brand-new provider/model appears and is selectable.
- [ ] **F8.4** ⟂ *(optional)* per-dispatch override: thread a chosen preset/panel through `/send` like `effort`/`model` for one-off use without changing saved config. · *verify:* a one-off pick does not persist.

*When shipped:* append a `Phase 11 — Fusion ✅` entry to `PLAN.md` and a short `## Fusion` note to `CLAUDE.md`.

---

## 9. Deviation acknowledgment

The honest version the hard rules demand. **One rule is relaxed, on purpose, opt-in
only — and one rule that the OpenRouter design broke is now preserved:**

1. ✅ *"No Anthropic API calls"* — **preserved.** The judge runs on the local `claude`
   CLI (subscription, visible tab), and no panelist is Anthropic-via-API. Fusion never
   calls the Anthropic API. (The OpenRouter design broke this; the direct design does
   not.)
2. ⚠️ *"Local only. No remote workers, no hosted services."* — **relaxed.** The panel
   runs on **multiple third-party provider APIs**, and the prompt — which includes the
   project bundle (CLAUDE.md, memory, recent tasks, source excerpts) — **leaves the
   laptop** to each of them.

**Why it's acceptable:**
- **Default-off.** The checkbox is the only way Fusion ever fires.
- **Strictly additive.** `run_claude_json()` and the entire local path are untouched;
  Fusion is a sibling, never a replacement.
- **Degrades to local automatically.** <2 keys, or providers down → `run_brain_json()`
  falls back to the visible-tab `claude` call. A flaky panel never hard-fails a dispatch.
- **The executor stays 100% local.** Only brain/rewrite *text* is sent out; the file
  edits and command execution still run in a local iTerm2 `claude` session.
- **The judge stays 100% local** (the `claude` CLI), so the highest-value synthesis step
  never leaves the machine.

**What's preserved (compliance points):** zero new Python deps (stdlib `urllib` +
`concurrent.futures`); the **Stop hook stays a no-op** (fusion tabs set their own
`ORCHESTRATOR_FUSION_ID`, never `ORCHESTRATOR_RUN_ID`); keys and config live in
`~/.orchestrator/`, never the repo; **no Anthropic API calls** (judge on the CLI); and
**every call stays watchable in iTerm2**.

**Data-egress note — wider, not narrower.** Going direct means the bundle goes to **each
provider you enable** (DeepSeek, xAI, Google, MiniMax, Z.ai, Qwen — several Chinese
labs), each with its own data/retention policy, instead of through one gateway. The
egress *surface* is therefore wider than the OpenRouter design, even though fewer hard
rules are broken. Treat the toggle as *"send this project's context to N third
parties."* Keep it opt-in per send; consider a one-time confirmation the first time it's
enabled. Do **not** enable Fusion for any project whose contents shouldn't leave the
machine.

---

## Appendix — implementer notes

**Dispatch order (one at a time):** F0 (config, no network) → F1 (`run_fusion_json`) →
F2 (rewriter) → F3 (`/send` wiring) → F4 (toggle) → F5 (surface/cost) → F6–F8 (optional).

**Start with ONE provider.** Wire the whole panel→judge mechanism against a single cheap
seat (DeepSeek — pennies) to prove F1, then add Grok/Gemini/MiniMax/GLM/Qwen by **editing
`config.json`** (adding a lab is never a code change). Minimum upfront signup to get
moving: one.

**Key file targets (absolute):**
- `/Users/tresmith/Documents/orchestrator/orchestrator/lib/config.py` *(new — F0: registry, per-provider keys, presets)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/lib/claude_runner.py` *(F1 — `run_fusion_json`, `_run_panel`, `_run_fusion_in_tab`, `run_brain_json`)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/lib/spawn.py` *(F1 — `spawn_fusion_tab`; mirror `spawn_brain_tab`/`brain_run.sh`)*
- `~/.orchestrator/bin/fusion_run.sh` + `fusion_call.py` *(F1 — the visible-tab parallel panel runner, written by `ensure_fusion_runner()`)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/lib/rewriter.py` *(F2)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/lib/fusion.py` *(new — F7, enrichment mode)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/app.py` *(`/send`, `_send_in_background`, `_view_ctx` — F3/F4/F5)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/templates/index.html` *(toggle — F4)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/templates/dispatch.html` + `_runs.html` *(surfacing — F5)*
- `/Users/tresmith/Documents/orchestrator/bin/install.sh` *(config.json registry template — F0)*
- `~/.orchestrator/config.json` *(runtime data — holds each `<provider>` `api_key`, never in repo)*

**Reuse / consistency:**
- **Everything visible — no hidden/headless calls.** Panel fan-out → visible `fusion`
  tab (`spawn_fusion_tab` → `fusion_run.sh`); judge → visible `brain` tab
  (`run_claude_json`). In-process fan-out is a fallback only when iTerm2 is absent.
- Mirror `embeddings.py` for the HTTP: stdlib `urllib.request`, never raise, return
  `ok=False` on any failure, log a warning. **No `httpx`/`requests`.**
- Reuse `claude_runner._strip_fences` for judge JSON — don't re-invent it.
- Mirror the `rewrite_event` recording pattern for any `fusion_event`.
- Provider base URLs, native model slugs, and OpenAI-compatibility (§6) are a
  2026-06-17 snapshot — **verify live before wiring**, especially **MiniMax** (may need
  the `adapter` shim). Prefer config-driven registry/presets so a swap is a config edit.
- **CLAUDE.md is stale on one point:** its hard rule still says brain work goes through
  *headless* subprocesses, but the code uses **visible iTerm2 tabs**. Update that wording
  when Fusion ships (the `## Fusion` note in F8 is a good spot).
- **Edits don't take effect until you restart `python -m orchestrator`** (uvicorn
  `reload=False` on :7878), and the **auto-push daemon commits within seconds** —
  `git diff` won't show your changes.
