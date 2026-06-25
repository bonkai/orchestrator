"""C4 tests — codex ENGINE config centralized in config.py SEEDS, IMPORTED (not
redefined) by claude_runner (sub-task C4.1).

Fully OFFLINE — no real `codex`, no iTerm2, no network.

  - config.CODEX_ENGINE_SEED is the single source of truth (codex model id + the
    exec/-s/--json flag set + the auth-probe command + the C6 auto-bypass flag +
    a default seat panel). config.codex_engine() merges config.json's `fusion.codex`
    over it — per-key override, exactly like the preset/lens merges (config.json
    wins; unoverridden keys stay seeded).
  - claude_runner does NOT redefine the codex model/flags: DEFAULT_CODEX_MODEL IS
    the seed value, run_codex_headless builds its cmd FROM the seed, and (in
    config.py) codex_cli_available() probes with the SEEDED command.
  - the selectable codex JUDGE (run_fusion_json judge_engine="codex") resolves its
    `-m` model from the MERGED config — so a config.json override wins — and never a
    Claude id ('opus' would be a silent downgrade, dispatch #3).

CRITICAL (mirrors test_codex_judge.py): the judge engine must be resolved from the
module namespace at CALL TIME, so mock.patch.object reaches both engine entrypoints
and no real tab spawns.

Usage:
    python -m unittest tests.test_codex_config -v
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

from orchestrator.lib import claude_runner, config, spawn
from orchestrator.lib.claude_runner import ClaudeRun


# ───────────────── C4: CODEX_ENGINE_SEED + codex_engine() merge ──────────────

class _IsolatedConfig(unittest.TestCase):
    """A temp config.json path so the merge tests don't read the real
    ~/.orchestrator/config.json (mirror of test_fusion_config._IsolatedConfig)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_codex_cfg_"))
        self._orig = config.CONFIG_PATH
        config.CONFIG_PATH = self.tmp / "config.json"

    def tearDown(self):
        config.CONFIG_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, obj):
        config.CONFIG_PATH.write_text(json.dumps(obj), encoding="utf-8")


