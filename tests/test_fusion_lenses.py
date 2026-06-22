"""F8.4 tests — per-seat LENS prompts (the §5 decorrelation refinement).

A lens makes a panel seat answer the SAME task through a particular perspective
("find the risks", "find the simplest path", …) so the seats make less
correlated errors. This file proves, OFFLINE (no network, no iTerm2, no TestClient
→ skipped count stays 4), that:

  config       FUSION_LENSES_SEED merges like presets; resolve_lens handles
               name / literal / empty; set_lens/remove_lens are corruption-guarded
               and key-preserving (mirrors test_fusion_settings).
  claude_runner _apply_lens (and its standalone twin in fusion_call) prepend the
               lens while keeping the prompt verbatim + last; _run_panel /
               _anthropic_seat_answer / run_fusion_json thread the right lens to
               the right seat and surface it in raw; a lens-free panel is
               byte-for-byte the pre-F8.4 behavior.
  fusion_call  the standalone tab runner applies the same lens per seat.
  app          _settings_ctx surfaces the configured lenses (not secrets).

Usage:
    python -m unittest tests.test_fusion_lenses -v
"""

import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orchestrator import app as app_module
from orchestrator import fusion_call
from orchestrator.lib import claude_runner, config
from orchestrator.lib.claude_runner import ClaudeRun

PROV = {"script": "providers/gemini.py", "model": "gemini-2.5-flash",
        "price_in": 0.30, "price_out": 1.50}


# ───────────────────────── config: lens seed + merge ───────────────────────

