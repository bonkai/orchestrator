"""C1 tests — the codex CLI invoker's PARSERS
(claude_runner._envelope_from_codex_stream + _build_codex_run), the codex twin of
_envelope_from_stream_jsonl + _build_claude_run.

NO NETWORK, NO TAB: pure fixture-driven parser tests. A captured
`codex exec --json` JSONL (codex-cli 0.141.0 schema, §0 of CODEX_PLAN.md) must
reconstruct the SAME ClaudeRun shape every brain caller expects, with
cost_usd == 0.0 (Branch A: subscription, never billed). The headless fallback is
exercised with a MOCKED subprocess (never a real `codex`) to prove it parses the
JSONL stream, scrubs OPENAI_API_KEY, and never raises.

(These tests never call run_codex_json end-to-end — that would need iTerm2 and a
live `codex`; C1 has no production caller, so the parser is its only exercise.)

Usage:
    python -m unittest tests.test_codex_parser -v
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orchestrator.lib import claude_runner


# A real codex-cli 0.141.0 `codex exec --json` transcript (the §0 schema): a
# thread.started (the resume handle), turn.started, a reasoning item + the
# agent_message item carrying the final text, then turn.completed carrying token
# usage. The reasoning item shares the `item.completed` type but a DIFFERENT
# item.type, so a faithful parser must not let it leak into the final text.
CAPTURED_JSONL = "\n".join([
    '{"type":"thread.started","thread_id":"019ef12d-aaaa-bbbb-cccc-000000000001"}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"item_0","type":"reasoning","text":"thinking out loud"}}',
    '{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"WAL is fine."}}',
    '{"type":"turn.completed","usage":{"input_tokens":15026,"cached_input_tokens":12032,"output_tokens":9,"reasoning_output_tokens":0}}',
]) + "\n"


class TestCodexEnvelopeParser(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="orch_codex_parse_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, content):
        p = Path(self.tmp) / name
        p.write_text(content, encoding="utf-8")
        return p

    def test_captured_jsonl_parses_to_populated_run(self):
        """ACCEPTANCE: a captured codex JSONL → a populated ClaudeRun, text/model
        set, cost_usd == 0 (Branch A), duration 0 (codex's stream has none)."""
        path = self._write("ok.jsonl", CAPTURED_JSONL)
        env = claude_runner._envelope_from_codex_stream(path)
        self.assertIsNotNone(env)
        run = claude_runner._build_codex_run(env, "gpt-5.5")
        self.assertTrue(run.ok)
        self.assertEqual(run.text, "WAL is fine.")
        # codex --json has no model field → fall back to the model we passed via -m
        self.assertEqual(run.model, "gpt-5.5")
        self.assertEqual(run.cost_usd, 0.0)       # subscription POLICY, not data absence
        self.assertEqual(run.duration_s, 0.0)

    def test_non_agent_message_items_are_ignored(self):
        """Only item.type == 'agent_message' becomes the text; the reasoning item
        sharing the item.completed type must not leak in."""
        path = self._write("r.jsonl", CAPTURED_JSONL)
        env = claude_runner._envelope_from_codex_stream(path)
        self.assertEqual(env["result"], "WAL is fine.")

    def test_last_agent_message_wins(self):
        """Multiple agent_messages → the LAST one's text. A single-message fixture
        would pass even a first-match bug; this multi-message fixture catches it."""
        jsonl = "\n".join([
            '{"type":"thread.started","thread_id":"t"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"FIRST draft"}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"FINAL answer"}}',
            '{"type":"turn.completed","usage":{"output_tokens":3}}',
        ]) + "\n"
        path = self._write("multi.jsonl", jsonl)
        run = claude_runner._build_codex_run(
            claude_runner._envelope_from_codex_stream(path), "gpt-5.5")
        self.assertEqual(run.text, "FINAL answer")

    def test_json_text_is_parsed(self):
        """A JSON (here ```json-fenced) agent_message → parsed_json populated,
        reusing _strip_fences exactly like the claude parser."""
        jsonl = "\n".join([
            '{"type":"item.completed","item":{"type":"agent_message","text":"```json\\n{\\"defect\\": false}\\n```"}}',
            '{"type":"turn.completed","usage":{}}',
        ]) + "\n"
        path = self._write("json.jsonl", jsonl)
        run = claude_runner._build_codex_run(
            claude_runner._envelope_from_codex_stream(path), "gpt-5.5")
        self.assertTrue(run.ok)
        self.assertEqual(run.parsed_json, {"defect": False})

    def test_usage_stashed_in_raw_but_cost_zero(self):
        """Branch A: token usage IS present (so a future paid seat is priceable —
        stash it in raw), but cost_usd is 0 by policy. Guards against a
        usage-reading parser silently pricing the subscription path."""
        path = self._write("u.jsonl", CAPTURED_JSONL)
        run = claude_runner._build_codex_run(
            claude_runner._envelope_from_codex_stream(path), "gpt-5.5")
        self.assertEqual(run.cost_usd, 0.0)
        self.assertEqual((run.raw or {}).get("usage", {}).get("input_tokens"), 15026)

    def test_missing_turn_completed_returns_none(self):
        """No terminal turn.completed (codex died mid-stream / tab cut) → None, the
        codex analogue of claude's 'no result event' → caller yields ok=False."""
        jsonl = "\n".join([
            '{"type":"thread.started","thread_id":"t"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"partial"}}',
        ]) + "\n"
        path = self._write("partial.jsonl", jsonl)
        self.assertIsNone(claude_runner._envelope_from_codex_stream(path))

    def test_turn_completed_without_message_is_ok_empty(self):
        """Clean turn.completed but no agent_message → ok=True with empty text,
        mirroring claude's empty-result behavior (the terminal event gates None,
        not the payload)."""
        path = self._write("empty.jsonl", '{"type":"turn.completed","usage":{}}\n')
        env = claude_runner._envelope_from_codex_stream(path)
        self.assertIsNotNone(env)
        run = claude_runner._build_codex_run(env, "gpt-5.5")
        self.assertTrue(run.ok)
        self.assertEqual(run.text, "")

    def test_empty_file_and_garbage_never_raise(self):
        """Empty file, non-JSON lines, and a missing file all return None — never
        raise (the parser is fail-soft; codex churns its schema across versions)."""
        empty = self._write("e.jsonl", "")
        self.assertIsNone(claude_runner._envelope_from_codex_stream(empty))
        garbage = self._write("g.jsonl", "not json\n{also not valid\n")
        self.assertIsNone(claude_runner._envelope_from_codex_stream(garbage))
        missing = Path(self.tmp) / "does_not_exist.jsonl"
        self.assertIsNone(claude_runner._envelope_from_codex_stream(missing))

    def test_build_codex_run_never_raises_on_weird_envelope(self):
        """_build_codex_run tolerates an empty/garbage envelope → ok=True, empty
        text, cost 0, model fallback — never an exception."""
        run = claude_runner._build_codex_run({}, "gpt-5.5")
        self.assertTrue(run.ok)
        self.assertEqual(run.text, "")
        self.assertEqual(run.cost_usd, 0.0)
        self.assertEqual(run.model, "gpt-5.5")


class TestCodexHeadlessFallback(unittest.TestCase):
    """The iTerm2-absent fallback: a captured `codex exec --json` subprocess,
    MOCKED — never spawns a real codex. Proves it parses the JSONL stream, scrubs
    OPENAI_API_KEY ($0 hard rule) + ORCHESTRATOR_RUN_ID, closes stdin, and never
    raises."""

    def _fake_proc(self, stdout="", returncode=0, stderr=""):
        m = mock.Mock()
        m.stdout = stdout
        m.stderr = stderr
        m.returncode = returncode
        return m

    def test_headless_parses_jsonl_and_scrubs_openai_key(self):
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            captured["env"] = kw.get("env") or {}
            captured["stdin"] = kw.get("stdin")
            return self._fake_proc(stdout=CAPTURED_JSONL, returncode=0)

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-should-be-scrubbed",
                                          "ORCHESTRATOR_RUN_ID": "999"}), \
                mock.patch.object(subprocess, "run", side_effect=fake_run):
            run = claude_runner.run_codex_headless("hi", "/tmp", model="gpt-5.5")

        self.assertTrue(run.ok)
        self.assertEqual(run.text, "WAL is fine.")
        self.assertEqual(run.cost_usd, 0.0)
        # $0 hard rule: the billed-API key is gone from the child env...
        self.assertNotIn("OPENAI_API_KEY", captured["env"])
        # ...and the Stop-hook trigger too (mirror of run_claude_headless).
        self.assertNotIn("ORCHESTRATOR_RUN_ID", captured["env"])
        # exec subcommand + EXPLICIT model + JSONL; stdin closed (no stdin hang).
        self.assertIn("exec", captured["cmd"])
        self.assertIn("-m", captured["cmd"])
        self.assertIn("gpt-5.5", captured["cmd"])
        self.assertIn("--json", captured["cmd"])
        self.assertEqual(captured["stdin"], subprocess.DEVNULL)

    def test_headless_effort_applied_only_when_given(self):
        """An effort → `-c model_reasoning_effort=<e>`; no effort → codex uses the
        model default (no override), what C0 verified working."""
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return self._fake_proc(stdout=CAPTURED_JSONL, returncode=0)

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            claude_runner.run_codex_headless("hi", "/tmp", effort="high")
        self.assertIn("model_reasoning_effort=high", " ".join(seen["cmd"]))

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            claude_runner.run_codex_headless("hi", "/tmp")  # default effort=""
        self.assertNotIn("model_reasoning_effort", " ".join(seen["cmd"]))

    def test_headless_nonzero_exit_is_ok_false(self):
        with mock.patch.object(subprocess, "run",
                               return_value=self._fake_proc(stdout="", returncode=1,
                                                            stderr="auth expired")):
            run = claude_runner.run_codex_headless("hi", "/tmp")
        self.assertFalse(run.ok)
        self.assertIn("codex exit 1", run.error)

    def test_headless_missing_binary_is_ok_false_not_raise(self):
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError()):
            run = claude_runner.run_codex_headless("hi", "/tmp")
        self.assertFalse(run.ok)
        self.assertIn("not found", run.error)

    def test_headless_timeout_is_ok_false(self):
        with mock.patch.object(subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("codex", 1)):
            run = claude_runner.run_codex_headless("hi", "/tmp", timeout_s=1)
        self.assertFalse(run.ok)
        self.assertIn("timed out", run.error)

    def test_headless_exit0_but_no_turn_completed_is_ok_false(self):
        """Exit 0 yet a truncated stream (no turn.completed) → ok=False, not a
        bogus ok=True empty answer."""
        truncated = '{"type":"item.completed","item":{"type":"agent_message","text":"x"}}\n'
        with mock.patch.object(subprocess, "run",
                               return_value=self._fake_proc(stdout=truncated, returncode=0)):
            run = claude_runner.run_codex_headless("hi", "/tmp")
        self.assertFalse(run.ok)
        self.assertIn("no turn.completed", run.error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
