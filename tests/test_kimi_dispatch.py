"""K4 tests — the dispatch-form kimi SEAT picker parsing (app._parse_fusion_panel
type=="kimi" → kind:"kimi_cli") + _kimi_seat_models. Mirrors the codex parse tests.
kimi-code has NO reasoning effort, so a kimi seat is {model[,lens]} — never an effort.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orchestrator import app
from orchestrator.lib import config

KIMI_ENGINE = {"model": "kimi-code/k3",
               "models": ["kimi-code/k3", "kimi-code/kimi-for-coding"],
               "seats": [{"kind": "kimi_cli", "model": "kimi-code/k3"}]}


class TestKimiSeatModels(unittest.TestCase):
    def test_sourced_from_engine(self):
        with mock.patch.object(config, "kimi_engine", return_value=KIMI_ENGINE):
            self.assertEqual(app._kimi_seat_models(),
                             {"kimi-code/k3", "kimi-code/kimi-for-coding"})


class TestParseFusionPanelKimi(unittest.TestCase):
    KM = {"kimi-code/k3", "kimi-code/kimi-for-coding"}

    def _parse(self, seats):
        return app._parse_fusion_panel(json.dumps(seats), "", {}, set(), set(), self.KM)

    def test_kimi_seat_becomes_kimi_cli(self):
        self.assertEqual(self._parse([{"type": "kimi", "model": "kimi-code/k3"}]),
                         [{"kind": "kimi_cli", "model": "kimi-code/k3"}])

    def test_kimi_seat_carries_lens(self):
        self.assertEqual(
            self._parse([{"type": "kimi", "model": "kimi-code/k3", "lens": "risks"}]),
            [{"kind": "kimi_cli", "model": "kimi-code/k3", "lens": "risks"}])

    def test_unknown_or_blank_alias_dropped(self):
        # No silent downgrade: a bogus/blank alias drops the seat entirely.
        self.assertEqual(self._parse([{"type": "kimi", "model": "bogus"}]), [])
        self.assertEqual(self._parse([{"type": "kimi", "model": ""}]), [])

    def test_kimi_seat_never_carries_effort(self):
        # Even if a crafted request smuggles an effort, kimi seats have no effort field.
        p = self._parse([{"type": "kimi", "model": "kimi-code/k3", "effort": "high"}])
        self.assertEqual(p, [{"kind": "kimi_cli", "model": "kimi-code/k3"}])
        self.assertNotIn("effort", p[0])

    def test_mixed_panel_parses_all_kinds(self):
        active = {"gemini": {"model": "gemini-2.5-flash"}}
        seats = [{"type": "claude", "model": "opus", "effort": "high"},
                 {"type": "codex", "model": "gpt-5.5"},
                 {"type": "kimi", "model": "kimi-code/k3"},
                 {"type": "provider", "name": "gemini"}]
        p = app._parse_fusion_panel(json.dumps(seats), "", active,
                                    {"gpt-5.5"}, {"high"}, self.KM)
        kinds = [s.get("kind") if isinstance(s, dict) else s for s in p]
        self.assertIn("claude_cli", kinds)
        self.assertIn("codex_cli", kinds)
        self.assertIn("kimi_cli", kinds)
        self.assertIn("gemini", kinds)          # provider seat is a bare name


if __name__ == "__main__":
    unittest.main(verbosity=2)
