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
> path automatically. See [§8](#8-deviation-acknowledgment) — read it before
> implementing Phase F1.

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
rewrite is waste (see [§6](#6-cost-model), [§7](#7-what-not-to-run-through-fusion)).

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
of headless `claude`. It produces the *same kind of artifact* (a rewritten
prompt, a summary) — just authored by a panel+judge.

This is the cleanest design because `claude_runner.py` is *"the single entry
point for all internal LLM calls"* (project lesson — when debugging "what LLM is
this calling," you start there). We add a sibling `run_fusion_json()` returning
the **same `ClaudeRun` dataclass** as `run_claude_json()`, so every existing
caller works unchanged, and route through one dispatcher so the single-entry
invariant holds.

```
                  ┌──────────── BRAIN CALL (swappable) ─────────────┐
 task ─▶ bundle ─▶│ run_brain_json(fusion?)                         │─▶ rewritten ─▶ spawn iTerm2 ─▶ claude
                  │   fusion OFF → run_claude_json  (headless -p)    │                 (EXECUTOR — unchanged)
                  │   fusion ON  → run_fusion_json  (OpenRouter)     │
                  └─────────────────────────────────────────────────┘
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
panel.

> ⚠️ **Verify field names at implementation time.** `openrouter:fusion`,
> `plugins[].analysis_models`, `judge_model` are the shapes described in the
> Fusion docs as of writing — confirm against the live OpenRouter API reference
> before F1 ships. The HTTP/auth plumbing below is stable; the body keys may drift.

---

## 4. Config & secrets

- **`OPENROUTER_API_KEY`** is the secret. Resolution precedence (in `config.py`):
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
completions, not one.

| Preset | Panel | Judge | Rough cost vs. solo Opus |
|--------|-------|-------|--------------------------|
| **budget** ✅ | Gemini Flash + Kimi + DeepSeek | Opus 4.8 | **~0.5×** — matches frontier quality on the DRACO benchmark (per OpenRouter) |
| **quality** | Opus 4.8 + GPT-latest + Gemini Pro | Opus 4.8 | ~4–5× |
| **server-tool** | escalates only on hard prompts | — | amortized; routine prompts ≈ 1× |

Takeaways: make the **budget preset the default panel** — cheap panel + frontier
judge reportedly beats a solo frontier call at about half the cost. Reserve
**quality** for high-stakes rewrites (architecture, irreversible migrations). The
**server-tool** mode is the cost-safety valve. Surface `run.cost_usd` on the
`rewrite_ok` stage event (already recorded there) so the premium is visible.

---

## 6. What NOT to run through Fusion

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

## 7. Phased rollout

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
| **F8** (opt) | Config UI & presets | switch budget/quality presets; key stays file-only | ☐ |

**F0–F5 deliver a working, shippable Fusion toggle.** F6–F8 are extensions and
polish. Each phase is independently testable and sized for a single dispatch.

### Phase F0 — Config & key management
New `orchestrator/lib/config.py`: `load_config() -> dict` (reads
`~/.orchestrator/config.json`, returns `{}` if absent/malformed — never raises),
`get_openrouter_key() -> str | None` (env → file → None), `fusion_config() -> dict`
(`{mode, panel, judge, preset, timeout_s}` merged over defaults),
`is_fusion_available() -> bool`. `bin/install.sh` writes a `config.json` template
**only if absent** (idempotent), all keys present with `openrouter_api_key`
empty, and prints where to paste the key.
*Acceptance:* `python -c "from orchestrator.lib import config; print(config.is_fusion_available())"`
is `False` clean, `True` once the key is set via either source.

### Phase F1 — `claude_runner.py` extension *(the core)*
Add `run_fusion_json()` beside `run_claude_json()`: same `ClaudeRun` return,
same never-raises contract, same stdlib-only HTTP style as `embeddings.py`
(`urllib.request`, **no new deps**). Reuse the module's existing `_strip_fences`
for JSON extraction.

```python
# ── additions to orchestrator/lib/claude_runner.py ───────────────────────────
import urllib.request, urllib.error            # (json, os already imported)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# A "panel" = analysis models; the judge synthesizes their answers.
FUSION_PANEL_BUDGET   = ["google/gemini-flash-latest",
                         "moonshotai/kimi-latest",
                         "deepseek/deepseek-chat"]
FUSION_PANEL_QUALITY  = ["anthropic/claude-opus-4-8", "openai/gpt-latest",
                         "google/gemini-pro-latest"]
DEFAULT_FUSION_PANEL     = FUSION_PANEL_BUDGET
DEFAULT_FUSION_JUDGE     = "anthropic/claude-opus-4-8"
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
    cwd: str = "",                                  # accepted for parity; unused (no local fs)
    panel: Optional[list] = None,                   # None → DEFAULT_FUSION_PANEL
    judge: str = DEFAULT_FUSION_JUDGE,
    mode: str = DEFAULT_FUSION_MODE,
    outer_model: str = DEFAULT_FUSION_JUDGE,        # used only in server_tool mode
    timeout_s: int = DEFAULT_FUSION_TIMEOUT_S,
    api_key: Optional[str] = None,                  # None → config.get_openrouter_key()
) -> ClaudeRun:
    """OpenRouter Fusion sibling of run_claude_json. Returns the SAME ClaudeRun so
    the rewriter/summarizer can call either interchangeably. Never raises: returns
    ClaudeRun(ok=False, error=...) on missing key, HTTP/timeout error, or
    unparseable body. NOTE: calls OpenRouter's servers — see FUSION_PLAN §8."""
    key = api_key or config.get_openrouter_key()
    if not key:
        return ClaudeRun(ok=False, error="OPENROUTER_API_KEY not set; fusion unavailable")

    body = _build_fusion_body(prompt, panel or DEFAULT_FUSION_PANEL, judge, mode, outer_model)
    req = urllib.request.Request(
        OPENROUTER_URL, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "HTTP-Referer": "http://localhost:7878",   # OpenRouter attribution
                 "X-Title": "orchestrator"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            envelope = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return ClaudeRun(ok=False, error=f"openrouter HTTP {e.code}: {e.read()[:300]!r}")
    except (urllib.error.URLError, TimeoutError) as e:
        return ClaudeRun(ok=False, error=f"openrouter unreachable: {e}")
    except (json.JSONDecodeError, OSError) as e:
        return ClaudeRun(ok=False, error=f"openrouter bad response: {e}")

    text  = (envelope.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    usage = envelope.get("usage") or {}
    cost  = float(usage.get("cost") or usage.get("total_cost") or 0.0)

    parsed, stripped = None, _strip_fences(text)
    if stripped.startswith("{") or stripped.startswith("["):
        try: parsed = json.loads(stripped)
        except json.JSONDecodeError: parsed = None
    if parsed is None and text:
        print(f"[claude_runner] fusion JSON parse failed; first 400 chars:\n{text[:400]}")

    return ClaudeRun(ok=True, text=text, parsed_json=parsed,
                     cost_usd=cost, model=envelope.get("model") or judge, raw=envelope)


def run_brain_json(prompt: str, cwd: str, fusion: bool = False, **kw) -> ClaudeRun:
    """Single entry point for brain calls. Routes to Fusion when requested AND
    available; falls back to headless claude automatically when fusion is on but
    the key is missing or OpenRouter errors — a flaky panel never hard-fails a run."""
    if fusion:
        run = run_fusion_json(prompt=prompt, **kw)
        if run.ok:
            return run
        print(f"[claude_runner] fusion unavailable ({run.error}); falling back to claude")
    return run_claude_json(prompt=prompt, cwd=cwd)
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
    "analysis_models": ["google/gemini-flash-latest",
                        "moonshotai/kimi-latest",
                        "deepseek/deepseek-chat"],
    "judge_model": "anthropic/claude-opus-4-8"
  }],
  "usage": { "include": true }            // return cost in the usage object
}
```

Response is OpenAI-shaped: `choices[0].message.content` is the synthesized
answer; `usage.cost` (with `usage.include=true`) is the summed dollar cost.
*Acceptance:* with a key set, `run_fusion_json("Should a single-writer local app
use SQLite WAL mode?")` returns `ok=True` with cost > 0; with no key, `ok=False,
error="OPENROUTER_API_KEY not set; fusion unavailable"`.

### Phase F2 — Rewriter integration
`rewriter.rewrite(user_task, project_path, fusion: bool = False)` swaps one line:
`run = claude_runner.run_brain_json(prompt=prompt, cwd=str(project), fusion=fusion)`.
Everything downstream (`run.ok` / `run.parsed_json` / `run.cost_usd`) is
unchanged. **The existing auto-retry must NOT re-run Fusion** — force the retry
through `run_claude_json` directly (a strict-JSON reminder to one model is the
cheap, reliable fix). Default panel: budget preset.
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
short sessions rarely justify panel cost (§6).

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

### Phase F8 — Config UI & presets *(optional polish)*
A read-only `/settings` view: fusion availability, current panel/judge/mode, and
preset switch (`budget` / `quality` / `custom`) resolved by `config.fusion_config()`.
The **key is never editable from the browser**. Append a `Phase 10 — Fusion ✅`
entry to `PLAN.md` and a short `## Fusion` note to `CLAUDE.md` once shipped.

---

## 8. Deviation acknowledgment

The honest version the hard rules demand — no hand-waving that "OpenRouter isn't
the Anthropic API, so nothing's broken." **Two rules are broken, on purpose,
opt-in only:**

1. *"No Anthropic API calls — all brain work via headless `claude` subprocesses."*
   Fusion replaces those subprocess calls with an outbound **OpenRouter HTTPS
   request** (which itself calls Anthropic/OpenAI/Google server-side). The
   *spirit* of the rule — brain work runs as local subprocesses — is relaxed.
2. *"Local only. No remote workers, no hosted services."* Fusion's panel + judge
   run **on OpenRouter's infrastructure**, and the prompt — which includes the
   project bundle (CLAUDE.md, memory, recent tasks, source excerpts) — **leaves
   the laptop.**

**Why it's acceptable:**
- **Default-off.** The checkbox is the only way Fusion ever fires.
- **Strictly additive.** `run_claude_json()` and the entire local path are
  untouched; Fusion is a sibling, never a replacement.
- **Degrades to local automatically.** No key, or OpenRouter down →
  `run_brain_json()` falls back to headless `claude`. A flaky panel never
  hard-fails a dispatch.
- **The executor stays 100% local.** Only brain/rewrite *text* is sent out; the
  actual file edits and command execution still run in a local iTerm2 `claude`
  session.

**What's still preserved (the true compliance points):** zero new Python deps
(stdlib `urllib`); the **Stop hook is unaffected** (Fusion makes HTTP calls, not
`claude` subprocesses, so it never touches `ORCHESTRATOR_RUN_ID`); the key and
config live in `~/.orchestrator/`, never the repo.

**Fallback when fusion is OFF (the default):** identical to today — headless
`claude -p`, env-scrubbed of `ORCHESTRATOR_RUN_ID`, no OpenRouter dependency, no
`OPENROUTER_API_KEY` required, no network egress beyond what `claude` already does.

**Data-egress note:** because the bundle can contain project memory and source,
treat the toggle as *"send this project's context to a third party."* Keep it
opt-in per send; consider a one-time confirmation the first time it's enabled. Do
**not** enable Fusion for any project whose contents shouldn't leave the machine.

---

## Appendix — implementer notes

**Dispatch order (one at a time):** F0 (config, no network) → F1 (`run_fusion_json`)
→ F2 (rewriter) → F3 (`/send` wiring) → F4 (toggle) → F5 (surface/cost) → F6–F8
(optional).

**Key file targets (absolute):**
- `/Users/tresmith/Documents/orchestrator/orchestrator/lib/config.py` *(new — F0)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/lib/claude_runner.py` *(F1 — `run_fusion_json`, `run_brain_json`)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/lib/rewriter.py` *(F2)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/lib/fusion.py` *(new — F7, enrichment mode)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/app.py` *(`/send`, `_send_in_background`, `_view_ctx` — F3/F4/F5)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/templates/index.html` *(toggle — F4)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/templates/dispatch.html` + `_runs.html` *(surfacing — F5)*
- `/Users/tresmith/Documents/orchestrator/bin/install.sh` *(config.json template — F0)*
- `~/.orchestrator/config.json` *(runtime data — holds `openrouter_api_key`, never in repo)*

**Reuse / consistency:**
- Mirror `embeddings.py` for HTTP: stdlib `urllib.request`, never raise, return
  `ok=False` on any failure, log a warning. **Do not add `httpx`/`requests`.**
- Reuse `claude_runner._strip_fences` for judge/panel JSON (battle-tested against
  prose-wrapped JSON) — don't re-invent it.
- Mirror the `rewrite_event` recording pattern for any `fusion_event`
  (`db.record_event(dispatch_id, "stage", …)` after the dispatch row exists).
- **Edits don't take effect until you restart `python -m orchestrator`** (uvicorn
  runs `reload=False` on :7878), and the **auto-push daemon commits within
  seconds** — `git diff` won't show your changes.