class _IsolatedConfig(unittest.TestCase):
    """A temp config.json path so the on-disk registry/tests don't interfere."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_lens_cfg_"))
        self._orig = config.CONFIG_PATH
        config.CONFIG_PATH = self.tmp / "config.json"

    def tearDown(self):
        config.CONFIG_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, obj):
        config.CONFIG_PATH.write_text(json.dumps(obj), encoding="utf-8")

    def _read(self):
        return json.loads(config.CONFIG_PATH.read_text())


class TestLensConfig(_IsolatedConfig):
    def test_seeds_present_with_no_file(self):
        fc = config.fusion_config()
        self.assertEqual(set(fc["lenses"]), set(config.FUSION_LENSES_SEED))
        self.assertIn("risks", fc["lenses"])
        self.assertEqual(config.fusion_lenses(), fc["lenses"])

    def test_file_lens_overrides_and_adds(self):
        self._write({"fusion": {"lenses": {
            "risks": "MY OWN RISK LENS",          # override a seed
            "coding": "Approach through a CODING lens.",  # add a new one
        }}})
        lenses = config.fusion_lenses()
        self.assertEqual(lenses["risks"], "MY OWN RISK LENS")     # overridden
        self.assertEqual(lenses["coding"], "Approach through a CODING lens.")
        self.assertIn("simplest", lenses)                         # seed kept

    def test_blank_or_non_str_file_lens_ignored(self):
        self._write({"fusion": {"lenses": {"risks": "   ", "bad": 5, "ok": "fine"}}})
        lenses = config.fusion_lenses()
        self.assertEqual(lenses["risks"], config.FUSION_LENSES_SEED["risks"])  # blank → seed
        self.assertNotIn("bad", lenses)                            # non-str dropped
        self.assertEqual(lenses["ok"], "fine")

    def test_garbage_fusion_block_still_yields_seed_lenses(self):
        self._write({"fusion": "not-a-dict"})
        self.assertEqual(set(config.fusion_lenses()), set(config.FUSION_LENSES_SEED))


class TestResolveLens(_IsolatedConfig):
    def test_name_resolves_to_text(self):
        self.assertEqual(config.resolve_lens("risks"),
                         config.FUSION_LENSES_SEED["risks"])

    def test_literal_text_passthrough(self):
        self.assertEqual(config.resolve_lens("just find the bugs"), "just find the bugs")

    def test_empty_and_none_resolve_to_blank(self):
        self.assertEqual(config.resolve_lens(""), "")
        self.assertEqual(config.resolve_lens("   "), "")
        self.assertEqual(config.resolve_lens(None), "")

    def test_explicit_lenses_dict_avoids_disk(self):
        # Passing the map in means no config read — and a name there wins.
        lenses = {"x": "TEXT-X"}
        self.assertEqual(config.resolve_lens("x", lenses), "TEXT-X")
        self.assertEqual(config.resolve_lens("y", lenses), "y")   # unknown → literal

    def test_new_decorrelation_lenses_resolve_by_name(self):
        # §11.c.3 (2026-06-22): the seven added lenses each resolve by NAME to
        # their exact seed text, so a seat opts in by name like the original
        # three. Each targets a DISTINCT failure axis from risks/simplest/
        # ambiguity — that orthogonality is a human judgment, not asserted here;
        # this proves only that the wiring (name → text) is live.
        for name in ("first-principles", "user-intent", "long-horizon", "concrete",
                     "adversary", "precedent", "evidence"):
            self.assertIn(name, config.FUSION_LENSES_SEED)
            self.assertEqual(config.resolve_lens(name),
                             config.FUSION_LENSES_SEED[name])

    def test_all_seed_lenses_follow_house_style(self):
        # Convention: every SEED lens is a terse "Approach this through a[n] X
        # lens: …" prefix that shifts EMPHASIS only — it must never imply an
        # output shape, since the judge always sees the original prompt verbatim
        # (a lens that deformed the format would break that contract).
        for name, text in config.FUSION_LENSES_SEED.items():
            self.assertTrue(text.startswith("Approach this through a"),
                            f"{name!r} lens breaks the house style: {text[:40]!r}")


class TestLensWriteHelpers(_IsolatedConfig):
    def test_set_lens_adds_and_preserves_keys(self):
        self._write({"fusion": {"providers": {"glm": {"api_key": "KEEP"}}}})
        fc = config.set_lens("coding", "Approach through a CODING lens.")
        self.assertEqual(fc["lenses"]["coding"], "Approach through a CODING lens.")
        self.assertEqual(self._read()["fusion"]["providers"]["glm"]["api_key"], "KEEP")

    def test_set_lens_edit_existing(self):
        config.set_lens("risks", "v1")
        config.set_lens("risks", "v2")
        self.assertEqual(self._read()["fusion"]["lenses"]["risks"], "v2")

    def test_blank_name_or_text_raises(self):
        with self.assertRaises(config.ConfigWriteError):
            config.set_lens("  ", "text")
        with self.assertRaises(config.ConfigWriteError):
            config.set_lens("name", "   ")

    def test_remove_custom_disappears_seed_reappears(self):
        config.set_lens("coding", "x")          # custom
        config.remove_lens("coding")
        self.assertNotIn("coding", config.fusion_lenses())
        # Removing a SEED override just falls back to the seed text.
        config.set_lens("risks", "override")
        config.remove_lens("risks")
        self.assertEqual(config.fusion_lenses()["risks"],
                         config.FUSION_LENSES_SEED["risks"])

    def test_malformed_file_never_overwritten(self):
        config.CONFIG_PATH.write_text("{ not valid json", encoding="utf-8")
        for call in (lambda: config.set_lens("a", "b"),
                     lambda: config.remove_lens("a")):
            with self.assertRaises(config.ConfigWriteError):
                call()
        self.assertEqual(config.CONFIG_PATH.read_text(), "{ not valid json")


# ───────────────────────────── _apply_lens ─────────────────────────────────

class TestApplyLens(unittest.TestCase):
    def test_empty_lens_unchanged(self):
        self.assertEqual(claude_runner._apply_lens("PROMPT", ""), "PROMPT")
        self.assertEqual(claude_runner._apply_lens("PROMPT", "   "), "PROMPT")
        self.assertEqual(claude_runner._apply_lens("PROMPT", None), "PROMPT")

    def test_lens_prepended_prompt_kept_verbatim_and_last(self):
        out = claude_runner._apply_lens("ORIGINAL TASK", "FIND RISKS")
        self.assertIn("FIND RISKS", out)
        self.assertIn("ORIGINAL TASK", out)
        # The original prompt is the LAST thing in the lensed text, so its own
        # trailing output-format instructions stay the final instruction.
        self.assertTrue(out.rstrip().endswith("ORIGINAL TASK"))
        self.assertLess(out.index("FIND RISKS"), out.index("ORIGINAL TASK"))

    def test_identical_to_fusion_call_standalone_twin(self):
        # The watchable-tab path (fusion_call.py) must build the SAME lensed
        # prompt as the in-process path — they can't share code (fusion_call is
        # standalone), so their _apply_lens must stay textually identical.
        for prompt, lens in (("q", "risks"), ("multi\nline\nprompt", "be terse"),
                             ("p", ""), ("p", None)):
            self.assertEqual(claude_runner._apply_lens(prompt, lens),
                             fusion_call._apply_lens(prompt, lens))


# ─────────────────── _run_panel: per-seat lens threading ────────────────────

class TestRunPanelLenses(unittest.TestCase):
    def test_each_seat_gets_its_own_lensed_prompt(self):
        seen = {}
        providers = {n: dict(PROV) for n in ("a", "b")}

        def fake_answer(n, prov, prompt, timeout_s):
            seen[n] = prompt
            return {"name": n, "ok": True}

        with mock.patch.object(claude_runner, "_panel_answer", side_effect=fake_answer):
            claude_runner._run_panel("BASE", ["a", "b"], providers, 60,
                                     lenses={"a": "LENS-A"})
        self.assertEqual(seen["a"], claude_runner._apply_lens("BASE", "LENS-A"))
        self.assertEqual(seen["b"], "BASE")              # no lens → verbatim

    def test_no_lenses_is_unchanged(self):
        seen = {}
        providers = {"a": dict(PROV)}
        with mock.patch.object(claude_runner, "_panel_answer",
                               side_effect=lambda n, p, q, t: seen.update({n: q}) or {"name": n, "ok": True}):
            claude_runner._run_panel("BASE", ["a"], providers, 60)
        self.assertEqual(seen["a"], "BASE")


# ─────────────── _anthropic_seat_answer: lens applied + surfaced ────────────

class TestClaudeSeatLens(unittest.TestCase):
    def test_lens_text_applied_and_name_surfaced(self):
        seat = {"model": "opus", "effort": "high", "name": "opus-high",
                "lens": "risks", "lens_text": "FIND RISKS"}
        captured = {}

        def fake_run(prompt, cwd, model, effort, label):
            captured.update(prompt=prompt, label=label)
            return ClaudeRun(ok=True, text="ANS", model="opus")

        with mock.patch.object(claude_runner, "run_claude_json", side_effect=fake_run):
            ans = claude_runner._anthropic_seat_answer(seat, "TASK", "/tmp")
        self.assertEqual(captured["prompt"], claude_runner._apply_lens("TASK", "FIND RISKS"))
        self.assertIn("risks", captured["label"])        # tab title shows the lens
        self.assertEqual(ans["lens"], "risks")           # surfaced on the answer
        self.assertTrue(ans["ok"])
        self.assertEqual(ans["cost"], 0.0)               # subscription → $0 unchanged

    def test_no_lens_prompt_unchanged(self):
        seat = {"model": "opus", "effort": "high", "name": "opus-high"}
        captured = {}
        with mock.patch.object(claude_runner, "run_claude_json",
                               side_effect=lambda prompt, cwd, model, effort, label:
                               captured.update(prompt=prompt) or ClaudeRun(ok=True, text="A")):
            ans = claude_runner._anthropic_seat_answer(seat, "TASK", "/tmp")
        self.assertEqual(captured["prompt"], "TASK")
        self.assertEqual(ans["lens"], "")


# ──────────────── _run_fusion_in_tab: lenses ride in the body ───────────────

class TestFusionTabBodyLenses(unittest.TestCase):
    def _run(self, lenses):
        tmp = Path(tempfile.mkdtemp(prefix="orch_lens_tab_"))
        captured = {}

        def fake_spawn(fid, body, cwd):
            captured["body"] = body
            (tmp / f"{fid}.json").write_text("[]")
            (tmp / f"{fid}.done").write_text("0")

        providers = {"a": {"script": "providers/a.py", "model": "m1"},
                     "b": {"script": "providers/b.py", "model": "m2"}}
        try:
            with mock.patch.object(claude_runner.spawn, "FUSION_DIR", tmp), \
                    mock.patch.object(claude_runner.spawn, "spawn_fusion_tab",
                                      side_effect=fake_spawn), \
                    mock.patch.object(claude_runner.spawn, "finish_fusion_tab"):
                out = claude_runner._run_fusion_in_tab(
                    "BASE", ["a", "b"], providers, 5, lenses=lenses)
            return out, captured["body"]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_only_nonempty_lenses_sent_base_prompt_unchanged(self):
        out, body = self._run({"a": "LENS-A", "b": ""})
        self.assertEqual(out, [])                         # priced from "[]"
        self.assertEqual(body["lenses"], {"a": "LENS-A"})  # empty b dropped
        self.assertEqual(body["prompt"], "BASE")           # lens is applied seat-side

    def test_no_lenses_yields_empty_map(self):
        _, body = self._run(None)
        self.assertEqual(body["lenses"], {})


# ─────────────── fusion_call.py (standalone) applies the lens ───────────────

# A fake provider that ECHOES the prompt it received, so we can prove the lens
# reached the script.
FAKE_ECHO = (
    "import sys, json\n"
    "req = json.load(sys.stdin)\n"
    "print(json.dumps({'ok': True, 'text': req['prompt'], 'model': req['model'],\n"
    "                  'prompt_tokens': 1, 'completion_tokens': 1, 'error': ''}))\n"
)
FUSION_CALL = REPO / "orchestrator" / "fusion_call.py"


class TestFusionCallAppliesLens(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_lens_call_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, body):
        (self.tmp / "a.py").write_text(FAKE_ECHO)
        req = self.tmp / "req.json"
        req.write_text(json.dumps(body))
        p = subprocess.run([sys.executable, str(FUSION_CALL), str(req)],
                           capture_output=True, text=True, timeout=30)
        return json.loads(p.stdout)

    def test_lens_reaches_the_provider_script(self):
        body = {"prompt": "ORIG", "timeout_s": 10, "panel": ["a"],
                "providers": {"a": {"script": str(self.tmp / "a.py"), "model": "m1"}},
                "lenses": {"a": "LENS-TEXT"}}
        out = self._run(body)
        self.assertEqual(out[0]["text"], fusion_call._apply_lens("ORIG", "LENS-TEXT"))

    def test_no_lenses_prompt_verbatim(self):
        body = {"prompt": "ORIG", "timeout_s": 10, "panel": ["a"],
                "providers": {"a": {"script": str(self.tmp / "a.py"), "model": "m1"}}}
        out = self._run(body)
        self.assertEqual(out[0]["text"], "ORIG")          # absent lenses → unchanged


# ─────────────── run_fusion_json: end-to-end lens wiring ────────────────────

class TestRunFusionJsonLenses(unittest.TestCase):
    CFG = {"preset": "budget", "timeout_s": 42,
           "providers": {"gemini": dict(PROV), "gemini2": dict(PROV)},
           "presets": {"budget": ["gemini", "gemini2"]},
           "lenses": {"risks": "RISK-TEXT", "simplest": "SIMPLE-TEXT"}}

    @contextlib.contextmanager
    def _env(self, active, panel_ans, seat_ans):
        with contextlib.ExitStack() as es:
            es.enter_context(mock.patch.object(claude_runner.config, "fusion_config",
                                               return_value=self.CFG))
            es.enter_context(mock.patch.object(claude_runner.config, "active_providers",
                                               return_value=active))
            es.enter_context(mock.patch.object(claude_runner.config,
                                               "claude_cli_available", return_value=True))
            es.enter_context(mock.patch.object(claude_runner.spawn,
                                               "ensure_fusion_providers"))
            rp = es.enter_context(mock.patch.object(claude_runner, "_panel_answers",
                                                    return_value=panel_ans))
            asa = es.enter_context(mock.patch.object(claude_runner, "_anthropic_seat_answer",
                                                     return_value=seat_ans))
            rcj = es.enter_context(mock.patch.object(claude_runner, "run_claude_json",
                                                     return_value=ClaudeRun(ok=True, text="SYNTH")))
            yield rp, asa, rcj

    def test_lens_names_resolved_and_threaded_to_each_seat(self):
        active = {"gemini": dict(PROV)}
        panel_ans = [{"name": "gemini", "ok": True, "cost": 0.001, "text": "A"}]
        seat_ans = {"name": "opus-high", "ok": True, "cost": 0.0, "text": "B",
                    "lens": "simplest"}
        panel = [{"name": "gemini", "lens": "risks"},
                 {"kind": "claude_cli", "model": "opus", "effort": "high",
                  "lens": "simplest"}]
        with self._env(active, panel_ans, seat_ans) as (rp, asa, rcj):
            run = claude_runner.run_fusion_json("q", cwd="/tmp", panel=panel)
        self.assertTrue(run.ok)
        # External lens resolved to TEXT and passed as the lenses map (6th arg).
        self.assertEqual(rp.call_args.args[5], {"gemini": "RISK-TEXT"})
        # Claude seat received the resolved lens text + the lens name.
        seat = asa.call_args.args[0]
        self.assertEqual(seat["lens"], "simplest")
        self.assertEqual(seat["lens_text"], "SIMPLE-TEXT")
        # raw surfaces both seats' lens NAMES; the external answer got tagged.
        self.assertIn({"seat": "gemini", "lens": "risks"}, run.raw["lenses"])
        self.assertIn({"seat": "opus-high", "lens": "simplest"}, run.raw["lenses"])
        by = {a["name"]: a for a in run.raw["panel"]}
        self.assertEqual(by["gemini"]["lens"], "risks")    # tagged after the fan-out

    def test_duplicate_provider_seats_get_unique_keys_and_lenses(self):
        # F12: the SAME cross-lab provider may appear N times, each its own seat
        # with its own lens (the provider analogue of duplicate Claude seats).
        # Each must get a UNIQUE key (gemini, gemini#2, gemini#3) so the name-keyed
        # fan-out (per-seat providers map, lenses map, and each answer's name)
        # never collapses two seats into one — which would silently drop seats and
        # let only the LAST lens win.
        active = {"gemini": dict(PROV)}
        panel_ans = [{"name": "gemini", "ok": True, "cost": 0.001, "text": "A"},
                     {"name": "gemini#2", "ok": True, "cost": 0.002, "text": "B"},
                     {"name": "gemini#3", "ok": True, "cost": 0.003, "text": "C"}]
        panel = [{"name": "gemini", "lens": "risks"},      # seat 1 — risk lens
                 {"name": "gemini", "lens": "simplest"},   # seat 2 — simplicity lens
                 "gemini"]                                  # seat 3 — no lens (bare name)
        with self._env(active, panel_ans, {}) as (rp, asa, rcj):
            run = claude_runner.run_fusion_json("q", cwd="/tmp", panel=panel)
        self.assertTrue(run.ok)
        # Three distinct seat keys fanned out — NOT deduped to a single "gemini".
        self.assertEqual(rp.call_args.args[1], ["gemini", "gemini#2", "gemini#3"])
        # Every key resolves to the same provider config (script/model/prices),
        # so pricing and the tab body stay correct for each duplicate seat.
        seat_providers = rp.call_args.args[2]
        self.assertEqual(set(seat_providers), {"gemini", "gemini#2", "gemini#3"})
        for prov in seat_providers.values():
            self.assertEqual(prov["model"], PROV["model"])
        # Per-seat lenses are keyed by the UNIQUE seat key — no collision, so two
        # gemini seats carry two different lenses (the un-lensed 3rd carries none).
        self.assertEqual(rp.call_args.args[5],
                         {"gemini": "RISK-TEXT", "gemini#2": "SIMPLE-TEXT"})
        self.assertIn({"seat": "gemini", "lens": "risks"}, run.raw["lenses"])
        self.assertIn({"seat": "gemini#2", "lens": "simplest"}, run.raw["lenses"])
        # Each answer is tagged back by its unique key — #2 stays distinct.
        by = {a["name"]: a for a in run.raw["panel"]}
        self.assertEqual(by["gemini"]["lens"], "risks")
        self.assertEqual(by["gemini#2"]["lens"], "simplest")
        self.assertEqual(by["gemini#3"]["lens"], "")       # no lens → blank

    def test_judge_sees_original_prompt_not_the_lensed_one(self):
        active = {"gemini": dict(PROV)}
        panel_ans = [{"name": "gemini", "ok": True, "cost": 0.0, "text": "A"}]
        seat_ans = {"name": "opus-high", "ok": True, "cost": 0.0, "text": "B", "lens": "risks"}
        panel = [{"name": "gemini", "lens": "risks"},
                 {"kind": "claude_cli", "model": "opus", "effort": "high", "lens": "risks"}]
        with self._env(active, panel_ans, seat_ans) as (rp, asa, rcj):
            claude_runner.run_fusion_json("ORIGINAL-Q", cwd="/tmp", panel=panel)
        judge_prompt = rcj.call_args.kwargs["prompt"]
        self.assertIn("ORIGINAL-Q", judge_prompt)          # original task verbatim
        # The lens framing must NOT have been wrapped around the judge prompt.
        self.assertNotIn("answer the task in full", judge_prompt)

    def test_lensless_panel_is_backward_compatible(self):
        active = {"gemini": dict(PROV), "gemini2": dict(PROV)}
        panel_ans = [{"name": "gemini", "ok": True, "cost": 0.0, "text": "A"},
                     {"name": "gemini2", "ok": True, "cost": 0.0, "text": "B"}]
        with self._env(active, panel_ans, {}) as (rp, asa, rcj):
            run = claude_runner.run_fusion_json("q", cwd="/tmp",
                                                panel=["gemini", "gemini2"])
        self.assertTrue(run.ok)
        self.assertEqual(rp.call_args.args[5], {})          # no lenses threaded
        self.assertEqual(run.raw["lenses"], [])             # nothing surfaced
        asa.assert_not_called()                             # no Claude seats


# ─────────────── app: _settings_ctx surfaces lenses (not secrets) ───────────

class TestSettingsCtxLenses(unittest.TestCase):
    FCFG = {"preset": "budget", "timeout_s": 300, "providers": {},
            "presets": {"budget": []},
            "lenses": {"risks": "FIND RISKS", "simplest": "BE SIMPLE"}}

    def test_lenses_surfaced(self):
        with mock.patch.object(app_module.config, "fusion_config", return_value=self.FCFG), \
                mock.patch.object(app_module.config, "active_providers", return_value={}), \
                mock.patch.object(app_module.config, "get_provider_key", return_value=None), \
                mock.patch.object(app_module.config, "is_fusion_available", return_value=True), \
                mock.patch.object(app_module.config, "claude_cli_available", return_value=True):
            ctx = app_module._settings_ctx()
        by = {l["name"]: l["text"] for l in ctx["lenses"]}
        self.assertEqual(by["risks"], "FIND RISKS")
        self.assertEqual(by["simplest"], "BE SIMPLE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
