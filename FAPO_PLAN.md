# Orchestrator — FAPO-style Brain-Prompt Optimizer *(DESIGN STUDY — not approved, not built)*

**Scope of this document.** Research + design only. It evaluates whether the orchestrator
should build a **local, `claude`-CLI version of a FAPO-style closed-loop, eval-driven
optimizer** for its brain-call pipeline (rewriter / summarizer / onboarding). **It modifies
zero source under `orchestrator/`** — the only artifact is this file at repo root (peer of
`FUSION_PLAN.md` / `PLAN.md`). No optimizer code is written or approved here.

**The question, three ways.** (1) Adopt Cisco's FAPO framework? (2) Build a *full*,
autonomous closed-loop optimizer from scratch on the `claude` CLI? (3) Borrow FAPO's loop
*shape* at a drastically reduced, human-gated scope? The verdict (§7) answers each
separately: **No / No-not-now / Qualified-yes** — because the decision hinges on something
internal to the orchestrator, not on FAPO.

> **Read §1 first.** FAPO needs three things the orchestrator does not have: a labeled
> dataset with **expected outputs**, a **scorer**, and a **pipeline definition**. A confident
> "yes, build it" silently assumes we can cheaply manufacture the first two. We cannot — at
> least not validly. That gap, not FAPO's integration seam, is the whole decision.

> **DECISION — 2026-06-22: NO-GO.** The operator declined **any** human in the loop — neither
> the active approval gate (§9 Fa3) nor passive capture of the accept/edit/reject signal the
> human already produces at dispatch (§8). That human judgment was the **only valid scorer**
> available (§1, §5): without it, a candidate prompt can be scored only by Claude-grading-Claude
> (circular — converges on prompts that flatter the judge) or by `outcomes.outcome` (confounded
> process status). An unattended optimizer would therefore drift toward self-pleasing prompts —
> precisely the failure FAPO's guardrail layer exists to prevent. The §9 human-gated MVP is
> declined and the overall verdict resolves to **no-go**. §1–§11 stand as the rationale of
> record; **revisit only if a valid, non-circular, non-human scorer becomes available** (none
> exists today). The §3 finding still holds and is worth remembering: FAPO is *not* dead at the
> integration seam (its optimizer is already a `claude`-CLI agent) — the blocker is internal.

---

## 1. The lead ambiguity — the dataset + scorer gap *(this is the decision)*

FAPO is an **eval-driven** optimizer: it improves a prompt by *measuring* candidate prompts
against ground truth and keeping the ones that score higher. That presupposes three inputs.
The orchestrator has **none** of them, and the two hard ones cannot be conjured cheaply
without re-introducing the exact failure modes FAPO was built to prevent (§5).

| FAPO requires (verified, README) | Orchestrator has | Verdict |
|---|---|---|
| **A labeled dataset** — JSONL cases each with `expected` *"(required)"* ground truth | `outcomes.outcome` ∈ {`completed`,`killed`,`failed_to_spawn`,`orphaned`,`paused`} — a **process-status enum** (`db.py`, `app.py:502`); `what_worked`/`what_broke`/`lessons` free-text from the summarizer | **MISSING.** No expected output, no quality grade. |
| **A scorer** → `composite_score ∈ [0,100]` | No score/grade/reward/label column **anywhere** in `db.py` (the only `correctness` token is a comment: *"events are best-effort UX, not correctness-critical"*) | **MISSING.** |
| **A pipeline definition** (a LangGraph state graph) | rewriter → executor → summarizer → embed → retrieve, glued in `app.py`/`rewriter.py` — not a replayable graph | **MISSING (different shape).** |

**Why `outcomes.outcome` is not a reward.** It is a *process status*, not a *correctness
grade*, and it is **confounded** in both directions:

- `completed` only means the executor reached Stop without a kill — it **hides a bad rewrite
  the executor recovered from** (a strong Claude executor papers over a weak prompt).
- `killed` is frequently the **loop-watchdog** firing (`manual_kill(reason="loop:<tool>")`,
  Phase 7) — a runaway *tool* pattern unrelated to rewrite quality.

Optimizing a prompt toward "more `completed`" would chase a signal that barely correlates
with the thing we want to improve.

