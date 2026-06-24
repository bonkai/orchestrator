"""F4 (server side) tests — _view_ctx() shapes the dispatch-form Fusion picker
data the template + JS rely on:

  fusion_providers      every registry seat, in registry order, each flagged
                        active (keyed+enabled → checkable) or not (→ greyed),
                        carrying its model id for display.
  fusion_available      True only at >= 2 active seats (gates the checkbox).
  fusion_default_panel  the configured preset's ACTIVE members, in preset order
                        (seeds the picker's checked set when nothing is saved).

NO NETWORK / NO DB: db.list_tabs/list_projects and config.fusion_config/
active_providers are mocked, so this exercises only the context shaping — the
client-side bits (localStorage persistence, reveal-on-toggle) are DOM behavior
verified in the browser, not here.

Usage:
    python3 -m unittest tests.test_fusion_view_ctx -v
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orchestrator import app as app_module

# Three registered seats; the preset lists all three in a deliberate order
# (deepseek, xai, gemini) so we can prove default-panel order follows the PRESET,
# not the registry or the active set.
FCFG = {
    "preset": "budget",
    "timeout_s": 300,
    "providers": {
        "deepseek": {"model": "deepseek-chat", "price_in": 0.44, "price_out": 0.87},
        "gemini": {"model": "gemini-2.5-flash", "price_in": 0.30, "price_out": 1.50},
        "xai": {"model": "grok-4", "price_in": 1.25, "price_out": 2.50},
    },
    "presets": {"budget": ["deepseek", "xai", "gemini"]},
}

# C5.2 / C6: _view_ctx also surfaces codex availability + the codex model list (the
# default model + the full valid-model set + the seat-panel models, sourced from the
# codex ENGINE config). Stub config.codex_engine so _codex_seat_models() doesn't read
# the real ~/.orchestrator/config.json.
CODEX_ENGINE = {"model": "gpt-5.5",
                "models": ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini"],
                "efforts": ["minimal", "low", "medium", "high", "xhigh"],
                "seats": [{"kind": "codex_cli", "model": "gpt-5.5"}]}


class TestViewCtxFusion(unittest.TestCase):
    def _ctx(self, active, codex_available=False):
        """_view_ctx() with the DB + config seams mocked. `active` is the
        active_providers() return (name → merged entry; only the names matter);
        `codex_available` drives the (mocked) codex_cli_available()."""
        # Disable the local CLI seats: post-F9 the `claude` CLI alone satisfies
        # is_fusion_available(), and post-C2.1 a logged-in `codex` does too — but
        # this test verifies the EXTERNAL-provider gating (>=2 active keyed
        # providers), so isolate that path (codex_cli_available would otherwise
        # shell out to a real, host-dependent probe).
        with mock.patch.object(app_module.db, "list_tabs", return_value=[]), \
                mock.patch.object(app_module.db, "list_projects", return_value=[]), \
                mock.patch.object(app_module.config, "fusion_config", return_value=FCFG), \
                mock.patch.object(app_module.config, "claude_cli_available", return_value=False), \
                mock.patch.object(app_module.config, "codex_cli_available", return_value=codex_available), \
                mock.patch.object(app_module.config, "codex_engine", return_value=CODEX_ENGINE), \
                mock.patch.object(app_module.config, "active_providers", return_value=active):
            return app_module._view_ctx()

    def test_lists_every_seat_with_active_flag_in_registry_order(self):
        ctx = self._ctx({"deepseek": {}, "gemini": {}})
        rows = ctx["fusion_providers"]
        self.assertEqual([r["name"] for r in rows], ["deepseek", "gemini", "xai"])
        # deepseek + gemini keyed → active; xai unkeyed → greyed.
        self.assertEqual([r["active"] for r in rows], [True, True, False])
        self.assertEqual(rows[0]["model"], "deepseek-chat")   # model id carried for display

    def test_available_requires_two_active(self):
        self.assertTrue(self._ctx({"deepseek": {}, "gemini": {}})["fusion_available"])
        self.assertFalse(self._ctx({"deepseek": {}})["fusion_available"])
        self.assertFalse(self._ctx({})["fusion_available"])

    def test_default_panel_is_preset_active_members_in_preset_order(self):
        # preset = [deepseek, xai, gemini]; active = deepseek + gemini →
        # default = [deepseek, gemini]: preset order kept, inactive xai dropped.
        ctx = self._ctx({"deepseek": {}, "gemini": {}})
        self.assertEqual(ctx["fusion_default_panel"], ["deepseek", "gemini"])

    def test_default_panel_empty_when_no_active_preset_members(self):
        # An active seat that isn't in the preset contributes nothing to the seed.
        ctx = self._ctx({"glm": {}})
        self.assertEqual(ctx["fusion_default_panel"], [])

    def test_keys_present_so_template_never_hits_undefined(self):
        # The template/JS reference these unconditionally; guarantee they exist.
        ctx = self._ctx({})
        for key in ("fusion_providers", "fusion_available", "fusion_default_panel",
                    "codex_cli_available", "codex_seat_models", "codex_seat_efforts"):
            self.assertIn(key, ctx)

    def test_view_ctx_exposes_codex_availability_and_models(self):
        # C5.2/C6: the engine picker reads codex_cli_available to grey the codex <option>,
        # and codex_seat_models to populate the codex model select. C6 exposes the FULL
        # valid set (default first so engine=codex defaults to gpt-5.5, then the rest
        # sorted) — all codex ids, never a Claude id.
        off = self._ctx({"deepseek": {}, "gemini": {}}, codex_available=False)
        self.assertFalse(off["codex_cli_available"])
        self.assertEqual(off["codex_seat_models"], ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini"])
        self.assertEqual(off["codex_seat_models"][0], "gpt-5.5")   # DEFAULT model first
        self.assertNotIn("opus", off["codex_seat_models"])
        # The codex-seat picker's thinking-level ladder, in SEED order (not sorted —
        # minimal→xhigh is meaningful), and codex's OWN vocabulary (no claude "max").
        self.assertEqual(off["codex_seat_efforts"],
                         ["minimal", "low", "medium", "high", "xhigh"])
        self.assertNotIn("max", off["codex_seat_efforts"])
        on = self._ctx({"deepseek": {}, "gemini": {}}, codex_available=True)
        self.assertTrue(on["codex_cli_available"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
