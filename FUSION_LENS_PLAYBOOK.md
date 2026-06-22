# Fusion Lens Playbook

How to assign **per-seat lenses** across a Fusion panel for different kinds of work — which lens
to put on which model, and how many seats to run.

A lens makes a panel seat answer the *same* task through one perspective, so the seats make less
**correlated** errors and the judge gets genuinely different angles to synthesize. The judge always
sees the original task **verbatim** — lenses bias only the panel, never the required output format.

> **Honest caveat:** this orchestrator has no scorer / ground-truth, so none of these combos are
> *measured*-best. They're principled: maximize **distinct angles** (decorrelation) and **match each
> lens's reasoning demand to the model's capability**. Treat them as strong starting points — then
> read the per-seat lens badge in the dispatch breakdown and adjust what actually helps.

---

## The 10 lenses at a glance

| Lens | The failure axis it owns |
|---|---|
| `risks` | downside enumeration — failure modes, edge cases, security/correctness hazards |
| `simplest` | the minimal path *now*; cut needless complexity |
| `ambiguity` | what's underspecified in the **question**; assumptions a confident answer smuggles in |
| `first-principles` | reject the framing; re-derive from the actual goal + constraints |
| `user-intent` | the goal **behind** the literal request; who it's for |
| `long-horizon` | future-change cost — aging, scale, lock-in (*not* present minimalism) |
| `concrete` | the exact **runnable artifact** — code, command, value, worked example |
| `adversary` | red-team a *committed* answer — the counterexample, the input that breaks it |
| `precedent` | reuse prior art — the existing pattern, library, in-repo convention |
| `evidence` | distrust the **facts** — demand sources/verification, seek disconfirming evidence |

---

## Three rules for "which lens on which model"

### Rule 1 — your smartest model is the **judge**, not a panel seat
Resolving disagreement is the hardest job and caps final quality. Don't spend your #1 as one voice
among many. Build the panel from #2 downward.

### Rule 2 — tier lenses by reasoning demand

| Bucket | Lenses | Put on |
|---|---|---|
| **Deep / generative** | `first-principles` · `adversary` · `long-horizon` | your **top** panel seats — a weak model here yields confident nonsense |
| **Discipline / stance** | `user-intent` · `risks` · `evidence` · `simplest` · `ambiguity` | your **mid** seats — applies a viewpoint, no deep derivation |
| **Grounding / lookup** | `concrete` · `precedent` | your **weakest** seats — hard to botch; "show the exact artifact / use the known pattern" keeps weak models honest |

### Rule 3 — pick lenses by the task's **dominant failure mode**, not by maxing count
There are only ~6–7 truly orthogonal angles, so past ~6 seats you repeat axes and just tax the
judge. More distinct angles beat more headcount, every time.

---

## Scenario loadouts

Judge = your #1 (constant). Loadouts are ordered **smartest seat → weakest**. **★** = keep this one
if you trim seats. `(parens)` = optional / add for high stakes.

### Design & greenfield

| Task | Seats | Lens loadout (smart → weak) | Dominant failures |
|---|---|---|---|
| **Architecting systems** | 5–6 | ★`first-principles` · `long-horizon` · `adversary` · `simplest` · `precedent` (+`risks` if safety/security-critical) | wrong framing · over-engineering · scale/lock-in · reinventing |
| **Designing an API / interface / schema** (a contract others depend on) | 4–5 | ★`long-horizon` · `user-intent` · `adversary` · `simplest` · `precedent` | can't evolve it later · awkward for consumers · unhandled misuse · bloated surface |
| **Websites / apps / UIs** | 3–4 | ★`user-intent` · `simplest` · `precedent` · `concrete` | wrong user flow · over-complex UI · reinvented components · hand-wavy output |
| **Building games** | 3–4 | ★`user-intent` (fun) · `simplest` · `concrete` · (`adversary` for exploits) | not fun · scope creep · vague mechanics |

### Working in an existing codebase

