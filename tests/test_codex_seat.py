"""C2 tests — the Fusion codex SEAT (`kind:"codex_cli"`), sub-tasks C2.1–C2.3.

Fully OFFLINE — no real `codex`, no iTerm2, no network:
  C2.1  config.codex_cli_available()  — PATH + a NON-BILLING `codex login status`
        auth probe (shutil.which / subprocess.run mocked); fail-safe to False,
        never raises, never a real `codex exec`. + is_fusion_available() ORs codex.
  C2.2  claude_runner._codex_seat_answer — the codex twin of _anthropic_seat_answer
        (run_codex_json mocked): normalized $0/subscription dict, model passed
        EXPLICITLY, codex's empty-effort default, lens via _apply_lens, fail-soft.
  C2.3  run_fusion_json learns the THIRD seat kind (_codex_seat_answer /
        _anthropic_seat_answer / _panel_answers / the claude judge all mocked):
        a pure-codex pair clears the >=2 gate, a mixed panel fans out three ways,
        and the existing claude_cli path is unchanged.

The judge is STILL claude (C3 deferred), so every run_fusion_json test mocks
run_claude_json too — otherwise a "pure-codex" panel would spawn a real claude
tab/headless call.

Usage:
    python -m unittest tests.test_codex_seat -v
"""

import contextlib
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orchestrator.lib import claude_runner, config
from orchestrator.lib.claude_runner import ClaudeRun


def _fake_proc(returncode=0, stdout="", stderr=""):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


def _ok_seat(seat, prompt, cwd):
    """Stand-in for _codex_seat_answer / _anthropic_seat_answer: a FRESH ok answer
    keyed on the seat's name, so two mocked seats never alias one dict (the
    lens-tagging loop mutates each answer)."""
    return {"name": seat["name"], "model": seat.get("model", ""),
            "text": f"ANS-{seat['name']}", "cost": 0.0, "subscription": True,
            "lens": seat.get("lens", ""), "ok": True}


# ───────────────────────── C2.1: codex_cli_available() ──────────────────────

