"""Unit tests for orchestrator.lib.config — the Fusion F0 config layer.

Covers the F0 verifies:
  F0.1  load_config() / get_provider_key() / active_providers() /
        is_fusion_available()
  F0.2  fusion_config() seed-merge (seeds with no file; your values when set)
  F0.3  the config.json registry template embedded in bin/install.sh — valid,
        complete, chmod 600, and written idempotently (a 2nd run never clobbers
        keys you've pasted).

Isolated like test_e2e.py: points config.CONFIG_PATH at a tempdir and SCRUBS
every provider key env var in setUp, so the suite ignores any real
~/.orchestrator/config.json and any keys exported in the host shell.

Usage:
    python -m pytest tests/test_fusion_config.py -v
    # or
    python tests/test_fusion_config.py
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Import the in-repo orchestrator (not any installed version).
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orchestrator.lib import config

# Every provider key env var the seeds reference — scrubbed so the host
# environment can't make a "clean config" look populated (or vice versa).
KEY_ENVS = [p["key_env"] for p in config.FUSION_PROVIDERS_SEED.values()]


class _IsolatedConfig(unittest.TestCase):
    """Base: a temp config.json path + a clean env (no provider keys leak in)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_fusion_cfg_"))
        self._orig_config_path = config.CONFIG_PATH
        config.CONFIG_PATH = self.tmp / "config.json"
        # Snapshot + remove provider key env vars for a deterministic baseline.
        self._env_backup = {k: os.environ.pop(k, None) for k in KEY_ENVS}
        # The host may have the `claude` CLI installed, which post-F9 ALONE makes
        # is_fusion_available() true (a pure Claude-seat panel needs no key). These
        # tests verify the EXTERNAL-provider gating (active_providers + the >=2
        # rule), so disable the local CLI seat for a host-independent baseline.
        cli_patch = mock.patch.object(config, "claude_cli_available", return_value=False)
        cli_patch.start()
        self.addCleanup(cli_patch.stop)
        # C2.1: is_fusion_available() now ALSO consults codex_cli_available(), which
        # shells out to a real `codex login status` probe — host-dependent (it is
        # True on a laptop where codex is logged in), so the assertFalse cases below
        # would flip. Disable codex too for the same host-independent baseline.
        codex_patch = mock.patch.object(config, "codex_cli_available", return_value=False)
        codex_patch.start()
        self.addCleanup(codex_patch.stop)

    def tearDown(self):
        config.CONFIG_PATH = self._orig_config_path
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_config(self, obj):
        config.CONFIG_PATH.write_text(json.dumps(obj), encoding="utf-8")


# ───────────────────────── F0.1: load_config() ─────────────────────────────

class TestLoadConfig(_IsolatedConfig):
    def test_absent_returns_empty_dict(self):
        self.assertEqual(config.load_config(), {})  # never raises

    def test_malformed_json_returns_empty_dict(self):
        config.CONFIG_PATH.write_text("{ not valid json", encoding="utf-8")
        self.assertEqual(config.load_config(), {})  # never raises

    def test_non_object_json_returns_empty_dict(self):
        config.CONFIG_PATH.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(config.load_config(), {})

    def test_valid_object_returned_verbatim(self):
        self._write_config({"fusion": {"preset": "max"}})
        self.assertEqual(config.load_config(), {"fusion": {"preset": "max"}})


# ───────────────────────── F0.2: fusion_config() ───────────────────────────

