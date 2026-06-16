# Fusion Mode — Plan

## Goal
Add a toggleable **Fusion mode** to the dispatch form so that, on top of the existing rewrite step, any "complex question" task can be enriched by a panel of multiple external models (via OpenRouter Fusion) before Claude Code executes it.

Claude Code stays the **outer/executor** model for every dispatched iTerm2 session — exactly as today. Fusion is a new, opt-in **brain-call layer** that runs *between* the rewriter and the dispatch: it fans the task out to a panel of models, a judge synthesizes their answers, and the synthesized multi-model perspective is folded into the final prompt that Claude Code receives.

When Fusion is **off**, behavior is byte-for-byte unchanged.

## Pipeline placement
Current `/send` background flow (`orchestrator/lib/.../app.py:_send_in_background`):

```
task ──(optional rewrite via rewriter.py)──> final_task ──> _run_dispatch ──> iTerm2 claude
```

New flow:

```
task ──(optional rewrite)──> rewritten ──(optional FUSION)──> fused_task ──> _run_dispatch ──> iTerm2 claude
                                              │
                                              ├─ fan-out to N panel models (OpenRouter, web search on)
                                              ├─ judge model → JSON {consensus, contradictions,
                                              │     partial_coverage, unique_insights, blind_spots}
                                              └─ inject judge JSON as a "Multi-model analysis" block
```

Fusion runs **after** the rewriter so it operates on the already-context-enriched prompt (project bundle + similar past tasks are baked in by the rewriter). If rewrite is off but fusion is on, fusion runs on the raw task. Fusion does **not** write the final answer — it produces an *enrichment block* appended to the prompt; Claude Code remains the model that does the actual work in the repo.

## Hard-rule compliance
- **"No Anthropic API calls"** — unchanged. OpenRouter is a *different* external service (not the Anthropic API), and it is **opt-in only**. The rewriter/summarizer brain calls still go through headless `claude` subprocesses. We are not adding an Anthropic HTTP client.
- **Local only** — the orchestrator process still runs locally; OpenRouter is the one allowed outbound HTTP call, gated behind an explicit toggle + a configured key. No remote workers.
- **Data lives in `~/.orchestrator/`** — the OpenRouter key and Fusion config live in `~/.orchestrator/config.json` (gitignored, never in repo). Fusion outputs are logged to the existing SQLite DB + transcripts dir.
- **Stop hook env-gating** — unaffected; Fusion makes HTTP calls, not `claude` subprocesses, so it never touches `ORCHESTRATOR_RUN_ID`.
- **Zero new deps** — use stdlib `urllib.request` for the OpenRouter HTTP call, mirroring `orchestrator/lib/embeddings.py`. No `httpx`/`requests`.

---

## Phase F0 — Config & key management (foundation)  ☐
**Single dispatch.** No network calls yet; just the config plumbing every later phase depends on.

- [ ] New module `orchestrator/lib/config.py`:
  - `load_config() -> dict` — reads `~/.orchestrator/config.json` (returns `{}` if absent; never raises).
  - `get_openrouter_key() -> str | None` — precedence: `OPENROUTER_API_KEY` env var → `config.json["openrouter_api_key"]` → `None`.
  - `fusion_config() -> dict` — returns `{panel_models, judge_model, outer_note, max_panel, timeout_s}` merged over defaults (see F1).
  - `is_fusion_available() -> bool` — `get_openrouter_key() is not None`.