| Task | Seats | Lens loadout (smart → weak) | Dominant failures |
|---|---|---|---|
| **Add / modify a feature in a large codebase** | 4–5 | `adversary` · `long-horizon` · ★`precedent` · `risks` · `simplest` | broke something else · ignored conventions · added debt. *(`first-principles` deliberately omitted — work* within *the design, don't re-derive it)* |
| **Fixing broken code** | 3 | ★`adversary` · `first-principles` · `simplest` | wrong root cause · unreproduced trigger · over-broad fix. *(Keep it small — debugging is convergent)* |
| **Refactoring / migration / tech-debt** | 4 | ★`adversary` · `long-horizon` · `simplest` · `precedent` (+`risks` if load-bearing) | silently changed behavior · "refactor" that adds complexity · half-migration leaving two patterns |
| **Performance optimization** | 4–5 | `first-principles` · `adversary` · ★`evidence` · `long-horizon` · `simplest` | optimized the wrong thing · unmeasured/vibes · regressed correctness for marginal gain |
| **Code review / reviewing a diff** | 4–5 | ★`adversary` · `risks` · `long-horizon` · `precedent` · `simplest` | missed bug · regression · debt · convention drift |

### Correctness, risk & quality

| Task | Seats | Lens loadout (smart → weak) | Dominant failures |
|---|---|---|---|
| **Security review / audit** | 4–5 | ★`adversary` · `first-principles` · `risks` · `evidence` · `precedent` | missed exploit path · misplaced trust · theoretical findings with no real impact |
| **Writing / improving tests** | 3–4 | ★`adversary` · `evidence` · `concrete` · `risks` | only the happy path · tautological asserts · tests the mock, not the behavior |
| **Data analysis** | 3–4 | `adversary` · `first-principles` · ★`evidence` · (`concrete`) | unsupported claims · confounders · wrong metric for the question |
| **DevOps / infra / deploy / CI-CD** | 4 | ★`adversary` · `risks` · `long-horizon` · `precedent` (+`concrete` for the actual config) | untested failure path · secret leak / blast radius · bespoke fragile setup |

### Product, data surfaces & content

| Task | Seats | Lens loadout (smart → weak) | Dominant failures |
|---|---|---|---|
| **Dashboards** | 3–4 | ★`user-intent` · `evidence` · `simplest` · (`precedent`) | shows-everything-answers-nothing · wrong/inconsistent numbers · clutter |
| **Ecommerce tasks** | 3–4 | ★`adversary` · `risks` · `user-intent` · (`precedent`) | money/checkout edge cases · security/PII · lost conversion intent |
| **Documentation / technical writing** | 3 | ★`user-intent` · `concrete` · `simplest` (+`ambiguity` for a fuzzy system) | writer-centric not reader-centric · no examples · bloated |

### Investigate & plan

| Task | Seats | Lens loadout (smart → weak) | Dominant failures |
|---|---|---|---|
| **Research / investigation / "how does X work"** | 3–4 | `adversary` · `first-principles` · ★`evidence` · `ambiguity` | confident-but-unsourced · accepted the first explanation · no disconfirmation |
| **Planning / estimation / scoping** | 3–4 | `first-principles` · `risks` · ★`ambiguity` · `simplest` | missed unknowns · hidden scope · gold-plating the plan |

### Default / mixed / unsure

| Task | Seats | Lens loadout (smart → weak) | Dominant failures |
|---|---|---|---|
| **Anything not listed / mixed** | 4 | ★`risks` · `user-intent` · `simplest` · `first-principles` (→`concrete` on weak models) | breaks · wrong goal · over-built · wrong framing |

---

## Other / niche situations

Quick combos for less-common work (no full rows — adapt seat counts):

- **Prompt / LLM-pipeline engineering** — `adversary` (adversarial inputs, jailbreaks, failure cases) + `first-principles` (the actual task the prompt must do) + `evidence` (test cases prove it works) + `ambiguity` (underspecified instructions).
- **Accessibility / i18n / compliance pass** — `risks` (what's non-compliant) + `adversary` (screen-reader / RTL / edge-locale cases) + `precedent` (WCAG / standard patterns).
- **Content / copywriting / marketing** — `user-intent` (audience + conversion) + `adversary` (objections, what won't land) + `concrete` (specific, not generic) + `simplest`.
- **Naming / small design choices** — `user-intent` + `precedent` + `simplest` (2–3 seats; low-stakes — don't over-panel).
- **Config / build / tooling setup** — `precedent` (standard setup) + `concrete` (exact config) + `risks` (footguns). Weak-model-friendly.

---

## How to generalize to anything else

When a task isn't listed, classify it by **where it tends to fail**, then pull that bucket:

- **Greenfield / architecture** → framing & future: `first-principles` · `long-horizon` · `simplest` · `precedent`
- **Inside an existing system** → fit & blast-radius: `precedent` · `risks` · `long-horizon` *(skip `first-principles` — you can't re-derive the whole framing)*
- **Money / security / correctness** → `adversary` + `risks` + `evidence`
- **Product / UX** (UIs, sites, dashboards, games, storefronts) → `user-intent` + `concrete` + `precedent`
- **Debugging** → `adversary` + `first-principles` + `simplest`, and stay small

### Productive pairs (give the judge real tension to resolve, not echoes)
- `first-principles` ↔ `precedent` — *invent vs. reuse* (architecture & greenfield)
- `simplest` ↔ `long-horizon` — *minimal now vs. resilient later*
- `user-intent` ↔ `concrete` — *the goal vs. the exact artifact*
- `risks` **or** `adversary` as the stress-tester on top — run **both** only in money/security contexts; otherwise they correlate, so pick one (and never put both on your two weakest seats).

### Two more
- **If the request itself is vague**, swap one mid seat (usually `simplest` or `precedent`) for `ambiguity` — one seat surfacing what to clarify beats another answer to the wrong question. That's why `ambiguity` is a keystone only for *planning/research*; elsewhere it's a swap-in.
- **To actually use all 10 models:** don't reach for 10 different lenses (you run out of orthogonal angles around 6). Run 5–6 distinct lenses and **duplicate your keystone (★) lens across two capability tiers** — e.g. `adversary` on your #2 *and* on a mid model for a debugging- or security-heavy task. Distinct angles beat headcount.

---

## Applying this in the orchestrator

- **Assign lenses per seat** in the dispatch form's Fusion picker — each Claude seat and each
  cross-lab provider seat has its own lens dropdown (hover an option to see what the lens does).
  Duplicates are allowed, so you can run the same provider/model several times under different lenses.
- **Add or edit lenses** (custom or seed overrides) in **Settings → lenses**; those take effect on
  the next fusion call with no restart. New *seed* lenses (shipped in code) need a server restart.
- **Verify it ran as intended:** the dispatch result's panel breakdown now shows a `· <lens>` badge
  per seat, so you can confirm the panel actually ran decorrelated.
- The judge is the local `claude` CLI (opus/high by default) — it always sees the original task
  verbatim, so a bold lens can't deform the final output format.