class TestFusionConfig(_IsolatedConfig):
    def test_seeds_when_no_file(self):
        fc = config.fusion_config()
        self.assertEqual(fc["preset"], "budget")
        self.assertEqual(fc["timeout_s"], 300)
        self.assertEqual(set(fc["providers"]), set(config.FUSION_PROVIDERS_SEED))
        self.assertEqual(fc["providers"]["deepseek"]["model"], "deepseek-chat")
        self.assertEqual(fc["presets"]["budget"], ["deepseek", "minimax", "gemini"])
        self.assertEqual(fc["presets"]["max"],
                         ["deepseek", "xai", "gemini", "minimax", "glm", "qwen"])

    def test_file_values_override_seeds(self):
        self._write_config({"fusion": {
            "preset": "balanced",
            "timeout_s": 120,
            "providers": {"deepseek": {"model": "deepseek-reasoner"}},
            "presets": {"budget": ["deepseek", "xai"]},
        }})
        fc = config.fusion_config()
        self.assertEqual(fc["preset"], "balanced")
        self.assertEqual(fc["timeout_s"], 120)
        # A partial provider override keeps the seed's script/key_env/prices.
        ds = fc["providers"]["deepseek"]
        self.assertEqual(ds["model"], "deepseek-reasoner")
        self.assertEqual(ds["key_env"], "DEEPSEEK_API_KEY")
        self.assertEqual(ds["price_in"], 0.44)
        # An overridden preset replaces that one; the others stay seeded.
        self.assertEqual(fc["presets"]["budget"], ["deepseek", "xai"])
        self.assertEqual(fc["presets"]["balanced"], ["deepseek", "xai", "qwen"])

    def test_provider_present_only_in_file_is_added(self):
        self._write_config({"fusion": {"providers": {
            "moonshot": {"script": "providers/moonshot.py",
                         "key_env": "MOONSHOT_API_KEY", "model": "kimi-k2",
                         "price_in": 0.6, "price_out": 2.5},
        }}})
        fc = config.fusion_config()
        self.assertIn("moonshot", fc["providers"])
        self.assertEqual(fc["providers"]["moonshot"]["model"], "kimi-k2")
        # Seeds still present alongside the new one.
        self.assertIn("deepseek", fc["providers"])

    def test_empty_or_falsy_preset_falls_back_to_default(self):
        self._write_config({"fusion": {"preset": "", "timeout_s": 0}})
        fc = config.fusion_config()
        self.assertEqual(fc["preset"], "budget")
        self.assertEqual(fc["timeout_s"], 300)

    def test_garbage_fusion_block_ignored(self):
        self._write_config({"fusion": "not-a-dict"})
        fc = config.fusion_config()
        self.assertEqual(fc["preset"], "budget")
        self.assertEqual(set(fc["providers"]), set(config.FUSION_PROVIDERS_SEED))

    def test_verify_defaults_false(self):
        self.assertFalse(config.fusion_config()["verify"])

    def test_verify_true_in_file_is_read(self):
        self._write_config({"fusion": {"verify": True}})
        self.assertTrue(config.fusion_config()["verify"])


# ──────────────── F11.c.1: set_verify() writer ──────────────────────────────

class TestSetVerify(_IsolatedConfig):
    def test_round_trips_on_and_off(self):
        config.set_verify(True)
        self.assertTrue(config.fusion_config()["verify"])
        config.set_verify(False)
        self.assertFalse(config.fusion_config()["verify"])

    def test_preserves_api_keys_and_other_fusion(self):
        self._write_config({"fusion": {
            "preset": "max",
            "providers": {"deepseek": {"api_key": "sk-keepme"}},
        }})
        config.set_verify(True)
        raw = json.loads(config.CONFIG_PATH.read_text())["fusion"]
        self.assertTrue(raw["verify"])                 # flag flips on …
        self.assertEqual(raw["preset"], "max")         # … preset untouched …
        self.assertEqual(raw["providers"]["deepseek"]["api_key"], "sk-keepme")  # … key kept


# ──────────────── F0.1: get_provider_key() precedence ──────────────────────

class TestKeyResolution(_IsolatedConfig):
    def test_no_key_anywhere_returns_none(self):
        self.assertIsNone(config.get_provider_key("deepseek"))

    def test_env_var_resolves(self):
        os.environ["DEEPSEEK_API_KEY"] = "sk-env"
        self.assertEqual(config.get_provider_key("deepseek"), "sk-env")

    def test_file_api_key_resolves(self):
        self._write_config({"fusion": {"providers": {"deepseek": {"api_key": "sk-file"}}}})
        self.assertEqual(config.get_provider_key("deepseek"), "sk-file")

    def test_env_takes_precedence_over_file(self):
        os.environ["DEEPSEEK_API_KEY"] = "sk-env"
        self._write_config({"fusion": {"providers": {"deepseek": {"api_key": "sk-file"}}}})
        self.assertEqual(config.get_provider_key("deepseek"), "sk-env")

    def test_whitespace_only_value_is_unset(self):
        os.environ["DEEPSEEK_API_KEY"] = "   "
        self.assertIsNone(config.get_provider_key("deepseek"))

    def test_unknown_provider_returns_none(self):
        self.assertIsNone(config.get_provider_key("does-not-exist"))


# ───── F0.1: active_providers() + is_fusion_available() (the headline verify) ─

