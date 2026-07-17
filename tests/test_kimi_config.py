"""Unit tests for the Kimi ENGINE config layer (K3) — KIMI_ENGINE_SEED merge,
kimi_engine(), and kimi_cli_available(). Mirrors the codex analogs in
tests/test_codex_config.py / tests/test_codex_seat.py (TestCodexCliAvailable).

The availability probe shells out to a real `kimi provider list`, so every case
here mocks _resolve_kimi_bin / os.path.exists / subprocess.run for a
host-independent result (the suite must pass whether or not kimi-code is
installed + logged in on the box running it).
"""

import json
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

from orchestrator.lib import config


# ───────────────────────── KIMI_ENGINE_SEED merge ──────────────────────────

class TestKimiEngineSeedMerge(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_kimi_cfg_"))
        self._orig = config.CONFIG_PATH
        config.CONFIG_PATH = self.tmp / "config.json"

    def tearDown(self):
        config.CONFIG_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, obj):
        config.CONFIG_PATH.write_text(json.dumps(obj), encoding="utf-8")

    def test_seed_defaults_when_no_file(self):
        k = config.kimi_engine()
        self.assertEqual(k["model"], "kimi-code/k3")
        self.assertIn("kimi-code/k3", k["models"])
        self.assertEqual(k["prompt_flag"], "-p")                 # NOT --print (legacy kimi-cli)
        self.assertEqual(k["output_format"], "stream-json")
        self.assertEqual(k["auth_probe"], ["kimi", "provider", "list"])
        self.assertNotIn("effort", k)                            # kimi-code has no per-call effort
        self.assertNotIn("sandbox", k)                           # nor sandbox modes

    def test_file_overrides_per_key(self):
        self._write({"fusion": {"kimi": {"model": "kimi-code/kimi-for-coding",
                                          "max_concurrent_dispatches": 5}}})
        k = config.kimi_engine()
        self.assertEqual(k["model"], "kimi-code/kimi-for-coding")   # overridden
        self.assertEqual(k["max_concurrent_dispatches"], 5)
        self.assertEqual(k["prompt_flag"], "-p")                    # seed value kept

    def test_garbage_kimi_block_ignored(self):
        self._write({"fusion": {"kimi": "not-a-dict"}})
        self.assertEqual(config.kimi_engine()["model"], "kimi-code/k3")

    def test_returned_config_cannot_corrupt_seed(self):
        config.kimi_engine()["seats"].append({"junk": 1})
        config.kimi_engine()["models"].append("junk")
        config.kimi_engine()["auth_probe"].append("junk")
        self.assertEqual(len(config.KIMI_ENGINE_SEED["seats"]), 2)
        self.assertNotIn("junk", config.KIMI_ENGINE_SEED["models"])
        self.assertEqual(config.KIMI_ENGINE_SEED["auth_probe"], ["kimi", "provider", "list"])


# ───────────────────────── kimi_cli_available() ────────────────────────────

class TestKimiCliAvailable(unittest.TestCase):
    def setUp(self):
        # Baseline: binary resolves + creds file present. Individual tests override.
        p = mock.patch.object(config, "_resolve_kimi_bin", return_value="/fake/bin/kimi")
        p.start(); self.addCleanup(p.stop)
        e = mock.patch.object(config.os.path, "exists", return_value=True)
        e.start(); self.addCleanup(e.stop)

    def test_missing_binary_returns_false_without_probing(self):
        with mock.patch.object(config, "_resolve_kimi_bin", return_value=None), \
             mock.patch.object(config.subprocess, "run") as run:
            self.assertFalse(config.kimi_cli_available())
            run.assert_not_called()

    def test_missing_creds_returns_false_without_probing(self):
        with mock.patch.object(config.os.path, "exists", return_value=False), \
             mock.patch.object(config.subprocess, "run") as run:
            self.assertFalse(config.kimi_cli_available())
            run.assert_not_called()

    def test_exit_zero_is_available(self):
        with mock.patch.object(config.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="managed:kimi-code source=oauth")):
            self.assertTrue(config.kimi_cli_available())

    def test_nonzero_exit_is_unavailable(self):
        with mock.patch.object(config.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="")):
            self.assertFalse(config.kimi_cli_available())

    def test_timeout_returns_false(self):
        with mock.patch.object(config.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(cmd="kimi", timeout=10)):
            self.assertFalse(config.kimi_cli_available())

    def test_oserror_returns_false(self):
        with mock.patch.object(config.subprocess, "run", side_effect=OSError("boom")):
            self.assertFalse(config.kimi_cli_available())

    def test_probe_is_non_billing_and_closes_stdin(self):
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            captured["env"] = kw.get("env")
            captured["stdin"] = kw.get("stdin")
            return mock.Mock(returncode=0, stdout="source=oauth")

        with mock.patch.dict(os.environ, {"MOONSHOT_API_KEY": "sk-x", "OPENAI_API_KEY": "sk-y"}), \
             mock.patch.object(config.subprocess, "run", side_effect=fake_run):
            self.assertTrue(config.kimi_cli_available())
        self.assertNotIn("MOONSHOT_API_KEY", captured["env"])      # billed keys scrubbed → subscription probe
        self.assertNotIn("OPENAI_API_KEY", captured["env"])
        self.assertEqual(captured["stdin"], subprocess.DEVNULL)    # can't hang on stdin
        self.assertEqual(captured["cmd"][1:], ["provider", "list"])  # resolved bin + `provider list`


# ───────────────────────── is_fusion_available() gate ──────────────────────

class TestIsFusionAvailableKimi(unittest.TestCase):
    def _patch_all(self, *, claude=False, codex=False, providers=None, kimi=False):
        return [
            mock.patch.object(config, "claude_cli_available", return_value=claude),
            mock.patch.object(config, "codex_cli_available", return_value=codex),
            mock.patch.object(config, "active_providers", return_value=providers or {}),
            mock.patch.object(config, "kimi_cli_available", return_value=kimi),
        ]

    def test_kimi_alone_makes_fusion_available(self):
        ps = self._patch_all(kimi=True)
        [p.start() for p in ps]
        try:
            self.assertTrue(config.is_fusion_available())
        finally:
            [p.stop() for p in ps]

    def test_all_engines_off_is_unavailable(self):
        ps = self._patch_all()
        [p.start() for p in ps]
        try:
            self.assertFalse(config.is_fusion_available())
        finally:
            [p.stop() for p in ps]


if __name__ == "__main__":
    unittest.main(verbosity=2)
