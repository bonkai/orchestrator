# Supermax Mode — Plan

> **Goal (the literal ask):** once a claude/codex session is going, every
> follow-up the user types is first run through the Fusion panel
> (`run_fusion_json`) for improvement, then sent into the **same** ongoing
> session — not just the initial question.

This doc splits that goal into two architectures: **v1 (copy-paste refine)**,
which is shipped, and **v2 (live injection)**, which is designed and gated. The
hard part — getting text into a *live* session's stdin — is isolated in v2 and
called out plainly.

---

## 0. Feasibility assessment (verified against the code, not assumed)

| Claim in the task | Reality in the code | Verdict |
|---|---|---|
| `run_fusion_json(prompt, cwd, ...)` exists and returns a `ClaudeRun` | `claude_runner.py:971` — signature + `ClaudeRun` confirmed | ✅ |
| `.text` is "the synthesized **prompt**" | `_judge_prompt` (`claude_runner.py:722`) tells the judge to **answer** the task. `.text` is a synthesized **answer**. | ❌ **wrong** — see §1 |
| A resume seam exists (`/dispatch/{id}/open`) | `app.py:485`. Running → `select_iterm2_tab` (**focuses** the tab). Finished+`session_id` → spawns a **fresh** `claude --resume` tab. | ✅ (but it focuses/respawns; it does **not** inject) |
| There is **no** seam to inject text into a live session's stdin | `write text` appears only at tab **creation** (`spawn.py:367`). No "write into existing session" helper exists. | ✅ confirmed |
| `dispatch.html` exists to host the UI | 13.6 KB template + `GET /dispatch/{id}` at `app.py:1857` | ✅ |

**Bottom line:** v1 is purely additive and achievable now. v2's *targeting*
primitive is mostly solved (see §3) but reliable mid-conversation *injection* is
the genuinely hard, fragile part — so v1 ships first and v2 is gated behind it.

---

## 1. The make-or-break correction: `.text` is an ANSWER, not an improved prompt

`run_fusion_json` runs the panel, then the judge synthesizes **"the single best
response to the original task"** in the task's requested format. If we hand it a
raw follow-up like *"also do the other file"*, every seat **does the work** (or
tries to) and the judge returns an **answer**, not a cleaner instruction. Pasted
back into the live session, the user's message would get **answered twice**.

**Fix:** wrap the follow-up in a rewriter-style instruction so "the task" *is*
"improve this message", and the requested output format *is* "plain improved
message text". Then:

- each panel seat returns an improved message,
- the judge synthesizes the single best improved message,
- the format-preservation contract makes `.text` exactly the paste-ready text.

Crucially the **single-model fallback** (`run_brain_json` → `run_claude_json`
when <2 seats answer) gets the **same wrapper**, so it *also* improves instead of
silently answering. The wrapper is generic English ("rewrite this instruction"),
not Claude-tuned phrasing, so it is engine-neutral for the panel seats.

Implemented as `_supermax_refine_prompt(original_task, followup)` in `app.py`.

---

## 2. v1 — copy-paste refine (SHIPPED)

```
┌─ dispatch.html (detail page, when running OR resumable) ─────────────┐
│  supermax refine                                                     │
│  [ textarea: your raw follow-up ]                                    │
│  ( improve through fusion panel )   ← HTMX POST                      │
│  ┌─ #refine-result (HTMX swap) ──────────────────────────────────┐  │
│  │  ⚡ fused · 3 seats · $0.0021     (or "single model")          │  │
│  │  [ textarea: improved message (editable) ]   [ copy ]          │  │
│  └───────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

**Flow:**
1. `POST /dispatch/{id}/refine` with form field `followup`.
2. Endpoint reads the dispatch's `project_path` + `user_task` from the **DB**
   (`get_dispatch_with_project`) — never a client-supplied path.
3. Wraps the follow-up (§1), then calls
   `run_brain_json(prompt=wrapped, cwd=project_path, fusion=True, model="opus",
   effort="high", judge_model="opus", judge_effort="high")` **in a threadpool**
   (`loop.run_in_executor`) so the minutes-long panel never stalls the event loop.
4. Returns the `_refine.html` fragment: the improved text in an editable
   textarea + a copy button, plus an **honest** `fused / single-model` label
   derived from `run.raw['panel']` (not merely "text came back").
5. The user copies it back into their iTerm session by hand.

**Why this is the right v1:**
- Purely additive; sidesteps the unsolved live-injection problem.
- Reuses the existing panel → judge → fallback chain (`run_brain_json`) — no
  re-implemented fan-out, no hidden brain calls (the panel runs in its usual
  visible iTerm2 tab, satisfying the "visible, never headless" rule for free).
- **Engine-agnostic:** it keys off `cwd` + `text` only, so it works identically
  whether the live session is claude or codex → **codex parity is free at v1.**
- The user already endorsed a copy-paste-only v1.

**Honest naming / known v1 limitations (documented, not hidden):**
- The refine is **context-blind**: it sees the original task as background but
  **not** the live transcript, nor the project bundle / `retrieval.find_similar`
  context the initial rewrite had. It can't reliably resolve anaphora ("do the
  same to the *other* file") and a context-blind "improvement" can occasionally
  degrade an already-context-aware message. v1 is a **prompt-polisher**, not yet
  true "supermax".
- **Latency:** a full panel takes minutes. v1 holds one synchronous (threadpool)
  POST open; if the user navigates away the result is lost. The robustness
  upgrade is **v1.5: fire-and-forget + poll** (reuse the jobs/poll pattern from
  `/send` + `/api/events`) — deferred to keep v1 minimal.
- **Silent degrade:** with no iTerm2 / <2 seats, "supermax" quietly becomes one
  model. The fragment labels this explicitly so a single-model run can't pass as
  a fused one.

---

## 3. v2 — live injection (DESIGNED, GATED)

This is the actual ask: intercept every follow-up automatically and send the
improved text **into the same live session**.

**Targeting is mostly solved.** Each dispatch tab is tagged at spawn with the
`user.orch_id` session variable (`spawn.py:_setuservar_printf`), and
`close_iterm2_session_by_var("orch_id", id)` already *finds* that live session
reliably (it survives claude clobbering the tab title). v2's primitive is that
function with `close` swapped for `write text`:

```python
def write_text_to_session_by_var(var_name, value, text, submit=False) -> bool:
    # find the session whose user.<var_name> == value, then:
    #   tell that session to write text "<escaped text>" newline {submit}
