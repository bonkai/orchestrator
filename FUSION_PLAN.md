# Orchestrator — Fusion Integration Plan

Adding **OpenRouter Fusion** as an *optional, opt-in* multi-model "brain" layer.
This is a phased implementation plan, not a built feature. When the Fusion
toggle is **off**, behavior is byte-for-byte identical to today.

> ⚠️ **Hard-rule deviation, stated up front.** CLAUDE.md says *"No Anthropic API
> calls — all brain work goes through headless `claude` subprocesses"* and
> *"Local only. No remote workers, no hosted services."* Fusion makes outbound
> HTTPS calls to **OpenRouter's servers**, and the prompt (which includes the
> project bundle) leaves the laptop. This plan **intentionally relaxes both
> rules**, but only behind a default-off toggle that falls back to the local
> path automatically.
>
> **On "headless":** the code already moved brain calls from headless subprocesses
> to **visible iTerm2 tabs** (`run_claude_json`) — so CLAUDE.md's "headless" wording
> is itself stale, and **Fusion runs in a watchable tab too** (F1). The deviation
> that actually remains is *external API egress*, not headless-vs-visible. See §9
> (Deviation acknowledgment) — read it before implementing Phase F1.

---

## 1. What Fusion is

OpenRouter Fusion is a **one-shot, multi-model ensemble**:

1. **Panel** — the same prompt fans out to N "analysis" models in parallel.
2. **Judge** — a strong model reads all N answers and synthesizes one.
3. We get back **a single completion** through the normal OpenAI-compatible
   chat endpoint.

Key properties:

- **NOT iterative.** No back-and-forth, no agent loop, no tool-use turns. One
  request in, one synthesized answer out. (Claude Code's iTerm2 sessions remain
  the only iterative/agentic part of the system.)
- **One API call** from our side — we don't orchestrate the panel; OpenRouter
  does. We just pick the panel + judge (or let OpenRouter pick).

**Good for:** architecture decisions, research questions, complex/ambiguous
prompt-rewriting, "what's the best way to approach this" Q&A — anywhere multiple
perspectives beat one.

**Overkill for:** routine coding, short rewrites, classification/tagging,
anything a single Sonnet/Opus call already nails. Paying 4–5× for a one-line
rewrite is waste (see §5 Cost model and §7 What NOT to run).

---

## 2. Architecture decision

**Claude Code stays the EXECUTOR. Fusion only touches the BRAIN CALLS.** The
dispatched iTerm2 `claude` session — the thing that actually edits files and runs
commands — is **completely untouched**. `spawn.spawn_iterm2(...)` does not change.
Fusion can't run the executor anyway; it isn't agentic.

There are **two distinct ways** a panel can help, and the plan supports both —
one primary, one optional:

### 2a. Primary mode — drop-in brain-call replacement  *(Phases F1–F6)*

When the toggle is on, the **internal LLM call** inside the rewriter (and,
optionally, the summarizer / onboarding analyzer) routes through Fusion instead
of the visible-tab `claude` call. It produces the *same kind of artifact* (a rewritten
prompt, a summary) — just authored by a panel+judge.

This is the cleanest design because `claude_runner.py` is *"the single entry
point for all internal LLM calls"* (project lesson — when debugging "what LLM is
this calling," you start there). We add a sibling `run_fusion_json()` returning
the **same `ClaudeRun` dataclass** as `run_claude_json()`, so every existing
caller works unchanged, and route through one dispatcher so the single-entry
invariant holds.

And like `run_claude_json`, **`run_fusion_json` runs in its own watchable iTerm2
tab** — a `spawn_fusion_tab` mirroring the existing `spawn_brain_tab` — so the
OpenRouter request and the panel/judge response stream where you can see them.
No hidden in-process HTTP. See F1.

```
                  ┌────── BRAIN CALL (swappable — ALWAYS in a watchable tab) ───────┐
 task ─▶ bundle ─▶│ run_brain_json(fusion?)                                         │─▶ rewritten ─▶ spawn iTerm2 ─▶ claude
                  │   fusion OFF → run_claude_json → visible iTerm2 brain  tab       │                 (EXECUTOR — unchanged)
                  │   fusion ON  → run_fusion_json → visible iTerm2 fusion tab       │
                  └─────────────────────────────────────────────────────────────────┘
  Brain, fusion, and executor each run in their own iTerm2 tab you can watch live.
```

Bonus: fusion cost rides through the **existing** `rewrite_ok` cost plumbing for
free, because `run_fusion_json` populates `ClaudeRun.cost_usd` — no separate
accounting needed for this path.

### 2b. Optional mode — multi-model enrichment block  *(Phase F7)*

Instead of *replacing* the rewrite, run a panel purely to **reason about the
task** and append its synthesis to the prompt the executor sees, as a fenced
"Multi-model analysis" block (`{consensus, contradictions, partial_coverage,
unique_insights, blind_spots}`). The executor weighs it as context, not gospel.

