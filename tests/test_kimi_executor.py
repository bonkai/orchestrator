"""K5 tests — the kimi EXECUTOR + watchable seat tab: runner drift-guards (pinned to the
kimi ENGINE SEED), is_kimi_dispatch, cleanup, _derive_executor/_validate routing, and the
_kimi_tool_events mapper. Mirrors the codex executor tests.

NOTE: the LIVE iTerm dispatch behavior (poller finalization, the `kimi -r <id> -y` resume
hand-off, the cap) is verified by running a real kimi dispatch after a server restart — it
can't be exercised in a headless test. These tests pin the STATIC contract (flags / schema /
routing / cleanup) that can drift SILENTLY, which is where the real bugs hide.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orchestrator import app
from orchestrator.lib import config, spawn, claude_runner

SEED = config.KIMI_ENGINE_SEED


# ───────────── runner drift-guards (pinned to the kimi ENGINE SEED) ─────────

class TestSpawnKimiRunShPinnedToSeed(unittest.TestCase):
    """The SEAT runner (kimi_run.sh) must match the seed — a seed flag/model change that
    forgets this runner fails HERE."""
    C = spawn.KIMI_RUN_SH_CONTENT

    def test_no_unresolved_placeholder(self):
        self.assertNotIn("@@", self.C)

    def test_uses_dash_p_not_legacy_print(self):
        self.assertIn('-p "$PROMPT"', self.C)                 # the -p invocation (NOT --print)

    def test_flags_and_model_and_bin_from_seed(self):
        self.assertIn(SEED["output_format"], self.C)          # stream-json
        self.assertIn(SEED["model"], self.C)                  # kimi-code/k3
        self.assertIn(spawn._kimi_runner_bin(), self.C)       # resolved binary

    def test_seat_env_and_key_scrub(self):
        self.assertIn("ORCHESTRATOR_KIMI_ID", self.C)         # seat id (NOT ORCHESTRATOR_RUN_ID)
        self.assertIn("unset MOONSHOT_API_KEY", self.C)       # $0 subscription, never billed
        self.assertIn("unset OPENAI_API_KEY", self.C)


class TestSpawnKimiDispatchRunShPinnedToSeed(unittest.TestCase):
    """The EXECUTOR runner (kimi_dispatch_run.sh) drift guard."""
    C = spawn.KIMI_DISPATCH_RUN_SH_CONTENT

    def test_no_unresolved_placeholder(self):
        self.assertNotIn("@@", self.C)

    def test_pid_at_claude_path(self):
        # The executor PID goes to the CLAUDE pids dir so kill / cap / reaper find it.
        self.assertIn(".orchestrator/pids/", self.C)
        self.assertIn("KIMI_PID=$!", self.C)                  # kimi backgrounded → its REAL pid
        self.assertIn("mkfifo", self.C)

    def test_executor_env_and_key_scrub(self):
        self.assertIn("ORCHESTRATOR_KIMI_RUN_ID", self.C)
        self.assertIn("unset MOONSHOT_API_KEY", self.C)

    def test_turn1_flags_from_seed(self):
        self.assertIn('-p "$PROMPT"', self.C)
        self.assertIn(SEED["output_format"], self.C)
        self.assertIn(SEED["model"], self.C)

    def test_resume_handoff_from_session_hint(self):
        self.assertIn("session.resume_hint", self.C)          # capture the resume session id
        self.assertIn('"$SESSION_ID"', self.C)
        self.assertIn(SEED["resume_flag"], self.C)            # -r
        self.assertIn(SEED["resume_approve_flag"], self.C)    # -y (never-prompt, claude parity)


# ───────────────────────── is_kimi_dispatch + cleanup ──────────────────────

class TestIsKimiDispatchAndCleanup(unittest.TestCase):
    def test_detects_by_prompt_sidecar(self):
        d = Path(tempfile.mkdtemp())
        try:
            with mock.patch.object(spawn, "KIMI_DIR", d):
                self.assertFalse(spawn.is_kimi_dispatch(999))
                (d / "999.prompt").write_text("x")
                self.assertTrue(spawn.is_kimi_dispatch(999))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_cleanup_clears_kimi_sidecars(self):
        kd, td, pd, cd = (Path(tempfile.mkdtemp()) for _ in range(4))
        try:
            with mock.patch.object(spawn, "KIMI_DIR", kd), \
                 mock.patch.object(spawn, "TASKS_DIR", td), \
                 mock.patch.object(spawn, "PIDS_DIR", pd), \
                 mock.patch.object(spawn, "CODEX_DIR", cd), \
                 mock.patch.object(spawn, "auto_close_enabled", return_value=False):
                for suf in ("prompt", "model", "jsonl", "done", "fifo"):
                    (kd / f"7.{suf}").write_text("x")
                spawn.cleanup_dispatch_files(7)
                left = [suf for suf in ("prompt", "model", "jsonl", "done", "fifo")
                        if (kd / f"7.{suf}").exists()]
                self.assertEqual(left, [])          # is_kimi_dispatch stops matching after finalize
        finally:
            for d in (kd, td, pd, cd):
                shutil.rmtree(d, ignore_errors=True)


# ───────────────────── _derive_executor / _validate routing ────────────────

class TestKimiExecutorRouting(unittest.TestCase):
    KM = {"kimi-code/k3", "kimi-code/kimi-for-coding"}
    CX = {"gpt-5.5"}

    def test_kimi_id_routes_to_kimi(self):
        self.assertEqual(app._derive_executor("kimi-code/k3", self.CX, self.KM),
                         ("kimi", "", "kimi-code/k3"))

    def test_claude_id_still_claude(self):
        self.assertEqual(app._derive_executor("opus", self.CX, self.KM), ("claude", "opus", ""))

    def test_unknown_id_is_invalid(self):
        self.assertEqual(app._derive_executor("bogus", self.CX, self.KM), ("invalid", "", "bogus"))

    def test_validate_ok(self):
        self.assertEqual(
            app._validate_executor_engine("kimi", "kimi-code/k3", set(),
                                          kimi_models=self.KM, kimi_available=True),
            ("kimi", "kimi-code/k3"))

    def test_validate_blank_model_rejected(self):
        with self.assertRaises(ValueError):        # no silent downgrade
            app._validate_executor_engine("kimi", "", set(), kimi_models=self.KM)

    def test_validate_unknown_model_rejected(self):
        with self.assertRaises(ValueError):
            app._validate_executor_engine("kimi", "nope", set(), kimi_models=self.KM)

    def test_validate_unavailable_rejected(self):
        with self.assertRaises(ValueError):
            app._validate_executor_engine("kimi", "kimi-code/k3", set(),
                                          kimi_models=self.KM, kimi_available=False)


# ──────────────────────────── _kimi_tool_events ────────────────────────────

class TestKimiToolEvents(unittest.TestCase):
    def test_assistant_tool_calls_become_start_events(self):
        obj = {"role": "assistant", "content": "",
               "tool_calls": [
                   {"id": "tc1", "function": {"name": "Shell", "arguments": '{"cmd":"ls"}'}},
                   {"id": "tc2", "function": {"name": "Edit", "arguments": "{}"}}]}
        evs = claude_runner._kimi_tool_events(obj)
        self.assertEqual([e["tool_name"] for e in evs], ["Shell", "Edit"])
        self.assertTrue(all(e["phase"] == "start" for e in evs))
        self.assertEqual(evs[0]["id"], "tc1")

    def test_tool_line_becomes_end_event(self):
        evs = claude_runner._kimi_tool_events(
            {"role": "tool", "tool_call_id": "tc1", "content": "ok"})
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["phase"], "end")
        self.assertEqual(evs[0]["id"], "tc1")

    def test_plain_assistant_has_no_events(self):
        self.assertEqual(claude_runner._kimi_tool_events({"role": "assistant", "content": "hi"}), [])

    def test_garbage_never_raises(self):
        self.assertEqual(claude_runner._kimi_tool_events("not a dict"), [])
        self.assertEqual(claude_runner._kimi_tool_events(
            {"role": "assistant", "tool_calls": "bad"}), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