```

Wire it as `POST /dispatch/{id}/refine_inject` (or a checkbox on the v1 refine):
produce the improved text exactly as v1, then write it into the tab whose
`user.orch_id == id`.

**Why it is gated, not shipped — the genuinely hard parts:**

1. **Input readiness.** The TUI is usually *mid-turn*. There is no
   readiness/idle signal exposed over osascript, and `write text` types
   keystrokes into whatever the session currently is. Injecting mid-response
   corrupts session state, and there is no undo on text the user never saw.
2. **Keystroke semantics.** `write text` simulates typing. A trailing newline
   **auto-submits**; embedded newlines in a multi-line improved prompt submit it
   **line-by-line** (each `\n` = a turn). `write text … newline NO` can inject
   *without* submitting (user reviews, then hits Enter) — the safer mode — but
   bracketed-paste and slash-command (`/…`) parsing can still misbehave.
3. **Escaping.** The improved text carries quotes, backslashes, `$`, backticks
   that must survive the cmd → AppleScript hop or they break injection / run
   unintended shell.
4. **claude ≠ codex (parity breaks here).** The codex *executor* runs one-shot
   `codex exec "$PROMPT" < /dev/null` (`spawn.py:1047`) — **not** an interactive
   REPL. There is no live stdin to inject into; "inject into the live codex
   session" is not even the same operation. So v2's write-text trick is
   **claude-only**; codex's equivalent is a fresh `codex` turn, not injection.
   This directly conflicts with the codex↔claude parity preference, so v2 must
   handle the two engines **differently** (or restrict to claude).
5. **Fragility by constraint.** iTerm2's *Python* API offers reliable injection
   + prompt detection, but CLAUDE.md mandates **osascript-only**. So v2 couples
   us to two third-party TUIs' private, version-pinned input handling — a silent
   break on any claude/codex update.
6. **Stale target / race.** The tab may be closed/crashed (Stop hook fired), or
   an orphaned session. And the panel runs ~minutes in a separate tab; the user
   may type into the live session meanwhile, making the injected text land
   mid-thought. Re-resolve via the session var immediately before writing.

**v2 gating rule:** do not claim v2 works until a probe confirms
`user.orch_id` still resolves to a *live* session and a `write text … newline NO`
round-trips into it without corrupting state — for **claude** sessions only.

---

## 4. Recommended middle path — refine-then-resume (the safe "supermax")

A cheaper, reversible alternative to live-TUI injection that still delivers
"the improved follow-up continues the same session":

- Improve the follow-up exactly as v1.
- Instead of injecting into a live TUI, reuse the **proven** `claude --resume`
  seam (`spawn_iterm2_resume`, already used by `/dispatch/{id}/open`) once the
  session pauses/finishes: start the next turn with the improved text as the new
  task, resuming the **same `session_id`**.

This keeps the same conversation thread, avoids every TUI-injection hazard in
§3, and stays within the existing, tested resume machinery. It is the
recommended evolution of v1 before (or instead of) live injection.

---

## 5. Status

| Item | State |
|---|---|
| §1 follow-up wrapper (`_supermax_refine_prompt`) | ✅ shipped |
| §2 `POST /dispatch/{id}/refine` (threadpool, DB cwd, honest fused label) | ✅ shipped |
| §2 `_refine.html` fragment + dispatch.html pane (gated on running/resumable) | ✅ shipped |
| v1.5 fire-and-forget + poll (latency robustness) | ⬜ next |
| §3 `write_text_to_session_by_var` + `/refine_inject` (claude-only) | ⬜ designed, gated |
| §4 refine-then-resume | ⬜ recommended next |

> Reminder for testing: the server runs `reload=False` on `:7878` — restart
> `python -m orchestrator` for edits to take effect. Auto-push commits edits to
> origin/main within seconds, so don't verify via `git diff`.