- [ ] `bin/install.sh`: create `~/.orchestrator/config.json` from a template **only if absent** (idempotent), with all keys present but `openrouter_api_key` empty. Print a one-line note telling the user where to paste the key.
- [ ] Add `config.json` to whatever ignore the repo uses for `~/.orchestrator/` (it already lives outside the repo, so this is just a doc note in `CLAUDE.md` — key is never committed).
- [ ] Defensive: `config.json` malformed → `load_config()` logs a warning and returns `{}` (degrade, don't crash startup).

**Acceptance:** `python -c "from orchestrator.lib import config; print(config.is_fusion_available())"` returns `False` on a clean machine, `True` after the key is set via either env var or file.

---

## Phase F1 — OpenRouter Fusion client  ☐
**Single dispatch.** The HTTP layer, isolated and independently testable.

- [ ] New module `orchestrator/lib/fusion_runner.py` (parallels `claude_runner.py`'s shape: never raises, returns a dataclass result).
- [ ] Constants / defaults:
  ```python
  OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
  DEFAULT_PANEL  = ["openai/gpt-4o", "google/gemini-2.5-pro", "deepseek/deepseek-chat"]
  DEFAULT_JUDGE  = "openai/gpt-4o"          # judge synthesizes the panel
  DEFAULT_TIMEOUT_S = 120
  ```
  (All overridable via `config.fusion_config()`. Budget panel = Gemini Flash + Kimi + DeepSeek per the user's benchmark notes; expose as a named preset.)
- [ ] Dataclass `FusionRun`:
  ```python
  @dataclass
  class FusionRun:
      ok: bool
      analysis: Optional[dict] = None   # {consensus, contradictions, partial_coverage,
                                        #  unique_insights, blind_spots}
      enrichment_md: str = ""           # rendered block to append to the dispatch prompt
      panel_models: list[str] = field(default_factory=list)
      cost_usd: float = 0.0             # summed from OpenRouter usage if present
      duration_s: float = 0.0
      error: str = ""
      raw: Optional[dict] = None
  ```
- [ ] `run_fusion(question: str, *, panel=None, judge=None, timeout_s=...) -> FusionRun`. Implementation uses the **OpenRouter Fusion server tool** entry point so OpenRouter does the fan-out/judge orchestration for us (simplest, matches "attach a tool to a model"):
  - Single POST: `{"model": <judge>, "tools": [{"type": "openrouter:fusion"}], "messages": [...]}` — OpenRouter fans out to the panel (with web search), runs the judge, and returns the structured analysis.
  - Headers: `Authorization: Bearer <key>`, `HTTP-Referer: http://localhost:7878`, `X-Title: orchestrator`.
  - If the server-tool path proves unavailable, fall back to the **plugin/config** entry point (`{"model": "openrouter/fusion", "plugins": [{"id":"fusion","panel":...,"judge":...}]}`) — keep both behind a `_call_openrouter()` helper so the public `run_fusion` signature doesn't change.
- [ ] Parse the judge's structured JSON into `analysis`; if the model returns prose, reuse a `_strip_fences`-style extractor (copy the proven one from `claude_runner.py` rather than re-inventing).
- [ ] `render_enrichment(analysis: dict) -> str` — produce a clearly-fenced Markdown block:
  ```
  ## Multi-model analysis (Fusion — not authoritative, weigh against the repo)
  **Consensus:** …
  **Contradictions:** …
  **Partial coverage:** …
  **Unique insights:** …
  **Blind spots / open questions:** …
  ```
- [ ] Failure modes return `ok=False` with a readable `error` (no key, HTTP non-200, timeout, unparseable judge JSON) — exactly like `embeddings.embed` degrades. **Fusion failure must never block the dispatch** (see F3 policy).

**Acceptance:** with a real key set, `run_fusion("Should we use SQLite WAL mode for a single-writer local app?")` returns `ok=True` with a populated `enrichment_md`; with no key it returns `ok=False, error="OpenRouter key not configured"`.

---

## Phase F2 — Fusion enrichment orchestration  ☐
**Single dispatch.** Glue between the runner and the dispatch pipeline (no UI yet, no app routes).

- [ ] New module `orchestrator/lib/fusion.py` (the "brain layer", parallels `rewriter.py`):
  - `enrich(prompt: str, project_path: str) -> FusionResult` where `FusionResult` carries `{ok, fused_prompt, analysis, cost_usd, duration_s, panel_models, error}`.
  - Builds the fusion *question* from the (already-rewritten or raw) `prompt`. Optionally prepend a one-line framing: "A panel of models is asked to reason about the following engineering task before an executor agent implements it: …".
  - Calls `fusion_runner.run_fusion(...)`.
  - On success: `fused_prompt = prompt + "\n\n" + run.enrichment_md`.
  - On failure: `fused_prompt = prompt` (passthrough) and `ok=False` with the reason — the caller decides whether to surface it.
- [ ] Cap inputs: truncate the fusion question to a sane char limit (mirror `embeddings.MAX_INPUT_CHARS` philosophy; ~12K chars) so we don't fan out a 50KB bundle to 4 paid models.

**Acceptance:** `fusion.enrich(rewritten, project_path)` returns a `fused_prompt` that is the input with the analysis block appended, and passes the input straight through unchanged when the key is missing.

---

## Phase F3 — Wire Fusion into the dispatch pipeline  ☐
**Single dispatch.** Thread a `do_fusion` flag through `/send` → `_send_in_background`.

Target: `app.py`.

- [ ] Extend `_send_in_background(project_id, task, wall_cap_s, do_rewrite, effort, model, do_fusion=False)`:
  - After the existing rewrite block computes `final_task`, and **only if `do_fusion`**, run fusion in the executor (it's blocking HTTP):
    ```python
    fres = await loop.run_in_executor(None, fusion.enrich, final_task, proj["path"])
    if fres.ok:
        final_task = fres.fused_prompt
        fusion_event = {"stage": "fusion_ok", "cost_usd": ..., "duration_s": ...,
                        "panel_models": fres.panel_models}
    else:
        fusion_event = {"stage": "fusion_skipped", "reason": fres.error}
    ```
  - **Policy (deliberate, differs from rewrite):** a fusion *failure does NOT abort the dispatch*. Unlike a failed rewrite (which aborts so the user knows their enrichment never happened), fusion is additive — if it fails we log `fusion_skipped` and dispatch the rewritten/raw task. Rationale: the user already paid for the rewrite and wants the work done; a flaky external API shouldn't black-hole the dispatch.
  - Record the `fusion_event` on the dispatch timeline via `db.record_event(dispatch_id, "stage", fusion_event)` right next to the existing `rewrite_event` recording (after `dispatch_id` exists).
- [ ] Extend the `/send` route signature: `fusion: str = Form("false")`; parse `do_fusion = fusion.lower() in ("1","true","yes","on")`; pass through; include `"fusion": do_fusion` in the JSON response.
- [ ] Guard: if `do_fusion` but `not config.is_fusion_available()`, record a `fusion_skipped` event with reason `"no OpenRouter key configured"` and proceed (don't fail the dispatch).

**Acceptance:** dispatching with `fusion=true` and a valid key produces a dispatch whose final prompt contains the "Multi-model analysis" block and a `fusion_ok` stage event in the timeline; with `fusion=true` and no key, the dispatch still runs and shows a `fusion_skipped` event.

---

## Phase F4 — Dispatch form UI (the toggle)  ☐
**Single dispatch.** Target: `orchestrator/templates/index.html`.

- [ ] Add a **Fusion toggle** (checkbox) to the dispatch form, next to the existing rewrite controls (near the `effort`/`model` selects at index.html:76–94). Label: `fusion (multi-model) ⚡`.
- [ ] In the `send(rewrite)` JS helper (index.html:259), append the fusion flag:
  `fd.append('fusion', document.getElementById('chk-fusion').checked ? 'true' : 'false');`
  Fusion can be combined with either button (`rewrite & send` or `skip rewrite & send`).
- [ ] **Default OFF.** Persist the checkbox state in `localStorage` (same pattern the textarea uses) so a user who likes fusion keeps it on per their preference, but it never defaults on for new users.
- [ ] **Cost hint inline:** small muted text under the toggle: `~4–5× cost; best for architecture/research/high-stakes questions`. (From the user's benchmark notes.)
- [ ] **Key-missing state:** when `config.is_fusion_available()` is `False`, render the toggle **disabled** with a tooltip/inline note: `Set openrouter_api_key in ~/.orchestrator/config.json to enable.` Pass `fusion_available` into the index template context (extend `_view_ctx()` in app.py to include it).
- [ ] Loading copy: when fusion is on, the in-flight banner (index.html:294 area) should read e.g. `rewriting + fusing then dispatching (~15–40s).` so the longer latency is expected.

**Acceptance:** the toggle appears, is disabled with a helpful note when no key is set, persists its state, and sends `fusion=true/false` correctly.

---

## Phase F5 — Surface Fusion in the timeline & cost  ☐
**Single dispatch.** Make fusion runs visible and auditable.

- [ ] In the dispatch detail view (`/dispatch/{id}`, template `templates/dispatch.html`), render the `fusion_ok` / `fusion_skipped` stage events the same way `rewrite_ok` / `rewrite_skipped` are shown: panel models used, cost, duration, and (on skip) the reason.
- [ ] Show the **fusion analysis block** (collapsed by default) on the detail page so the user can read consensus/contradictions/blind-spots that informed the executor.
- [ ] **Cost accounting:** add the fusion `cost_usd` into the dispatch's recorded cost. The outcomes row already has a `cost_usd` column (db.py:113). When `/api/complete` writes the outcome, include any fusion cost recorded on the timeline so the learning loop sees the *true* cost of a fused dispatch (so the future "when is fusion worth it?" analysis is grounded in real numbers).
- [ ] (Optional, same dispatch) a small ⚡ badge on fused rows in the runs panel (`templates/_runs.html`) so fused dispatches are scannable.

**Acceptance:** a fused dispatch's detail page shows panel models, fusion cost, and the collapsible analysis; the outcomes row's `cost_usd` includes fusion spend.

---

## Phase F6 — Config UI & presets (polish)  ☐
**Single dispatch. Optional / last.**

- [ ] Lightweight settings affordance (a `/settings` page or a section on the index) that shows: fusion availability (key present?), current panel models, judge model, and the two presets:
  - **Quality:** GPT-4o + Gemini Pro + DeepSeek, judge GPT-4o.
  - **Budget:** Gemini Flash + Kimi + DeepSeek (≈ half cost, per benchmark notes).
- [ ] Preset is a single `config.json` key (`"fusion_preset": "quality"|"budget"|"custom"`); `config.fusion_config()` resolves it. The page only *reads* config + lets the user pick a preset; the **key is never editable from the browser** (edit the file directly — keeps the secret off any HTTP surface).
- [ ] Document the whole thing in `PLAN.md` (append a "Phase 10 — Fusion mode ✅" entry once shipped) and add a short `## Fusion` section to `CLAUDE.md` noting OpenRouter is the one sanctioned non-Anthropic external service and is opt-in.

**Acceptance:** the user can switch quality/budget presets without editing code; the key stays file-only.

---

## Dispatch order (do these one at a time)
1. **F0** — `config.py` + install.sh template (foundation; no network).
2. **F1** — `fusion_runner.py` (OpenRouter HTTP via stdlib `urllib`).
3. **F2** — `fusion.py` enrichment orchestration.
4. **F3** — wire `do_fusion` through `/send` → `_send_in_background`.
5. **F4** — dispatch-form toggle in `index.html` (+ `fusion_available` in `_view_ctx`).
6. **F5** — timeline/detail surfacing + cost accounting.
7. **F6** — presets/config UI + docs (optional).

Each phase is independently testable and small enough for a single Claude Code dispatch. F0–F4 deliver a working, shippable Fusion toggle; F5–F6 are observability and polish.

## Key file targets (absolute)
- `/Users/tresmith/Documents/orchestrator/orchestrator/lib/config.py` *(new — F0)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/lib/fusion_runner.py` *(new — F1)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/lib/fusion.py` *(new — F2)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/app.py` *(`_send_in_background`, `/send`, `_view_ctx`, `/api/complete` — F3, F5)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/templates/index.html` *(toggle — F4)*
- `/Users/tresmith/Documents/orchestrator/orchestrator/templates/dispatch.html` + `_runs.html` *(surfacing — F5)*
- `/Users/tresmith/Documents/orchestrator/bin/install.sh` *(config.json template — F0)*
- `~/.orchestrator/config.json` *(runtime data — holds `openrouter_api_key`, never in repo)*

## Reuse / consistency notes for the implementer
- Mirror `embeddings.py` for the HTTP call: stdlib `urllib.request`, never raise, return `None`/`ok=False` on any failure, log a warning. **Do not add `httpx`/`requests`.**
- Mirror `claude_runner.py`'s `_strip_fences` for parsing the judge's JSON (copy it; it's battle-tested against prose-wrapped JSON).
- Mirror the `rewrite_event` pattern in `_send_in_background` for the `fusion_event` (record via `db.record_event(dispatch_id, "stage", …)` after the dispatch row exists).
- Remember: **edits don't take effect until you restart `python -m orchestrator`** (uvicorn runs `reload=False` per project memory), and the **auto-push daemon commits within seconds** — `git diff` won't show your changes.