This is a genuinely different value-add (diverse perspective injected into the
*executor's* context, not a panel-rewritten prompt) and is deliberately a later,
optional phase — the drop-in covers the common case with a smaller diff.

---

## 3. Three usage modes (how we call OpenRouter)

All hit the same endpoint (`https://openrouter.ai/api/v1/chat/completions`,
OpenAI-compatible).

| Mode | Request shape | Control | Notes |
|------|---------------|---------|-------|
| **Model alias** | `"model": "openrouter/fusion"` | OpenRouter picks panel + judge | Simplest drop-in. Least cost control. |
| **Server tool** ✅ | strong outer model + `tools:[{"type":"openrouter:fusion"}]` | The outer model decides *when* to convene the panel | Answers easy prompts itself, escalates only hard ones — cost-aware by construction. |
| **Plugin / custom** | `plugins:[{id:"fusion", analysis_models:[…], judge_model:…}]` | We name every panel model + judge | Max control / determinism. Best for a fixed budget panel. |

**Recommendation:** start with the **server-tool** mode — a strong outer model
(e.g. `anthropic/claude-opus-4-8`) with the Fusion tool attached. It answers
routine rewrites directly and fans out only when the panel earns it, so we don't
pay 5× on every dispatch. Keep both server-tool and plugin paths behind one
private `_build_fusion_body()` helper so the public signature never changes; the
**plugin** mode is the natural second step once we want a fixed, auditable budget
panel from the catalog in §6.

> ⚠️ **Verify field names at implementation time.** `openrouter:fusion`,
> `plugins[].analysis_models`, `judge_model` are the shapes described in the
> Fusion docs as of writing — confirm against the live OpenRouter API reference
> before F1 ships. The HTTP/auth plumbing below is stable; the body keys may drift.

---

## 4. Config & secrets

- **`OPENROUTER_API_KEY`** is the secret — and the *only* one. OpenRouter is a
  unified gateway: one key reaches **every** provider in §6 (OpenAI, Google,
  xAI, DeepSeek, Moonshot, Z.ai, MiniMax, Qwen, Anthropic). We do **not** manage
  nine separate provider keys. Resolution precedence (in `config.py`):
  **`OPENROUTER_API_KEY` env var → `~/.orchestrator/config.json["openrouter_api_key"]` → `None`.**
  Env-var-first lets a user keep the secret out of any file; the file fallback
  lets `install.sh` scaffold it and gives presets a home.
- **Config + presets** live in **`~/.orchestrator/config.json`** (panel list,
  judge, mode, preset name) — matching *"Data lives in `~/.orchestrator/`, not in
  the repo."* The `DATA_DIR = Path.home() / ".orchestrator"` constant in `db.py`
  is the anchor.
- **Never committed.** The key never touches the repo and is never editable from
  the browser (edit the file directly — keeps the secret off any HTTP surface).
- **Startup probe** (mirror the embeddings probe in `app.py` lifespan): if no key
  resolves, log one clear WARNING so the user knows the toggle will fall back.

---

## 5. Cost model

Fusion bills as the **sum of every panel completion plus the judge**. A 3-model
panel + judge ≈ **4–5× the cost of one equivalent call** — you pay for all four
completions, not one. (Exact slugs + prices for every preset below are in §6.)

| Preset | Panel (cross-vendor) | Judge | Rough cost vs. solo Opus |
|--------|----------------------|-------|--------------------------|
| **budget** ✅ | DeepSeek V4 Pro + MiniMax M3 + Gemini 3.1 Flash-Lite | Opus 4.8 *(or Gemini 3.1 Pro to go cheaper)* | **~0.4–0.6×** — frontier-class panel for less than one frontier call |
| **balanced** | DeepSeek V4 Pro + Grok 4.3 + Kimi K2.6 | Gemini 3.1 Pro Preview | ~1–1.5× |
| **frontier** | GPT-5.5 + Gemini 3.1 Pro + Grok 4.3 | GPT-5.5 Pro *(or Opus 4.8)* | ~4–5× |
| **server-tool** | escalates only on hard prompts | — | amortized; routine prompts ≈ 1× |

Takeaways: make the **budget preset the default panel** — with mid-2026 pricing a
cheap cross-vendor panel + frontier judge beats a solo frontier call at roughly
half the cost. Reserve **frontier** for high-stakes rewrites (architecture,
irreversible migrations). The **server-tool** mode is the cost-safety valve.
Surface `run.cost_usd` on the `rewrite_ok` stage event (already recorded there)
so the premium is visible.

---

## 6. Model catalog & panel presets

> **Snapshot: 2026-06-16.** Every slug below was verified against its live
> `openrouter.ai` model page on this date. These labs ship point releases almost
> weekly and OpenRouter deprecates/renames slugs — **re-verify before wiring**,
> and prefer the config-driven presets (§4) over hard-coding so a model swap is a
> config edit, not a code change. Prices are list rates ($/M tokens, input→output)
> and vary slightly by the sub-provider OpenRouter routes to.

**Models are pass-through, not baked in.** The API functions forward *any* slug at
*any* level — each panel seat, the judge, and the server-tool outer model are just
strings handed verbatim to OpenRouter. There is no allowlist, enum, or validation
against this catalog, so a brand-new version works the instant OpenRouter lists it,
with **zero code changes**. This table is therefore a *dated reference*, not a fixed
set; the live source of truth is OpenRouter's own `/api/v1/models` (fetched at
runtime via `list_openrouter_models()`, surfaced in the F8 picker). Model choice
resolves **explicit arg → `~/.orchestrator/config.json` → last-resort seed**, so
"use a different model/version" is a config edit or a UI pick, never a redeploy.

**Why cross-vendor matters.** An ensemble only beats a solo call when panelists
make *uncorrelated* errors. Build a panel from **different labs**, not three tiers
of one vendor — and pick a judge from a different family than the panel majority.