class TestCodexEngineSeedMerge(_IsolatedConfig):
    def test_seed_when_no_file(self):
        ce = config.codex_engine()
        self.assertEqual(ce["model"], "gpt-5.5")
        self.assertEqual(ce["effort"], "")
        self.assertEqual(ce["exec_subcmd"], "exec")
        self.assertEqual(ce["sandbox"], "read-only")
        self.assertEqual(ce["json_flag"], "--json")
        self.assertEqual(ce["auth_probe"], ["codex", "login", "status"])
        self.assertEqual(ce["auto_bypass_flag"],
                         "--dangerously-bypass-approvals-and-sandbox")
        # C6: the full valid ChatGPT-account model set (live-verified 2026-06-23) + the
        # executor sandbox + the concurrency-cap knob.
        self.assertEqual(ce["models"], ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini"])
        self.assertIn(ce["model"], ce["models"])       # the default is one of the valid ids
        self.assertEqual(ce["executor_sandbox"], "danger-full-access")
        self.assertEqual(ce["max_concurrent_dispatches"], 2)
        # default seat panel for the C5 picker: >=2 codex seats.
        self.assertGreaterEqual(len(ce["seats"]), 2)
        self.assertTrue(all(s.get("kind") == "codex_cli" for s in ce["seats"]))

    def test_file_model_override_merges_over_seed(self):
        # ACCEPTANCE: a config.json override merges over the seed (per-key) — the
        # overridden key wins, every unoverridden key stays seeded.
        self._write({"fusion": {"codex": {"model": "gpt-5.1-codex-max"}}})
        ce = config.codex_engine()
        self.assertEqual(ce["model"], "gpt-5.1-codex-max")     # overridden
        self.assertEqual(ce["sandbox"], "read-only")           # seed kept
        self.assertEqual(ce["exec_subcmd"], "exec")            # seed kept
        self.assertEqual(ce["auth_probe"], ["codex", "login", "status"])

    def test_file_flag_override_merges_over_seed(self):
        self._write({"fusion": {"codex": {"sandbox": "workspace-write",
                                          "effort": "high"}}})
        ce = config.codex_engine()
        self.assertEqual(ce["sandbox"], "workspace-write")
        self.assertEqual(ce["effort"], "high")
        self.assertEqual(ce["model"], "gpt-5.5")           # seed kept

    def test_no_codex_block_returns_seed(self):
        self._write({"fusion": {"preset": "max"}})             # codex absent
        self.assertEqual(config.codex_engine()["model"], "gpt-5.5")

    def test_garbage_codex_block_ignored(self):
        self._write({"fusion": {"codex": "not-a-dict"}})
        self.assertEqual(config.codex_engine()["model"], "gpt-5.5")

    def test_garbage_top_level_config_returns_seed(self):
        config.CONFIG_PATH.write_text("{ not valid json", encoding="utf-8")
        self.assertEqual(config.codex_engine()["model"], "gpt-5.5")

    def test_fusion_config_codex_key_matches_accessor(self):
        # The accessor is just sugar over fusion_config()["codex"] (mirror of
        # fusion_lenses()); they must agree.
        self.assertEqual(config.fusion_config()["codex"], config.codex_engine())

    def test_returned_config_cannot_corrupt_seed(self):
        # The merge re-copies the mutable seed values, so mutating the returned
        # config never bleeds back into the module seed.
        ce = config.codex_engine()
        ce["seats"].append({"kind": "codex_cli", "model": "evil"})
        ce["auth_probe"].append("--evil")
        self.assertEqual(len(config.CODEX_ENGINE_SEED["seats"]), 2)
        self.assertEqual(config.CODEX_ENGINE_SEED["auth_probe"],
                         ["codex", "login", "status"])


# ─────────── C4.1: claude_runner IMPORTS the codex config (no redefinition) ──

class TestClaudeRunnerImportsSeed(unittest.TestCase):
    def test_default_codex_model_is_the_seed_value(self):
        # No duplicate literal: the module constant is sourced from the seed.
        self.assertEqual(claude_runner.DEFAULT_CODEX_MODEL,
                         config.CODEX_ENGINE_SEED["model"])

    def test_headless_cmd_built_from_seed_flags_not_inline_literals(self):
        # Proof the exec/-s/--json flags come FROM the seed: patch the seed and the
        # emitted `codex exec` cmd follows (an inline literal would ignore the patch).
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return mock.Mock(returncode=0,
                             stdout='{"type":"turn.completed","usage":{}}\n', stderr="")

        patched = {**config.CODEX_ENGINE_SEED,
                   "exec_subcmd": "EXECX", "sandbox": "SBX", "json_flag": "--JSONX"}
        with mock.patch.object(config, "CODEX_ENGINE_SEED", patched), \
                mock.patch.object(subprocess, "run", side_effect=fake_run):
            run = claude_runner.run_codex_headless("hi", "/tmp", model="m-x")
        self.assertTrue(run.ok)
        self.assertEqual(seen["cmd"][:2], ["codex", "EXECX"])  # subcommand from seed
        self.assertIn("SBX", seen["cmd"])                      # sandbox from seed
        self.assertIn("--JSONX", seen["cmd"])                  # json flag from seed
        self.assertIn("m-x", seen["cmd"])                      # explicit model preserved


class TestCodexCliAvailableUsesSeededProbe(unittest.TestCase):
    """config.py's auth probe is also seed-sourced — single source of truth."""

    def test_probe_command_comes_from_seed(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return mock.Mock(returncode=0, stdout="", stderr="")

        patched = {**config.CODEX_ENGINE_SEED, "auth_probe": ["codex", "whoami", "-x"]}
        with mock.patch.object(config, "CODEX_ENGINE_SEED", patched), \
                mock.patch.object(config.shutil, "which", return_value="/bin/codex"), \
                mock.patch.object(config.subprocess, "run", side_effect=fake_run):
            self.assertTrue(config.codex_cli_available())
        self.assertEqual(seen["cmd"], ["codex", "whoami", "-x"])   # seeded, not inline


# ─────── C4: the codex JUDGE resolves the SEEDED model (config override wins) ─

class TestCodexJudgeResolvesSeededModel(unittest.TestCase):
    """A pure-PROVIDER panel synthesized by a codex judge: BOTH engine entrypoints
    are mocked, so the assertions are about WHICH `-m` model the codex judge gets —
    the seeded/merged codex id, never the 'opus' Claude default (dispatch #3)."""

    PROV = {"script": "providers/gemini.py", "model": "gemini-2.5-flash",
            "price_in": 0.30, "price_out": 1.50}
    PROVIDERS = {"gemini": dict(PROV), "gemini2": dict(PROV)}
    ACTIVE = {"gemini": dict(PROV), "gemini2": dict(PROV)}
    PANEL = [{"name": "gemini", "text": "A", "cost": 0.001, "ok": True},
             {"name": "gemini2", "text": "B", "cost": 0.002, "ok": True}]

    @contextlib.contextmanager
    def _env(self, codex_cfg):
        # codex_cfg is the cfg["codex"] block (or None to omit it entirely).
        cfg = {"preset": "budget", "timeout_s": 42, "verify": False,
               "providers": self.PROVIDERS,
               "presets": {"budget": ["gemini", "gemini2"]}}
        if codex_cfg is not None:
            cfg["codex"] = codex_cfg
        with contextlib.ExitStack() as es:
            es.enter_context(mock.patch.object(
                claude_runner.config, "fusion_config", return_value=cfg))
            es.enter_context(mock.patch.object(
                claude_runner.config, "active_providers", return_value=self.ACTIVE))
            es.enter_context(mock.patch.object(
                claude_runner.config, "codex_cli_available", return_value=False))
            es.enter_context(mock.patch.object(
                claude_runner.spawn, "ensure_fusion_providers"))
            rp = es.enter_context(mock.patch.object(claude_runner, "_panel_answers"))
            rp.return_value = [dict(a) for a in self.PANEL]   # fresh dicts per test
            rcj = es.enter_context(mock.patch.object(claude_runner, "run_claude_json"))
            rcx = es.enter_context(mock.patch.object(
                claude_runner, "run_codex_json",
                return_value=ClaudeRun(ok=True, text="SYNTH", model="x")))
            yield rcx, rcj

    def test_codex_judge_uses_seeded_model_not_a_claude_id(self):
        with self._env({"model": "gpt-5.5"}) as (rcx, rcj):
            run = claude_runner.run_fusion_json("q", cwd="/tmp", judge_engine="codex")
        self.assertTrue(run.ok)
        rcx.assert_called_once()
        self.assertEqual(rcx.call_args.kwargs.get("model"), "gpt-5.5")
        self.assertNotEqual(rcx.call_args.kwargs.get("model"), "opus")
        rcj.assert_not_called()

    def test_codex_judge_honors_config_model_override(self):
        # C4's new capability: a config.json fusion.codex.model override flows all
        # the way to the codex judge's `-m` (the dispatch-#3 path now config-driven).
        with self._env({"model": "gpt-5.1-codex-max"}) as (rcx, rcj):
            claude_runner.run_fusion_json("q", cwd="/tmp", judge_engine="codex")
        self.assertEqual(rcx.call_args.kwargs.get("model"), "gpt-5.1-codex-max")
        rcj.assert_not_called()

    def test_codex_judge_falls_back_to_seed_when_codex_block_absent(self):
        # An older config (no fusion.codex) → fall back to DEFAULT_CODEX_MODEL, the
        # seed value. (This is exactly the existing test_codex_judge.py path.)
        with self._env(None) as (rcx, rcj):
            claude_runner.run_fusion_json("q", cwd="/tmp", judge_engine="codex")
        self.assertEqual(rcx.call_args.kwargs.get("model"),
                         claude_runner.DEFAULT_CODEX_MODEL)
        rcj.assert_not_called()

    def test_default_claude_judge_unaffected_by_codex_block(self):
        # Additive proof: a fusion.codex block never perturbs the default claude
        # judge path — it still gets the opus default and codex is never called.
        with self._env({"model": "gpt-5.1-codex-max"}) as (rcx, rcj):
            rcj.return_value = ClaudeRun(ok=True, text="SYNTH", model="opus")
            run = claude_runner.run_fusion_json("q", cwd="/tmp")   # default engine
        self.assertTrue(run.ok)
        rcj.assert_called_once()
        self.assertEqual(rcj.call_args.kwargs.get("model"), "opus")
        rcx.assert_not_called()


# ── C4 drift guard: spawn's codex run.sh heredoc stays pinned to the seed ────

class TestSpawnCodexRunShPinnedToSeed(unittest.TestCase):
    """The codex run.sh heredoc (spawn.CODEX_RUN_SH_CONTENT) still DUPLICATES the
    codex flag set + model fallback in bash — deduping it needs seed→bash
    interpolation at spawn time (bash can't import Python), which is C6's
    codex-dispatch-runner work. Until then, pin those copies to the seed here.

    Direction this guards (the realistic drift): the SEED is now the source of
    truth, so a flag/model change ORIGINATES there. After editing the seed (e.g. a
    codex upgrade renames `--json`), the assertion looks for the NEW seed value in
    the heredoc; it isn't there until the runner is updated too → RED test instead
    of a silently stale flag shipping in the watchable tab. (The values also live
    in this heredoc's prose, so the reverse — editing the command but not the seed
    — is not fully caught; that's an anti-pattern post-C4: edit the seed, not the
    runner. The seed→runner direction is the one that matters.)"""

    SH = spawn.CODEX_RUN_SH_CONTENT
    SEED = config.CODEX_ENGINE_SEED

    def test_exec_subcommand_matches_seed(self):
        self.assertIn(f"codex {self.SEED['exec_subcmd']}", self.SH)

    def test_sandbox_flag_matches_seed(self):
        self.assertIn(f"-s {self.SEED['sandbox']}", self.SH)

    def test_json_flag_matches_seed(self):
        self.assertIn(self.SEED["json_flag"], self.SH)

    def test_model_fallback_matches_seed(self):
        # the bash `... || echo <model>` fallback must be the seed model id.
        self.assertIn(self.SEED["model"], self.SH)


class TestSpawnCodexDispatchRunShPinnedToSeed(unittest.TestCase):
    """C6.1: the EXECUTOR run.sh (spawn.CODEX_DISPATCH_RUN_SH_CONTENT) is GENERATED by
    interpolating the SEED (the C4-deferred seed→bash interp, finished in C6), so its
    flag set is the seed's by construction. These pin that — a seed change (e.g. a codex
    upgrade renaming --json, or flipping executor_sandbox) regenerates the runner; if
    someone hardcodes a literal instead, the assertion goes RED.

    Two safety invariants beyond C4's: the executor uses the WRITE-capable
    `executor_sandbox` (NOT the seat's read-only `sandbox`), and does NOT emit the
    `auto_bypass_flag` (C6.0 found it OVERRIDES -s to full-access — the operator chose
    confined workspace-write)."""

    SH = spawn.CODEX_DISPATCH_RUN_SH_CONTENT
    SEED = config.CODEX_ENGINE_SEED

    def test_exec_subcommand_matches_seed(self):
        self.assertIn(f"codex {self.SEED['exec_subcmd']}", self.SH)

    def test_executor_sandbox_flag_matches_seed(self):
        # Write-capable EXECUTOR sandbox — must be the seed's executor_sandbox.
        self.assertIn(f"-s {self.SEED['executor_sandbox']}", self.SH)

    def test_json_flag_matches_seed(self):
        self.assertIn(self.SEED["json_flag"], self.SH)

    def test_model_fallback_matches_seed(self):
        self.assertIn(self.SEED["model"], self.SH)

    def test_reads_effort_sidecar_and_can_forward_it(self):
        # C6: the executor forwards an OPTIONAL reasoning effort. The run.sh reads the
        # per-dispatch .effort sidecar and emits `-c model_reasoning_effort=<e>` only when
        # non-empty (empty ⇒ the model's own default) — mirroring the codex SEAT runner.
        self.assertIn(".effort", self.SH)
        self.assertIn("model_reasoning_effort", self.SH)

    def test_effort_applied_via_array_on_invocation_line(self):
        # Applied through the EFFORT_FLAG array on the actual `codex exec` line, so an
        # empty effort expands to nothing (no stray `-c` / no model-default override).
        self.assertIn("EFFORT_FLAG", self._codex_cmd_line())

    def _codex_cmd_line(self) -> str:
        # The ACTUAL `codex exec "$PROMPT" …` invocation line (not the doc comments,
        # which mention the seat's `-s read-only` descriptively).
        for ln in self.SH.splitlines():
            if f"codex {self.SEED['exec_subcmd']} " in ln and "$PROMPT" in ln:
                return ln
        return ""

    def test_command_uses_executor_sandbox_not_read_only(self):
        # The actual codex invocation must run the WRITE-capable executor_sandbox, never
        # the seat's read-only `sandbox` (it WRITES the project).
        cmd = self._codex_cmd_line()
        self.assertTrue(cmd, "codex exec invocation line not found in the executor run.sh")
        self.assertIn(f"-s {self.SEED['executor_sandbox']}", cmd)
        self.assertNotIn(f"-s {self.SEED['sandbox']}", cmd)

    def test_does_not_emit_auto_bypass_flag(self):
        # C6.0: the bypass flag OVERRIDES -s to full-access; the confined executor omits
        # it (the operator-chosen workspace-write confinement). Checked on the command line
        # AND whole-file (the full flag string never appears in the doc comments either).
        self.assertNotIn(self.SEED["auto_bypass_flag"], self._codex_cmd_line())
        self.assertNotIn(self.SEED["auto_bypass_flag"], self.SH)

    def test_pid_written_to_claude_path_not_codex_dir(self):
        # CODEX_PLAN note 2: the PID goes to the CLAUDE pids path so kill/cap/reaper/boot
        # all locate it unchanged — NOT the seat's $CODEX_DIR/<id>.pid.
        self.assertIn(".orchestrator/pids/", self.SH)

    def test_keys_off_codex_run_id_env(self):
        # ORCHESTRATOR_CODEX_RUN_ID (distinct id) keeps the env-gated Stop hook a no-op.
        self.assertIn("ORCHESTRATOR_CODEX_RUN_ID", self.SH)

    def test_closes_stdin_to_avoid_hang(self):
        # codex exec blocks 'Reading additional input from stdin…' on a non-TTY otherwise.
        self.assertIn("< /dev/null", self.SH)

    def test_resume_subcommand_matches_seed(self):
        # C6 HYBRID (#246 fix): after the captured one-shot, the runner hands the tab off to
        # an interactive `codex <resume_subcmd>` — the subcommand comes from the SEED.
        self.assertIn(f"codex {self.SEED['resume_subcmd']}", self.SH)

    def test_resume_flags_match_seed(self):
        # The resume flag set (e.g. --include-non-interactive, so the interactive resume can
        # pick up the exec-CREATED session by id) is the seed's — a codex upgrade renaming it
        # regenerates the runner; a hardcoded literal goes RED.
        self.assertIn(self.SEED["resume_flags"], self.SH)


if __name__ == "__main__":
    unittest.main(verbosity=2)
