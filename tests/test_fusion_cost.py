"""F5 tests — fusion surface + cost accounting.

Covers:
  - the additive DB migration (cost_usd + fused on dispatches; cost_usd on
    outcomes), incl. a LEGACY old-shape DB upgraded in place + idempotency;
  - set_dispatch_cost → the cost flowing onto the outcome row for EVERY terminal
    path (complete / kill / pause / orphan / failed_to_spawn);
  - rewriter threading the fusion panel breakdown (run.raw['panel']) into
    RewriteResult;
  - app._fusion_panel_breakdown trimming seats for persistence.

NO NETWORK. The DB is pointed at a tempdir (mirrors test_e2e); the rewriter's one
brain call is mocked.

Usage:
    python -m unittest tests.test_fusion_cost -v
"""

import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _setup_isolated_home():
    tmp = Path(tempfile.mkdtemp(prefix="orch_cost_test_"))
    import orchestrator.lib.db as db_mod
    db_mod.DATA_DIR = tmp
    db_mod.DB_PATH = tmp / "orchestrator.db"
    db_mod.TRANSCRIPTS_DIR = tmp / "transcripts"
    return tmp


TMP_HOME = _setup_isolated_home()

from orchestrator.lib import db, rewriter, claude_runner          # noqa: E402
from orchestrator.lib.claude_runner import ClaudeRun              # noqa: E402
from orchestrator import app as app_module                        # noqa: E402

db.init_db()


def teardown_module(module):
    shutil.rmtree(TMP_HOME, ignore_errors=True)


def _cols(c, table):
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}


def _new_dispatch():
    """A pending dispatch under a throwaway project. Returns its id."""
    td = Path(tempfile.mkdtemp(prefix="orch_cost_proj_"))
    proj = db.add_project(str(td))
    return db.create_dispatch(proj["id"], "do a thing"), td


def _outcome_cost(dispatch_id):
    with db.conn() as c:
        row = c.execute("SELECT cost_usd FROM outcomes WHERE dispatch_id = ?",
                        (dispatch_id,)).fetchone()
    return row["cost_usd"] if row else None


# ───────────────────────────── migration ───────────────────────────────────

class TestMigration(unittest.TestCase):
    def test_fresh_db_has_new_columns(self):
        with db.conn() as c:
            self.assertIn("cost_usd", _cols(c, "dispatches"))
            self.assertIn("fused", _cols(c, "dispatches"))
            self.assertIn("cost_usd", _cols(c, "outcomes"))

    def test_init_db_is_idempotent(self):
        db.init_db()      # second run must not raise (ALTERs are guarded)
        db.init_db()
        with db.conn() as c:
            self.assertIn("fused", _cols(c, "dispatches"))

    def test_legacy_db_upgraded_in_place_preserving_rows(self):
        """An old-shape DB (no cost columns) gains them without losing data."""
        f = Path(tempfile.mkdtemp(prefix="orch_legacy_")) / "old.db"
        c = sqlite3.connect(str(f))
        c.execute("CREATE TABLE dispatches (id INTEGER PRIMARY KEY, user_task TEXT, "
                  "wall_clock_cap_s INTEGER)")
        c.execute("CREATE TABLE outcomes (dispatch_id INTEGER PRIMARY KEY, outcome TEXT, "
                  "reason TEXT, duration_s INTEGER)")
        c.execute("INSERT INTO dispatches(id, user_task, wall_clock_cap_s) VALUES (7, 'old', 60)")
        c.execute("INSERT INTO outcomes(dispatch_id, outcome) VALUES (7, 'completed')")
        c.commit()

        db._migrate(c)        # the function under test
        c.commit()

        self.assertIn("cost_usd", _cols(c, "dispatches"))
        self.assertIn("fused", _cols(c, "dispatches"))
        self.assertIn("cost_usd", _cols(c, "outcomes"))
        # existing rows survive; new columns backfill to 0
        row = c.execute("SELECT user_task, cost_usd, fused FROM dispatches WHERE id = 7").fetchone()
        self.assertEqual(row[0], "old")
        self.assertEqual(row[1], 0)
        self.assertEqual(row[2], 0)
        db._migrate(c)        # idempotent — second run is a no-op, no raise
        c.close()
        shutil.rmtree(f.parent, ignore_errors=True)


# ───────────────────── cost flows onto every outcome ───────────────────────