### Catalog (verified-live, June 2026)

| Model | OpenRouter slug | Context | $/M (in→out) | Best panel role |
|-------|-----------------|---------|--------------|-----------------|
| **OpenAI** |
| GPT-5.5 Pro | `openai/gpt-5.5-pro` | 1.05M | $30 → $180 | judge / frontier analyst |
| GPT-5.5 | `openai/gpt-5.5` | 1.05M | $5 → $30 | frontier analyst |
| GPT-5.4 Nano | `openai/gpt-5.4-nano` | 400K | $0.20 → $1.25 | cheap classify/score |
| **Google** |
| Gemini 3.1 Pro Preview | `google/gemini-3.1-pro-preview` | ~1M | $2 → $12 | analyst / judge (multimodal) |
| Gemini 3.5 Flash | `google/gemini-3.5-flash` | ~1M | $1.50 → $9 | fast, near-Pro quality |
| Gemini 3.1 Flash-Lite | `google/gemini-3.1-flash-lite` | ~1M | $0.25 → $1.50 | budget analyst (1M ctx) |
| **xAI** |
| Grok 4.3 | `x-ai/grok-4.3` | 1M | $1.25 → $2.50 | frontier analyst (newest reasoning) |
| Grok 4.20 | `x-ai/grok-4.20` | **2M** | $1.25 → $2.50 | long-context judge |
| Grok 4 Fast | `x-ai/grok-4-fast` | 2M | $0.20 → $0.50 | budget seat |
| **DeepSeek** |
| DeepSeek V4 Pro | `deepseek/deepseek-v4-pro` | 1.05M | $0.44 → $0.87 | **best quality/$ analyst** |
| DeepSeek V4 Flash | `deepseek/deepseek-v4-flash` | 1.05M | $0.09 → $0.18 | ultra-cheap filler |
| **Moonshot (Kimi)** |
| Kimi K2.6 | `moonshotai/kimi-k2.6` | 262K | $0.68 → $3.41 | open reasoning/agentic analyst |
| Kimi K2 Thinking | `moonshotai/kimi-k2-thinking` | 262K | $0.60 → $2.50 | reasoning specialist |
| Kimi K2.7 Code | `moonshotai/kimi-k2.7-code` | 262K | $0.74 → $3.50 | coding specialist |
| **Z.ai (GLM)** | *(prefix is `z-ai`, not `zhipu`)* |
| GLM-5.2 | `z-ai/glm-5.2` | ~1M | $1.40 → $4.40 | agentic/coding analyst |
| GLM-4.7 Flash | `z-ai/glm-4.7-flash` | 203K | $0.06 → $0.40 | cheapest seat |
| **MiniMax** |
| MiniMax M3 | `minimax/minimax-m3` | ~1M | $0.30 → $1.20 | **value analyst (multimodal)** |
| MiniMax M2.5 | `minimax/minimax-m2.5` | 205K | $0.15 → $0.90 | budget seat |
| **Alibaba (Qwen)** |
| Qwen3.7 Max | `qwen/qwen3.7-max` | 1M | $1.25 → $3.75 | flagship analyst |
| Qwen3.7 Plus | `qwen/qwen3.7-plus` | 1M | $0.32 → $1.28 | value analyst (1M ctx) |
| **Anthropic** | *(⚠ confirm exact slug — see caveats)* |
| Claude Opus 4.8 | `anthropic/claude-opus-4-8` ⚠ | — | judge / executor |
| Claude Sonnet 4.6 | `anthropic/claude-sonnet-4-6` ⚠ | — | balanced analyst |
| Claude Haiku 4.5 | `anthropic/claude-haiku-4-5` ⚠ | — | cheap seat |

### Starting-point presets (store in `config.json`, edit freely — not hard-coded)

```jsonc
// budget — cross-vendor, frontier-class quality, ~0.4–0.6× a solo frontier call
{ "panel": ["deepseek/deepseek-v4-pro", "minimax/minimax-m3", "google/gemini-3.1-flash-lite"],
  "judge": "anthropic/claude-opus-4-8" }            // swap → "google/gemini-3.1-pro-preview" to cut cost

// balanced — three strong, maximally-diverse labs
{ "panel": ["deepseek/deepseek-v4-pro", "x-ai/grok-4.3", "moonshotai/kimi-k2.6"],
  "judge": "google/gemini-3.1-pro-preview" }

// frontier — max quality, high-stakes only
{ "panel": ["openai/gpt-5.5", "google/gemini-3.1-pro-preview", "x-ai/grok-4.3"],
  "judge": "openai/gpt-5.5-pro" }                   // swap → "anthropic/claude-opus-4-8"
```

### Caveats the implementer must know

- **One key, all providers.** Enabling a provider = referencing its slug; billing
  and auth are unified through the single OpenRouter key (§4). No per-vendor keys.
- **Gemini 3.5 Pro is NOT live on OpenRouter yet** (`google/gemini-3.5-pro` 404s as
  of 2026-06-16 — announced at I/O, GA "slated for June"). Use
  `google/gemini-3.1-pro-preview` until it resolves.
- **GLM provider prefix is `z-ai`** (e.g. `z-ai/glm-5.2`), a common gotcha — not
  `zhipu`/`zhipuai`.