class TestActiveProvidersAndAvailability(_IsolatedConfig):
    def test_clean_config_is_not_available(self):
        self.assertEqual(config.active_providers(), {})
        self.assertFalse(config.is_fusion_available())

    def test_one_key_lists_one_but_not_available(self):
        os.environ["DEEPSEEK_API_KEY"] = "sk-1"
        self.assertEqual(set(config.active_providers()), {"deepseek"})
        self.assertFalse(config.is_fusion_available())  # needs >= 2

    def test_two_keys_become_available(self):
        os.environ["DEEPSEEK_API_KEY"] = "sk-1"
        os.environ["GEMINI_API_KEY"] = "sk-2"
        self.assertEqual(set(config.active_providers()), {"deepseek", "gemini"})
        self.assertTrue(config.is_fusion_available())

    def test_two_file_keys_become_available(self):
        self._write_config({"fusion": {"providers": {
            "deepseek": {"api_key": "sk-a"},
            "qwen": {"api_key": "sk-b"},
        }}})
        self.assertEqual(set(config.active_providers()), {"deepseek", "qwen"})
        self.assertTrue(config.is_fusion_available())

    def test_active_lists_exactly_the_keyed_and_enabled(self):
        # deepseek: keyed + enabled (active). xai: keyed but disabled (inactive).
        os.environ["DEEPSEEK_API_KEY"] = "sk-1"
        self._write_config({"fusion": {"providers": {
            "xai": {"enabled": False, "api_key": "sk-file"},
        }}})
        active = config.active_providers()
        self.assertEqual(set(active), {"deepseek"})
        self.assertNotIn("xai", active)               # keyed but disabled
        self.assertFalse(config.is_fusion_available())  # only 1 active

    def test_enabled_true_is_still_active(self):
        os.environ["DEEPSEEK_API_KEY"] = "sk-1"
        os.environ["GEMINI_API_KEY"] = "sk-2"
        self._write_config({"fusion": {"providers": {
            "deepseek": {"enabled": True},
        }}})
        self.assertEqual(set(config.active_providers()), {"deepseek", "gemini"})
        self.assertTrue(config.is_fusion_available())

    def test_membership_and_model_exposed(self):
        os.environ["DEEPSEEK_API_KEY"] = "sk-1"
        active = config.active_providers()
        self.assertIn("deepseek", active)                       # membership (F3.1)
        self.assertEqual(active["deepseek"]["model"], "deepseek-chat")  # model (F4.2)

    def test_api_key_never_leaks_into_active_entry(self):
        # active_providers() feeds the browser; keys must never reach it.
        self._write_config({"fusion": {"providers": {
            "deepseek": {"api_key": "sk-secret-1"},
            "gemini": {"api_key": "sk-secret-2"},
        }}})
        active = config.active_providers()
        self.assertEqual(set(active), {"deepseek", "gemini"})
        for entry in active.values():
            self.assertNotIn("api_key", entry)


# ─────────── F0.3: the install.sh config.json registry template ─────────────

class TestInstallShConfigTemplate(unittest.TestCase):
    """Extracts the real FUSION_CONFIG_BLOCK from bin/install.sh and runs it in
    an isolated $ORCH_HOME (the block depends only on that var), proving the
    template is valid + complete and that a 2nd run never clobbers keys."""

    INSTALL_SH = REPO / "bin" / "install.sh"

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="orch_install_"))
        self.cfg = self.home / "config.json"

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def _block(self):
        text = self.INSTALL_SH.read_text(encoding="utf-8")
        m = re.search(r"# >>> FUSION_CONFIG_BLOCK\b.*?\n(.*?)# <<< FUSION_CONFIG_BLOCK",
                      text, re.DOTALL)
        if not m:
            self.fail("FUSION_CONFIG_BLOCK sentinels not found in bin/install.sh")
        return m.group(1)

    def _run_block(self):
        script = "set -e\nexport ORCH_HOME=%s\n%s" % (
            json.dumps(str(self.home)), self._block())
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"install block failed:\n{r.stderr}")
        return r

    def test_writes_valid_complete_template_when_absent(self):
        self._run_block()
        self.assertTrue(self.cfg.exists())
        fusion = json.loads(self.cfg.read_text())["fusion"]
        self.assertEqual(fusion["preset"], "budget")
        self.assertEqual(fusion["timeout_s"], 300)
        self.assertEqual(set(fusion["providers"]), set(config.FUSION_PROVIDERS_SEED))
        self.assertEqual(set(fusion["presets"]), {"budget", "balanced", "max"})
        for entry in fusion["providers"].values():
            self.assertEqual(entry["api_key"], "")   # empty — keys pasted later
            self.assertTrue(entry["key_env"])
            self.assertTrue(entry["model"])

    def test_template_written_chmod_600(self):
        self._run_block()
        self.assertEqual(oct(self.cfg.stat().st_mode & 0o777), "0o600")

    def test_second_run_is_noop_and_never_clobbers_keys(self):
        self._run_block()                       # 1st run: writes template
        obj = json.loads(self.cfg.read_text())
        obj["fusion"]["providers"]["deepseek"]["api_key"] = "sk-USER-PASTED"
        obj["fusion"]["preset"] = "max"
        self.cfg.write_text(json.dumps(obj))    # user edits the file
        self._run_block()                       # 2nd run: must be a no-op
        obj2 = json.loads(self.cfg.read_text())
        self.assertEqual(
            obj2["fusion"]["providers"]["deepseek"]["api_key"], "sk-USER-PASTED")
        self.assertEqual(obj2["fusion"]["preset"], "max")


if __name__ == "__main__":
    unittest.main(verbosity=2)