class TestCostFlowsToOutcome(unittest.TestCase):
    def test_complete_dispatch_copies_cost(self):
        did, td = _new_dispatch()
        try:
            db.set_dispatch_cost(did, 0.0123, fused=True)
            db.complete_dispatch(did, "sess", None, "ok", outcome="completed")
            self.assertAlmostEqual(_outcome_cost(did), 0.0123, places=6)
            d = db.get_dispatch(did)
            self.assertEqual(d["fused"], 1)
            self.assertAlmostEqual(d["cost_usd"], 0.0123, places=6)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_failed_to_spawn_copies_cost(self):
        did, td = _new_dispatch()
        try:
            db.set_dispatch_cost(did, 0.005, fused=True)
            db.mark_failed_to_spawn(did, "rewrite failed: bad json")
            self.assertAlmostEqual(_outcome_cost(did), 0.005, places=6)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_kill_copies_cost(self):
        did, td = _new_dispatch()
        try:
            db.set_dispatch_cost(did, 0.002)
            db.kill_dispatch_record(did, "manual")
            self.assertAlmostEqual(_outcome_cost(did), 0.002, places=6)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_pause_copies_cost(self):
        did, td = _new_dispatch()
        try:
            db.set_dispatch_cost(did, 0.003)
            db.mark_paused(did, "wall clock")
            self.assertAlmostEqual(_outcome_cost(did), 0.003, places=6)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_orphan_copies_cost(self):
        did, td = _new_dispatch()
        try:
            db.set_dispatch_cost(did, 0.004)
            db.mark_orphaned(did)
            self.assertAlmostEqual(_outcome_cost(did), 0.004, places=6)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_default_cost_is_zero(self):
        did, td = _new_dispatch()
        try:
            db.complete_dispatch(did, None, None, "ok")
            self.assertEqual(_outcome_cost(did), 0)     # non-fusion send → $0
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_set_dispatch_cost_never_raises_on_bad_id(self):
        db.set_dispatch_cost(999999, 0.01)              # no such dispatch — silent


# ─────────────── rewriter threads fusion panel into RewriteResult ───────────

class TestRewriterCarriesPanel(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_rw_cost_"))
        (self.tmp / "README.md").write_text("# demo\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fused_run_populates_fusion_fields(self):
        seats = [{"name": "opus-high", "model": "opus", "text": "A", "cost": 0.0,
                  "subscription": True, "ok": True},
                 {"name": "gemini-lite", "model": "gemini-3.1-flash-lite", "text": "B",
                  "cost": 0.0009, "prompt_tokens": 1000, "completion_tokens": 200, "ok": True}]
        fused = ClaudeRun(ok=True, text='{"rewritten_prompt":"R"}',
                          parsed_json={"rewritten_prompt": "R"}, cost_usd=0.0009, model="opus",
                          raw={"panel": seats, "preset": "hybrid",
                               "seats": ["opus-high (cli)", "gemini-lite"]})
        with mock.patch.object(claude_runner, "run_brain_json", return_value=fused):
            r = rewriter.rewrite("task", str(self.tmp), fusion=True)
        self.assertTrue(r.ok)
        self.assertEqual(r.fusion_preset, "hybrid")
        self.assertEqual(r.fusion_seats, ["opus-high (cli)", "gemini-lite"])
        self.assertEqual([s["name"] for s in r.fusion_panel], ["opus-high", "gemini-lite"])

    def test_non_fusion_run_leaves_fields_empty(self):
        # raw is the plain claude envelope (no 'panel' key) → not a fused rewrite.
        plain = ClaudeRun(ok=True, text='{"rewritten_prompt":"R"}',
                          parsed_json={"rewritten_prompt": "R"},
                          raw={"result": "...", "total_cost_usd": 0.0})
        with mock.patch.object(claude_runner, "run_brain_json", return_value=plain):
            r = rewriter.rewrite("task", str(self.tmp))
        self.assertEqual(r.fusion_panel, [])
        self.assertEqual(r.fusion_preset, "")


# ─────────────────── app._fusion_panel_breakdown trimming ───────────────────

class TestPanelBreakdown(unittest.TestCase):
    def test_trims_ok_failed_and_subscription_seats(self):
        result = SimpleNamespace(fusion_panel=[
            {"name": "opus-high", "model": "opus", "ok": True, "cost": 0.0,
             "subscription": True, "text": "x" * 1000},
            {"name": "gemini", "model": "g", "ok": True, "cost": 0.0009,
             "prompt_tokens": 10, "completion_tokens": 5, "text": "short answer"},
            {"name": "glm", "ok": False, "error": "boom" * 400},
        ])
        out = app_module._fusion_panel_breakdown(result)
        self.assertEqual(len(out), 3)
        opus, gem, glm = out
        # subscription seat: $0 marker, preview bounded to 600 chars
        self.assertTrue(opus["subscription"])
        self.assertEqual(opus["cost"], 0.0)
        self.assertEqual(len(opus["preview"]), 600)
        self.assertNotIn("error", opus)
        # ok seat: preview kept, error absent
        self.assertEqual(gem["preview"], "short answer")
        self.assertEqual(gem["prompt_tokens"], 10)
        # failed seat: error preview bounded, no preview key
        self.assertFalse(glm["ok"])
        self.assertLessEqual(len(glm["error"]), 600)
        self.assertNotIn("preview", glm)

    def test_empty_for_non_fused(self):
        self.assertEqual(app_module._fusion_panel_breakdown(SimpleNamespace(fusion_panel=[])), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