- **Grok numbering is misleading:** `x-ai/grok-4.3` (Apr 30) is *newer* than
  `x-ai/grok-4.20` (Mar 31, meme-numbered). Use 4.3 for newest reasoning, 4.20 for
  the 2M window — same price.
- **`openai/gpt-5.5-mini` does not exist;** OpenAI's cheap tier is the `gpt-5.4`
  family (`gpt-5.4-mini`, `gpt-5.4-nano`).
- **Anthropic slugs are unverified** by this research (the ⚠ rows): confirm the
  exact OpenRouter form (hyphen vs dot, e.g. `claude-opus-4-8` vs `claude-opus-4.8`)
  before wiring. Opus 4.8 is also our executor model, so it's a natural judge.
- **Llama / Mistral omitted:** in mid-2026 the open frontier is Chinese (DeepSeek
  V4, Kimi K2.6/2.7, GLM-5.2, Qwen3.7, MiniMax M3); Llama 4 and Mistral have fallen
  behind on coding/agentic benchmarks. Add back only if a future release leads.
- **Promo pricing:** MiniMax M3's $0.30→$1.20 is a launch promo and may rise.

---

## 7. What NOT to run through Fusion

Keep these on the existing single-`claude` path (or no LLM at all):

- **Verbatim dispatch** — "skip rewrite & send" and `/dispatch` of the raw task
  make **no brain call**; nothing to route.
- **Prompt/bundle construction** — pure string work, no model.
- **Short-session transcript distillation** — summarizing a 30-second dispatch
  doesn't need a panel; one Sonnet call is plenty.
- **Onboarding scans of small projects** — a handful of files doesn't warrant 5×.
- **Classification / tagging** — single-label outputs; a panel adds cost, not accuracy.
- **The rewriter's auto-retry** — retry on a *single* model; never re-fan-out (5× twice).
- **Latency-sensitive paths** — the interactive "preview rewrite" where the user
  watches a spinner. A panel is slower (slowest-model-bound); prefer it for
  fire-and-forget `/send`, not live preview.

Rule of thumb: **Fusion is for hard, ambiguous, one-shot reasoning that benefits
from disagreement among models.** Everything routine stays solo.

---

## 8. Phased rollout

| Phase | Scope | Deliverable | Status |
|-------|-------|-------------|--------|
| **F0** | Config & key mgmt | `config.py` + idempotent `install.sh` template | ☐ |
| **F1** | `claude_runner` extension | `run_fusion_json() → ClaudeRun` + `run_brain_json()` dispatcher | ☐ |
| **F2** | Rewriter integration | rewriter routes through fusion when toggled; budget panel | ☐ |
| **F3** | Pipeline wiring | thread `fusion` flag `/send` → `_send_in_background` | ☐ |
| **F4** | Dispatch-form toggle | checkbox, localStorage, disabled-when-no-key, cost hint | ☐ |
| **F5** | Surface + cost | show `fusion`/`rewrite` events; cost in outcomes | ☐ |
| **F6** (opt) | Summarizer + onboarding | same drop-in for the other two brain calls | ☐ |
| **F7** (opt) | Enrichment-block mode | panel → analysis block appended to executor prompt | ☐ |
| **F8** (opt) | Model-selection UI | pick a preset or hand-select each model/version (live `/api/v1/models` list) | ☐ |

**F0–F5 deliver a working, shippable Fusion toggle.** F6–F8 are extensions and
polish. Each phase is independently testable and sized for a single dispatch.

### Phase F0 — Config & key management
New `orchestrator/lib/config.py`: `load_config() -> dict` (reads
`~/.orchestrator/config.json`, returns `{}` if absent/malformed — never raises),
`get_openrouter_key() -> str | None` (env → file → None), `fusion_config() -> dict`
(`{mode, panel, judge, preset, timeout_s}` merged over the §6 defaults),
`is_fusion_available() -> bool`. `bin/install.sh` writes a `config.json` template
**only if absent** (idempotent), all keys present with `openrouter_api_key`
empty, and prints where to paste the key.
*Acceptance:* `python -c "from orchestrator.lib import config; print(config.is_fusion_available())"`
is `False` clean, `True` once the key is set via either source.