**Why the only cheap scorer is invalid.** The sole "scorer" the orchestrator could stand up
for free is **another `claude` brain call** — LLM-as-judge. But the rewriter *author*, the
proposed-edit *proposer*, and that *judge* are all the same model family. That is a
**self-referential reward loop**: it converges on prompts that please Claude's own scorer,
not prompts that produce better dispatches. (§5 develops this.)

**The only near-ground-truth the system can actually generate is human.** Not
`outcomes.outcome` — the **human accept / edit / reject** decision on the rewrite preview.
The UI already surfaces an *editable* rewrite and a rewritten-vs-original dispatch choice
(`app.py` `/send`, `apply_edits.html` `rewritten_task`), but **nothing persists which path
the human took or how far they edited**. That signal is the cheapest *valid* label available
— and it must be **captured before any eval-driven work** (§8).

**This is not a new realization for this repo.** `FUSION_PLAN.md` **§11.c.6** ("Task-type
routing via local outcome logs") is FAPO's smaller cousin and was ranked last, with the
decisive RISK line:

> *"eval-driven selection presumes **labeled ground-truth the orchestrator does not have** —
> 'which past dispatch was better?' is unscored today, so this quietly smuggles in a
> benchmark/scoring build effort. Speculative until outcomes are scored (11.g-6)."*

FAPO is the **more ambitious** version of that same idea and hits the **same wall, harder**
(it needs per-case `expected`, not just a coarse preset preference). Nothing below pretends
otherwise.

---

## 2. What FAPO actually is *(primary-source grounded)*

Released **2026-06-17** by Cisco Foundation AI. Verified directly against the repo's own
README and source (not journalism):

- **Repo:** `github.com/cisco-foundation-ai/fully-automated-prompt-optimization` (Apache-2.0,
  Python 3.10+, package `hephaestus` v0.1.0). **Paper:** arXiv `2606.19605` (Kassianik et al.,
  submitted 2026-06-17). *Naming nuance:* the repo slug says **Automated**, the README H1 +
  paper title say **Autonomous** — same system.
- **Maturity:** young but well-engineered research drop (~73★ at fetch, ~30-file `tests/`,
  CI, multiple example "tenants"). Not production-proven at ~5 days old. *Provenance caveat:
  README and the four decisive source files were read verbatim; the paper PDF body was not —
  treat in-paper numbers beyond the README (15/18 vs GEPA, +14.1pp) as secondary.*

**The architecture splits into two independent layers** — conflating them is the trap:

1. **The optimizer** (the brain running the loop) **IS Claude Code itself, driven as a CLI
   agent.** Verbatim README: *"The optimizer is Claude Code. It reads the playbook, runs
   evals, dispatches subagents, writes variants, compares results, and decides when to
   escalate. **It never appears in your config.**"* It ships as Claude Code slash-commands +
   subagents (`.claude/agents/optimization.md`, `step-attribution.md`, `variant-reviewer.md`),
   mirrored for Codex. There is **no `anthropic` dependency** in `pyproject.toml`.
2. **The task model** *being optimized* is reached through a tiny
   `ProviderClient.generate(messages) -> str` interface — *"The two are independent — you can
   optimize a Gemma pipeline using Claude as the optimizer."*

**The loop (six stages, verbatim README):**

> 1. **Evaluate** — Dataset → Chain → Scorer → Results
> 2. **Attribute** — classify failures by pipeline step and fix type
> 3. **Propose** — generate one scoped variant (prompt / parameter / chain)
> 4. **Review** — independent guardrail check (scope, leakage, placeholders)
> 5. **Compare** — re-run the variant; compare to the previous best
> 6. **Iterate or escalate** — keep improved variants; escalate level when attribution requires

**The guardrails (this is the sophisticated part):** *"Split access controls — the optimizer
sees individual **training** cases; validation and test expose **aggregate scores only**."*
Plus a dedicated **`variant-reviewer`** subagent that runs with *"fresh context — no carryover
from the writing process"* and **blocks** (`pass|warn|fail`) on *No Dataset Leakage*, *No
Train-Example Leakage*, *No Example-Specific Hints* ("rules so narrow they could only fire on
one input" = metric-gaming), and *Scope Compliance*. Variants are immutable numbered files;
there is an iteration-memory log. **This machinery exists precisely because autonomous
prompt-optimization overfits and games the scorer by default.**

---

## 3. The integration seam — does FAPO die like `headroom`? *(No — but that's not the point)*

The test that killed the `headroom` token-optimizer was the **integration seam**: it needed
an LLM **API key**, and on a $0 subscription with no API path that made it both
non-compliant and pointless. Applying the same test to FAPO:

| Seam question | Answer | Source |
|---|---|---|
| Does the **optimizer** need an API key / SDK? | **No.** It *is* the `claude` CLI, "never appears in your config." | README (verbatim, §2) |
| Is the **task-model** client hard-wired to a provider SDK (DSPy-style)? | **No.** `ProviderClient` ABC; `build_provider_client(name, settings)` dispatches `openai`/`baseten`/`base10`/`sagemaker`, else `raise ValueError` | `providers/__init__.py` (read verbatim) |
| Can the task model be a **local / arbitrary** endpoint? | **Yes** — `provider:"baseten"` is a generic OpenAI-compatible client with a config-settable `base_url`; an `[local-models]` extra exists | `providers/baseten.py` |
| Does it require **batched/concurrent** calls? | **No.** `generate()` is one synchronous `chat.completions.create(n=1, stream=False)`; `max_workers` **defaults to sequential** | `providers/openai.py`, `runs/eval_runner.py` |

**So the seam is passable** — strikingly so. FAPO's optimizer is *already* the "brain runs
through the visible `claude` CLI, never a hidden API call" pattern this project mandates, and
its task-model client runs serially and can point at a local shim. **Unlike `headroom`, FAPO
does not die at the seam.**

**But the seam was never the binding constraint here.** The blocker is §1: FAPO mandates a
labeled dataset + scorer + graph the orchestrator lacks. The headroom lesson was "check the
seam *that would kill adoption*." For FAPO that seam is **internal** — the missing
ground-truth — and no amount of CLI-compatibility fixes it.

> **Could we drive `hephaestus` itself?** In principle: write a custom `ProviderClient` whose
> `generate()` shells out to `run_claude_json` (each call = one visible tab), edit the
> hard-coded factory, re-express the brain pipeline as a LangGraph chain, hand-label a dataset,
> and write a scorer. That is a large build whose hard prerequisite (labeled `expected`) we
> still can't satisfy validly — and whose eval step floods iTerm2 (§6). **Adopting the
> framework is not the cheap path; see §7.**

---

## 4. Mapping FAPO's loop onto the *actual* pipeline

The orchestrator's pipeline is **rewriter → dispatched executor → summarizer → embed →
retrieve**. FAPO's six stages map onto it as follows — with three partial analogs that are
*less* than they look:

| FAPO stage | Orchestrator analog today | Gap |
|---|---|---|
| **Evaluate** (dataset→chain→scorer) | — *(no scorer, no labeled set)* | **Absent.** |
| **Attribute** (step-level, 2-phase) | `summarizer` `what_broke` (one free-text field) | **Wrong granularity** — see below. |
| **Propose** (one scoped variant) | — *(prompts are hand-edited)* | Absent (this is what an MVP would add, §9). |
| **Review** (independent guardrail) | `_verify_prompt`/`_rejudge_prompt` (F11.c.1) | **Wrong altitude** — see below. |
| **Compare** (re-run vs best) | — | Absent (needs the scorer). |
| **Iterate/escalate** | — | Absent. |

**Altitude insight — the orchestrator already runs a *degenerate* FAPO loop, but at the wrong
level.** `run_fusion_json` does panel → judge → **verify → re-judge → keep-the-better-text**
*per call, at inference time*. FAPO lifts that same shape to the **prompt-template level,
across calls** — it edits `REWRITER.md`, not one rewrite's output. So FAPO is not a new
*capability* for this repo; it is the existing keep-if-better discipline moved up one altitude
(from "this answer" to "the prompt that generates all answers"). That reframing is the single
most useful thing to take from FAPO.

**The reviewer analog is real but mis-aimed.** `_verify_prompt`/`_rejudge_prompt`
(`claude_runner.py`) is a **correctness reviewer of a fusion *output***: "did the judge's
synthesis get the task right?" FAPO's `variant-reviewer` is a **quality+safety reviewer of a
proposed *prompt change***: "does this edit leak a training case / over-narrow to one input /
exceed scope?" They share a shape (a fresh-context critic) but guard **different things**. We
have the former; a FAPO-style optimizer needs the **latter**, which does not exist. Claiming
"we already have the reviewer" is half-true and hides the genuinely missing component.

**Is `what_broke` enough for failure attribution? No.** FAPO attributes *step-level*
(deterministic heuristics over recorded `step_outputs`, then LLM analysis) and tags each
failure **prompt-addressable vs structural**. `what_broke` is a single post-hoc free-text
field summarizing the **executor's** trace — it attributes *within execution*, one level below
where a prompt-optimizer needs it. It **cannot distinguish**:

- a **bad rewrite** (the prompt-optimizer's target), from
- a **good rewrite the executor botched** downstream, from
- **bad retrieval context** (`rewriter.py:104` injected an irrelevant past task).

`dispatch_events` logs `stage`/`tool_use`/`tool_result` and a `rewrite_ok` event with a
`panel_breakdown` — but that is **descriptive cost/token telemetry, carrying no quality grade
of the rewrite**. Genuine FAPO-style attribution would require **new per-pipeline-step
structured logging** (rewrite-quality, retrieval-relevance, executor-outcome as *separable*
fields) that the schema does not have.

---

## 5. Scorer-hacking, circularity & data-leakage *(why a naive "yes" is dangerous)*

FAPO ships an entire guardrail layer because eval-driven optimization **games the metric by
default**. A local reimplementation inherits every one of these hazards — and the
orchestrator's architecture makes two of them *worse*.

- **Circular / self-referential scoring.** If the scorer is an LLM-as-judge `claude` call,
  the **same model family authors the rewrite, proposes the edit, *and* grades it**. The loop
  converges on prompts that flatter Claude's own judgment, not prompts that produce better
  dispatches. The only escape is to score against the **executor's downstream outcome** — but
  that is `outcomes.outcome`, which is **confounded** (§1). So both available scorers are bad:
  one circular, one confounded.
- **Built-in train/test leakage.** FAPO explicitly blocks *"No Train-Example Leakage"* (few-shot
  examples copied from training cases). The orchestrator's rewriter **already injects the top-5
  semantically-similar past dispatches** into its prompt (`rewriter.py:104` →
  `retrieval.find_similar`). So **any eval set drawn from past tasks is pre-leaked** — the test
  input is already in the few-shot context, and the eval would measure recall of a near-identical
  past example, not generalization. FAPO's own guardrail names this exact failure; here it is
  *baked into the production pipeline*.
- **Tiny-sample meaninglessness.** "Keep-if-better" on a handful of noisy LLM-scored cases can
  **enshrine a worse prompt**. FAPO guards this with train/validation/test splits where test
  exposes *aggregate scores only* — machinery that **presupposes a dataset big enough to split**.
  A single project's dispatch volume likely cannot fill even one split. Keep-if-better is not
  safely operable here yet.
- **The tier hyperparameter is a trap.** Memory + `CLAUDE.md`: summarizer and onboarding are
  **deliberately Sonnet/medium**; an optimizer must **not** "fix" them to Opus. FAPO's action
  space normally *includes* parameters (it would happily search the model tier). The guard must
  be **structural, not lexical**: the MVP optimizer's action space is **prompt *text* only — it
  literally cannot emit a model/effort change.** (A lexical "don't say Opus" guard is
  insufficient — a prompt can be tuned toward Opus-only phrasing without ever naming it.)

**Net:** a local FAPO would need to *rebuild FAPO's guardrail layer* (variant-reviewer with
leakage/scope/narrowness blocks, split access, immutable variants) **on top of a scorer it
doesn't validly have.** That is the real cost of "yes."

---

## 6. Cost / runtime / tab-spam — the $0 paradox

The $0 subscription **removes FAPO's headline concern** (token cost of many eval iterations).
But the constitution's **"visible, never headless"** rule **re-imposes a harder ceiling**, and
it bites exactly where FAPO spends: the eval loop.

**Every eval iteration is a visible-tab `claude` call.** A single FAPO-style optimization of
*one* prompt is roughly `V variants × N eval-cases × (1 generate + 1 LLM-judge)` calls, plus
the long-running optimizer agent tab, plus a step-attribution and a variant-reviewer subagent
per variant. Concretely, a *minimal* run:

> 6 variants × 8 eval-cases × (rewrite + judge) ≈ **96 visible tabs**, **serialized** under
> `spawn.py`'s `_TAB_SPAWN_LOCK`, each a full `claude` session of tens of seconds to minutes —
> i.e. **~100 tabs and hours of wall-clock for ONE prompt's optimization.**

This is `FUSION_PLAN.md` §11.e's "tab storm" taken to its logical extreme, and it **must never
go headless to scale** (hard rule). The binding costs are therefore **not dollars** but:

1. **Wall-clock** — serial visible tabs; a real search is hours.
2. **Tab-spam** — ~100 tabs/run floods iTerm2 (the §11.e ceiling).
3. **Subscription self-throttle** — every eval tab draws the *same* subscription as the user's
   real dispatches and can rate-limit the very work it's meant to improve (the §10.b / §11.e
   self-defeat).
4. **Operator attention** — "visible" only has value if a human *can* watch; 100 tabs no one
   watches is the worst of both worlds.

**The method's power (many automated iterations) trades directly against the constraint
(each iteration is slow + screen-occupying).** This is decisive for scope: any buildable
version must **shrink the iteration count to near-1**, i.e. abandon the automated *search* and
keep only the *single scoped proposal* (§9).

---

## 7. Verdict — Go / No-Go

Three questions, three answers:

**7.1 — Adopt the FAPO / `hephaestus` framework? → NO.** It mandates a LangGraph state-graph
pipeline, a labeled `expected` dataset, and a `[0,100]` scorer — the orchestrator has none.
Driving it would also require a custom `ProviderClient` shimming `run_claude_json` (one tab per
`generate()`) and ~100-tab eval sweeps (§6). The seam is compatible (§3) but the prerequisites
are not present; this is not the cheap path.

**7.2 — Build a *full*, autonomous closed-loop optimizer locally? → NO, not now.** The "F"
(full automation) collides head-on with three project realities:
- **No valid scorer** — the only candidates are circular (Claude grading Claude) or confounded
  (process status) (§1, §5).
- **The auto-push daemon** commits + pushes any edit to `REWRITER.md` to `origin/main` *within
  seconds* — an autonomous optimizer would ship un-reviewed prompt changes to the live repo with
  no human in the loop. The gate **cannot** sit after the write.
- **The visible-tab throughput ceiling** (§6) makes a real automated search operationally
  brutal. FAPO's whole value is iteration count; the constitution caps iteration count.

**7.3 — Borrow FAPO's loop *shape* at radically reduced, human-gated scope? → QUALIFIED YES**,
as the MVP in §9 — **and be honest about the framing.** With a **human as the scorer and
keeper** (FAPO stages 1/5/6), what remains is FAPO stages 2–4 (attribute → propose → review) as
a **one-shot prompt-edit *proposer*, not an optimizer.** That is **no longer "F"APO** — the
automation is gone. The FAPO framing **earns its keep only as a source of *discipline***: the
"smallest scoped change," the immutable-numbered-variant log, and especially the
**variant-reviewer's leakage/scope/over-narrowness checks**, which are exactly the guards a
naive prompt-suggester would skip. It does **not** earn its keep as an automation target here.

**Honest bottom line.** This mirrors the `headroom` (RESEARCH → DECLINED) and the §10/§11
design-study (DEFER) precedents: **decline the literal tool and the full loop; adopt only the
shape, gated by a human, and only after the prerequisite signal exists (§8).** If you build
nothing, the existing `_verify`/`_rejudge` critic already captures much of the *per-call*
quality gain — the marginal value of a *cross-call* prompt loop is unproven (§11-Q1).

---

## 8. Precursor — capture the human signal *(do this first, regardless of FAPO)*

**Before any eval-driven anything**, instrument the **only non-circular ground-truth the
system can generate**: the human's decision on the rewrite preview. The raw material already
flows through the UI; it is simply not persisted as a label.

- **Capture:** on `/send`, record whether the human **(a) dispatched the rewrite verbatim,
  (b) edited it then dispatched** (store an edit-distance / diff), or **(c) dispatched the
  original / rejected the rewrite.** Persist on the dispatch row or a new `rewrite_feedback`
  table — **in the DB / `~/.orchestrator/`, never the repo.**
- **Why it matters:** "verbatim-accept" ≈ a positive label, "rejected-for-original" ≈ a
  negative label, edit-distance ≈ a graded signal — all **human-sourced, not Claude-grading-
  Claude.** This is the cheap signal §11.g-6 asked for and directly de-speculates §11.c.6.
- **Cost:** trivial, no LLM call, no API, fully local. It is the highest-leverage step in this
  whole document and is **worth doing even if §9 is never built.**

Until this exists, the MVP's "is this prompt better?" question has **no valid answer**, and any
keep-if-better gate is statistically hollow (§5).

---

## 9. The MVP — *if/when* you build it *(smallest useful, human-gated)*

A **single-shot, human-approved prompt-edit *proposer*** for the **rewriter only**. It
implements FAPO stages 2–4 (attribute → propose → review); the **human is stages 1/5/6**
(eval / compare / keep). **No automated eval, no scorer, no keep-if-better loop.**

**Flow:** recurring rewriter-failure pattern (from summaries) → one optimizer brain call
(visible tab) proposes ONE scoped `REWRITER.md` diff → variant-reviewer check → **human
approves in the UI** → *human applies the edit*.

| Phase | Scope | Deliverable | Hard constraints |
|---|---|---|---|
| **Fa0** | Signal | (Depends on §8.) Cluster recurring `what_broke` themes across rewriter-failed dispatches into a pattern with a recurrence count `K`. | Read-only over the DB; **no LLM call** (string/embedding clustering, reuse `retrieval`). State in `~/.orchestrator/`. |
| **Fa1** | Propose | When a pattern recurs ≥ K (e.g. 3), **one** `run_claude_json` brain tab reads {clustered failures + current `prompts/REWRITER.md`} and returns **one scoped diff** + rationale. | Visible tab, $0, no API. Action space = **`REWRITER.md` text only** — the schema cannot express a model/effort/tier change (§5 structural guard). |
| **Fa2** | Review | A **fresh-context** `run_claude_json` variant-reviewer (FAPO's blocking checks): no train-example leakage, no over-narrow rule, in-scope, preserves the JSON-output contract. Returns `pass\|warn\|fail`. | Distinct from the existing `_verify`/`_rejudge` (which reviews *fusion output*, not a *prompt edit*) — §4. |
| **Fa3** | Human gate | Surface the **diff** (old vs proposed `REWRITER.md`) + rationale + reviewer verdict in a UI pane; **Approve** writes the file, **Reject** discards. | **The gate sits BEFORE the file write** — `REWRITER.md` is under `orchestrator/` and auto-pushed to `origin/main` within seconds, so an un-gated write ships un-reviewed. Proposed variants stage in `~/.orchestrator/`; only an approved one touches the repo, applied as a human-owned commit. |

**Deliberately excluded from the MVP** (each is a separate, harder decision):
- **summarizer / onboarding prompts** — out of scope; they are deliberately Sonnet/medium and
  carry no UI quality signal. Rewriter only.
- **automated eval / scorer / keep-if-better** — the human is the scorer. Adding the automated
  loop reintroduces §5 (circular scorer) and §6 (tab-spam) and is **not** part of "smallest
  useful."
- **auto-apply** — never. Every prompt change is human-approved before the write.

**Does the FAPO framing earn its keep in the MVP?** Partially, and the doc should say so
plainly: the *value* is the **discipline** (attribute a concrete recurring failure → propose
*one scoped* change → independent leakage/scope review → human keeps). Strip that and the MVP
collapses into "a hint button that suggests a prompt tweak." The framing's worth is the
**guardrails (Fa2)**, not the automation.

---

## 10. Hard-rule compliance *(per rule, per component)*

| Rule | How the MVP honors it |
|---|---|
| **No Anthropic API calls** | Optimizer (Fa1) + reviewer (Fa2) are `run_claude_json` visible tabs on the subscription — $0, no API. FAPO's own optimizer is *already* CLI-driven, so this is the natural shape. Strictly *better* than Fusion (which egresses to external providers); a FAPO-lite proposer touches **no external API at all**. |
| **Visible, never headless** | Every optimizer/reviewer call is a watchable tab. The MVP **shrinks iteration to ~1 proposal** precisely so the visible-tab ceiling (§6) is not breached; it must **degrade by reducing scope, never by going headless.** |
| **Local only** | No remote workers, no hosted eval. All compute is the local `claude` CLI + local clustering. |
| **Data in `~/.orchestrator/`, repo stays clean** | Patterns, staged variants, iteration memory, and human-signal labels all live in the DB / `~/.orchestrator/`. **The one exception** — the optimization *target* `prompts/REWRITER.md` — is source under `orchestrator/`; resolved by Fa3's **human-gate-before-write** (the human's approval *is* the commit). |
| **Stop-hook env-gating** | Optimizer/reviewer tabs set `ORCHESTRATOR_BRAIN_ID`, never `ORCHESTRATOR_RUN_ID` (as all brain calls already do), so the Stop hook stays a no-op. |
| **Don't "fix" Sonnet tiers** | Structural action-space guard (§5): the proposer can emit prompt **text** only; it cannot express a tier change, and summarizer/onboarding are out of scope entirely. |
| **Auto-push hazard** | The human gate is **before** the `REWRITER.md` write; verify any landed change with `git show HEAD`, not `git diff` (the daemon commits within seconds). |

---

## 11. Open questions *(resolve before any implementation)*

1. **Does a cross-call prompt loop beat the existing per-call `_verify`/`_rejudge` critic?**
   The §10.d / §11-Q1 caution recurs: multi-step quality machinery helps most on hard, complete
   judgments. Unproven that editing `REWRITER.md` beats the inference-time critic we already
   ship. Needs an A/B — which presumes the §8 signal.
2. **Is the human accept/edit/reject signal (§8) frequent and clean enough** to label patterns,
   given that many dispatches skip the rewrite entirely?
3. **What recurrence threshold `K`** avoids both over-eager proposals (noise) and never-firing?
   How are `what_broke` themes clustered without an LLM call (embedding distance? tag overlap?)?
4. **Can failure attribution actually separate "bad rewrite" from "good rewrite / bad executor"**
   (§4) with current logging, or does Fa0 first require new per-step fields? If the latter, the
   MVP grows a schema change before it can even fire.
5. **Diff-review UX:** what does the Fa3 approve/reject pane look like, and how is a rejected
   proposal remembered so it is not re-proposed next cycle (FAPO's iteration memory)?
6. **Rollback:** if an approved edit later regresses rewrites, what reverts it? With no scorer,
   regression is only detectable via the §8 human signal trending down — define that tripwire.
7. **Does the MVP's value survive honest framing?** If, after §8 data, the proposer mostly
   restates what a human would hand-edit anyway, decline it — the §8 signal-capture alone may be
   the entire worthwhile outcome of this study.

---

**STOP — design only. No optimizer code written or approved. Zero source under `orchestrator/`
modified by this document.**

### Provenance
- **Verified verbatim (read directly):** repo README; `src/hephaestus/providers/__init__.py`
  (factory dispatch + `raise ValueError`, no `anthropic`/`claude`); the two-layer optimizer/
  task-model split; four required inputs + `expected` "(required)"; the six-stage loop; split-
  access + variant-reviewer guards.
- **Inferred (code, not run):** serial backend works (`max_workers` default-sequential +
  blocking `generate()`); ~30-line custom `ProviderClient` to add a CLI backend.
- **Secondary (not independently read):** arXiv `2606.19605` PDF body; MarkTechPost. Benchmark
  numbers beyond README (15/18 vs GEPA, +14.1pp) treated as secondary.
- **Orchestrator side (this repo):** `outcomes` enum (`db.py`, `app.py:502`); no score column
  (`db.py`); retrieval injection (`rewriter.py:104`); fusion critic (`claude_runner.py`
  `_verify_prompt`/`_rejudge_prompt`); prior art `FUSION_PLAN.md` §11.c.6 / §11.g-6.