class TestCodexCliAvailable(unittest.TestCase):
    """PATH + auth probe, all mocked. The auth probe is what makes this NOT a bare
    `shutil.which` (a codex login expires); it must be cheap, non-billing, never
    hang, and fail-safe to False."""

    def _which(self, path):
        return mock.patch.object(config.shutil, "which", return_value=path)

    def test_missing_binary_is_false_and_never_probes(self):
        with self._which(None), \
                mock.patch.object(config.subprocess, "run") as run:
            self.assertFalse(config.codex_cli_available())
        run.assert_not_called()                       # no probe when the binary is absent

    def test_logged_in_zero_exit_is_true(self):
        with self._which("/usr/local/bin/codex"), \
                mock.patch.object(config.subprocess, "run", return_value=_fake_proc(0)):
            self.assertTrue(config.codex_cli_available())

    def test_logged_out_nonzero_exit_is_false(self):
        # ACCEPTANCE: binary present but logged-out/expired (nonzero exit) → False.
        with self._which("/usr/local/bin/codex"), \
                mock.patch.object(config.subprocess, "run", return_value=_fake_proc(1)):
            self.assertFalse(config.codex_cli_available())

    def test_probe_is_non_billing_scrubs_key_and_closes_stdin(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            seen["env"] = kw.get("env") or {}
            seen["stdin"] = kw.get("stdin")
            seen["timeout"] = kw.get("timeout")
            return _fake_proc(0)

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-should-be-scrubbed"}), \
                self._which("/usr/local/bin/codex"), \
                mock.patch.object(config.subprocess, "run", side_effect=fake_run):
            self.assertTrue(config.codex_cli_available())
        # the cheap STATUS probe — NEVER a billable `codex exec`/model call.
        self.assertEqual(seen["cmd"], ["codex", "login", "status"])
        self.assertNotIn("exec", seen["cmd"])
        # $0 hard rule: no OpenAI key reaches the child env (so the probe reflects
        # the subscription login, not a billed API key).
        self.assertNotIn("OPENAI_API_KEY", seen["env"])
        # can't hang: stdin closed (codex blocks on stdin in a non-TTY) + finite timeout.
        self.assertEqual(seen["stdin"], subprocess.DEVNULL)
        self.assertIsNotNone(seen["timeout"])
        self.assertGreater(seen["timeout"], 0)

    def test_timeout_is_false_never_raises(self):
        with self._which("/usr/local/bin/codex"), \
                mock.patch.object(config.subprocess, "run",
                                  side_effect=subprocess.TimeoutExpired("codex", 10)):
            self.assertFalse(config.codex_cli_available())

    def test_filenotfound_between_which_and_run_is_false(self):
        with self._which("/usr/local/bin/codex"), \
                mock.patch.object(config.subprocess, "run", side_effect=FileNotFoundError()):
            self.assertFalse(config.codex_cli_available())

    def test_oserror_is_false_never_raises(self):
        with self._which("/usr/local/bin/codex"), \
                mock.patch.object(config.subprocess, "run", side_effect=OSError("boom")):
            self.assertFalse(config.codex_cli_available())


# ─────────────────── C2.1: is_fusion_available() ORs in codex ───────────────

class TestIsFusionAvailableCodex(unittest.TestCase):
    @contextlib.contextmanager
    def _env(self, *, claude_ok, codex_ok, active):
        with contextlib.ExitStack() as es:
            es.enter_context(mock.patch.object(config, "claude_cli_available", return_value=claude_ok))
            es.enter_context(mock.patch.object(config, "codex_cli_available", return_value=codex_ok))
            es.enter_context(mock.patch.object(config, "active_providers", return_value=active))
            yield

    def test_codex_alone_makes_fusion_available(self):
        # No claude CLI, <2 providers — codex login alone is enough.
        with self._env(claude_ok=False, codex_ok=True, active={}):
            self.assertTrue(config.is_fusion_available())

    def test_nothing_available_is_unavailable(self):
        with self._env(claude_ok=False, codex_ok=False, active={}):
            self.assertFalse(config.is_fusion_available())

    def test_codex_probe_skipped_when_claude_present(self):
        # The cheap `which` claude check short-circuits BEFORE the codex subprocess.
        with contextlib.ExitStack() as es:
            es.enter_context(mock.patch.object(config, "claude_cli_available", return_value=True))
            es.enter_context(mock.patch.object(config, "active_providers", return_value={}))
            cx = es.enter_context(mock.patch.object(config, "codex_cli_available"))
            self.assertTrue(config.is_fusion_available())
            cx.assert_not_called()


# ─────────────────────── C2.2: _codex_seat_answer ───────────────────────────

class TestCodexSeatAnswer(unittest.TestCase):
    """Mirrors _anthropic_seat_answer, with codex's real asymmetries. run_codex_json
    is mocked — never a real codex call."""

    def _capture(self, run_result):
        captured = {}

        def fake(**kw):
            captured.update(kw)
            return run_result

        return captured, mock.patch.object(claude_runner, "run_codex_json", side_effect=fake)

    def test_ok_returns_normalized_subscription_dict(self):
        cap, p = self._capture(ClaudeRun(ok=True, text="ANS", model="gpt-5.5"))
        with p:
            ans = claude_runner._codex_seat_answer(
                {"model": "gpt-5.5", "effort": "high", "name": "gpt-5.5-high"},
                "TASK", "/tmp")
        self.assertTrue(ans["ok"])
        self.assertEqual(ans["text"], "ANS")
        self.assertEqual(ans["model"], "gpt-5.5")
        self.assertEqual(ans["cost"], 0.0)            # subscription → $0 by policy
        self.assertTrue(ans["subscription"])
        self.assertEqual(ans["lens"], "")

    def test_model_passed_explicitly_to_run_codex_json(self):
        # dispatch #3: a non-default model must NOT be downgraded to the placeholder.
        cap, p = self._capture(ClaudeRun(ok=True, text="A"))
        with p:
            claude_runner._codex_seat_answer({"model": "gpt-5.5-mini"}, "T", "/tmp")
        self.assertEqual(cap["model"], "gpt-5.5-mini")

    def test_model_defaults_to_constant_when_absent(self):
        cap, p = self._capture(ClaudeRun(ok=True, text="A"))
        with p:
            claude_runner._codex_seat_answer({}, "T", "/tmp")
        self.assertEqual(cap["model"], claude_runner.DEFAULT_CODEX_MODEL)

    def test_empty_effort_no_high_injected_and_no_trailing_dash(self):
        cap, p = self._capture(ClaudeRun(ok=True, text="A"))
        with p:
            ans = claude_runner._codex_seat_answer({"model": "gpt-5.5"}, "T", "/tmp")
        self.assertEqual(cap["effort"], "")           # codex's own default, NOT 'high'
        self.assertEqual(ans["effort"], "")
        self.assertEqual(ans["name"], "gpt-5.5")  # not 'gpt-5.5-'

    def test_lens_applied_to_prompt_and_surfaced(self):
        cap, p = self._capture(ClaudeRun(ok=True, text="A", model="gpt-5.5"))
        with p:
            ans = claude_runner._codex_seat_answer(
                {"model": "gpt-5.5", "lens": "risks", "lens_text": "FIND RISKS"},
                "TASK", "/tmp")
        self.assertEqual(cap["prompt"], claude_runner._apply_lens("TASK", "FIND RISKS"))
        self.assertIn("risks", cap["label"])          # the tab title shows the lens
        self.assertEqual(ans["lens"], "risks")

    def test_no_lens_prompt_unchanged(self):
        cap, p = self._capture(ClaudeRun(ok=True, text="A"))
        with p:
            ans = claude_runner._codex_seat_answer({"model": "gpt-5.5"}, "TASK", "/tmp")
        self.assertEqual(cap["prompt"], "TASK")
        self.assertEqual(ans["lens"], "")

    def test_fails_soft_ok_false_carries_lens_no_raise(self):
        cap, p = self._capture(ClaudeRun(ok=False, error="auth expired"))
        with p:
            ans = claude_runner._codex_seat_answer(
                {"model": "gpt-5.5", "lens": "risks", "lens_text": "X"}, "T", "/tmp")
        self.assertFalse(ans["ok"])
        self.assertIn("auth expired", ans["error"])
        self.assertEqual(ans["lens"], "risks")        # failure dict carries lens too


# ─────────────────── C2.3: run_fusion_json fans out codex seats ─────────────

class TestRunFusionJsonCodexSeat(unittest.TestCase):
    PROV = {"script": "providers/gemini.py", "model": "gemini-2.5-flash",
            "price_in": 0.30, "price_out": 1.50}

    @contextlib.contextmanager
    def _env(self, *, active=None, claude_ok=False, codex_ok=True,
             panel_ans=None, judge=None):
        cfg = {"preset": "budget", "timeout_s": 42,
               "providers": {"gemini": dict(self.PROV)}, "presets": {},
               "lenses": {"risks": "RISK-TEXT"}}
        with contextlib.ExitStack() as es:
            es.enter_context(mock.patch.object(claude_runner.config, "fusion_config", return_value=cfg))
            es.enter_context(mock.patch.object(claude_runner.config, "active_providers",
                                               return_value=active or {}))
            es.enter_context(mock.patch.object(claude_runner.config, "claude_cli_available",
                                               return_value=claude_ok))
            es.enter_context(mock.patch.object(claude_runner.config, "codex_cli_available",
                                               return_value=codex_ok))
            es.enter_context(mock.patch.object(claude_runner.spawn, "ensure_fusion_providers"))
            rp = es.enter_context(mock.patch.object(claude_runner, "_panel_answers",
                                                    return_value=(panel_ans or [])))
            asa = es.enter_context(mock.patch.object(claude_runner, "_anthropic_seat_answer",
                                                     side_effect=_ok_seat))
            csa = es.enter_context(mock.patch.object(claude_runner, "_codex_seat_answer",
                                                     side_effect=_ok_seat))
            # The judge is STILL claude (C3 deferred) — mock it or it spawns a real tab.
            rcj = es.enter_context(mock.patch.object(
                claude_runner, "run_claude_json",
                return_value=(judge or ClaudeRun(ok=True, text="SYNTH"))))
            yield rp, asa, csa, rcj

    def test_pure_codex_pair_clears_two_gate(self):
        # ACCEPTANCE: a pure-codex pair satisfies the >=2 gate; judge synthesizes.
        panel = [{"kind": "codex_cli", "model": "gpt-5.5"},
                 {"kind": "codex_cli", "model": "gpt-5.5", "effort": "high"}]
        with self._env(codex_ok=True) as (rp, asa, csa, rcj):
            run = claude_runner.run_fusion_json("q", cwd="/tmp", panel=panel)
        self.assertTrue(run.ok)
        self.assertEqual(run.text, "SYNTH")
        self.assertEqual(csa.call_count, 2)           # both codex seats fanned out
        rcj.assert_called_once()                      # judge ran (claude — C3 deferred)
        self.assertEqual(rcj.call_args.kwargs.get("label"), "fusion-judge")
        names = [a["name"] for a in run.raw["panel"]]
        self.assertIn("gpt-5.5", names)           # empty-effort name: no trailing dash
        self.assertIn("gpt-5.5-high", names)

    def test_pure_codex_pair_falls_back_when_codex_unavailable(self):
        # The gate is REAL, not merely present: logged-out codex → <2 seats → fall back.
        panel = [{"kind": "codex_cli", "model": "gpt-5.5"},
                 {"kind": "codex_cli", "model": "gpt-5.5", "effort": "high"}]
        with self._env(codex_ok=False) as (rp, asa, csa, rcj):
            run = claude_runner.run_fusion_json("q", cwd="/tmp", panel=panel)
        self.assertFalse(run.ok)
        self.assertIn(">=2", run.error)
        csa.assert_not_called()                       # no seat ran for a logged-out codex
        rcj.assert_not_called()                       # never reached the judge

    def test_mixed_panel_codex_claude_provider_clears_gate(self):
        active = {"gemini": dict(self.PROV)}
        panel_ans = [{"name": "gemini", "ok": True, "cost": 0.001, "text": "G"}]
        panel = ["gemini",
                 {"kind": "claude_cli", "model": "opus", "effort": "high"},
                 {"kind": "codex_cli", "model": "gpt-5.5"}]
        with self._env(active=active, claude_ok=True, codex_ok=True,
                       panel_ans=panel_ans) as (rp, asa, csa, rcj):
            run = claude_runner.run_fusion_json("q", cwd="/tmp", panel=panel)
        self.assertTrue(run.ok)
        rp.assert_called_once()                       # provider group fanned out
        asa.assert_called_once()                      # claude seat
        csa.assert_called_once()                      # codex seat
        names = {a["name"] for a in run.raw["panel"]}
        self.assertEqual(names, {"gemini", "opus-high", "gpt-5.5"})

    def test_existing_claude_cli_panel_unchanged_with_codex_off(self):
        # Additive proof: a pure claude_cli panel behaves exactly as pre-C2 —
        # codex is never consulted for a seat and the gate still clears.
        panel = [{"kind": "claude_cli", "model": "opus", "effort": "high"},
                 {"kind": "claude_cli", "model": "opus", "effort": "low"}]
        with self._env(claude_ok=True, codex_ok=False) as (rp, asa, csa, rcj):
            run = claude_runner.run_fusion_json("q", cwd="/tmp", panel=panel)
        self.assertTrue(run.ok)
        self.assertEqual(asa.call_count, 2)
        csa.assert_not_called()
        rcj.assert_called_once()

    def test_codex_seat_surfaced_in_raw_seats_and_lenses(self):
        # Diagnostics parity: a "(codex)" seat label + a lensed codex seat in lenses_used.
        panel = [{"kind": "codex_cli", "model": "gpt-5.5", "lens": "risks"},
                 {"kind": "codex_cli", "model": "gpt-5.5", "effort": "high"}]
        with self._env(codex_ok=True) as (rp, asa, csa, rcj):
            run = claude_runner.run_fusion_json("q", cwd="/tmp", panel=panel)
        self.assertTrue(run.ok)
        self.assertTrue(any("(codex)" in s for s in run.raw["seats"]))
        self.assertIn({"seat": "gpt-5.5", "lens": "risks"}, run.raw["lenses"])


# ── codex seats persist in saved Fusion PROFILES (the "+ add codex seat" picker) ──

class TestNormalizeProfileCodexSeats(unittest.TestCase):
    """Codex seats persist in saved profiles alongside claude/provider seats, so the
    dispatch picker's "+ add codex seat" rows aren't silently dropped on save/apply.
    config._normalize_profile is PURE (no IO) → tested directly. Codex effort is
    OPTIONAL ("" ⇒ the model's own reasoning default — NOT defaulted to "high" like a
    claude seat); a model-less codex seat is dropped, exactly like a model-less claude
    seat. Offline, no skip."""

    def test_codex_seats_carried_with_optional_effort_and_lens(self):
        out = config._normalize_profile({
            "codex_seats": [{"model": "gpt-5.5", "effort": "high", "lens": "risks"},
                            {"model": "gpt-5.4"},        # effort/lens blank → kept blank
                            {"effort": "high"},          # no model → dropped
                            "nope"]})                    # not a dict → dropped
        self.assertEqual(out["codex_seats"],
                         [{"model": "gpt-5.5", "effort": "high", "lens": "risks"},
                          {"model": "gpt-5.4", "effort": "", "lens": ""}])

    def test_codex_effort_not_defaulted_to_high_unlike_claude(self):
        out = config._normalize_profile({
            "claude_seats": [{"model": "opus"}],
            "codex_seats": [{"model": "gpt-5.5"}]})
        self.assertEqual(out["claude_seats"][0]["effort"], "high")   # claude default
        self.assertEqual(out["codex_seats"][0]["effort"], "")        # codex: model default

    def test_profile_without_codex_key_is_backward_compatible(self):
        # A profile saved before codex seats existed has no codex_seats key → [].
        out = config._normalize_profile({"claude_seats": [{"model": "opus"}],
                                         "provider_seats": [{"name": "glm"}]})
        self.assertEqual(out["codex_seats"], [])
        self.assertEqual(out["claude_seats"], [{"model": "opus", "effort": "high", "lens": ""}])
        self.assertEqual(out["provider_seats"], [{"name": "glm", "lens": ""}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