### Phase F1 — `claude_runner.py` extension *(the core)*
Add `run_fusion_json()` beside `run_claude_json()`: same `ClaudeRun` return, same
never-raises contract, and — crucially — the **same visible-tab behavior**.
`run_claude_json` already runs every brain call in a watchable iTerm2 tab
(`spawn_brain_tab` + `brain_run.sh`, stream tee'd to a sidecar it parses back);
`run_fusion_json` mirrors that with `spawn_fusion_tab` + `fusion_run.sh` so the
OpenRouter call is equally watchable. An in-process `urllib` call (stdlib, **no
new deps**) is kept as a fallback for when iTerm2 isn't installed. Reuse the
module's existing `_strip_fences` for JSON extraction.

```python
# ── additions to orchestrator/lib/claude_runner.py ───────────────────────────
import urllib.request, urllib.error            # (json, os already imported)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# A "panel" = analysis models; the judge synthesizes their answers.
# SEED FALLBACKS ONLY — not an allowlist. Real models resolve per call:
#   explicit arg → config.fusion_config() (~/.orchestrator/config.json) → these.
# Nothing validates a slug; any model OpenRouter lists works the moment it ships
# (§6). The slugs below are a 2026-06-16 snapshot — expect weekly churn.
FUSION_PANEL_BUDGET   = ["deepseek/deepseek-v4-pro",       # frontier quality/$  $0.44→$0.87
                         "minimax/minimax-m3",             # multimodal value    $0.30→$1.20
                         "google/gemini-3.1-flash-lite"]   # cheap 1M context    $0.25→$1.50
FUSION_PANEL_FRONTIER = ["openai/gpt-5.5",                 # $5→$30
                         "google/gemini-3.1-pro-preview",  # $2→$12
                         "x-ai/grok-4.3"]                  # $1.25→$2.50
DEFAULT_FUSION_PANEL     = FUSION_PANEL_BUDGET
DEFAULT_FUSION_JUDGE     = "anthropic/claude-opus-4-8"     # strong cross-family synthesizer
DEFAULT_FUSION_MODE      = "server_tool"        # "alias" | "server_tool" | "plugin"
DEFAULT_FUSION_TIMEOUT_S = 300


def _build_fusion_body(prompt, panel, judge, mode, outer_model) -> dict:
    msgs = [{"role": "user", "content": prompt}]
    common = {"messages": msgs, "usage": {"include": True}}   # include → cost in usage
    if mode == "alias":               # OpenRouter picks panel + judge
        return {"model": "openrouter/fusion", **common}
    if mode == "server_tool":         # strong outer model decides when to convene
        return {"model": outer_model,
                "tools": [{"type": "openrouter:fusion"}], **common}
    return {"model": "openrouter/fusion",   # plugin: explicit panel + judge
            "plugins": [{"id": "fusion",
                         "analysis_models": panel, "judge_model": judge}], **common}


def run_fusion_json(
    prompt: str,
    cwd: str = "",                                  # working dir for the visible tab
    panel: Optional[list] = None,                   # any list of slugs; None → config → seed
    judge: Optional[str] = None,                    # any slug;          None → config → seed
    mode: Optional[str] = None,                     # alias|server_tool|plugin; None → config → seed
    outer_model: Optional[str] = None,              # server_tool outer model;  None → judge
    timeout_s: Optional[int] = None,                # None → config → seed
    api_key: Optional[str] = None,                  # None → config.get_openrouter_key()
) -> ClaudeRun:
    """OpenRouter Fusion sibling of run_claude_json — and, exactly like it, the
    call runs in a WATCHABLE iTerm2 tab: you see the request go out and the
    panel/judge response come back, no hidden in-process HTTP. Returns the SAME
    ClaudeRun. Any slug works at any level (panel/judge/outer) — forwarded
    verbatim, no allowlist; choice resolves arg → config → seed. Falls back to an
    in-process HTTP call ONLY when iTerm2 is absent. Never raises. See §9."""
    key = api_key or config.get_openrouter_key()
    if not key:
        return ClaudeRun(ok=False, error="OPENROUTER_API_KEY not set; fusion unavailable")

    cfg = config.fusion_config()                        # {} if unset → pure seed fallback
    panel       = panel       or cfg.get("panel")     or DEFAULT_FUSION_PANEL
    judge       = judge       or cfg.get("judge")     or DEFAULT_FUSION_JUDGE
    mode        = mode        or cfg.get("mode")      or DEFAULT_FUSION_MODE
    outer_model = outer_model or cfg.get("outer")     or judge
    timeout_s   = timeout_s   or cfg.get("timeout_s") or DEFAULT_FUSION_TIMEOUT_S
    body = _build_fusion_body(prompt, panel, judge, mode, outer_model)

    # PRIMARY: run the OpenRouter call in a visible iTerm2 tab, just like a brain
    # call. fusion_run.sh streams the response on screen and tee's the JSON
    # envelope to <id>.json; _run_fusion_in_tab polls <id>.done/<id>.pid with the
    # same loop as run_claude_json. The tab sets ORCHESTRATOR_FUSION_ID (never
    # ORCHESTRATOR_RUN_ID), so the Stop hook stays a no-op.
    if spawn.iterm2_installed():
        envelope = _run_fusion_in_tab(body, cwd, timeout_s)   # None on tab spawn/poll failure
        if envelope is not None:
            return _fusion_envelope_to_run(envelope, judge)
        print("[claude_runner] fusion tab failed; falling back to in-process call")
    # FALLBACK (no iTerm2, or tab failed): the invisible in-process HTTPS call.
    return _run_fusion_headless(body, key, timeout_s, judge)


def _fusion_envelope_to_run(envelope: dict, judge: str) -> ClaudeRun:
    """OpenRouter (OpenAI-shaped) envelope → ClaudeRun, parsing JSON out of the
    synthesized content. Shared by the tab path and the headless fallback."""
    text  = (envelope.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    usage = envelope.get("usage") or {}
    cost  = float(usage.get("cost") or usage.get("total_cost") or 0.0)
    parsed, stripped = None, _strip_fences(text)
    if stripped.startswith("{") or stripped.startswith("["):
        try: parsed = json.loads(stripped)
        except json.JSONDecodeError: parsed = None
    return ClaudeRun(ok=True, text=text, parsed_json=parsed,
                     cost_usd=cost, model=envelope.get("model") or judge, raw=envelope)


def _run_fusion_headless(body: dict, key: str, timeout_s: int, judge: str) -> ClaudeRun:
    """FALLBACK only (iTerm2 absent): the invisible in-process HTTPS call."""
    req = urllib.request.Request(
        OPENROUTER_URL, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "HTTP-Referer": "http://localhost:7878", "X-Title": "orchestrator"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            envelope = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return ClaudeRun(ok=False, error=f"openrouter HTTP {e.code}: {e.read()[:300]!r}")
    except (urllib.error.URLError, TimeoutError) as e:
        return ClaudeRun(ok=False, error=f"openrouter unreachable: {e}")
    except (json.JSONDecodeError, OSError) as e:
        return ClaudeRun(ok=False, error=f"openrouter bad response: {e}")
    return _fusion_envelope_to_run(envelope, judge)


def run_brain_json(prompt: str, cwd: str, fusion: bool = False, **kw) -> ClaudeRun:
    """Single entry point for brain calls. Routes to Fusion when requested AND
    available; falls back to the standard visible-tab claude call when fusion is on
    but the key is missing or OpenRouter errors — a flaky panel never hard-fails a run."""
    if fusion:
        run = run_fusion_json(prompt=prompt, **kw)
        if run.ok:
            return run
        print(f"[claude_runner] fusion unavailable ({run.error}); falling back to claude")
    return run_claude_json(prompt=prompt, cwd=cwd)


def list_openrouter_models(api_key: Optional[str] = None, timeout_s: int = 15) -> list:
    """GET /api/v1/models — the LIVE catalog OpenRouter serves right now, so the
    F8 picker can show currently-available models/versions instead of a hard-coded
    list that goes stale weekly. New releases appear here automatically. Never
    raises; returns [] on failure. Each item: {id, name, context_length, pricing}."""
    key = api_key or config.get_openrouter_key()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/models",
        headers={"Authorization": f"Bearer {key}"} if key else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return (json.loads(r.read().decode()) or {}).get("data", [])
    except Exception as e:
        print(f"[claude_runner] could not fetch OpenRouter model list: {e}")
        return []
```

**OpenRouter request body** (plugin mode — the most explicit):

```jsonc
POST https://openrouter.ai/api/v1/chat/completions
Authorization: Bearer $OPENROUTER_API_KEY
Content-Type: application/json
{
  "model": "openrouter/fusion",
  "messages": [{ "role": "user", "content": "<the rewriter prompt>" }],
  "plugins": [{
    "id": "fusion",
    "analysis_models": ["deepseek/deepseek-v4-pro",
                        "minimax/minimax-m3",
                        "google/gemini-3.1-flash-lite"],
    "judge_model": "anthropic/claude-opus-4-8"
  }],
  "usage": { "include": true }            // return cost in the usage object
}
```

Response is OpenAI-shaped: `choices[0].message.content` is the synthesized
answer; `usage.cost` (with `usage.include=true`) is the summed dollar cost.

**Visible-tab plumbing (mirror the existing brain-tab path).** `run_claude_json`
already runs brain calls in watchable tabs via `spawn.spawn_brain_tab` +
`brain_run.sh` (sidecars in `~/.orchestrator/brain/`, `.done`/`.pid` poll loop).
Fusion reuses that exact shape: add `spawn.spawn_fusion_tab(body, cwd)` + a
`fusion_run.sh`, sidecars in `~/.orchestrator/fusion/`, and `_run_fusion_in_tab`
polls `<id>.done`/`<id>.pid` just like the brain loop. The OpenRouter key is read
**inside the tab** (env → `config.json`) — never passed through AppleScript,
never written to a temp file.

```bash
#!/bin/bash
# ~/.orchestrator/bin/fusion_run.sh — execed in an iTerm2 tab so the OpenRouter
# panel call is WATCHABLE (same principle as brain_run.sh: no hidden HTTP). A
# tiny stdlib-python runner POSTs the prepared request, echoes the panel + the
# synthesized answer to the SCREEN (stderr), and prints ONLY the JSON envelope to
# stdout — which `tee` captures to <id>.json for the orchestrator to parse.
ID="$ORCHESTRATOR_FUSION_ID"; DIR="$HOME/.orchestrator/fusion"
echo $$ > "$DIR/$ID.pid"
echo "---- orchestrator fusion call: $ID (watching live) ----"
python3 "$HOME/.orchestrator/bin/fusion_call.py" "$DIR/$ID.request.json" | tee "$DIR/$ID.json"
echo "${PIPESTATUS[0]}" > "$DIR/$ID.done"
echo "---- fusion call finished ----"
```

`fusion_call.py` is ~30 lines of stdlib `urllib` sharing the POST with
`_run_fusion_headless`: read the request-body sidecar, resolve the key
(env → `config.json`), POST, echo the answer to stderr for watching, print the
envelope to stdout for capture. (Add `"stream": true` later if you want tokens to
appear live — non-streaming still shows the call fire and the response land.)

*Acceptance:* with a key set, `run_fusion_json("Should a single-writer local app
use SQLite WAL mode?")` opens a visible iTerm2 tab, and on completion returns
`ok=True` with cost > 0; with no key, `ok=False, error="OPENROUTER_API_KEY not
set; fusion unavailable"`; with iTerm2 uninstalled, it still returns a valid
`ClaudeRun` via the in-process fallback.

### Phase F2 — Rewriter integration
`rewriter.rewrite(user_task, project_path, fusion: bool = False)` swaps one line:
`run = claude_runner.run_brain_json(prompt=prompt, cwd=str(project), fusion=fusion)`.
Everything downstream (`run.ok` / `run.parsed_json` / `run.cost_usd`) is
unchanged. **The existing auto-retry must NOT re-run Fusion** — force the retry
through `run_claude_json` directly (a strict-JSON reminder to one model is the
cheap, reliable fix). Default panel: budget preset (§6).
*Acceptance:* with fusion on + key, the rewrite's cost reflects panel spend and
the prompt is panel-authored; with fusion on + no key, it transparently produces
the same result as today.

### Phase F3 — Pipeline wiring
`app.py`: add `fusion: str = Form("false")` to `/send`; parse
`do_fusion = fusion.lower() in ("1","true","yes","on")`; pass through
`_send_in_background(... do_fusion=False)` → `rewriter.rewrite(..., fusion=do_fusion)`.
`_run_dispatch` needs **no change** (fusion only affects the rewrite call). Record
`do_fusion` on the existing `rewrite_ok` stage event for later cost analysis. No
DB column needed for the MVP.
*Acceptance:* `fusion=true` + key produces a panel-authored rewrite with cost on
the timeline; `fusion=true` + no key still dispatches (fallback), no hard failure.

### Phase F4 — Dispatch-form toggle
`templates/index.html`: a **Fusion checkbox** next to the effort/model selects
(~index.html:76–94). Label `fusion (multi-model) ⚡`; muted hint
`~4–5× cost; best for architecture / research / high-stakes`. In `send(rewrite)`
(index.html:259) append `fd.append('fusion', chkFusion.checked ? 'true':'false')`
alongside the existing `effort`/`model`/`rewrite` appends (lines 266–271). Works
with **either** button (`rewrite & send` or `skip rewrite & send`). **Default
OFF**, persisted in `localStorage` like the draft textarea. When
`config.is_fusion_available()` is `False`, render the checkbox **disabled** with
the note *"Set openrouter_api_key in ~/.orchestrator/config.json to enable."*
(extend `_view_ctx()` to pass `fusion_available`). When on, the in-flight banner
(index.html:294) should read `rewriting (multi-model) then dispatching (~15–40s).`

### Phase F5 — Surface + cost accounting
In `/dispatch/{id}` (`templates/dispatch.html`), render `rewrite_ok`/`rewrite_skipped`
events as today — they now carry fusion cost automatically (§2a). The `outcomes`
row already has `cost_usd` (db.py:113); ensure the fused rewrite's cost flows into
it so the future "when is fusion worth it?" learning loop sees true cost. Optional:
a ⚡ badge on fused rows in `templates/_runs.html`.

### Phase F6 — Summarizer + onboarding *(optional)*
Same drop-in: `summarizer.summarize(...)` and `onboarding.analyze(...)` each take
a `fusion` flag and call `run_brain_json(..., fusion=fusion)`. Lower priority —
short sessions rarely justify panel cost (§7).

### Phase F7 — Enrichment-block mode *(optional, advanced)*
The existing-plan idea, preserved as a distinct capability. New
`orchestrator/lib/fusion.py`: `enrich(prompt, project_path) -> FusionResult` runs
a panel to *reason about* the (already-rewritten) prompt and returns a fenced
analysis block appended to the executor prompt — it does **not** rewrite the task.

```python
@dataclass
class FusionRun:                      # in fusion.py / fusion_runner.py
    ok: bool
    analysis: Optional[dict] = None   # {consensus, contradictions, partial_coverage,
                                      #  unique_insights, blind_spots}
    enrichment_md: str = ""           # rendered "## Multi-model analysis" block
    panel_models: list = field(default_factory=list)
    cost_usd: float = 0.0
    error: str = ""
```

On success `fused_prompt = prompt + "\n\n" + enrichment_md`; on failure, pass the
prompt through unchanged (additive — **a fusion failure here must never abort the
dispatch**, unlike a failed rewrite). Cap the panel input (~12K chars, à la
`embeddings.MAX_INPUT_CHARS`) so we don't fan a 50KB bundle to 4 paid models.
Record a separate `fusion_ok`/`fusion_skipped` stage event (this path's cost is
*not* the rewrite's). Surface the collapsible analysis on the detail page.

### Phase F8 — Model-selection UI *(optional — the capability already exists)*
You can already change models **without** this phase: edit `panel`/`judge`/`mode`
in `~/.orchestrator/config.json`, or pass them per call. F8 just makes it clickable.
A `/settings` (or dispatch-form) picker that (a) lists **currently-available**
models/versions via `claude_runner.list_openrouter_models()` (live `/api/v1/models`,
so new releases show up with zero code change), (b) lets you choose a preset
(`budget`/`balanced`/`frontier`/`custom`) **or** hand-pick each panel seat + judge +
version, and (c) saves the choice to `config.json` (or threads it per-dispatch like
`effort`/`model`). The **key is never editable from the browser**. Append a
`Phase 11 — Fusion ✅` entry to `PLAN.md` and a short `## Fusion` note to `CLAUDE.md`
once shipped.

---

## 9. Deviation acknowledgment

The honest version the hard rules demand — no hand-waving that "OpenRouter isn't
the Anthropic API, so nothing's broken." **Two rules are broken, on purpose,
opt-in only:**

1. *"No Anthropic API calls — all brain work via headless `claude` subprocesses."*
   Fusion replaces those subprocess calls with an outbound **OpenRouter HTTPS
   request** (which itself calls Anthropic/OpenAI/Google server-side). The
   *spirit* of the rule — brain work runs as local subprocesses — is relaxed.
   (Aside: CLAUDE.md's "headless" wording is itself outdated — the code now runs
   those subprocesses in **visible iTerm2 tabs**, and the Fusion call runs in a
   watchable tab too. So this deviation is about *API egress*, not headless-vs-visible.)
2. *"Local only. No remote workers, no hosted services."* Fusion's panel + judge
   run **on OpenRouter's infrastructure**, and the prompt — which includes the
   project bundle (CLAUDE.md, memory, recent tasks, source excerpts) — **leaves
   the laptop.**

**Why it's acceptable:**
- **Default-off.** The checkbox is the only way Fusion ever fires.
- **Strictly additive.** `run_claude_json()` and the entire local path are
  untouched; Fusion is a sibling, never a replacement.
- **Degrades to local automatically.** No key, or OpenRouter down →
  `run_brain_json()` falls back to the visible-tab `claude` call. A flaky panel
  never hard-fails a dispatch.
- **The executor stays 100% local.** Only brain/rewrite *text* is sent out; the
  actual file edits and command execution still run in a local iTerm2 `claude`
  session.

**What's still preserved (the true compliance points):** zero new Python deps
(stdlib `urllib`); the **Stop hook stays a no-op** (brain and fusion tabs set their
own `ORCHESTRATOR_BRAIN_ID` / `ORCHESTRATOR_FUSION_ID`, never `ORCHESTRATOR_RUN_ID`,
so they don't post to `/api/complete`); the key and config live in
`~/.orchestrator/`, never the repo; and **every call stays watchable in iTerm2**.

**Fallback when fusion is OFF (the default):** identical to today — the
**visible-tab** `claude` call (`run_claude_json`), headless only as a last resort
if iTerm2 is absent. No OpenRouter dependency, no `OPENROUTER_API_KEY` required, no
network egress beyond what `claude` already does.

**Data-egress note:** because the bundle can contain project memory and source,
treat the toggle as *"send this project's context to a third party."* The panel
also fans your prompt out to **non-Anthropic** providers (OpenAI, Google, xAI,
DeepSeek, Moonshot, Z.ai, MiniMax, Qwen — several of them Chinese labs), so the
data-egress surface is wider than a single vendor. Keep it opt-in per send;
consider a one-time confirmation the first time it's enabled. Do **not** enable
Fusion for any project whose contents shouldn't leave the machine.

---

## Appendix — implementer notes

**Dispatch order (one at a time):** F0 (config, no network) → F1 (`run_fusion_json`)
→ F2 (rewriter) → F3 (`/send` wiring) → F4 (toggle) → F5 (surface/cost) → F6–F8
(optional).

**Key file targets (absolute):**
- `/Users/tresmith/Documents/orchestrator/orchestrator/lib/config.py` *(new — F0)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/lib/claude_runner.py` *(F1 — `run_fusion_json`, `_run_fusion_in_tab`, `run_brain_json`)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/lib/spawn.py` *(F1 — `spawn_fusion_tab`; mirror `spawn_brain_tab`/`brain_run.sh`)*
- `~/.orchestrator/bin/fusion_run.sh` + `fusion_call.py` *(F1 — the visible-tab runner, written by an `ensure_fusion_runner()`)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/lib/rewriter.py` *(F2)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/lib/fusion.py` *(new — F7, enrichment mode)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/app.py` *(`/send`, `_send_in_background`, `_view_ctx` — F3/F4/F5)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/templates/index.html` *(toggle — F4)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/templates/dispatch.html` + `_runs.html` *(surfacing — F5)*
- `/Users/tresmith/Documents/orchestrator/bin/install.sh` *(config.json template — F0)*
- `~/.orchestrator/config.json` *(runtime data — holds `openrouter_api_key`, never in repo)*

**Reuse / consistency:**
- **Everything visible — no hidden/headless calls.** Brain calls already run in
  watchable iTerm2 tabs (`run_claude_json` → `spawn_brain_tab` → `brain_run.sh`);
  the Fusion call mirrors that (`spawn_fusion_tab` → `fusion_run.sh`), reusing the
  `.done`/`.pid` sidecar poll. In-process HTTP is a fallback only when iTerm2 is absent.
- Mirror `embeddings.py` for the fallback HTTP: stdlib `urllib.request`, never
  raise, return `ok=False` on any failure, log a warning. **No `httpx`/`requests`.**
- Reuse `claude_runner._strip_fences` for judge/panel JSON (battle-tested against
  prose-wrapped JSON) — don't re-invent it.
- Mirror the `rewrite_event` recording pattern for any `fusion_event`
  (`db.record_event(dispatch_id, "stage", …)` after the dispatch row exists).
- Model slugs (§6) are a 2026-06-16 snapshot — verify live before wiring; prefer
  config-driven presets so a swap is a config edit, not a code change.
- **CLAUDE.md is stale on this point:** its hard rule still says brain work goes
  through *headless* subprocesses, but the code now uses **visible iTerm2 tabs**.
  Update that wording when Fusion ships (the `## Fusion` note in F8 is a good spot).
- **Edits don't take effect until you restart `python -m orchestrator`** (uvicorn
  runs `reload=False` on :7878), and the **auto-push daemon commits within
  seconds** — `git diff` won't show your changes.
