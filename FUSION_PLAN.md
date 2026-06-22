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
> Owning the orchestration also lets us tune the judge prompt and give each panel
> seat a different lens (things OpenRouter's black-box Fusion can't do).

> 🧩 **One script per provider.** Each model is called through **its own small,
> standalone script** (`providers/<name>.py`) that speaks that lab's *native* API in
> whatever shape it uses — there is **no shared "OpenAI-compatible" client and no
> adapter hook**. A script that isn't OpenAI-shaped (e.g. MiniMax) is just a different
> script, not a special case. Every script emits the **same normalized result**, so
> the orchestrator treats them all identically. Adding a provider = drop in one more
> script + a registry line.

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
   in **parallel**. Each lab is called through **its own provider script** that speaks
   that lab's native API; every script returns the same normalized result.
2. **Judge** — our local **`claude` CLI** (Opus) reads all N answers and synthesizes
   one. The judge is free on the existing subscription and runs in a visible tab.
3. We get back **one synthesized completion**, returned as the same `ClaudeRun`
   dataclass every existing brain caller already expects.

Key properties:

- **NOT iterative.** No agent loop, no tool-use turns. Fan out once, judge once,
  done. (Claude Code's iTerm2 executor sessions remain the only agentic part.)
- **We orchestrate, not a gateway.** Our side runs **N provider scripts + 1 judge
  call**. The scripts run as parallel subprocesses; the judge is `run_claude_json`.
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

- **Cost = Σ(panel provider token costs).** Each provider script returns normalized
  `prompt_tokens`/`completion_tokens`; the orchestrator multiplies by the per-provider
  `$/M` price in the registry (§4) and sums. Keeping price in the registry (not the
  script) means a price update is a config edit. There is no unified `usage.cost` like
  OpenRouter returned — we compute it.
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
                  │        panel fan-out (visible fusion tab, N provider scripts)    │
                  │        + judge (visible brain tab, local claude CLI)             │
                  └──────────────────────────────────────────────────────────────────┘
  Panel scripts, judge, and executor each run in iTerm2 tabs you can watch live.
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

## 4. Provider registry, scripts & secrets

**Each provider is one standalone script + one registry entry + one secret.**

- **Scripts** live in `~/.orchestrator/bin/providers/<name>.py`. A script owns that
  lab's **base URL, auth scheme, request body, response parsing, and streaming** — in
  whatever native format the lab uses. It resolves its own key, prints progress to
  **stderr** (watchable) and a **normalized JSON** result to **stdout**. Stdlib only,
  never raises. The normalized contract (every script, no exceptions):

  ```jsonc
  // stdout of any providers/<name>.py
  { "ok": true, "text": "<answer>", "model": "deepseek-chat",
    "prompt_tokens": 1234, "completion_tokens": 567, "error": "" }
  ```

- **Registry** lives in `~/.orchestrator/config.json` and keeps only the *swappable
  knobs*: which script, key env var, model id, and price (for cost). It does **not**
  hold base URLs or formats — those belong to the script.

- **Secrets:** one per provider. Resolution precedence, **per provider** (in
  `config.py`, also honored inside each script): **`<PROVIDER>_API_KEY` env var →
  `~/.orchestrator/config.json["fusion"]["providers"][name]["api_key"]` → `None`.** The
  key is read **inside the script** (env → `config.json`) — never passed via AppleScript.

```jsonc
// ~/.orchestrator/config.json  (chmod 600 — holds secrets; NEVER in the repo)
{
  "fusion": {
    "preset": "budget",
    "timeout_s": 300,
    "providers": {
      "deepseek": { "script": "providers/deepseek.py", "key_env": "DEEPSEEK_API_KEY",
                    "api_key": "", "model": "deepseek-chat",     "price_in": 0.44, "price_out": 0.87 },
      "xai":      { "script": "providers/xai.py",      "key_env": "XAI_API_KEY",
                    "api_key": "", "model": "grok-4",            "price_in": 1.25, "price_out": 2.50 },
      "gemini":   { "script": "providers/gemini.py",   "key_env": "GEMINI_API_KEY",
                    "api_key": "", "model": "gemini-2.5-flash",  "price_in": 0.30, "price_out": 1.50 },
      "minimax":  { "script": "providers/minimax.py",  "key_env": "MINIMAX_API_KEY",
                    "api_key": "", "model": "MiniMax-Text-01",   "price_in": 0.30, "price_out": 1.20 },
      "glm":      { "script": "providers/glm.py",      "key_env": "ZAI_API_KEY",
                    "api_key": "", "model": "glm-4.6",           "price_in": 1.40, "price_out": 4.40 },
      "qwen":     { "script": "providers/qwen.py",     "key_env": "DASHSCOPE_API_KEY",
                    "api_key": "", "model": "qwen-max",          "price_in": 1.25, "price_out": 3.75 }
    },
    "presets": {
      "budget":   ["deepseek", "minimax", "gemini"],
      "balanced": ["deepseek", "xai", "qwen"],
      "max":      ["deepseek", "xai", "gemini", "minimax", "glm", "qwen"]
    }
  }
}
```

- **Env-var-first** keeps secrets out of files; the `api_key` field is the fallback so
  `install.sh` can scaffold it.
- **Never committed, never browser-editable** (edit the file directly).
- **Active providers drive the UI.** Each entry may carry `"enabled": true|false`
  (default `true`) so you can deactivate a provider without deleting its key. A provider
  is **active** when its key resolves *and* it's enabled; `config.active_providers()`
  returns that list, and the dispatch form lists exactly those as selectable panel models
  (F4). Inactive providers (no key, or disabled) are shown greyed-out.
- A script MAY read an optional `base_url`/`region` override from its registry entry for
  region switching (Qwen / Z.ai international-vs-China hosts) — but the default host
  lives in the script.
- **Startup probe** (mirror the embeddings probe in `app.py` lifespan): log one WARNING
  if fewer than 2 panel providers have both a script and a resolvable key, so the user
  knows the toggle will fall back.
- The judge needs **no key and no script** — it's the local `claude` CLI.

**Where the scripts come from.** Keep canonical templates in the repo at
`orchestrator/providers/<name>.py` (reviewable, version-controlled); `ensure_fusion_runner()`
materializes them into `~/.orchestrator/bin/providers/` on first run (same idea as
`brain_run.sh` being written into the data dir). They run from the data dir so they
stay editable per-machine without touching the repo.

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
is a **new script + a registry line**, never a change to the core orchestrator.

**Per-dispatch selection.** Presets are the quick-pick, but the dispatch form also lets
you hand-check **which active-key models** go in the panel for a given send (F4). The
effective panel is "the models you ticked, intersected with the ones whose key is
active" — so you can only ever select models you actually have a key for.

**Optional refinement (owning the orchestration buys this):** give each seat a different
*lens* instead of the identical prompt — e.g. "find the risks," "find the simplest
path," "find what's ambiguous." Store per-seat prompt prefixes in the registry. Defer to
after F5.

---

## 6. Provider catalog (direct APIs — one script each)

> **Verify before wiring.** Each lab's **base URL**, **native model id**, and **request
> shape** drift and must be confirmed against the provider's own docs before writing its
> script. ⚠ Native model ids are **NOT** OpenRouter's `vendor/model` slugs — each lab
> uses its own (e.g. DeepSeek's API wants `deepseek-chat`, not `deepseek/deepseek-v4-pro`).
> Prices are list rates ($/M tokens, input→output), snapshot **2026-06-17** — re-verify;
> keep them in the config registry, not in code. **Each row below = one provider script
> to write.** Whether a lab is OpenAI-shaped or not no longer matters to the core — the
> script absorbs it.

| Lab | Get the key | Base URL the script targets | Native model id (verify) | Key env | $/M (in→out) |
|-----|-------------|-----------------------------|--------------------------|---------|--------------|
| **DeepSeek** | https://platform.deepseek.com/api_keys | `https://api.deepseek.com` (OpenAI-shaped) | `deepseek-chat` / `deepseek-reasoner` | `DEEPSEEK_API_KEY` | $0.44 → $0.87 |
| **xAI (Grok)** | https://console.x.ai | `https://api.x.ai/v1` (OpenAI-shaped) | `grok-4` / `grok-4-fast` | `XAI_API_KEY` | $1.25 → $2.50 |
| **Google (Gemini)** | https://aistudio.google.com/apikey | `https://generativelanguage.googleapis.com/v1beta` (native or `/openai`) | `gemini-2.5-flash` / `gemini-2.5-pro` | `GEMINI_API_KEY` | ~$0.30 → $1.50 (free tier exists) |
| **MiniMax** | https://www.minimax.io/platform | `https://api.minimax.io/v1` (native `/text/chatcompletion_v2`) | `MiniMax-Text-01` (current M-series) | `MINIMAX_API_KEY` | $0.30 → $1.20 |
| **Z.ai (GLM)** | https://z.ai → API keys | `https://api.z.ai/api/paas/v4` (OpenAI-shaped) | `glm-4.6` / `glm-4.5` | `ZAI_API_KEY` | $1.40 → $4.40 |
| **Qwen (Alibaba)** | https://modelstudio.console.alibabacloud.com | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | `qwen-max` / `qwen-plus` / `qwen3-max` | `DASHSCOPE_API_KEY` | $1.25 → $3.75 |
| *(optional)* **OpenAI** | https://platform.openai.com/api-keys | `https://api.openai.com/v1` | `gpt-5...` | `OPENAI_API_KEY` | varies |
| *(optional)* **Moonshot (Kimi)** | https://platform.moonshot.ai | `https://api.moonshot.ai/v1` | `kimi-k2-...` | `MOONSHOT_API_KEY` | $0.60 → $2.50 |
| **Judge → Anthropic (Opus)** | — uses your `claude` CLI | — (local subprocess, visible tab) | (executor model) | — none | **$0 marginal** |

### Per-provider notes for the script authors
- **DeepSeek / xAI / Z.ai** — OpenAI-shaped `/chat/completions`; their scripts are
  near-identical (differ only in base URL + key env). Start with `deepseek.py` (cheapest)
  as the template, then copy for xai/glm.
- **MiniMax** — native shape (`/v1/text/chatcompletion_v2`), not OpenAI. `minimax.py`
  owns that body/parse difference; it still emits the same normalized JSON. Use the
  international host (`api.minimax.io`), not the China host.
- **Qwen / Alibaba** — use the **`-intl`** DashScope host + `compatible-mode/v1`; the bare
  `dashscope.aliyuncs.com` is the China region.
- **Gemini** — has a native `generateContent` API *and* an OpenAI-compat `/openai` path;
  pick either in `gemini.py`. It has a **free tier** (rate-limited), then paid via a linked
  Cloud billing project.

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
| **F0** | Config & key mgmt | `config.py` (registry + per-provider keys + presets) + idempotent `install.sh` template | ✅ |
| **F1** | Provider scripts + `claude_runner` | `providers/*.py` + `run_fusion_json()` (parallel scripts + `claude` judge) + `run_brain_json()` | ◐ *(code complete — all 6 provider scripts written + offline-tested; LIVE per-script paid verify still pending keys for deepseek/xai/minimax/qwen)* |
| **F2** | Rewriter integration | rewriter routes through fusion when toggled | ✅ |
| **F3** | Pipeline wiring | thread `fusion` flag `/send` → `_send_in_background` | ✅ *(via F9 — `fusion_seats`)* |
| **F4** | Toggle + model picker | on/off checkbox + key-gated model multiselect, localStorage, disabled-when-<2-providers | ✅ *(via F9 — seat picker)* |
| **F5** | Surface + cost | show panel breakdown + summed cost; cost in outcomes | ✅ |
| **F6** (opt) | Summarizer + onboarding | same drop-in for the other two brain calls | ✅ |
| **F7** (opt) | Enrichment-block mode | panel → analysis block appended to executor prompt | ✅ |
| **F8** (opt) | Settings UI (advanced) | edit the registry, manage presets, add new providers from the browser | ✅ *(incl. F8.4 per-seat lenses)* |
| **F9** (opt) | Claude Code panel seats | per-dispatch picker seats (model+effort, duplicates) via local `claude` CLI — **no API, $0, no egress** | ✅ *(implemented)* |

**F0–F5 deliver a working, shippable on/off Fusion toggle.** Build strictly in order;
don't start a task until the previous one's verify passes. **⟂** marks order-independent
tasks.

> **📍 Current status (2026-06-19).** Fusion runs end-to-end today: toggle it on, pick a
> panel (cross-lab providers + Claude Code seats), and the rewriter fans out → judges →
> dispatches, falling back to the plain `claude` call when <2 seats are usable.
> **Done (code + offline tests):** F0, **F1 *core*** (`run_fusion_json`/`_panel_answer`/
> `_run_panel`/judge/fusion tab/`run_brain_json`), **F1.2/F1.2b** (all 6 provider scripts),
> F2, F3, F4, **F5** (durable per-seat surface + cost into `outcomes`), **F6** (summarizer +
> onboarding fusion-capable), **F7** (enrichment-block mode), **F8** (settings registry/preset
> editor **+ F8.4 per-seat lenses**), F9. The whole suite is green — `python -m unittest` →
> **313 ok, 4 skipped** (the skips need `httpx`, which isn't in the venv).
> **NOT done (still roadmap):**
> - **F1.2 / F1.2b LIVE verify** — all six scripts pass offline `urllib`-mocked tests, but
>   `deepseek`, `xai`, `minimax`, `qwen` have **no key** in `config.json`, so their
>   one-real-paid-call verify hasn't run and any preset naming them silently drops those seats.
>   Usable external providers today = **gemini, glm** (script *and* key). A real cross-lab panel
>   right now = **glm + gemini + Claude Code seats** (the keys actually present). **This paid
>   per-script verify is the ONLY remaining no-key-blocked item** — every other F0–F9 task,
>   F8.4 included, is built + offline-tested.
>
> **Next up:** add the four missing provider keys when available + run the paid F1.2/F1.2b
> live verify. Everything else in F0–F9 — F8.4 lenses included — is built.

### Pre-build checklist *(verified against the live code 2026-06-17 — read before F0)*
- **Get ≥1 provider key before F1** (DeepSeek is cheapest). F0 needs no network, but
  every F1 verify makes a **real paid call**. A **2nd key** is needed to exercise
  `is_fusion_available()` (≥2 active) and the F4 multiselect — so grab two.
- **Confirmed reusable (no surprises):** `ClaudeRun` is a plain **mutable** `@dataclass`
  with `ok/text/parsed_json/cost_usd/duration_s/model/error/raw` (so `run_fusion_json`
  setting `judge.cost_usd`/`judge.raw` is safe); `spawn.iterm2_installed`/`pid_alive` and
  `db.DATA_DIR` exist; the **brain-tab block in `spawn.py` is a clean template to mirror**
  (`BRAIN_DIR`, lazy `ensure_brain_runner()`, `spawn_brain_tab`, `finish_brain_tab`,
  `cleanup_brain_files`, `.done`/`.pid`/`.jsonl` sidecars, `_brain_tab_cmd` setting
  `ORCHESTRATOR_BRAIN_ID`); `db.record_event`, `_view_ctx`, and `_send_in_background`
  (which **already emits `rewrite_ok`/`rewrite_skipped`**) and the outcomes `cost_usd`
  column all exist where F3–F5 expect them.
- **Two gotchas now baked into the tasks:** (1) `run_claude_json` **defaults to Sonnet**
  (`DEFAULT_MODEL="sonnet"`), so the Opus judge model MUST be passed explicitly (F1.5).
  (2) the rewriter's existing **auto-retry already calls `run_claude_json` directly**
  (rewriter.py), so F2.2 ("never re-fan-out on retry") is essentially already true — just
  keep the retry off `run_brain_json`.

### Phase F0 — Config & key management
*Goal: a `config.py` that resolves per-provider keys + the registry/presets, and an installer that scaffolds the config file.*
- [x] **F0.1** `config.py`: `load_config()` (reads `~/.orchestrator/config.json`; `{}` if absent/malformed; never raises) + `get_provider_key(name)` (env `key_env` → file `api_key` → None) + `active_providers()` (provider names whose key resolves **and** `enabled != false`, each with its `model` id) + `is_fusion_available()` (≥2 active providers). · *verify:* `is_fusion_available()` → `False` clean, `True` once two providers' keys are set; `active_providers()` lists exactly the keyed+enabled ones.
- [x] **F0.2** `config.py`: `fusion_config()` → `{preset, timeout_s, providers, presets}` merged over the §4/§6 seed defaults. · *verify:* returns seeds with no file; returns your values when `config.json` sets them.
- [x] **F0.3** `install.sh`: write the `config.json` template **only if absent** (idempotent) — full registry with empty `api_key`s + presets — and print where to paste each key. · *verify:* run twice; the 2nd run is a no-op and never clobbers existing keys.

### Phase F1 — Provider scripts + `claude_runner.py` extension *(the core)*
The new unit is **one standalone provider script per model**; the orchestrator just
runs the panel's scripts in parallel and synthesizes with the `claude` judge. The panel
fan-out runs in a watchable `fusion` tab (`spawn_fusion_tab` + `fusion_run.sh` →
`fusion_call.py`, which runs the scripts); the judge reuses `run_claude_json` (already a
watchable `brain` tab). **No new Python deps** (stdlib `urllib`, `subprocess`,
`concurrent.futures`). Reuse `_strip_fences` for the judge's JSON.

- [x] **F1.1** Define the **normalized provider-script contract** (above) and write the first script, `providers/gemini.py` (stdlib `urllib`): read `<request.json>` (`{prompt, model, timeout_s}`), resolve the key (env → `config.json`), POST to DeepSeek, echo progress + answer to **stderr**, print normalized JSON to **stdout**; never raise. · *verify:* `python3 providers/deepseek.py req.json` prints normalized JSON with `ok=true`, token counts, and you SEE the answer on stderr; missing key → `ok=false`, no traceback.
- [x] **F1.2** ⟂ Write the **OpenAI-shaped** seed scripts by copying `deepseek.py` and swapping base URL + key env: `xai.py`, `glm.py`, `gemini.py` (its `/openai` path), `qwen.py` (DashScope `compatible-mode`). Each independently runnable, each emits the same normalized JSON. · *verify:* **code+offline done** — `deepseek.py`/`xai.py`/`qwen.py` written as exact mirrors of `gemini.py`/`glm.py`; endpoints re-verified against live docs 2026-06-18; `tests/test_fusion_providers.py` exercises request shape + parse + never-raise with `urllib` mocked. ⚠ **LIVE per-script run by hand (a real paid call) still pending** — deepseek/xai/qwen have **no key** in `config.json`.
- [x] **F1.2b** ⟂ *(spike)* `minimax.py` — **resolved:** as of the 2026-06-18 docs MiniMax ALSO exposes a fully OpenAI-compatible `POST https://api.minimax.io/v1/chat/completions` (standard `messages`/roles, `choices[0].message.content`, OpenAI-shaped `usage`), so it no longer needs the bespoke `/v1/text/chatcompletion_v2` body/parser — it's just another copy of the OpenAI-shaped template. · *verify:* **code+offline done** (same test file). ⚠ **LIVE run pending** — `minimax` has **no key**.
- [x] **F1.3** `claude_runner._panel_answer(name, prov, prompt, timeout)` — run `prov["script"]` as a subprocess with the request, parse the normalized JSON, compute `cost = (in×price_in + out×price_out)/1e6` from the registry. Never raises. · *verify:* returns `{ok, text, cost, …}` for one provider; a script that errors → `ok=False`, no raise.
- [x] **F1.4** `_run_panel(prompt, panel, providers, timeout)` — run the preset's subset **in parallel** (`ThreadPoolExecutor` over `_panel_answer`). · *verify:* a 3-seat preset returns 3 answers; wall-clock ≈ slowest seat, not the sum.
- [x] **F1.5** `_judge_prompt(orig, answers)` — **reuse the original prompt verbatim** (so its output JSON schema travels with it), then append the N panel answers + *"synthesize the single best response, in the exact same format."* **+** `run_fusion_json(..., judge_model="opus", judge_effort="high")` resolves preset/panel/timeout, runs the panel, then calls `run_claude_json(synthesis, cwd, model=judge_model, effort=judge_effort, label="fusion-judge")` — ⚠ **the model MUST be passed explicitly** (`run_claude_json` defaults to *sonnet*); sets `cost_usd = Σ panel cost`. · *verify:* `run_fusion_json("Should a single-writer app use SQLite WAL?")` → `ok=True`, `cost>0`, judge tab runs **Opus**; editing a registry `model` changes which models are billed. *(In-process subprocess fan-out for now; F1.7b puts the visible tab in front.)*
- [x] **F1.6** ⟂ `fusion_call.py` (standalone, **must NOT import the orchestrator package**): read `<id>.request.json` (`{prompt, panel, providers, timeout}`), run each panel provider's script as a parallel subprocess, **interleave their stderr** to the screen (watchable), collect the normalized outputs, print the collected JSON to stdout. · *verify:* run by hand → you SEE each provider answer stream; `<id>.json` holds all collected answers.
- [x] **F1.7a** `spawn.py` — mirror the brain-tab block (it's a clean template): `FUSION_DIR`, lazy `ensure_fusion_runner()` (writes `fusion_run.sh` + `fusion_call.py` + materializes `providers/*.py`), `spawn_fusion_tab(fusion_id, body, cwd)` (writes `<id>.request.json`, sets `ORCHESTRATOR_FUSION_ID` — never `ORCHESTRATOR_RUN_ID`, execs `fusion_run.sh`), `finish_fusion_tab` + `cleanup_fusion_files`. · *verify:* `spawn_fusion_tab` with a test id opens a visible tab that writes `.pid`/`.json`/`.done`.
- [x] **F1.7b** `claude_runner._run_fusion_in_tab(body, cwd, timeout)` — poll `<id>.done`/`.pid` (copy `run_claude_json`'s loop; **simpler** — `<id>.json` is already the final collected answers, so no stream-jsonl reconstruction) **+** rewire `run_fusion_json` to prefer the tab, falling back to the in-process `_run_panel` only when `spawn.iterm2_installed()` is false or the tab fails. · *verify:* `run_fusion_json(...)` opens a visible fusion tab (panel) + a brain tab (judge); faked-no-iTerm2 still returns a valid `ClaudeRun` via fallback.
- [x] **F1.8** `run_brain_json(prompt, cwd, fusion=False, **kw)` dispatcher (fusion→`run_fusion_json`; else, or on failure→`run_claude_json`). · *verify:* `fusion=True` with <2 keys falls back to the normal visible-tab claude call, no hard error.

*Code reference for F1 — target shapes (not extra work):*

```python
# ── orchestrator side: orchestrator/lib/claude_runner.py ─────────────────────
import subprocess, concurrent.futures            # (json, os already imported)

# Registry SEED — fallback only, NOT an allowlist. Real config in
# ~/.orchestrator/config.json["fusion"]["providers"] (§4). Each entry names a SCRIPT;
# the script owns the base URL + native format. Model/price/key are swappable knobs.
FUSION_PROVIDERS_SEED = {
  "deepseek": {"script": "providers/deepseek.py", "key_env": "DEEPSEEK_API_KEY",
               "model": "deepseek-chat",     "price_in": 0.44, "price_out": 0.87},
  "xai":      {"script": "providers/xai.py",      "key_env": "XAI_API_KEY",
               "model": "grok-4",            "price_in": 1.25, "price_out": 2.50},
  "gemini":   {"script": "providers/gemini.py",   "key_env": "GEMINI_API_KEY",
               "model": "gemini-2.5-flash",  "price_in": 0.30, "price_out": 1.50},
  "minimax":  {"script": "providers/minimax.py",  "key_env": "MINIMAX_API_KEY",
               "model": "MiniMax-Text-01",   "price_in": 0.30, "price_out": 1.20},
  "glm":      {"script": "providers/glm.py",      "key_env": "ZAI_API_KEY",
               "model": "glm-4.6",           "price_in": 1.40, "price_out": 4.40},
  "qwen":     {"script": "providers/qwen.py",     "key_env": "DASHSCOPE_API_KEY",
               "model": "qwen-max",          "price_in": 1.25, "price_out": 3.75},
}
FUSION_PRESETS_SEED = {
  "budget":   ["deepseek", "minimax", "gemini"],
  "balanced": ["deepseek", "xai", "qwen"],
  "max":      ["deepseek", "xai", "gemini", "minimax", "glm", "qwen"],  # high-stakes only
}
DEFAULT_FUSION_PRESET    = "budget"
DEFAULT_FUSION_TIMEOUT_S = 300
PROVIDERS_DIR = str(DATA_DIR / "bin")            # scripts live under ~/.orchestrator/bin/
# JUDGE is ALWAYS the local claude CLI (run_claude_json): free, keeps "No Anthropic API
# calls" intact, runs in a visible brain tab.


def _panel_answer(name: str, prov: dict, prompt: str, timeout_s: int) -> dict:
    """Run ONE provider's script as a subprocess → normalized dict + computed cost.
    Never raises. The script owns the lab's native API; we only read its stdout."""
    req = json.dumps({"prompt": prompt, "model": prov["model"], "timeout_s": timeout_s})
    try:
        p = subprocess.run(["python3", os.path.join(PROVIDERS_DIR, prov["script"])],
                           input=req, capture_output=True, text=True, timeout=timeout_s + 15)
        out = json.loads(p.stdout or "{}")
    except Exception as e:                       # spawn/timeout/parse — never propagate
        return {"name": name, "ok": False, "error": str(e)}
    if not out.get("ok"):
        return {"name": name, "ok": False, "error": out.get("error", "unknown")}
    cost = (out.get("prompt_tokens", 0) * prov["price_in"]
            + out.get("completion_tokens", 0) * prov["price_out"]) / 1e6
    return {"name": name, "model": out.get("model", prov["model"]),
            "text": out.get("text", ""), "cost": cost, "ok": True}


def _run_panel(prompt: str, panel: list, providers: dict, timeout_s: int) -> list:
    """Fan out to the preset's subset IN PARALLEL (slowest-seat-bound, not the sum)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(panel))) as ex:
        return list(ex.map(lambda n: _panel_answer(n, providers[n], prompt, timeout_s), panel))


def run_fusion_json(prompt: str, cwd: str = "", preset: Optional[str] = None,
                    panel: Optional[list] = None, timeout_s: Optional[int] = None,
                    judge_model: str = "opus", judge_effort: str = "high") -> ClaudeRun:
    """Fusion sibling of run_claude_json. Runs a PANEL of per-provider scripts (parallel,
    visible fusion tab), then synthesizes via the local claude CLI judge (run_claude_json,
    visible brain tab — free on the subscription). cost_usd = Σ panel provider costs.
    NOTE: run_claude_json defaults to sonnet, so the judge model is passed EXPLICITLY
    (default opus/high; a summarizer caller can pass sonnet). Never raises. See §9."""
    cfg       = config.fusion_config()
    providers = {**FUSION_PROVIDERS_SEED, **cfg.get("providers", {})}
    presets   = {**FUSION_PRESETS_SEED,   **cfg.get("presets", {})}
    preset    = preset or cfg.get("preset") or DEFAULT_FUSION_PRESET
    panel     = panel  or presets.get(preset) or FUSION_PRESETS_SEED["budget"]
    timeout_s = timeout_s or cfg.get("timeout_s") or DEFAULT_FUSION_TIMEOUT_S

    answers = _run_panel(prompt, panel, providers, timeout_s)   # F1.7: in the visible tab
    ok = [a for a in answers if a.get("ok")]
    if len(ok) < 2:
        errs = "; ".join(f"{a['name']}: {a.get('error')}" for a in answers if not a.get("ok"))
        return ClaudeRun(ok=False, error=f"fusion panel: only {len(ok)} provider(s) answered ({errs})")

    judge = run_claude_json(prompt=_judge_prompt(prompt, ok), cwd=cwd,   # local claude judge
                            model=judge_model, effort=judge_effort, label="fusion-judge")
    judge.cost_usd = sum(a["cost"] for a in ok)        # real out-of-pocket = panel only
    judge.raw = {"panel": answers, "preset": preset}
    return judge


def run_brain_json(prompt: str, cwd: str, fusion: bool = False, **kw) -> ClaudeRun:
    """Single entry point for brain calls. Fusion when requested AND available; else, or
    on failure, the standard visible-tab claude call — a flaky panel never hard-fails."""
    if fusion:
        run = run_fusion_json(prompt=prompt, cwd=cwd, **kw)
        if run.ok:
            return run
        print(f"[claude_runner] fusion unavailable ({run.error}); falling back to claude")
    return run_claude_json(prompt=prompt, cwd=cwd)
```

```python
# ── one provider script: orchestrator/providers/deepseek.py (template) ───────
# Standalone — NO imports from the orchestrator package (runs in the tab's own process).
# Stdlib only. Reads {prompt, model, timeout_s} from argv[1], prints normalized JSON.
# A non-OpenAI lab (minimax.py) keeps this SAME stdout contract but its own POST/parse.
import sys, json, os, urllib.request

BASE_URL, KEY_ENV, NAME = "https://api.deepseek.com", "DEEPSEEK_API_KEY", "deepseek"

def _key():
    if os.environ.get(KEY_ENV): return os.environ[KEY_ENV]
    try:                                          # config.json fallback (no pkg import)
        cfg = json.load(open(os.path.expanduser("~/.orchestrator/config.json")))
        return cfg["fusion"]["providers"][NAME].get("api_key") or ""
    except Exception: return ""

def main(req_path):
    req = json.load(open(req_path)); key = _key()
    if not key:
        print(json.dumps({"ok": False, "error": f"{KEY_ENV} not set"})); return
    body = {"model": req["model"], "messages": [{"role": "user", "content": req["prompt"]}]}
    r = urllib.request.Request(BASE_URL + "/chat/completions",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    sys.stderr.write(f"→ {NAME} {req['model']} …\n"); sys.stderr.flush()
    try:
        env = json.loads(urllib.request.urlopen(r, timeout=req.get("timeout_s", 300)).read())
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)})); return
    text = (env.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    u = env.get("usage") or {}
    sys.stderr.write(text + "\n")                  # echo answer to the watchable tab
    print(json.dumps({"ok": True, "text": text, "model": req["model"],
                      "prompt_tokens": u.get("prompt_tokens", 0),
                      "completion_tokens": u.get("completion_tokens", 0), "error": ""}))

if __name__ == "__main__":
    try: main(sys.argv[1])
    except Exception as e: print(json.dumps({"ok": False, "error": str(e)}))
```

**Visible-tab plumbing (mirror the brain-tab path).** `_run_fusion_in_tab` generates a
`fusion_id`, calls `spawn.spawn_fusion_tab(fusion_id, body, cwd)` (writes
`~/.orchestrator/fusion/<id>.request.json` with `{prompt, panel, providers, timeout}`,
opens the tab via `fusion_run.sh`), then polls `<id>.done`/`.pid` like the brain loop.

```bash
#!/bin/bash
# ~/.orchestrator/bin/fusion_run.sh — execed in an iTerm2 tab so the panel fan-out is
# WATCHABLE (same principle as brain_run.sh). fusion_call.py runs each panel provider's
# script in parallel, interleaving their stderr on SCREEN, and prints ONLY the collected
# answers JSON to stdout — which `tee` captures to <id>.json.
ID="$ORCHESTRATOR_FUSION_ID"; DIR="$HOME/.orchestrator/fusion"
echo $$ > "$DIR/$ID.pid"
echo "---- orchestrator fusion panel: $ID (watching live) ----"
python3 "$HOME/.orchestrator/bin/fusion_call.py" "$DIR/$ID.request.json" | tee "$DIR/$ID.json"
echo "${PIPESTATUS[0]}" > "$DIR/$ID.done"
echo "---- fusion panel finished (judge runs next in a brain tab) ----"
```

`fusion_call.py` is standalone (stdlib `subprocess` + `concurrent.futures`) — it runs in
the tab's own process, so it must NOT import the `orchestrator` package. It reads the
request, runs each panel provider's `providers/<name>.py` as a parallel subprocess,
streams each script's stderr to the screen for watching, and prints the collected
answers to stdout for capture. The **judge** then runs as a normal `run_claude_json`
brain tab — so both halves stay visible.

*Acceptance:* with ≥2 keys set, `run_fusion_json("Should a single-writer local app use
SQLite WAL mode?")` opens a visible fusion tab (panel scripts) + a brain tab (judge) and
returns `ok=True` with `cost>0` (panel sum); with <2 keys, `ok=False`; with iTerm2
uninstalled, it still returns a valid `ClaudeRun` via the in-process subprocess fan-out.

### Phase F2 — Rewriter integration
*Goal: the rewriter can route its one brain call through Fusion.*
- [x] **F2.1** Add `fusion: bool = False` **and `panel: Optional[list] = None`** to `rewriter.rewrite(...)` and swap the brain call to `run = claude_runner.run_brain_json(prompt=prompt, cwd=str(project), fusion=fusion, panel=panel)` (`run_brain_json`'s `**kw` already forwards `panel` to `run_fusion_json`). Downstream (`run.ok`/`run.parsed_json`/`run.cost_usd`) unchanged. · *verify:* `fusion=False` behaves exactly as today; `fusion=True` opens a fusion panel + judge; `panel=["deepseek","gemini"]` bills exactly those two.
- [x] **F2.2** Make the existing auto-retry force `run_claude_json` directly (a strict-JSON reminder to one model) so a flaky panel never re-fans-out. · *verify:* trigger a retry → it does **not** open a second fusion panel.

### Phase F3 — Pipeline wiring *(the on/off toggle, server side)*
> ✅ **Shipped via F9 (2026-06-18).** `/send` threads Fusion through end-to-end, but using
> the richer **`fusion_seats`** JSON (Claude seats + providers) rather than the `fusion_panel`
> comma-list sketched below (legacy `fusion_panel` is still accepted). F3.1/F3.2 describe the
> superseded mechanism.
- [x] **F3.1** `app.py` `/send`: add `fusion: str = Form("false")` **+ `fusion_panel: str = Form("")`** (comma-separated provider names), parse `do_fusion = fusion.lower() in ("1","true","yes","on")` and `panel = [p for p in fusion_panel.split(",") if p in config.active_providers()]` (silently drops any model without an active key), thread both into `_send_in_background(... do_fusion=..., panel=panel)`. `_run_dispatch` needs **no change**. · *verify:* POST `fusion=true&fusion_panel=deepseek,minimax` → a temporary log shows `do_fusion=True` and the validated panel; an unkeyed name in the list is dropped.
- [ ] **F3.2** `_send_in_background`: pass `fusion=do_fusion` **and `panel=panel`** into `rewriter.rewrite(...)`; record `do_fusion` + the chosen panel on the `rewrite_ok` stage event. An empty `panel` falls back to the configured preset. · *verify:* a `fusion=true` send with a 2-model panel bills exactly those two; `fusion=true` + empty panel uses the preset; `fusion=true` + <2 keys still dispatches via fallback.

### Phase F4 — Dispatch-form toggle + model picker *(on/off + which-models, UI side)*
*Goal: the two things the toggle UX must have — a checkbox to turn Fusion on, and a key-gated list to pick which models go in the panel.*
> ✅ **Shipped via F9 (2026-06-18).** The dispatch form has the Fusion toggle + a seat picker
> (cross-lab providers *and* Claude Code seats), localStorage-persisted, default-OFF. F4.1–F4.4
> describe the original provider-only multiselect, now subsumed by the F9 picker.
- [x] **F4.1** Add the **Fusion checkbox** to `index.html` next to the effort/model selects. Label `fusion (multi-model) ⚡` + muted hint `multi-model panel — costs API tokens at each provider; best for architecture / research / high-stakes`. Persist in `localStorage`; **default OFF**. · *verify:* toggling + reloading keeps state.
- [ ] **F4.2** **Model multiselect (key-gated).** Extend `_view_ctx()` to pass `fusion_providers = config.active_providers()` (each with its `model` id) **plus** the inactive ones for display. Render a checklist beneath the checkbox — one row per provider showing its model id; **active** providers are checkable, **inactive** ones (no key / disabled) are greyed with *"no API key set."* Persist the checked set in `localStorage`, seeded from the current preset's active members. Reveal the list only when the Fusion checkbox is on. · *verify:* only keyed providers are checkable; an unkeyed one is greyed; the checked set survives reload.
- [ ] **F4.3** In `send(rewrite)` append `fd.append('fusion', chkFusion.checked ? 'true':'false')` **and `fd.append('fusion_panel', checkedProviders.join(','))`** next to `effort`/`model`/`rewrite`; works with **both** buttons. · *verify:* the `/send` payload includes `fusion` + the chosen `fusion_panel`.
- [ ] **F4.4** Disabled state: when `config.is_fusion_available()` is false (<2 active providers), render the checkbox **disabled** with *"Configure ≥2 providers' keys in ~/.orchestrator/config.json to enable."* When on, the in-flight banner reads `rewriting (multi-model) then dispatching (~15–40s).` · *verify:* <2 keys → disabled w/ note; ≥2 keys → enabled. **End-to-end toggle + model selection now works (F3 + F4).**

### Phase F5 — Surface + cost accounting ✅ *(2026-06-18)*
- [x] **F5.1** `/dispatch/{id}` (`templates/dispatch.html`): render the `rewrite_ok`/`rewrite_skipped` stage events, including the **per-seat panel breakdown** (`run.raw["panel"]`) + summed cost. · *done:* `rewriter` now carries `fusion_panel`/`fusion_preset`/`fusion_seats` (captured from `run.raw` BEFORE the single-model retry); `_send_in_background` records a trimmed `panel_breakdown` (name/model/cost/tokens + bounded text-or-error preview, Claude seats marked `subscription`) on the stage event; `view_dispatch` pulls the rewrite event and `dispatch.html` renders a **server-side** "rewrite" pane (durable — inspectable after the run) + a compact live-timeline summary. *verify:* offline render test asserts the fused pane shows each seat + total.
- [x] **F5.2** Ensure the fused rewrite's `cost_usd` (panel sum) flows into the `outcomes` row. · ⚠ *plan pointer was wrong:* there was **no `cost_usd` on `outcomes`** (db.py:113 is `onboarding_runs`). Added `cost_usd`+`fused` to `dispatches` and `cost_usd` to `outcomes`, with an idempotent additive `_migrate()` (guarded `ALTER`s — safe on the existing real DB). `set_dispatch_cost()` stamps the dispatch at `/send`; **every** outcome writer (complete/kill/pause/orphan/failed_to_spawn) copies it forward via a subquery. *verify:* `test_fusion_cost` proves cost lands on the outcome for all five terminal paths + a legacy-DB upgrade.
- [x] **F5.3** ⟂ a ⚡ badge on fused rows in `templates/_runs.html` (both the per-project and all-recent tables; driven by `dispatches.fused`, cost in the tooltip). · *verify:* fused runs are visually distinguishable.

### Phase F6 — Summarizer + onboarding *(optional)* ✅ *(2026-06-18)*
- [x] **F6.1** ⟂ `summarizer.summarize(..., fusion=False, panel=None)` → `run_brain_json(..., fusion=fusion, panel=panel)`. Tier kept DELIBERATELY low — sonnet/medium for the call AND a sonnet/medium judge (a distillation must not escalate to the Opus judge the rewriter uses). fusion=False is byte-for-byte the original single-claude path. · *verify:* `test_fusion_brain_calls` asserts the forwarded fusion/panel/tier/judge kwargs.
- [x] **F6.2** ⟂ `onboarding.analyze(..., fusion=False, panel=None)` → `run_brain_json(...)`, same sonnet/medium tier + sonnet/medium judge. · *verify:* same test file.

*(Capable but default-OFF; no UI toggle wired — short sessions rarely justify panel cost, §7. A caller opts in by passing `fusion=True`.)*

### Phase F7 — Enrichment-block mode *(optional, advanced)* ✅ *(2026-06-18)*
*Goal: optionally append a "multi-model analysis" block to the executor's prompt instead of replacing the rewrite.*
- [x] **F7.1** New `orchestrator/lib/fusion.py`: `enrich(prompt, project_path, panel=, preset=) -> FusionResult` — calls `run_fusion_json` with an ANALYSIS prompt (not a rewrite), parses the judge JSON (`_strip_fences` reused), and `render_block()`s a `## Multi-model analysis` section (empty sub-sections dropped). Input capped at `MAX_INPUT_CHARS = 12_000`. **NEVER raises** — panel-unavailable / unparseable / empty-analysis / crash all return `ok=False`. · *verify:* `test_fusion_enrich` (8 enrich cases + render/coerce).
- [x] **F7.2** Wired into `_send_in_background` (separate from rewrite, runs on the post-rewrite `final_task`): on success `final_task += "\n\n" + enrichment_md` and cost is added to the dispatch spend; **a failure NEVER aborts** — it dispatches the un-enriched prompt. Records a distinct `fusion_ok`/`fusion_skipped` stage event. Threaded `fusion_enrich` through `/send` (independent of `do_fusion`; works with BOTH buttons). · *verify:* async tests assert block-appended + failure-still-dispatches.
- [x] **F7.3** ⟂ Collapsible `<details>` "multi-model analysis" pane on the dispatch detail page (open on success, shows skip reason on failure), rendered server-side from the `fusion_ok`/`fusion_skipped` event. Dispatch-form gained an **`enrich ⚡`** checkbox (sibling of the fusion toggle; shares the seat picker; localStorage-persisted, default-OFF). · *verify:* render test asserts the analysis pane shows each section.
- *Also fixed in passing:* a pre-existing `/send` bug where the seat-parse loop shadowed the `model`/`effort` **Form params** (a Claude seat selection silently rewrote the EXECUTOR's model/effort) — loop locals renamed to `seat_model`/`seat_effort`.

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

### Phase F8 — Settings UI *(advanced registry management — basic selection already ships in F4)* ✅ *(2026-06-18; F8.4 lenses 2026-06-19)*
*Goal: the browser-side **registry editor** — add/remove providers, change a model slug, manage presets globally, see which providers are active.*
- [x] **F8.1** `/settings` read view (`templates/settings.html`, `app._settings_ctx`): shows fusion availability, the current `preset`/`presets`/`providers` from `config.fusion_config()`, and a derived `has_key` ●/— per provider. **Keys are never shown or editable in the browser** (`_settings_ctx` strips `api_key`; a no-leak test guards it). Linked from the dispatch form (⚙). · *verify:* `test_fusion_settings::TestSettingsReadModel`.
- [x] **F8.2** Preset switch — `POST /settings/preset` → `config.set_preset()` (merge-preserving). · *verify:* settings tests + the switch changes which providers the next fusion call bills.
- [x] **F8.3** Registry editor — `POST /settings/provider` (upsert: model + prices + enabled), `.../enabled` (toggle), `.../remove`. The write helpers live in `config.py` (`save_config`/`upsert_provider`/`set_provider_enabled`/`remove_provider`) with two hard safety invariants: **api_keys are file-only** (never set from the browser, always preserved) and a **malformed `config.json` is never overwritten** (`ConfigWriteError` aborts the save). All writes are atomic (tmp+rename, chmod 600). · *verify:* `test_fusion_settings` (12 cases incl. key-survives-edits + corruption-guard).
- [x] **F8.4** ⟂ per-seat **lens prompts** (§5 refinement). ✅ *(2026-06-19)* A lens makes a seat answer the SAME task through a perspective ("find the risks" / "the simplest path" / "what's ambiguous") to decorrelate the panel; the judge still sees the original prompt verbatim, and a lens-free panel is byte-for-byte the pre-F8.4 behavior. *Shipped:*
  - **`config.py`** — `FUSION_LENSES_SEED` (3 §5 lenses) merged like presets in `fusion_config()` (→ `"lenses"`); `fusion_lenses()` + `resolve_lens(name-or-literal[, lenses])`; corruption-guarded, key-preserving `set_lens`/`remove_lens` (mirror `set_preset`).
  - **`claude_runner.py`** — `_apply_lens(prompt, lens)` prepends the lens, keeping the prompt **verbatim and last** (its output-format instructions still travel). `run_fusion_json` now accepts a lens on either seat form (`{"name":<provider>,"lens":…}` and `{"kind":"claude_cli",…,"lens":…}`), threads the resolved TEXT to each seat — external via a `lenses` map down `_panel_answers`/`_run_panel`/`_run_fusion_in_tab`, Claude seats via `_anthropic_seat_answer` — and surfaces the seat→lens map in `raw["lenses"]` (+ tags each panel answer). Cost plumbing unchanged ($0 Claude seats; external billed from real token counts).
  - **`fusion_call.py`** — a textually-identical `_apply_lens` applies the per-seat lens from the request `lenses` map, so the watchable-tab panel builds the SAME lensed prompt as the in-process fallback.
  - **`app.py`** — `/send` reads an optional per-seat `lens` (capped, resolved seat-side); `_view_ctx`/`_settings_ctx` surface the configured lenses; `/settings/lens` + `/settings/lens/{name}/remove` routes; the rewrite breakdown carries each seat's `lens`.
  - **templates** — a per-seat lens `<select>` on each Claude seat + active provider in the dispatch picker (localStorage-persisted, default "no lens"); a lens CRUD section in `settings.html`.
  - **tests** — `tests/test_fusion_lenses.py` (28 offline cases: config merge/resolve/CRUD, `_apply_lens` + standalone-twin parity, `_run_panel`/`_anthropic_seat_answer`/`_run_fusion_in_tab`/`fusion_call.py` threading, `run_fusion_json` end-to-end + lens-free back-compat, settings surface). · *verify:* a seat's lens prefix is applied on the next fusion call.
  - **usage guide** — [`FUSION_LENS_PLAYBOOK.md`](FUSION_LENS_PLAYBOOK.md): per-scenario lens→seat assignment (which lens on which model, how many seats) for architecture, debugging, data analysis, UIs, security, refactoring, … plus the capability-tiering + dominant-failure-mode rules. *(Seed lens set expanded 3 → 10 on 2026-06-22; see §11.c.3.)*

### Phase F9 — Claude Code panel seats *(✅ implemented 2026-06-18)*

> 🧭 **Three findings to confirm before any code (the headline of this phase):**
> 1. **The API-vs-CLI tension — resolved one way only.** Panel seats today are provider
>    scripts that hit an **external API** (`_panel_answer` → `providers/<name>.py`). An
>    Anthropic Opus seat **cannot** take that path — *"No Anthropic API calls"* is a hard
>    rule. The **only** compliant way to put Opus in the panel is to run each seat as a
>    **local `claude` CLI call** — exactly what the judge already does
>    (`run_claude_json(model="opus", effort=…)`): visible tab, subscription, **$0**, no
>    Anthropic API. So "a panel of two Opus seats" = **two `run_claude_json` calls at
>    different `--effort`**, not two API panelists.
> 2. **Temperature is NOT available; effort IS (verified against the live CLI 2026-06-18).**
>    `claude --help` exposes **`--effort <level>` with choices `low, medium, high, xhigh, max`**
>    and **no `--temperature`** (nor `--top-p`/`--seed` — grepped, none exist). Seats can be
>    differentiated by **thinking level** ("1 high + 1 medium" ✓) but **NOT by temperature** —
>    there is no CLI flag for it, so it is off the table unless we break the no-API rule (we
>    won't). *(The brief said efforts are `medium/high/xhigh/max`; the CLI actually also
>    accepts **`low`**, giving a wider `low`↔`max` spread to differentiate seats.)*
> 3. **A same-model ensemble has correlated errors (§5).** Two Opus 4.8 seats share weights,
>    training, and blind spots; effort only changes *how long it thinks*, not *what it knows*.
>    Diversity is **non-zero but weak** versus the cross-lab seats. Tradeoff table in F9.e.
>
> **✅ Built (2026-06-18).** Findings 1–3 confirmed by the user; the feature shipped as a
> **per-dispatch picker** (richer than the registry sketch in F9.b/c below): the dispatch form
> lets you add any number of Claude Code seats — each its own **model (opus/sonnet/haiku) +
> effort** dropdown, **duplicates allowed** — alongside the key-gated cross-lab providers, and
> sends the whole panel as a JSON seat list. No `config.json` registry entries or presets are
> needed for Claude seats. See **F9 — what shipped** below; F9.a/d/e (CLI capabilities,
> compliance, the correlation caveat) all still hold.

#### F9.a — What the CLI actually exposes (verified `claude --help`, 2026-06-18)

| Knob | Flag | Available? | Use for seats |
|------|------|-----------|---------------|
| Thinking level | `--effort low\|medium\|high\|xhigh\|max` | ✅ yes | **the** differentiator |
| Temperature | `--temperature` | ❌ **absent** | not possible via CLI |
| Top-p / seed | — | ❌ absent | not possible via CLI |
| Model | `--model opus` | ✅ yes | pin every seat to Opus 4.8 |
| Per-seat lens | `--append-system-prompt` (or a prompt prefix) | ✅ yes | optional decorrelation (§5 / F8.4) |

An Anthropic seat is therefore fully specified by **(model, effort, optional lens)** — and
"different temperatures" is unreachable through the CLI.

#### F9.b — The new seat type: `kind: "claude_cli"`

Every registry entry today implies "external provider script + key + price." An Anthropic
seat is a **different kind** of entry — no `script`, no `key_env`, no price — distinguished by
**`effort`**, and (because the *same* model appears at two efforts) keyed by a **per-seat
name** rather than per-lab:

```jsonc
// ~/.orchestrator/config.json  →  fusion.providers  (additions)
"opus-high":   { "kind": "claude_cli", "model": "opus", "effort": "high"   },
"opus-medium": { "kind": "claude_cli", "model": "opus", "effort": "medium" }
// no "script", no "key_env", no "price_in/out" — it's the local CLI, billed $0.
// "model" MUST be set: run_claude_json defaults to SONNET (dispatch #3 lesson),
// so an Opus seat that omits it would silently downgrade.
```

```jsonc
// fusion.presets  (additions)
"anthropic-local": ["opus-high", "opus-medium"],          // $0, ZERO egress, fully compliant
"hybrid":          ["opus-high", "deepseek", "gemini"]    // 1 free frontier seat + 2 cross-lab
```

#### F9.c — How it slots into the existing machinery

- **`run_fusion_json` / fan-out.** Split the panel into **script seats** (today's path — the
  fusion tab, or the in-process subprocess fallback) and **`claude_cli` seats**. Each
  `claude_cli` seat is its own `run_claude_json(model=…, effort=…, label="fusion-seat:opus-high")`
  — i.e. its own **visible brain tab**, like the judge. Run both groups in parallel, then
  merge into the same normalized answer list `_run_panel` already returns. A **pure-Anthropic
  panel needs no fusion tab and no `ensure_fusion_providers()` at all** — just N seat tabs + 1
  judge tab.
- **Cost.** Each `claude_cli` seat reports `cost = 0.0`, so the existing
  `judge.cost_usd = Σ panel cost` is already correct: a pure-Anthropic panel costs **$0**; a
  hybrid panel's cost is just its external seats. No change to the cost plumbing.
- **`active_providers()` / `is_fusion_available()`.** A `claude_cli` seat has **no key**, so
  the current "key resolves" gate would wrongly mark it inactive. Teach `active_providers()`
  that a `kind == "claude_cli"` seat is active whenever the `claude` binary is on PATH
  (`shutil.which("claude")` — **not** an import of `spawn`, to avoid a config↔spawn cycle).
  Good consequence: **two Opus seats alone satisfy `is_fusion_available()` — Fusion can run
  with zero external keys.**
- **Judge.** Still `run_claude_json(model="opus", …)`. For a pure-Opus panel, set
  **`judge_effort` ABOVE the seats** (e.g. seats `high`+`medium`, judge `xhigh`/`max`) so the
  synthesizer reasons at least as hard as the hardest seat — otherwise the judge (also Opus)
  shares the seats' ceiling.

*Code-shape sketch (target shapes, not extra work — mirrors the F1 block):*

```python
# claude_runner.py — one Anthropic seat = a local claude CLI call, normalized like a panelist
def _anthropic_seat_answer(name: str, prov: dict, prompt: str, cwd: str) -> dict:
    """A kind=claude_cli seat: run_claude_json (visible brain tab), $0, no API.
    Model passed EXPLICITLY (run_claude_json defaults to sonnet). Never raises."""
    run = run_claude_json(prompt=prompt, cwd=cwd or os.getcwd(),
                          model=prov.get("model", "opus"),
                          effort=prov.get("effort", "high"),
                          label=f"fusion-seat:{name}")
    if not run.ok:
        return {"name": name, "ok": False, "error": run.error}
    return {"name": name, "model": run.model or prov.get("model", "opus"),
            "text": run.text, "cost": 0.0, "prompt_tokens": 0,
            "completion_tokens": 0, "ok": True}     # subscription → $0 marginal

# fan-out dispatch: branch by kind BEFORE touching a provider script
#   if prov.get("kind") == "claude_cli":  -> _anthropic_seat_answer(name, prov, prompt, cwd)
#   else:                                 -> _panel_answer(name, prov, prompt, timeout_s)
```

```python
# config.py — a CLI seat needs no key; it's active iff `claude` is installed
import shutil
def _claude_cli_available() -> bool:
    return shutil.which("claude") is not None
# in active_providers():
#   active = _claude_cli_available() if prov.get("kind") == "claude_cli" else bool(_resolve_key(prov))
```

#### F9.d — Compliance: this seat type breaks **zero** hard rules

Unlike the cross-lab seats (which §9 says *relax* "Local only"), an Anthropic CLI seat keeps
**every** hard rule:

- ✅ **No Anthropic API calls** — runs the `claude` CLI on the subscription, like the judge.
- ✅ **Local only / no egress** — a **pure-Anthropic panel sends nothing off the laptop**; §9's
  cross-lab data-egress relaxation simply **does not apply** to it.
- ✅ **Visible, never headless** — each seat is its own watchable brain tab.
- ✅ **Stop hook stays a no-op** — `run_claude_json` already avoids `ORCHESTRATOR_RUN_ID`.

So **`anthropic-local` would be the first Fusion preset that is 100% compliant** — the
multi-model *synthesis* benefit with **none** of §9's egress deviation.

#### F9.e — The cost/diversity tradeoff (the caveat, spelled out)

| Panel | Egress | $ (panel) | Error diversity | Hard rules |
|-------|--------|-----------|-----------------|------------|
| `anthropic-local` — Opus high+medium | **none** | **$0** | **low** (same family, §5) | **all preserved ✓** |
| `hybrid` — Opus-high + DeepSeek + Gemini | 2 labs | low | high | "Local only" relaxed |
| `budget` (today) — DeepSeek+MiniMax+Gemini | 3 labs | pennies | high | "Local only" relaxed |

The honest read: **two Opus seats are not two independent opinions.** They differ only in
thinking budget (plus the CLI's inherent sampling nondeterminism), so they tend to be right
together and **wrong together** — and the judge, also Opus, can't synthesize away a blind spot
all of them share. Recommendations:

- **Best value: use Anthropic seats to *augment* a cross-lab panel** (`hybrid`) — a free
  frontier seat without giving up cross-vendor decorrelation.
- **Pure `anthropic-local`** is for when **compliance/no-egress matters more than diversity**
  (a project whose context must not leave the laptop — exactly §9's "do not enable Fusion"
  case). It's a cheap best-of-N + synthesis that still beats a lone call, just by less than a
  cross-lab panel.
- **Decorrelate what you can:** give each seat a different **lens** (§5 / F8.4 — "find the
  risks" vs. "find the simplest path") via `--append-system-prompt`, and keep the **judge
  effort above the seats'**. Effort spread (`low`↔`max`) is the widest lever the CLI offers.
- **Quota & latency, not dollars:** $0 ≠ free — a `max`-effort pure-Anthropic panel is
  (N seats + judge) Opus calls per dispatch, burning **subscription rate limit** and running
  **slowest-seat-bound** on the heaviest effort. Keep heavy Anthropic panels for high-stakes,
  fire-and-forget `/send` (§7), not the live preview path.

#### F9 — what shipped *(✅ 2026-06-18 — the picker form, not the registry sketch above)*
- **`config.py`** — `claude_cli_available()` (a Claude seat needs no key, only the CLI on PATH,
  via `shutil.which`); `is_fusion_available()` is now true when the CLI is present **OR** ≥2
  external providers are active. No registry `kind:claude_cli` entries — seats are picker-driven.
- **`claude_runner.py`** — `_anthropic_seat_answer()` runs one seat as `run_claude_json(model,
  effort)` (visible brain tab, $0, model passed **explicitly** so it can't downgrade to sonnet).
  `run_fusion_json()` now takes a **mixed `panel`** (a `str` = external provider; a dict
  `{kind:claude_cli,model,effort}` = Claude seat), splits it, fans **both groups out in
  parallel** (providers via the fusion tab; each Claude seat its own brain tab), and bills only
  the external seats (`cost_usd = Σ external`; Claude seats are $0). Duplicate Claude seats are
  kept; usable seats must total ≥2 or it returns `ok=False` (→ `run_brain_json` falls back).
- **`spawn.py`** — a module-level **`_TAB_SPAWN_LOCK`** serializes iTerm2 tab *creation* (the
  osascript moment) across every spawn path, so N concurrent seats can't race AppleScript while
  the per-tab polling still overlaps. *(Resolves the "concurrent brain-tab spawns" open question.)*
- **`app.py`** — `/send` accepts **`fusion_seats`** (a JSON list of `{type:"claude",model,
  effort}` / `{type:"provider",name}`), validating Claude seats against the model/effort
  whitelist (`CLAUDE_SEAT_MODELS`/`CLAUDE_SEAT_EFFORTS`) and provider seats against active keys;
  legacy comma `fusion_panel` is still accepted. `_view_ctx` passes the seat models/efforts to
  the form.
- **`index.html`** — the Fusion picker gained a **"Claude Code seats"** section (add/remove
  rows, each a model + effort `<select>`, defaulting to 2 Opus seats at high+medium) above the
  cross-lab providers, a live seat counter, and JSON-encoded submit. State persists in
  `localStorage`; the toggle stays **default-OFF**.

**Still open / deferred:** judge-effort-above-seats for same-family panels (the judge stays
opus/high by default for now); the F5 per-seat cost breakdown should label Claude seats as
`$0 (subscription)`. The **same-model correlation caveat (F9.e) stands** — prefer a *hybrid*
panel (Claude seats + a cross-lab seat) for real error diversity; a pure-Opus panel trades
diversity for zero egress + zero cost.

---

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
`subprocess` + `concurrent.futures`); each provider is an **isolated stdlib script**
(one lab's breakage can't take down the others); the **Stop hook stays a no-op** (fusion
tabs set their own `ORCHESTRATOR_FUSION_ID`, never `ORCHESTRATOR_RUN_ID`); keys and
config live in `~/.orchestrator/`, never the repo; **no Anthropic API calls** (judge on
the CLI); and **every call stays watchable in iTerm2**.

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

**Dispatch order (one at a time):** F0 (config, no network) → F1 (provider scripts +
`run_fusion_json`) → F2 (rewriter) → F3 (`/send` wiring) → F4 (toggle) → F5
(surface/cost) → F6–F8 (optional).

**Start with ONE provider.** Write `providers/deepseek.py` first (cheapest), wire the
whole panel→judge mechanism, prove F1, then add the rest by **dropping in one script +
one registry line each** (never a core change). Minimum upfront signup to get moving: one.

**Key file targets (absolute):**
- `/Users/tresmith/Documents/orchestrator/orchestrator/lib/config.py` *(new — F0: registry, per-provider keys, presets)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/providers/*.py` *(new — F1: one script per model; repo-canonical templates)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/lib/claude_runner.py` *(F1 — `run_fusion_json`, `_panel_answer`, `_run_panel`, `_run_fusion_in_tab`, `run_brain_json`)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/lib/spawn.py` *(F1 — `spawn_fusion_tab`, `ensure_fusion_runner`; mirror `spawn_brain_tab`/`brain_run.sh`)*
- `~/.orchestrator/bin/fusion_run.sh` + `fusion_call.py` + `providers/*.py` *(F1 — the visible-tab runner + per-model scripts, materialized by `ensure_fusion_runner()`)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/lib/rewriter.py` *(F2)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/lib/fusion.py` *(new — F7, enrichment mode)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/app.py` *(`/send`, `_send_in_background`, `_view_ctx` — F3/F4/F5)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/templates/index.html` *(toggle — F4)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/templates/dispatch.html` + `_runs.html` *(surfacing — F5)*
- `/Users/tresmith/Documents/orchestrator/bin/install.sh` *(config.json registry template — F0)*
- `~/.orchestrator/config.json` *(runtime data — holds each `<provider>` `api_key`, never in repo)*

**Reuse / consistency:**
- **One script per provider — no shared client, no adapter.** Each `providers/<name>.py`
  owns its lab's native API and emits the normalized `{ok,text,prompt_tokens,
  completion_tokens,model,error}`. OpenAI-shaped labs share a near-identical script;
  outliers (MiniMax) just differ inside their own file.
- **Everything visible — no hidden/headless calls.** Panel → visible `fusion` tab
  (`spawn_fusion_tab` → `fusion_run.sh` → `fusion_call.py` → the scripts); judge →
  visible `brain` tab (`run_claude_json`). In-process subprocess fan-out is a fallback
  only when iTerm2 is absent.
- Mirror `embeddings.py` for the HTTP inside each script: stdlib `urllib.request`, never
  raise, emit `ok=false` on any failure. **No `httpx`/`requests`.**
- Reuse `claude_runner._strip_fences` for the judge's JSON — don't re-invent it.
- Mirror the `rewrite_event` recording pattern for any `fusion_event`.
- Provider base URLs, native model slugs, and request shapes (§6) are a 2026-06-17
  snapshot — **verify live before writing each script**. Prefer config-driven
  registry/presets so a model/price swap is a config edit.
- **CLAUDE.md is stale on one point:** its hard rule still says brain work goes through
  *headless* subprocesses, but the code uses **visible iTerm2 tabs**. Update that wording
  when Fusion ships (the `## Fusion` note in F8 is a good spot).
- **Edits don't take effect until you restart `python -m orchestrator`** (uvicorn
  `reload=False` on :7878), and the **auto-push daemon commits within seconds** —
  `git diff` won't show your changes.

---

## 10. Design study — mid-flight executor watcher *(DESIGN ONLY — not approved, not built)*

**The question.** Could the Fusion panel help **long-context executor** dispatches by
periodically inspecting an *in-progress* `claude` executor session — surfacing blind
spots, missing work, and errors as mid-flight critique? Three sub-verdicts asked for:
**would it help? would it cost too many tokens? is it a good idea?** This section answers
all three, grounded in what the CLI *actually* exposes (verified, not assumed), and ends
with an open-questions list. **No executor-watching code is written here.**

It is the natural generalization of the summarizer (§F6): `summarizer.py` distills the
transcript JSONL (`distill_transcript`, caps `PER_BLOCK_MAX=1.5KB` / `DISTILLED_MAX_CHARS=30KB`)
and fires **one** panel/brain call **after** Stop. A watcher fires that *same* call
**every N tool-calls or M minutes, while the executor runs.** Everything below reuses
that machinery rather than inventing new instrumentation.

### 10.a — What the CLI actually exposes (verified `claude --help`, 2026-06-18)

The central unverified claim — *can you observe or inject into a RUNNING session?* — was
tested against the live binary, not assumed (the prior `--effort`-list lesson applies):

| Capability | Flag | Available? | Implication for a watcher |
|------------|------|-----------|---------------------------|
| Attach to / inject into an **already-running turn** | — | ❌ **none exists** | **Live steering is impossible.** No flag pushes a message into the executor's in-flight turn. |
| Resume a session **after its turn ends** | `-r/--resume <id>`, `-c/--continue`, `--session-id <uuid>` | ✅ yes | Enables a **pause-then-resume gate** only — a *new* user turn, not mid-turn injection. |
| Fork on resume | `--fork-session` | ✅ yes | A resumed critique could branch instead of mutating the original session. |
| Realtime **input** streaming | `--input-format stream-json` | ✅ yes | Streams into a session **you start**, not one already spawned — does **not** reach the live executor. |
| Interactive remote control | `--remote-control [name]` | ✅ exists | Interactive, not a headless brain call; using it to steer would be exactly the **live-steering scope-creep** line we should not cross (see 10.e). |
| Read the live transcript | (filesystem) | ✅ yes | `~/.claude/projects/<slug>/<session>.jsonl` is append-only; the [orphan-recovery] path already proves sessions live there. This is the only live feed. |

**Confirmed conclusion:** a critique **cannot be injected into the running executor.**
It must land in one of exactly two ways:

1. **Non-blocking advisory** — surface to the orchestrator (DB / ring-buffer / HTMX side
   panel). The executor never sees it mid-run; a human reads it. *Safe, but the executor
   may never act on it.*
2. **Pause-and-resume gate** — let the current turn finish (or kill it), then
   `claude --resume <session_id>` with the critique as a new user turn. *Actionable, but
   interrupts a long-context session and risks the executor "chasing the panel."*

There is **no third option** where the panel quietly corrects a running turn. Any design
claiming otherwise is wrong about the CLI.

### 10.b — Cost: concrete tokens → USD (free CLI seats vs paid external seats)

Prices are the §6 snapshot ($/M tokens, in→out): deepseek 0.44/0.87 · xai 1.25/2.50 ·
gemini 0.30/1.50 · minimax 0.30/1.20 · glm 1.40/4.40 · qwen 1.25/3.75. As everywhere in
Fusion, **`cost_usd = Σ external seat cost only**; **Claude-CLI seats and the judge are
$0** (subscription, no Anthropic API).

**Per-checkpoint token model.** IN = distilled transcript-**delta** since the last
checkpoint (bounded by the summarizer caps) ≈ **~4K tokens**; OUT = the structured
critique ≈ **~1K tokens**. (The judge is a *second* round-trip — judge IN ≈ original
prompt + N panel answers, OUT ≈ 1K — but the judge is the local CLI, so it adds **latency,
not USD**.)

**Per-checkpoint external cost**, `balanced` preset (deepseek + xai + qwen), 4K in / 1K out each:

| seat | in cost | out cost | seat total |
|------|--------:|---------:|-----------:|
| deepseek | $0.00176 | $0.00087 | $0.0026 |
| xai      | $0.00500 | $0.00250 | $0.0075 |
| qwen     | $0.00500 | $0.00375 | $0.0088 |
| **per checkpoint** | | | **≈ $0.019** |

**Checkpoints per 30-min dispatch** (the default `wall_clock_cap_s=1800`): a busy executor
at ~5 tool-calls/min over 30 min ≈ 150 calls. Fire every N=20 calls → **~7–8 checkpoints**;
or a 5-min wall timer → **6 checkpoints**. So:

- `balanced` panel: 8 × $0.019 ≈ **$0.15 / dispatch.**
- `max` panel (6 seats incl. glm/gemini/minimax): ≈ **$0.30–0.45 / dispatch.**
- **pure Claude-CLI panel (e.g. `anthropic-local` = opus-high + opus-medium): $0** — the
  only out-of-pocket "cost" is shared subscription rate-limit + wall-clock.

**This compounds, and the naive read is ~quadratic.** If each checkpoint re-reads the
*whole growing transcript* instead of the delta, checkpoint *k* reads ≈ *k·δ* tokens; over
*C* checkpoints total IN ≈ δ·C(C+1)/2 = **O(C²)**. With a per-run **byte-offset cursor**
(stored in `~/.orchestrator`, append-only JSONL → no diff machinery) each checkpoint reads
δ **once**: total IN = δ·C = **O(C)**.

> Worked numbers (δ = 4K, C = 8): **cursor → 32K IN total; full re-read → 4K·36 = 144K IN
> total (≈4.5×), and the gap widens the longer the run.** The cursor + the existing
> 1.5KB/30KB caps are mandatory, not optional.

**Verdict on cost:** a **paid** watcher is affordable per-dispatch ($0.15–0.45) but
**multiplies** the single post-run summary many times over and scales with run length and
with concurrent executors. A **Claude-CLI-only** watcher is **$0** and is the only
economically sane default. The real "too many tokens?" risk for the free panel is **shared
subscription throttling**: checkpoints draw on the **same** subscription as the executor
and could rate-limit the very session they watch — a self-defeating failure mode.

### 10.c — Trigger / hook designs (what it RECEIVES, what it EMITS, where it lands)

Two concrete triggers, both reusing existing plumbing — no new executor instrumentation:

- **(A) N-tool-call piggyback (recommended).** The `PreToolUse` hook
  (`bin/notify_tool_use.sh`) already POSTs `{run_id, tool_name, input_hash}` to
  `/api/tool_use` **per call**. `/api/tool_use` counts per `run_id`; every Nth call (config
  `checkpoint_every_n`) fires a checkpoint. A tool-call boundary is a **cleaner snapshot**
  than a raw wall timer that can fire mid-tool-call. This is the same feed the planned loop
  watchdog uses — zero new hooks.
- **(B) Stuck-signal / milestone gate (cheapest, highest-value).** Instead of a blind
  cadence, fire **only when something looks wrong** — the loop-watchdog ring buffer shows
  repeated identical `input_hash`es (executor spinning). Spend tokens only on a stuck
  signal. If panel+judge latency exceeds inter-tool-call gaps (likely), a blind interval
  lands **stale** critique anyway, making (B) strictly better than a dumb timer.

A wall-clock interval (every M minutes) is the fallback when no tool-call feed is desired,
but it shares the staleness problem and can fire mid-tool-call.

- **RECEIVES:** the distilled transcript **delta** since the cursor (`distill_transcript`
  pointed at the executor's live JSONL, bounded by the caps) + optionally a short rolling
  summary of prior critiques (a **hidden cumulative token cost** — keep it to ≤1KB).
- **EMITS:** a structured critique `{blind_spots[], missing_work[], errors[], confidence}`
  → **non-blocking sink only:** an `outcomes`-style / `checkpoint_event` row + the dispatch
  HTMX side panel. Default sink is **advisory (mode 1)**; the pause-resume gate (mode 2) is
  a separate, louder opt-in.

### 10.d — The structural problem unique to reviewing *in-progress* work

Reviewing a partial transcript has a **built-in false-positive rate**: "missing work" and
"errors" are frequently just **"not done yet."** A panel handed a half-finished transcript
will hallucinate gaps the executor was about to fill, inflating noise. This is the single
biggest reason the feature may not pay off — and the reason the critique schema must carry
a **confidence/"may-be-incomplete" flag** and the prompt must say *"this transcript is a
snapshot of work in progress; do not flag as missing what may simply be unfinished."*

It also weakens Fusion's core premise here: a panel given a distilled **partial** transcript
may add little over a **single** Claude summarizer call — multi-model diversity helps most
on hard, complete judgments, least on "is this half-done run on track?" **Honest flag:
multi-model may not beat one-model for incremental critique.**

### 10.e — Rollout & hard-rule compliance (re-justified per component)

- **Default-off, opt-in, per-dispatch** — like every Fusion path. A `watch_executor`
  checkbox; off → byte-for-byte today's behavior.
- **No Anthropic API.** Panel = external scripts + local CLI seats; judge = local CLI.
  Same as §9.1. ✅
- **Visible, never headless.** Every checkpoint's seats + judge run in **watchable iTerm2
  tabs** via the existing `run_fusion_json` / `run_claude_json`. ⚠️ **Tab-storm caveat:** N
  checkpoints × (seats + judge) tabs, created under `spawn.py`'s serialization lock — which
  the executor's own spawns also contend on. A **single reused watcher tab** would be
  cleaner, but `run_brain_json`/`run_fusion_json` **don't currently support tab reuse**, so
  today this means real tab churn. Open question below.
- **Local only.** Same relaxation as §9.2 (external seats see the transcript delta) — and
  **wider**: the delta now leaves the laptop *repeatedly* per dispatch, not once. A
  Claude-CLI-only panel keeps it 100% local.
- **Stop-hook gate preserved.** Checkpoint brain calls must **scrub `ORCHESTRATOR_RUN_ID`**
  exactly like `run_claude_json`/`run_claude_headless` already do — otherwise each checkpoint
  fires the Stop hook and **pollutes `/api/complete`**. The watcher reads the executor's
  `run_id` to find its transcript, but must never *propagate* it into the panel env.
- **Killable / kill-all / wall-clock.** Global kill-all and the 1800s cap must **also
  terminate in-flight checkpoint tabs/panels**, or a killed dispatch orphans brain tabs. If
  kill-all fires mid-round-trip, an external seat may **still be billed** (open question).
- **Self-terminate on completion.** Race: the executor may hit Stop (→ summarizer) while a
  checkpoint is still running — overlapping panel calls on the same transcript. The watcher
  must stop on completion/death to avoid orphan loops.
- **The live-steering line.** Surfacing critique that a human then **manually pastes** into
  the executor is a back-door form of live steering. The design draws the line at: advisory
  sink (mode 1) is in-scope; automated injection is **not** without an explicit mode-2
  pause-resume gate; `--remote-control` steering is **out of scope** entirely.

### 10.f — Go / No-Go

- **Would it help?** **Partially / conditionally.** Real value only with a **stuck-signal
  gate (10.c-B)** and a confidence-flagged schema (10.d). A blind-cadence advisory watcher
  mostly produces stale, "not-done-yet" noise. Net: **qualified yes for the milestone-gate
  variant, no for the naive interval variant.**
- **Would it cost too many tokens?** **Not with a Claude-CLI-only panel ($0) + delta cursor
  + caps.** A **paid** panel is affordable per-dispatch ($0.15–0.45) but compounds across
  checkpoints, runs, and concurrent executors, and risks subscription throttling. **So: no
  if free-seat default; watch out if paid.**
- **Is it a good idea?** **Not yet — build the milestone-gate, Claude-CLI-only, advisory
  (mode-1) version behind a default-off flag *only if* the post-run summarizer proves
  insufficient in practice.** The partial-transcript false-positive problem and the unproven
  multi-model-over-single-model edge make this **lower priority than finishing F8.4 / live
  provider verify.** **Provisional: defer; do not build now.**

### 10.g — OPEN QUESTIONS *(resolve before any implementation)*

1. **Does the executor flush its transcript JSONL incrementally mid-run, or only at
   completion?** If only at completion, the watcher sees nothing and the whole feature is
   dead. *Not verified here — must be confirmed empirically against a live dispatch.* (The
   `PreToolUse` hook proves *events* are observable live; JSONL flush cadence is separate.)
2. **Partial-line tolerance:** reading the JSONL while `claude` appends can yield a
   truncated final line — does `distill_transcript`'s defensive parse + a cursor that
   rewinds to the last newline fully cover this?
3. **Does the panel's partial-transcript critique actually beat a single Claude
   summarizer call?** If not, drop the panel and use one CLI call (or drop the feature).
4. **Tab reuse:** is it worth teaching `run_brain_json`/`run_fusion_json` a reusable
   watcher tab to avoid N-checkpoint tab storms under `spawn.py`'s lock?
5. **Kill-all mid-round-trip billing:** if a dispatch is killed while an external seat is
   in flight, is that seat still billed — and how is its tab reaped?
6. **Concurrency:** multiple simultaneous executors each spawn their own watcher —
   multiplying cost, tabs, and subscription load. Cap total concurrent checkpoints?
7. **Cadence:** N tool-calls vs stuck-signal gate vs wall timer — which default? (10.c
   argues stuck-signal.)
8. **Mode-2 gate:** is a pause-and-resume (`claude --resume`) gate ever wanted, or is
   advisory-only the permanent ceiling to avoid the executor chasing the panel?
9. **Cursor location & lifecycle:** confirm `~/.orchestrator/<run_id>.cursor` (byte offset)
   is the right home and is cleaned up on completion/kill.

**STOP — design only. No executor-watching code written or approved.**

## 11. Design study — pushing the Fusion panel to full potential *(DESIGN ONLY — not approved, not built)*

**The question.** The panel today is a *single* round — fan out to N seats in parallel, then ONE
judge synthesizes (`run_fusion_json` → `_panel_answers` → `_judge_prompt`). What concrete steps —
extra rounds, more lenses, verifier/voting/routing seats — would push it toward its full potential,
and what does each cost? This section mines Abacus AI's multi-model products for mechanisms,
re-grounds each in the *existing* seam, ranks them under an explicit weighting, and ends with open
questions. **No Fusion code is written or changed here.**

Two facts shape everything below, both **verified against the live code/CLI 2026-06-21**:

- **The judge sees only the original prompt verbatim + the seat answers** (`_judge_prompt`) — never
  the lensed seat prompts, never a prior round. Any multi-round design must *define a new
  information-flow contract*, because none exists today.
- **The Claude CLI has no sampling knobs.** `claude --help` exposes `--effort {low,medium,high,xhigh,max}`
  and `--model`, but **no `--temperature`, `--top-p`, or `--seed`**. For the free Claude seats,
  **effort + lens are the ONLY decorrelation levers** — re-running identical Claude seats manufactures
  no diversity.

### 11.a — Abacus AI mechanisms (documented vs. marketing vs. black-box)

Abacus's multi-model story splits into two products, and **its own docs keep the internal algorithms
a black box** — so the honest extraction is "shapes, not internals":

- **RouteLLM (ChatLLM / RouteLLM API) — routing, not ensembling.** A single OpenAI-compatible
  endpoint over 50+ models that "intelligently routes to the best available model based on the
  complexity of the request" ([RouteLLM API reference](https://abacus.ai/help/developer-platform/route-llm/),
  [routellm-apis.abacus.ai](https://routellm-apis.abacus.ai/)). You either let the `route-llm`
  identifier auto-route or **force a model by name**; marketing adds "automatic failover for
  closed-source models" ([Abacus.AI on X](https://x.com/abacusai/status/1986111621845172386)). A
  review describes the router "evaluat[ing] query characteristics in real-time … token length,
  inference speed, or creative output"
  ([KDnuggets ChatLLM review](https://www.kdnuggets.com/2026/03/abacus/chatllm-all-in-one-ai-platform-review)).
  **Crucially, Abacus's own RouteLLM reference states the routing algorithm is *not disclosed*** — no
  documented voting, ensembling, or failover internals. The flagship mechanism is **pick ONE model**,
  the opposite of "ensemble many then judge."
- **DeepAgent — debate & self-reflection (mechanism undisclosed).** Abacus's autonomous agent
  ([github.com/abacusai/DeepAgent](https://github.com/abacusai/DeepAgent),
  [deepagent.abacus.ai](https://deepagent.abacus.ai/)) is marketed with "expert dialogues" that
  **orchestrate multi-persona debate** and multi-agent **self-reflection** where agents "correct their
  peers' mistakes," reporting 74% on SWE-bench Verified. But the
  [DeepAgent review](https://blog.abacus.ai/blog/2025/10/23/deepagent-review/) confirms the internal
  coordination is **not specified** ("integrates multiple AI models" is stated only for multimedia
  generation). So debate/self-reflection is a *documented shape* with an *undisclosed mechanism* — we
  design the internals ourselves.
- **Eval-driven selection (the disciplined external version).** Building "an Abacus-style app" is
  described as A/B'ing selection policies — route 5–10% of traffic to an experimental policy and
  measure **quality-per-cost**
  ([.NET guide](https://medium.com/@bergamo.gustavo/a-net-developers-guide-to-building-an-abacus-ai-style-4bde9794ddc8)).
  The LLM-as-judge literature adds **judge-ensembling** with a sharp caveat: aggregating *weak* judges
  helps slightly, while aggregating *strong* judges can show **no improvement**
  ([debate-judge benchmark](https://arxiv.org/pdf/2506.05062), [UniCBE](https://arxiv.org/pdf/2502.11454))
  — directly relevant to a judge-of-judges idea on one strong family (11.c.7). *(These are non-Abacus
  sources informing the design where Abacus is opaque; exact attribution of the ensembling finding is
  itself an open question, 11.g-7.)*

**Strategic takeaway.** Abacus's flagship optimizes the *opposite* objective to ours: RouteLLM routes
to one *cheaper* model to cut paid-API spend, whereas here **Claude seats are $0 and external seats are
paid.** A faithful copy would *shrink* the panel. The transferable move is the **inversion** — *prefer
the free Claude seats, escalate to paid external only when a cheap signal says it pays* (11.c.2) — the
one Abacus-derived idea that serves "full potential" *and* lowers cost.

### 11.b — The seam as it exists (cost & latency model, stated once)

So the per-proposal RISK lines stay terse, the ground truth (verified in `run_fusion_json` /
`_panel_answers` / `spawn.py`):

- **Out-of-pocket:** `cost_usd = Σ external-seat token cost only` (`judge.cost_usd = sum(a.get("cost",0.0) for a in ok)`).
  Claude seats and the judge are **$0 out-of-pocket** — but **not free**: each draws on the shared
  Claude subscription (rate-limit/throttle), adds wall-clock, and opens an iTerm2 tab. A RISK line that
  calls a Claude seat "$0" means *out-of-pocket only.*
- **Latency, one round:** seats fan out in parallel (`_panel_answers`/`_run_panel`/the futures in
  `run_fusion_json`), so a round ≈ **slowest seat**, NOT the sum; then the judge runs **serially** after
  (`run_claude_json`). One fusion ≈ slowest-seat + judge.
- **Tab-spawn serialization:** `spawn.py` holds `_TAB_SPAWN_LOCK` (verified) around tab creation, so S
  seats serialize at the *spawn moment* (~hundreds of ms each), then their runs overlap. Many seats add
  spawn-serial latency even though the runs are parallel.
- **Extra rounds serialize:** a 2nd round can't start until round 1 (+ optionally its judge) finishes →
  ~2× wall-clock. Parallel *width* is cheap; sequential *depth* is not.
- **The 30-min wall-clock cap** (default `wall_clock_cap_s=1800`) bounds the whole dispatch — any
  multi-round loop needs an explicit round cap + aggregate timeout.
- **Two-file `_apply_lens` duplication tax:** `_apply_lens` is **byte-for-byte in BOTH**
  `claude_runner.py` and `fusion_call.py` (the tab runner can't import the package). Any change to how
  *external* seats build a round-2 prompt must be made in **both** files; Claude-only rounds touch only
  `claude_runner.py`.

### 11.c — Enhancement proposals, ranked

Ranked under the explicit weighting in 11.f (out-of-pocket $, then wall-clock, then tab pressure, then
hard-rule contact). Each carries a **RISK** line.

| # | Proposal | Abacus root | $ | Wall-clock | Tabs | Hard-rule |
|---|----------|-------------|---|-----------|------|-----------|
| 1 | Verifier / critic seat (Claude) | judge-ensembling / self-reflection | $0 | +1 serial Claude round | +1–2 | none |
| 2 | Claude-first cost-aware escalation | RouteLLM (inverted) | **saves $** | +1 serial stage *on escalation only* | base: fewer | none (router must be local) |
| 3 | Genuinely-decorrelated lenses | judge-ensembling diversity | $0 | none (relabel) | none (relabel) | none |
| 4 | Parallel self-consistency (extra Claude seats) | ensembling / voting | $0 | ~none (parallel) | +K peak | none |
| 5 | Debate / cross-examination round | DeepAgent debate | ~2× external | ~2× (serial) | 2× seats | none* (injection surface) |
| 6 | Task-type routing via local outcome logs | eval-driven selection | $0 | negligible | none | none (logs, not hosted eval) |
| 7 | Judge-of-judges / meta-judge | judge-ensembling | $0 | +1 serial round | +2–3 | none |

**11.c.1 — Verifier / critic seat (rank 1).** After the judge returns, run ONE more Claude CLI call
(`run_claude_json`, opus/high) as an adversarial *verifier*: input = original prompt + the judge's
synthesis (+ optionally the panel answers), task = "find anything the synthesis gets wrong, omits, or
over-claims; if clean, say so." Either surface its critique alongside the result, or gate a single
re-judge when it finds a defect. Mirrors DeepAgent "self-reflection" and judge-ensembling, but as a
*checker*, not a second synthesizer. Seam: a thin wrapper after the `judge = run_claude_json(...)` line
in `run_fusion_json`; no panel/provider changes; no `fusion_call.py` touch.
**RISK —** $0 out-of-pocket (Claude-only). Wall-clock: +1 serial Claude round (+1 more if re-judge
fires) — the cost here is *latency*, which `cost_usd` does not show, so a "$0" tag hides a real
wall-clock add. Tabs: +1 (+1). Hard rule: none. Limit: the verifier shares Claude's model family → it
inherits the judge's blind spots (can't catch an error the whole family makes), and a single wrong
verifier can trigger a needless re-judge.

**11.c.2 — Claude-first cost-aware escalation (rank 2).** RouteLLM with the economics inverted. Stage 1:
a **free Claude-only panel** (2–3 effort/lens-differentiated seats) + judge. Stage 2: only if a *cheap
local signal* says it's worth it — the judge self-reports low confidence, or the Claude seats visibly
disagree — spawn the **paid external** preset and re-judge with the full set. Most dispatches finish at
$0; external spend happens only where it pays. Seam: wraps `run_fusion_json` in a two-call gate;
reuses the existing preset machinery (`FUSION_PRESETS_SEED`) for the stage-2 set. **The router MUST be a
local heuristic or a visible Claude CLI call — never a hosted router** (RouteLLM-the-service is OUT OF
SCOPE, 11.d).
**RISK —** $: **net savings** vs. always-on external — the only proposal that *reduces* spend.
Wall-clock: base case unchanged; +1 serial stage on escalation. Tabs: base case fewer than today's
external presets. Hard rule: none **iff** the router stays local; an auto-router that POSTs to a hosted
model-picker would break Local-only + No-hidden-HTTP. Limit: needs a trustworthy "is this hard?" signal
— the CLI emits no confidence (no logprobs), so it must be judge-elicited or disagreement-based, which
can misfire.

**11.c.3 — Genuinely-decorrelated lenses (rank 3).** ✅ *(shipped 2026-06-22)* `FUSION_LENSES_SEED`
grew from 3 (risks, simplest, ambiguity) to 10 — added `first-principles` (reject the framing,
re-derive), `user-intent` (the goal behind the literal request), `long-horizon` (future-change cost /
lock-in, *not* present minimalism — that's `simplest`'s axis), `concrete` (force the runnable
artifact), `adversary` (red-team the *committed* answer — sharpened vs. `risks`, the lens it sits
closest to), `precedent` (reuse prior art — the literal inverse of `first-principles`), and `evidence`
(distrust the *facts*, vs. `adversary`'s distrust of the *design*). Each attacks a *different* failure
axis, not a synonym; `completeness`/`what's-missing` was considered and **declined** as too correlated
with `ambiguity` + `risks`. Decorrelation — *not lens count* — is the lever; overlapping lenses add
tabs/latency without adding diversity. Seam was exactly as predicted: pure config (`config.py`
`FUSION_LENSES_SEED` / `fusion.lenses`), resolved by the existing `resolve_lens` + `_apply_lens`; no
logic change.
**RISK —** $0 (prompt strings only; no new seats if lenses just relabel existing ones). Wall-clock: none
when relabeling existing seats; +slowest-seat only if lenses *add* seats. Tabs: none (relabel) / +1 per
added seat. Hard rule: none. Limit: for Claude seats a lens is the *only* diversity knob (no
temperature/seed), and all Claude seats share one model family — lenses reduce but cannot eliminate a
shared blind spot; a too-strong lens can pull a seat off-task (mitigated by `_apply_lens` keeping the
original prompt verbatim and last).

**11.c.4 — Parallel self-consistency, extra Claude seats (rank 4).** The *self-consistency* reading of
"run the panel twice" (NOT the debate reading — see 11.c.5). Add K extra **Claude** seats (varied
effort/lens) in the *same parallel round*, then let the judge weigh agreement ("where the seats
converge, trust it; where they split, reason it out"). Because the extra seats ride the existing
parallel fan-out, they add **~no wall-clock** (only tab-spawn-serial time + a tab each). Seam: just a
longer `panel` list of `{"kind":"claude_cli",...}` seats — no new code path; `_anthropic_seat_answer`
already allows duplicates.
**RISK —** $0 (Claude seats). Wall-clock: ~none — extra seats are parallel (the cheap-on-latency option,
unlike 11.c.1/.5/.7). Tabs: **+K concurrent** — the peak-tab-pressure proposal; max preset + K extra can
mean a dozen+ tabs at once under the spawn lock. Hard rule: none. Limit: no true confidence signal (no
logprobs → "voting" is judge-elicited, not measured); correlated Claude seats can agree *and be wrong
together* — self-consistency across one family can't break a shared blind spot. External seats would
decorrelate better but cost $.

**11.c.5 — Debate / cross-examination round (rank 5).** The DeepAgent "expert dialogue / peer-correction"
shape: after round 1, give each seat the OTHER seats' answers and ask it to critique and revise; then
the judge synthesizes the *revised* set. This is the *refinement* reading of "run the panel twice" —
distinct from 11.c.4's identical re-run. It requires the new info-flow contract flagged up top: **round-2
seats see round-1 answers** (today only the judge does). Seam: a round-2 prompt builder + a second
`_panel_answers`/seat fan-out in `run_fusion_json`, then `_judge_prompt` over revised answers;
**external-seat round-2 prompts must be built in both `claude_runner.py` AND `fusion_call.py`** (the
duplication tax, 11.b).
**RISK —** $: **~2× external** (every external seat runs twice); Claude seats stay $0. Wall-clock: **~2×**
— round 2 serializes behind round 1's slowest seat, then itself ≈ slowest seat, then judge; the closest
of all proposals to the 30-min cap. Tabs: 2× seat tabs + judge. Hard rule: none broken (revised text
stays on the laptop) — **but two real hazards:** (a) feeding external-model text back into a later
seat/judge is a **prompt-injection surface** (untrusted text now steers a second round), and (b) **debate
herds** — seats converge toward consensus, *reducing* the very decorrelation lenses buy; more steps can
lower diversity. Needs a hard cap of exactly one extra round (no unbounded loop).

**11.c.6 — Task-type routing via local outcome logs (rank 6).** Abacus's eval-driven selection done
*locally*: instead of a fixed default preset, pick the preset + lens assignment from the orchestrator's
**own past-outcome logs** (its stated learning loop) by task type — "refactors did best on `balanced` +
risks/simplest," etc. No hosted eval service; a local lookup/heuristic over the `outcomes` table. Seam:
a selection step *before* `run_fusion_json` that sets `preset`/`panel`; the fusion call itself is
unchanged.
**RISK —** $0 (local lookup; no model call required). Wall-clock: negligible. Tabs: none extra. Hard
rule: none **iff** selection reads local logs — Abacus's *hosted* eval/A-B platform is OUT OF SCOPE
(11.d). Limit (the big one): eval-driven selection presumes **labeled ground-truth the orchestrator does
not have** — "which past dispatch was *better*?" is unscored today, so this quietly smuggles in a
benchmark/scoring build effort. Speculative until outcomes are scored (11.g-6).

**11.c.7 — Judge-of-judges / meta-judge (rank 7).** Run 2–3 independent judges (different effort/lens)
over the same panel answers, then a meta-judge merges/picks. Mirrors judge-ensembling. Seam: call
`_judge_prompt` → `run_claude_json` N times, then one more meta-judge call; all in `run_fusion_json`, no
panel change.
**RISK —** $0 (Claude). Wall-clock: +1 serial round for the meta-judge (the N base judges parallelize,
but the meta waits on all). Tabs: +2–3 judge tabs + meta. Hard rule: none. Limit (why it ranks last):
the LLM-as-judge literature finds **ensembling *strong* judges can yield no improvement** (it helps
mostly for weak judges), and all our judges are the same strong family — so the expected uplift is
smallest here while latency/tab cost is among the highest. Cost-risk ($0) and latency-risk (high) point
in *opposite* directions.

### 11.d — OUT OF SCOPE (hosted / hidden-HTTP — would break the hard rules)

- **RouteLLM as a service** (the unified OpenAI-compatible router over 50+ models behind one hosted
  endpoint): a hosted router + hidden HTTP → violates **Local-only** and **No hidden HTTP**. We borrow
  only the *idea* (11.c.2/.6) as a local heuristic or a visible Claude CLI call. *The "OpenRouter fusion
  api is what we copied" premise is **stale** — Fusion already dropped OpenRouter for direct providers;
  do **not** reintroduce any hosted router.*
- **Abacus hosted eval / A-B platform** (quality-per-cost selection as a service): hosted → OUT. Local
  analogue = 11.c.6 over our own logs.
- **DeepAgent cloud agents / SuperAgent:** hosted multi-agent service → OUT.
- **Any auto-router that calls an external endpoint to choose the model:** OUT, however cheap — the call
  must be local or a visible tab.

### 11.e — Cross-cutting risks (apply to every multi-round / multi-seat proposal)

- **Tab storm.** max preset (6 seats) × extra rounds × verifier/judge can spawn a dozen-plus iTerm2 tabs,
  all serializing at creation under `_TAB_SPAWN_LOCK` (shared with the executor's own spawns). A real
  UX/resource ceiling; `run_fusion_json`/`run_claude_json` have **no tab reuse** today.
- **Wall-clock cap.** Every depth-adding proposal (verifier, debate, meta-judge, escalation stage) eats
  the 1800s dispatch cap; multi-round needs an explicit round cap + aggregate timeout.
- **Subscription throttle.** "$0" Claude seats still draw the *same* subscription as the dispatch
  executor; piling on free seats/judges can rate-limit the very work they support.
- **Prompt-injection surface.** Any proposal that feeds external-model text into a *later* round or judge
  (11.c.5 especially) widens the untrusted-input path; today only the single judge ingests external text,
  once.
- **Two-file `_apply_lens` duplication.** Round/lens logic touching *external* seats must change both
  `claude_runner.py` and `fusion_call.py`; Claude-only steps avoid this.
- **The "$0 ≠ free" trap.** `cost_usd` counts external tokens only; for Claude-only proposals the real
  cost is wall-clock + tabs + throttle, which that number hides. Rank Claude-only steps on *latency*, not
  dollars.

### 11.f — Ranking rationale (the explicit weighting)

"Ranked" needs a stated objective, because cost-rank, latency-rank, and quality-rank disagree. The
ordering above weights, in order: **(1) out-of-pocket $ (lower = better; $0 Claude beats paid external),
(2) wall-clock added (parallel width is cheap, serial depth is not), (3) tab pressure, (4) hard-rule
contact (any contact heavily penalized).** Under that weighting the cheap, rule-safe, low-latency wins
(verifier, Claude-first escalation, better lenses) rank above the deep/expensive ones (debate,
meta-judge). A **quality-first** weighting would lift debate (11.c.5) — genuine cross-model refinement —
toward the top despite its 2× cost; that is the main trade-off to decide. **None of these is proposed as
a default; Fusion stays opt-in / default-off — this is a menu, not a switch.**

### 11.g — OPEN QUESTIONS *(resolve before any implementation)*

1. **Does any of this beat one more Claude seat?** The §10.d caution recurs: multi-model diversity helps
   most on hard, complete judgments. Does a verifier/debate round measurably beat simply adding one
   Claude seat to the panel? Unproven — needs an A/B on real dispatches (which presumes scoring; see Q6).
2. **What is the escalation signal (11.c.2)?** The CLI emits no confidence/logprobs. Is judge-elicited
   self-confidence trustworthy enough to gate paid external seats, or does disagreement-among-Claude-seats
   work better? Either can misfire.
3. **Debate info-flow contract (11.c.5):** do round-2 seats see *all* peers' answers or only a summary?
   Does the judge see the debate or only the final revised answers? Undefined today.
4. **Does debate herd?** Measure whether a cross-examination round *increases* answer quality or merely
   *converges* the seats (lowering the decorrelation that justified the panel).
5. **Tab reuse:** worth teaching `run_fusion_json`/`run_claude_json` a reusable tab to survive the
   tab-storm (11.e), or cap concurrent seats instead? (Same open question as §10.g-4, now sharper with
   extra rounds.)
6. **Scoring for eval-driven routing (11.c.6):** the `outcomes` table is unscored — what cheap signal
   labels a past dispatch "better"? Without it, task-type routing is speculative.
7. **Judge-ensembling uplift (11.c.7):** does the "strong judges ⇒ no improvement" finding from the
   LLM-as-judge literature hold for our single-family judges, making meta-judging pure cost? Confirm
   before building. *(Exact source attribution for that finding is itself unverified here — treat as a
   hypothesis, not a settled result.)*
8. **Kill-all / billing mid-round:** a 2nd external round doubles the window in which a kill-all can
   leave an external seat billed-but-orphaned (same hazard as §10.g-5, now twice).
9. **Where does multi-round state live?** Round-1 answers must reach round 2 without an Anthropic API
   call and without leaving the laptop — confirm the request-file/sidecar path (`fusion_call.py` ↔
   `_run_fusion_in_tab`) carries them cleanly.

**STOP — design only. No Fusion-enhancement code written or approved.**
