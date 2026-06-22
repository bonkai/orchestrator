"""F8 tests — the registry/preset WRITE helpers behind the Settings UI.

The whole settings surface rests on two safety invariants, tested hard here:
  1. api_keys are FILE-ONLY — preserved across every save, never settable via the
     write helpers (which take no api_key argument).
  2. a malformed config.json is NEVER overwritten (that would destroy the user's
     pasted keys) — the write aborts with ConfigWriteError.

Isolated like test_fusion_config: config.CONFIG_PATH points at a tempdir.

Usage:
    python -m unittest tests.test_fusion_settings -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orchestrator.lib import config
from orchestrator import app as app_module


class _IsolatedConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_settings_"))
        self._orig = config.CONFIG_PATH
        config.CONFIG_PATH = self.tmp / "config.json"

    def tearDown(self):
        config.CONFIG_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, obj):
        config.CONFIG_PATH.write_text(json.dumps(obj), encoding="utf-8")

    def _read(self):
        return json.loads(config.CONFIG_PATH.read_text())


class TestSetPreset(_IsolatedConfig):
    def test_sets_preset_and_preserves_keys(self):
        self._write({"fusion": {"preset": "budget", "providers": {
            "glm": {"api_key": "secret-glm", "model": "glm-5.2"}}}})
        fc = config.set_preset("max")
        self.assertEqual(fc["preset"], "max")
        # api_key untouched on disk
        self.assertEqual(self._read()["fusion"]["providers"]["glm"]["api_key"], "secret-glm")

    def test_works_when_file_absent(self):
        fc = config.set_preset("balanced")        # no file yet → created
        self.assertEqual(fc["preset"], "balanced")
        self.assertTrue(config.CONFIG_PATH.exists())


class TestUpsertProvider(_IsolatedConfig):
    def test_edit_existing_preserves_api_key(self):
        self._write({"fusion": {"providers": {
            "glm": {"api_key": "secret-glm", "model": "glm-5.2",
                    "script": "providers/glm.py", "key_env": "ZAI_API_KEY",
                    "price_in": 0, "price_out": 0}}}})
        config.upsert_provider("glm", script="providers/glm.py", key_env="ZAI_API_KEY",
                               model="glm-6.0", price_in=1.0, price_out=2.0, enabled=True)
        entry = self._read()["fusion"]["providers"]["glm"]
        self.assertEqual(entry["model"], "glm-6.0")         # edited
        self.assertEqual(entry["price_in"], 1.0)
        self.assertEqual(entry["api_key"], "secret-glm")    # PRESERVED

    def test_new_provider_gets_empty_key(self):
        config.upsert_provider("moonshot", script="providers/moonshot.py",
                               key_env="MOONSHOT_API_KEY", model="kimi-k2",
                               price_in=0.6, price_out=2.5)
        entry = self._read()["fusion"]["providers"]["moonshot"]
        self.assertEqual(entry["api_key"], "")              # never browser-supplied
        self.assertEqual(entry["model"], "kimi-k2")

    def test_blank_name_raises(self):
        with self.assertRaises(config.ConfigWriteError):
            config.upsert_provider("  ", script="s", key_env="K", model="m",
                                   price_in=0, price_out=0)


class TestEnabledAndRemove(_IsolatedConfig):
    def test_toggle_enabled_preserves_key(self):
        self._write({"fusion": {"providers": {
            "glm": {"api_key": "secret-glm", "model": "glm-5.2"}}}})
        config.set_provider_enabled("glm", False)
        entry = self._read()["fusion"]["providers"]["glm"]
        self.assertFalse(entry["enabled"])
        self.assertEqual(entry["api_key"], "secret-glm")

    def test_toggle_seed_only_provider_materializes_without_key(self):
        # deepseek is a SEED; not in the (empty) file. Toggling persists it.
        config.set_provider_enabled("deepseek", False)
        entry = self._read()["fusion"]["providers"]["deepseek"]
        self.assertFalse(entry["enabled"])
        self.assertNotIn("api_key", entry)      # seed carried no key; none invented

    def test_toggle_unknown_raises(self):
        with self.assertRaises(config.ConfigWriteError):
            config.set_provider_enabled("does-not-exist", False)

    def test_remove_drops_override(self):
        self._write({"fusion": {"providers": {
            "gemini-pro": {"api_key": "k", "model": "gemini-3.1-pro-preview"},
            "glm": {"api_key": "secret-glm"}}}})
        config.remove_provider("gemini-pro")
        provs = self._read()["fusion"]["providers"]
        self.assertNotIn("gemini-pro", provs)
        self.assertIn("glm", provs)                          # others untouched


class TestCorruptionGuard(_IsolatedConfig):
    def test_malformed_file_is_never_overwritten(self):
        config.CONFIG_PATH.write_text("{ this is not valid json", encoding="utf-8")
        for call in (lambda: config.set_preset("max"),
                     lambda: config.upsert_provider("x", script="s", key_env="K",
                                                    model="m", price_in=0, price_out=0),
                     lambda: config.remove_provider("x"),
                     lambda: config.set_provider_enabled("glm", False)):
            with self.assertRaises(config.ConfigWriteError):
                call()
        # the malformed file is left exactly as-is (keys, if any, not clobbered)
        self.assertEqual(config.CONFIG_PATH.read_text(), "{ this is not valid json")

    def test_saved_file_is_chmod_600(self):
        config.set_preset("budget")
        mode = oct(config.CONFIG_PATH.stat().st_mode & 0o777)
        self.assertEqual(mode, "0o600")

    def test_key_survives_a_sequence_of_edits(self):
        self._write({"fusion": {"preset": "budget", "providers": {
            "glm": {"api_key": "KEEP-ME", "model": "glm-5.2"}}}})
        config.set_preset("max")
        config.upsert_provider("glm", script="providers/glm.py", key_env="ZAI_API_KEY",
                               model="glm-6.0", price_in=0, price_out=0)
        config.set_provider_enabled("glm", True)
        config.upsert_provider("xai", script="providers/xai.py", key_env="XAI_API_KEY",
                               model="grok-4", price_in=1.25, price_out=2.5)
        self.assertEqual(self._read()["fusion"]["providers"]["glm"]["api_key"], "KEEP-ME")


class TestSettingsReadModel(unittest.TestCase):
    """app._settings_ctx() must surface the registry WITHOUT ever leaking a key —
    only a derived has_key boolean (like active_providers does for the form)."""

    FCFG = {"preset": "budget", "timeout_s": 300,
            "providers": {
                "glm": {"model": "glm-5.2", "api_key": "SECRET-GLM",
                        "script": "providers/glm.py", "key_env": "ZAI_API_KEY",
                        "price_in": 0, "price_out": 0},
                "deepseek": {"model": "deepseek-chat", "api_key": "",
                             "script": "providers/deepseek.py",
                             "key_env": "DEEPSEEK_API_KEY", "price_in": 0.44, "price_out": 0.87},
            },
            "presets": {"budget": ["glm", "deepseek"]}}

    def _ctx(self):
        with mock.patch.object(app_module.config, "fusion_config", return_value=self.FCFG), \
                mock.patch.object(app_module.config, "active_providers",
                                  return_value={"glm": {}}), \
                mock.patch.object(app_module.config, "get_provider_key",
                                  side_effect=lambda n: "SECRET-GLM" if n == "glm" else None), \
                mock.patch.object(app_module.config, "is_fusion_available", return_value=True), \
                mock.patch.object(app_module.config, "claude_cli_available", return_value=True):
            return app_module._settings_ctx(ok="saved")

    def test_never_leaks_api_key(self):
        ctx = self._ctx()
        for p in ctx["providers"]:
            self.assertNotIn("api_key", p)

    def test_has_key_and_active_derived(self):
        ctx = self._ctx()
        by = {p["name"]: p for p in ctx["providers"]}
        self.assertTrue(by["glm"]["has_key"])
        self.assertTrue(by["glm"]["active"])
        self.assertFalse(by["deepseek"]["has_key"])      # empty key → no key resolves
        self.assertFalse(by["deepseek"]["active"])
        self.assertEqual(ctx["preset"], "budget")
        self.assertEqual(ctx["active_count"], 1)

    def test_verify_enabled_surfaced(self):
        # F11.c.1: default (no verify key) is False; an explicit verify=True surfaces.
        self.assertFalse(self._ctx()["verify_enabled"])
        fcfg = dict(self.FCFG, verify=True)
        with mock.patch.object(app_module.config, "fusion_config", return_value=fcfg), \
                mock.patch.object(app_module.config, "active_providers", return_value={"glm": {}}), \
                mock.patch.object(app_module.config, "get_provider_key", side_effect=lambda n: None), \
                mock.patch.object(app_module.config, "is_fusion_available", return_value=True), \
                mock.patch.object(app_module.config, "claude_cli_available", return_value=True):
            self.assertTrue(app_module._settings_ctx()["verify_enabled"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
