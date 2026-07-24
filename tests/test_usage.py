"""U1 tests — usage schema + collector taps + idempotent backfill (USAGE_PLAN.md).

Fully OFFLINE — no real CLIs, no iTerm2, no network, and NEVER the real
~/.orchestrator DB: every writing test points db at a tempdir AND arms the
collector explicitly (it is disarmed by default precisely so the rest of the
suite's mock-heavy runner tests stay side-effect free), then disarms on
teardown.

Covers:
  - the two U1 tables exist with the spec'd columns (+ the `source` backfill
    idempotency key) and the partial-UNIQUE dedup actually dedups.
  - config.usage_engines() derives from the SEEDS (+ config.json customs) and
    contains the five §1 dashboard engines — the drift guard.
  - db.record_usage: fail-soft, disarmed no-op, state bookkeeping (monotonic
    last_ok_at / newest last_error), raw_error bounding, source dedup.
  - the claude_runner taps: engine/role/tokens per funnel (claude/codex/kimi
    × brain/judge/seat), failures recorded with the DEGRADED error string
    (U2's :877 detail fix is deliberately absent), provider-seat taps on both
    the in-process and tab paths ('glm#2' → engine glm), executor taps.
  - the backfill: panel_breakdown stage rows → usage_events (real payload
    shapes incl. failed seats, CLI 0/0 → NULL tokens, duplicate seat names,
    malformed rows, the fusion_ok text-only false positive), kimi-log 403s →
    limit events (UTC continuation-line stamping), the live/backfill cutoff,
    engine_limit_state recompute (kimi limited_since set + cleared), and
    IDEMPOTENCY — a second run inserts nothing and leaves state identical.

Usage:
    python -m unittest tests.test_usage -v
"""

import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orchestrator.lib import claude_runner, config, db, spawn, usage


class _TempDb(unittest.TestCase):
    """Point db at a tempdir and ARM the collector; restore + disarm on
    teardown so later suites (which exercise the tapped funnels against the
    real module paths) can never write usage rows."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_usage_"))
        self._orig = (db.DATA_DIR, db.DB_PATH, db.TRANSCRIPTS_DIR)
        db.DATA_DIR = self.tmp
        db.DB_PATH = self.tmp / "orchestrator.db"
        db.TRANSCRIPTS_DIR = self.tmp / "transcripts"
        self._orig_cfg = config.CONFIG_PATH
        config.CONFIG_PATH = self.tmp / "config.json"   # hermetic: seeds only
        db.init_db()
        db.enable_usage_collection(True)

    def tearDown(self):
        db.enable_usage_collection(False)
        db.DATA_DIR, db.DB_PATH, db.TRANSCRIPTS_DIR = self._orig
        config.CONFIG_PATH = self._orig_cfg
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers ---------------------------------------------------------------
    def rows(self, sql, args=()):
        with db.conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def usage_rows(self):
        return self.rows("SELECT * FROM usage_events ORDER BY id")

    def state(self, engine):
        r = self.rows("SELECT * FROM engine_limit_state WHERE engine = ?", (engine,))
        return r[0] if r else None


# ───────────────────────────── schema ───────────────────────────────────────

class TestUsageSchema(_TempDb):
    def test_usage_events_columns_match_spec(self):
        cols = {r["name"] for r in self.rows("PRAGMA table_info(usage_events)")}
        self.assertEqual(cols, {
            "id", "ts", "engine", "model", "role", "dispatch_id", "calls",
            "prompt_tokens", "completion_tokens", "ok", "error_class",
            "raw_error", "source",   # source = the backfill idempotency key
        })

    def test_engine_limit_state_columns_match_spec(self):
        cols = {r["name"] for r in self.rows("PRAGMA table_info(engine_limit_state)")}
        self.assertEqual(cols, {
            "engine", "limited_since", "reset_hint", "last_ok_at", "last_error",
        })

    def test_init_db_idempotent_with_new_tables(self):
        db.record_usage("glm", ok=True)
        db.init_db()                                    # re-run must not clobber
        self.assertEqual(len(self.usage_rows()), 1)

    def test_source_dedup_and_null_source_not_deduped(self):
        self.assertTrue(db.record_usage("glm", ok=True))          # live (NULL)
        self.assertTrue(db.record_usage("glm", ok=True))          # live (NULL)
        self.assertTrue(db.record_usage("glm", ok=True, source="pb:1:0"))
        self.assertFalse(db.record_usage("glm", ok=True, source="pb:1:0"))
        self.assertEqual(len(self.usage_rows()), 3)


# ───────────────────────── engine enumeration ───────────────────────────────

class TestUsageEngines(unittest.TestCase):
    """Seed-derived engine list (drift-guard convention: no literals in app.py)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_usage_cfg_"))
        self._orig = config.CONFIG_PATH
        config.CONFIG_PATH = self.tmp / "config.json"

    def tearDown(self):
        config.CONFIG_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dashboard_engines_present(self):
        engines = config.usage_engines()
        for e in ("claude", "codex", "kimi", "glm", "gemini"):   # USAGE_PLAN §1
            self.assertIn(e, engines)

    def test_derived_from_seeds_no_dupes_cli_first(self):
        engines = config.usage_engines()
        self.assertEqual(engines[:3], list(config.USAGE_CLI_ENGINES))
        for name in config.FUSION_PROVIDERS_SEED:
            self.assertIn(name, engines)
        self.assertEqual(len(engines), len(set(engines)))

    def test_config_json_custom_provider_included(self):
        config.CONFIG_PATH.write_text(json.dumps(
            {"fusion": {"providers": {"foolab": {"script": "providers/foolab.py",
                                                 "key_env": "FOO", "model": "foo-1"}}}}),
            encoding="utf-8")
        self.assertIn("foolab", config.usage_engines())

    def test_app_wiring_uses_config_not_literals(self):
        src = (REPO / "orchestrator" / "app.py").read_text(encoding="utf-8")
        self.assertIn("db.ensure_engine_limit_rows(config.usage_engines())", src)
        self.assertIn("db.enable_usage_collection()", src)
        # the executor pollers meter all three finalize paths per engine
        self.assertEqual(src.count("claude_runner.record_codex_executor_usage("), 3)
        self.assertEqual(src.count("claude_runner.record_kimi_executor_usage("), 3)
        # U3: the /usage route renders usage_page_data — no engine literals
        self.assertIn('@app.get("/usage"', src)
        self.assertIn("usage_mod.usage_page_data()", src)
        base = (REPO / "orchestrator" / "templates" / "base.html").read_text(encoding="utf-8")
        self.assertIn('href="/usage"', base)   # the nav link


# ───────────────────────────── record_usage ─────────────────────────────────

class TestRecordUsage(_TempDb):
    def test_disarmed_is_a_no_op(self):
        db.enable_usage_collection(False)
        self.assertFalse(db.record_usage("glm", ok=True))
        self.assertEqual(self.usage_rows(), [])

    def test_ok_row_fields_and_state_touch(self):
        self.assertTrue(db.record_usage(
            "glm", model="glm-4.6", role="seat", dispatch_id=7, ok=True,
            prompt_tokens=11, completion_tokens=5, ts=1234))
        (row,) = self.usage_rows()
        self.assertEqual(
            (row["engine"], row["model"], row["role"], row["dispatch_id"],
             row["calls"], row["prompt_tokens"], row["completion_tokens"],
             row["ok"], row["error_class"], row["raw_error"], row["source"]),
            ("glm", "glm-4.6", "seat", 7, 1, 11, 5, 1, None, None, None))
        st = self.state("glm")
        self.assertEqual(st["last_ok_at"], 1234)
        self.assertIsNone(st["limited_since"])
        self.assertIsNone(st["last_error"])

    def test_error_row_sets_last_error_not_last_ok(self):
        db.record_usage("kimi", ok=False, raw_error="kimi exit 1", ts=50)
        (row,) = self.usage_rows()
        self.assertEqual((row["ok"], row["raw_error"], row["error_class"]),
                         (0, "kimi exit 1", None))       # class is U2's, stays NULL
        st = self.state("kimi")
        self.assertEqual(st["last_error"], "kimi exit 1")
        self.assertIsNone(st["last_ok_at"])

    def test_last_ok_at_is_monotonic(self):
        db.record_usage("codex", ok=True, ts=200)
        db.record_usage("codex", ok=True, ts=100)        # out-of-order backfill
        self.assertEqual(self.state("codex")["last_ok_at"], 200)

    def test_raw_error_bounded(self):
        db.record_usage("glm", ok=False, raw_error="x" * 5000)
        (row,) = self.usage_rows()
        self.assertEqual(len(row["raw_error"]), db._RAW_ERROR_MAX)

    def test_never_raises(self):
        with mock.patch.object(db, "conn", side_effect=RuntimeError("boom")):
            self.assertFalse(db.record_usage("glm", ok=True))   # swallowed

    def test_ensure_engine_limit_rows_idempotent_and_non_clobbering(self):
        db.ensure_engine_limit_rows(["claude", "codex"])
        db.touch_engine_state("claude", last_error="err")
        db.ensure_engine_limit_rows(["claude", "codex", "glm"])
        rows = self.rows("SELECT engine FROM engine_limit_state ORDER BY engine")
        self.assertEqual([r["engine"] for r in rows], ["claude", "codex", "glm"])
        self.assertEqual(self.state("claude")["last_error"], "err")

    def test_set_engine_limited_sets_and_clears(self):
        db.set_engine_limited("kimi", 999, reset_hint="next cycle")
        st = self.state("kimi")
        self.assertEqual((st["limited_since"], st["reset_hint"]), (999, "next cycle"))
        db.set_engine_limited("kimi", None)
        st = self.state("kimi")
        self.assertEqual((st["limited_since"], st["reset_hint"]), (None, None))


# ─────────────────────────── runner taps ────────────────────────────────────

def _proc(stdout="", returncode=0, stderr=""):
    m = mock.Mock()
    m.stdout, m.returncode, m.stderr = stdout, returncode, stderr
    return m


class TestRunnerTaps(_TempDb):
    """The run_*_json funnels record every finished call — ok AND failed —
    with engine/role/tokens. All via the headless fallback (iTerm2 mocked
    absent) so no tab ever spawns."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(spawn, "iterm2_installed", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def test_claude_ok_records_brain_role_and_tokens(self):
        envelope = {"result": "hi", "total_cost_usd": 0.01, "duration_ms": 1200,
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 11, "output_tokens": 7}}
        with mock.patch.object(claude_runner.subprocess, "run",
                               return_value=_proc(stdout=json.dumps(envelope))):
            run = claude_runner.run_claude_json("p", str(self.tmp),
                                                model="opus", label="rewriter")
        self.assertTrue(run.ok)
        (row,) = self.usage_rows()
        self.assertEqual((row["engine"], row["role"], row["model"],
                          row["prompt_tokens"], row["completion_tokens"],
                          row["ok"], row["dispatch_id"]),
                         ("claude", "brain", "claude-opus-4-8", 11, 7, 1, None))
        self.assertEqual(self.state("claude")["last_ok_at"], row["ts"])

    def test_claude_labels_map_to_seat_and_judge_roles(self):
        envelope = {"result": "x", "duration_ms": 0}
        with mock.patch.object(claude_runner.subprocess, "run",
                               return_value=_proc(stdout=json.dumps(envelope))):
            claude_runner.run_claude_json("p", str(self.tmp), model="opus",
                                          label="fusion-seat:opus-high+risks")
            claude_runner.run_claude_json("p", str(self.tmp), model="opus",
                                          label="fusion-verify")
        roles = [r["role"] for r in self.usage_rows()]
        self.assertEqual(roles, ["seat", "judge"])

    def test_claude_failure_records_degraded_error_as_is(self):
        with mock.patch.object(claude_runner.subprocess, "run",
                               return_value=_proc(returncode=1, stderr="boom")):
            run = claude_runner.run_claude_json("p", str(self.tmp),
                                                model="opus", label="rewriter")
        self.assertFalse(run.ok)
        (row,) = self.usage_rows()
        self.assertEqual(row["ok"], 0)
        self.assertEqual(row["raw_error"], "claude exit 1: boom")   # verbatim, no U2 enrich
        self.assertEqual(self.state("claude")["last_error"], "claude exit 1: boom")

    def test_codex_ok_records_tokens_from_turn_completed(self):
        lines = [
            {"type": "thread.started", "thread_id": "th_1"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
            {"type": "turn.completed",
             "usage": {"input_tokens": 100, "cached_input_tokens": 20,
                       "output_tokens": 30}},
        ]
        out = "\n".join(json.dumps(l) for l in lines)
        with mock.patch.object(claude_runner.subprocess, "run",
                               return_value=_proc(stdout=out)):
            run = claude_runner.run_codex_json("p", str(self.tmp),
                                               model="gpt-5.6-sol",
                                               label="fusion-judge")
        self.assertTrue(run.ok)
        (row,) = self.usage_rows()
        self.assertEqual((row["engine"], row["role"], row["model"],
                          row["prompt_tokens"], row["completion_tokens"]),
                         ("codex", "judge", "gpt-5.6-sol", 100, 30))

    def test_kimi_ok_is_calls_only_metering(self):
        out = "\n".join([
            json.dumps({"role": "assistant", "content": "hey"}),
            json.dumps({"role": "meta", "type": "session.resume_hint",
                        "session_id": "session_x"}),
        ])
        with mock.patch.object(claude_runner.subprocess, "run",
                               return_value=_proc(stdout=out)):
            run = claude_runner.run_kimi_json("p", str(self.tmp),
                                              model="kimi-code/k3",
                                              label="fusion-seat:kimi-code/k3")
        self.assertTrue(run.ok)
        (row,) = self.usage_rows()
        self.assertEqual((row["engine"], row["role"], row["calls"],
                          row["prompt_tokens"], row["completion_tokens"]),
                         ("kimi", "seat", 1, None, None))

    def test_kimi_failure_records_degraded_exit_string(self):
        with mock.patch.object(claude_runner.subprocess, "run",
                               return_value=_proc(returncode=1, stderr="")):
            run = claude_runner.run_kimi_json("p", str(self.tmp),
                                              model="kimi-code/k3", label="kimi")
        self.assertFalse(run.ok)
        (row,) = self.usage_rows()
        self.assertEqual((row["engine"], row["ok"]), ("kimi", 0))
        self.assertTrue(row["raw_error"].startswith("kimi exit 1"))

    def test_provider_seat_in_process_panel_records_base_engine(self):
        prov = {"script": "providers/glm.py", "model": "glm-4.6",
                "price_in": 0, "price_out": 0}
        out = json.dumps({"ok": True, "text": "hi", "model": "glm-4.6",
                          "prompt_tokens": 5, "completion_tokens": 3})
        with mock.patch.object(claude_runner.subprocess, "run",
                               return_value=_proc(stdout=out)):
            answers = claude_runner._run_panel("p", ["glm#2"], {"glm#2": prov}, 5)
        self.assertTrue(answers[0]["ok"])
        (row,) = self.usage_rows()
        self.assertEqual((row["engine"], row["role"], row["model"],
                          row["prompt_tokens"], row["completion_tokens"]),
                         ("glm", "seat", "glm-4.6", 5, 3))   # '#2' is seat identity

    def test_provider_seats_tab_path_records_ok_and_error(self):
        raw = [
            {"name": "glm", "ok": True, "model": "glm-4.6", "text": "t",
             "prompt_tokens": 9, "completion_tokens": 4},
            {"name": "qwen", "ok": False, "error": "HTTP Error 429: Too Many Requests"},
        ]
        providers = {"glm": {"price_in": 0, "price_out": 0},
                     "qwen": {"price_in": 0, "price_out": 0}}
        claude_runner._price_tab_answers(raw, providers)
        rows = self.usage_rows()
        self.assertEqual([(r["engine"], r["ok"]) for r in rows],
                         [("glm", 1), ("qwen", 0)])
        self.assertEqual(rows[0]["prompt_tokens"], 9)
        self.assertIn("429", rows[1]["raw_error"])

    def test_codex_executor_tap_reads_sidecar_tokens(self):
        sidecar = self.tmp / "42.jsonl"
        sidecar.write_text("\n".join([
            json.dumps({"type": "item.completed",
                        "item": {"type": "agent_message", "text": "done"}}),
            json.dumps({"type": "turn.completed",
                        "usage": {"input_tokens": 7, "output_tokens": 2}}),
        ]), encoding="utf-8")
        claude_runner.record_codex_executor_usage(42, ok=True, jsonl_path=sidecar)
        claude_runner.record_codex_executor_usage(43, ok=False,
                                                  raw_error="codex exit 2")
        rows = self.usage_rows()
        self.assertEqual((rows[0]["engine"], rows[0]["role"], rows[0]["dispatch_id"],
                          rows[0]["prompt_tokens"], rows[0]["completion_tokens"]),
                         ("codex", "executor", 42, 7, 2))
        self.assertEqual((rows[1]["dispatch_id"], rows[1]["ok"],
                          rows[1]["raw_error"]), (43, 0, "codex exit 2"))

    def test_kimi_executor_tap_is_calls_only(self):
        claude_runner.record_kimi_executor_usage(361, ok=False, raw_error="kimi exit 1")
        (row,) = self.usage_rows()
        self.assertEqual((row["engine"], row["role"], row["dispatch_id"],
                          row["ok"], row["raw_error"], row["prompt_tokens"]),
                         ("kimi", "executor", 361, 0, "kimi exit 1", None))


# ─────────────────────────── kimi log parsing ───────────────────────────────

_KIMI_403 = ("  Error: provider.api_error: 403 You've reached your usage limit "
             "for this billing cycle. Your quota will be refreshed in the next "
             "cycle. To continue now, purchase extra usage or upgrade your plan: "
             "https://www.kimi.com/code/#pricing")


def _kimi_log_text():
    return "\n".join([
        "  Error: provider.api_error: 403 orphan before any timestamp",  # skipped
        "2026-07-20T20:08:29.357Z INFO  experimental flags enabled  flags=[]",
        "2026-07-20T21:49:11.832Z ERROR startup failed  operation=\"run prompt\"",
        _KIMI_403,
        "    at /Users/runner/work/kimi-code/main.cjs:432482:13",
        "2026-07-23T14:02:07.101Z ERROR startup failed  operation=\"run prompt\"",
        _KIMI_403,
        "",
    ])


class TestKimiLogParse(unittest.TestCase):
    def test_403s_stamped_from_preceding_utc_timestamp(self):
        tmp = Path(tempfile.mkdtemp(prefix="orch_usage_log_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        log = tmp / "kimi-code.log"
        log.write_text(_kimi_log_text(), encoding="utf-8")
        hits = usage.parse_kimi_log_403s(str(log), config.KIMI_LIMIT_SIGNAL)
        self.assertEqual(len(hits), 2)          # the orphan line was skipped
        # 2026-07-20T21:49:11Z / 2026-07-23T14:02:07Z as UTC epochs
        self.assertEqual(hits[0][0], 1784584151)
        self.assertEqual(hits[1][0], 1784815327)
        self.assertEqual(hits[0][1], "2026-07-20T21:49:11.832Z")
        self.assertIn("usage limit for this billing cycle", hits[0][2])

    def test_absent_log_returns_empty(self):
        self.assertEqual(usage.parse_kimi_log_403s("/nonexistent/x.log", "sig"), [])


# ───────────────────────────── backfill ─────────────────────────────────────

class TestBackfill(_TempDb):
    """Fixtures mirror the REAL live payloads (verified against the production
    DB on 2026-07-23): rewrite_ok / rewrite_skipped stage rows with mixed
    provider/claude/codex/kimi seats, failed seats, duplicate seat names, and
    the traps — a fusion_ok row whose analysis TEXT mentions panel_breakdown,
    a corrupt payload, and a non-list panel_breakdown."""

    def _event(self, dispatch_id, ts, payload_json):
        with db.conn() as c:
            c.execute("INSERT INTO dispatch_events(dispatch_id, ts, kind, payload_json) "
                      "VALUES (?,?,?,?)", (dispatch_id, ts, "stage", payload_json))

    def setUp(self):
        super().setUp()
        proj = db.add_project(str(self.tmp))
        self.d1 = db.create_dispatch(proj["id"], "task one")
        self.d2 = db.create_dispatch(proj["id"], "task two")
        breakdown_a = [
            {"name": "glm", "model": "glm-4.6", "ok": True, "cost": 0.0,
             "prompt_tokens": 100, "completion_tokens": 50,
             "subscription": False, "lens": "risks", "preview": "…"},
            {"name": "glm#2", "model": "glm-4.6", "ok": True, "cost": 0.0,
             "prompt_tokens": 7, "completion_tokens": 3,
             "subscription": False, "lens": "", "preview": "…"},
            {"name": "gemini", "model": "", "ok": False, "cost": 0.0,
             "prompt_tokens": 0, "completion_tokens": 0, "subscription": False,
             "lens": "risks", "error": "GEMINI_API_KEY not set (env or config.json)"},
            {"name": "opus-high", "model": "claude-opus-4-8", "ok": True,
             "cost": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
             "subscription": True, "lens": "adversary", "preview": "…"},
            {"name": "opus-high", "model": "claude-opus-4-8", "ok": True,
             "cost": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
             "subscription": True, "lens": "risks", "preview": "…"},   # duplicate name
            {"name": "gpt-5.6-sol", "model": "gpt-5.6-sol", "ok": True,
             "cost": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
             "subscription": True, "lens": "", "preview": "…"},
            {"name": "kimi-code/k3", "model": "kimi-code/k3", "ok": True,
             "cost": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
             "subscription": True, "lens": "", "preview": "…"},
            "not-a-dict",                                   # malformed seat
        ]
        self._event(self.d1, 1000, json.dumps(
            {"stage": "rewrite_ok", "fused": True, "panel_breakdown": breakdown_a}))
        self._event(self.d2, 1200, json.dumps(
            {"stage": "rewrite_skipped", "reason": "bad json",
             "panel_breakdown": [
                 {"name": "xai", "model": "", "ok": False, "cost": 0.0,
                  "prompt_tokens": 0, "completion_tokens": 0,
                  "subscription": False, "lens": "",
                  "error": "HTTP Error 429: Too Many Requests"}]}))
        # traps: key only inside TEXT / corrupt json / non-list value
        self._event(self.d2, 1300, json.dumps(
            {"stage": "fusion_ok", "analysis": "the `panel_breakdown` rows say…"}))
        self._event(self.d2, 1301, '{"stage": "broken", "panel_breakdown": [oops')
        self._event(self.d2, 1302, json.dumps(
            {"stage": "weird", "panel_breakdown": {"not": "a list"}}))
        self.log = self.tmp / "kimi-code.log"
        self.log.write_text(_kimi_log_text(), encoding="utf-8")
        # 403 epochs from the fixture (fall between the panel events and 'now')
        self.t403_a, self.t403_b = 1784584151, 1784815327

    def _run(self, now_ts=1800000000):
        return usage.backfill(kimi_log_path=str(self.log), now_ts=now_ts)

    def test_backfill_ingests_history_and_recomputes_state(self):
        s = self._run()
        # 7 valid seats in event A + 1 in event B; 1 malformed; 2 kimi 403s
        self.assertEqual(s["pb_seats_inserted"], 8)
        self.assertEqual(s["pb_seats_malformed"], 1)
        self.assertEqual(s["kimi_403s_inserted"], 2)
        rows = self.usage_rows()
        self.assertEqual(len(rows), 10)

        by_engine = {}
        for r in rows:
            by_engine.setdefault(r["engine"], []).append(r)
        # provider seats: base-name engine, real tokens, dispatch stamped
        self.assertEqual(len(by_engine["glm"]), 2)
        self.assertEqual({r["prompt_tokens"] for r in by_engine["glm"]}, {100, 7})
        self.assertTrue(all(r["dispatch_id"] == self.d1 for r in by_engine["glm"]))
        self.assertTrue(all(r["ts"] == 1000 for r in by_engine["glm"]))
        # CLI seats: subscription 0/0 → NULL tokens (unknown, not zero)
        self.assertEqual(len(by_engine["claude"]), 2)     # duplicate names kept
        self.assertTrue(all(r["prompt_tokens"] is None for r in by_engine["claude"]))
        self.assertEqual(by_engine["codex"][0]["model"], "gpt-5.6-sol")
        # failed seats: error verbatim, no tokens
        self.assertEqual(by_engine["gemini"][0]["ok"], 0)
        self.assertIn("GEMINI_API_KEY not set", by_engine["gemini"][0]["raw_error"])
        self.assertIn("429", by_engine["xai"][0]["raw_error"])
        # kimi: 1 ok seat from the panel + 2 backfilled 403 limit events
        kimi = by_engine["kimi"]
        self.assertEqual(len(kimi), 3)
        k403 = [r for r in kimi if r["ok"] == 0]
        self.assertEqual({r["ts"] for r in k403}, {self.t403_a, self.t403_b})
        self.assertTrue(all(r["dispatch_id"] is None for r in k403))
        # U2: the backfill classifies as it inserts — ok rows stay NULL
        self.assertTrue(all(r["error_class"] == "limit" for r in k403))
        self.assertEqual(by_engine["gemini"][0]["error_class"], "config")
        self.assertEqual(by_engine["xai"][0]["error_class"], "rate")
        self.assertTrue(all(r["error_class"] is None for r in rows if r["ok"] == 1))

        # state: kimi LIMITED since the newest 403 (no newer ok call); the
        # classifier (U2) parses the pinned message's reset hint
        st = self.state("kimi")
        self.assertEqual(st["limited_since"], self.t403_b)
        self.assertEqual(st["reset_hint"], "next billing cycle")
        self.assertEqual(st["last_ok_at"], 1000)
        self.assertIn("usage limit for this billing cycle", st["last_error"])
        # glm: ok history, never limited
        st = self.state("glm")
        self.assertEqual(st["last_ok_at"], 1000)
        self.assertIsNone(st["limited_since"])
        # gemini: newest error recorded, no limited state invented
        st = self.state("gemini")
        self.assertIn("GEMINI_API_KEY", st["last_error"])
        self.assertIsNone(st["limited_since"])
        # every configured engine got a row (page renders all engines day one)
        have = {r["engine"] for r in self.rows("SELECT engine FROM engine_limit_state")}
        self.assertTrue(set(config.usage_engines()) <= have)

    def test_backfill_is_idempotent(self):
        first = self._run()
        rows_before = self.usage_rows()
        state_before = self.rows("SELECT * FROM engine_limit_state ORDER BY engine")
        second = self._run()
        self.assertEqual(second["pb_seats_inserted"], 0)
        self.assertEqual(second["pb_seats_dup"], first["pb_seats_inserted"])
        self.assertEqual(second["kimi_403s_inserted"], 0)
        self.assertEqual(self.usage_rows(), rows_before)
        self.assertEqual(self.rows("SELECT * FROM engine_limit_state ORDER BY engine"),
                         state_before)

    def test_live_rows_bound_the_backfill_window(self):
        # a live-collected row (source NULL) at t=1100 becomes the cutoff:
        # older history (t=1000) is ingested, newer events are live-covered.
        db.record_usage("glm", ok=True, ts=1100)
        self._event(self.d2, 1150, json.dumps(
            {"stage": "rewrite_ok", "panel_breakdown": [
                {"name": "glm", "model": "glm-4.6", "ok": True, "cost": 0,
                 "prompt_tokens": 1, "completion_tokens": 1,
                 "subscription": False, "lens": "", "preview": "…"}]}))
        s = self._run()
        self.assertEqual(s["cutoff"], 1100)
        self.assertEqual(s["pb_seats_inserted"], 7)       # event A only (ts 1000)
        self.assertEqual(s["pb_after_cutoff"], 2)         # B (1200) + the 1150 event
        self.assertEqual(s["kimi_403s_inserted"], 0)      # 403s are newer than cutoff
        engines = [r["engine"] for r in self.usage_rows()]
        self.assertEqual(engines.count("glm"), 3)         # 1 live + 2 backfilled

    def test_recompute_clears_kimi_limited_after_newer_ok(self):
        self._run()
        self.assertEqual(self.state("kimi")["limited_since"], self.t403_b)
        db.record_usage("kimi", ok=True, ts=self.t403_b + 500, source="manual:1")
        usage.recompute_engine_state()
        st = self.state("kimi")
        self.assertIsNone(st["limited_since"])
        self.assertEqual(st["last_ok_at"], self.t403_b + 500)


# ─────────────────────── U2: classifier + transitions ───────────────────────

class TestClassifier(unittest.TestCase):
    """config.classify_error against the REAL pinned strings (§3/U0)."""

    def test_kimi_cycle_quota_is_limit_with_hint(self):
        cls, hint = config.classify_error("kimi", _KIMI_403)
        self.assertEqual((cls, hint), ("limit", "next billing cycle"))

    def test_glm_1305_overload_is_rate_and_1113_is_limit(self):
        pinned_429 = ('HTTP Error 429: Too Many Requests {"error":{"code":"1305",'
                      '"message":"The service may be temporarily overloaded, '
                      'please try again later"}}')
        self.assertEqual(config.classify_error("glm", pinned_429)[0], "rate")
        # 1113 (prepaid no-balance) carries a 429 too — the engine-specific
        # CODE rule must win over the generic 429→rate rule
        no_balance = 'HTTP Error 429: Too Many Requests {"error":{"code":"1113"}}'
        self.assertEqual(config.classify_error("glm", no_balance)[0], "limit")

    def test_claude_auth_strings_pinned_by_u0(self):
        self.assertEqual(config.classify_error(
            "claude", "Login expired · Please run /login")[0], "auth")
        self.assertEqual(config.classify_error(
            "claude", "Failed to authenticate: OAuth session expired and could "
                      "not be refreshed")[0], "auth")

    def test_config_infra_generic_and_unmatched(self):
        self.assertEqual(config.classify_error(
            "gemini", "GEMINI_API_KEY not set (env or config.json)")[0], "config")
        self.assertEqual(config.classify_error(
            "kimi", "kimi timed out after 900s")[0], "infra")
        self.assertEqual(config.classify_error(
            "codex", "HTTP Error 429: Too Many Requests")[0], "rate")
        self.assertEqual(config.classify_error("kimi", "kimi exit 1"), (None, None))
        self.assertEqual(config.classify_error("glm", None), (None, None))
        self.assertEqual(config.classify_error("glm", ""), (None, None))

    def test_limit_classes_constant(self):
        self.assertEqual(set(config.USAGE_LIMIT_CLASSES), {"limit", "rate"})


class TestLimitTransitions(_TempDb):
    """db.record_usage LIMITED transitions: hit ⇒ LIMITED since FIRST hit,
    next ok clears, non-limit failures leave the state alone."""

    def test_hit_sets_onset_and_repeat_keeps_it(self):
        db.record_usage("kimi", ok=False, raw_error=_KIMI_403, error_class="limit",
                        limit_hit=True, reset_hint="next billing cycle", ts=100)
        st = self.state("kimi")
        self.assertEqual((st["limited_since"], st["reset_hint"]),
                         (100, "next billing cycle"))
        db.record_usage("kimi", ok=False, raw_error=_KIMI_403, error_class="limit",
                        limit_hit=True, reset_hint="next billing cycle", ts=200)
        self.assertEqual(self.state("kimi")["limited_since"], 100)   # onset kept

    def test_ok_clears_limited(self):
        db.record_usage("kimi", ok=False, raw_error=_KIMI_403, error_class="limit",
                        limit_hit=True, reset_hint="next billing cycle", ts=100)
        db.record_usage("kimi", ok=True, ts=300)
        st = self.state("kimi")
        self.assertIsNone(st["limited_since"])
        self.assertIsNone(st["reset_hint"])
        self.assertEqual(st["last_ok_at"], 300)

    def test_non_limit_failure_leaves_limited_untouched(self):
        db.record_usage("glm", ok=False, raw_error="boom", error_class=None,
                        limit_hit=False, ts=50)
        self.assertIsNone(self.state("glm")["limited_since"])
        db.record_usage("glm", ok=False, raw_error="429 x", error_class="rate",
                        limit_hit=True, ts=60)
        db.record_usage("glm", ok=False, raw_error="boom", error_class=None,
                        limit_hit=False, ts=70)
        self.assertEqual(self.state("glm")["limited_since"], 60)   # untouched by 'boom'

    def test_error_class_stored_on_row(self):
        db.record_usage("glm", ok=False, raw_error="429", error_class="rate",
                        limit_hit=True, ts=10)
        (row,) = self.usage_rows()
        self.assertEqual(row["error_class"], "rate")


class TestExitErrorDetailFix(_TempDb):
    """The :877 detail loss: a failed TAB call now carries the stderr sidecar
    tail, so the panel row / classifier see kimi's real 403, not `kimi exit 1`."""

    def test_exit_error_helper_formats_like_headless(self):
        err = self.tmp / "x.err"
        err.write_text("  Error: provider.api_error: 403 quota gone\n", encoding="utf-8")
        self.assertEqual(claude_runner._exit_error("kimi", 1, err),
                         "kimi exit 1: Error: provider.api_error: 403 quota gone")
        self.assertEqual(claude_runner._exit_error("kimi", 1, self.tmp / "absent.err"),
                         "kimi exit 1")

    def test_kimi_tab_failure_carries_stderr_and_flips_limited(self):
        kd = self.tmp / "kimi"
        kd.mkdir()
        with mock.patch.object(spawn, "iterm2_installed", return_value=True), \
             mock.patch.object(spawn, "KIMI_DIR", kd), \
             mock.patch.object(spawn, "finish_kimi_tab"), \
             mock.patch.object(spawn, "spawn_kimi_tab") as sp:
            def fake_spawn(kimi_id, prompt, cwd, model="", label=""):
                (kd / f"{kimi_id}.err").write_text(_KIMI_403, encoding="utf-8")
                (kd / f"{kimi_id}.done").write_text("1", encoding="utf-8")
            sp.side_effect = fake_spawn
            run = claude_runner.run_kimi_json("p", str(self.tmp),
                                              model="kimi-code/k3",
                                              label="fusion-seat:kimi-code/k3")
        self.assertFalse(run.ok)
        self.assertIn("usage limit for this billing cycle", run.error)
        self.assertTrue(run.error.startswith("kimi exit 1: "))
        (row,) = self.usage_rows()
        self.assertEqual(row["error_class"], "limit")
        st = self.state("kimi")
        self.assertEqual(st["limited_since"], row["ts"])            # flipped live
        self.assertEqual(st["reset_hint"], "next billing cycle")

    def test_runner_scripts_capture_stderr_sidecar(self):
        for content in (spawn.BRAIN_RUN_SH_CONTENT, spawn.CODEX_RUN_SH_CONTENT,
                        spawn.KIMI_RUN_SH_CONTENT):
            self.assertIn('ERR_FILE=', content)
            self.assertIn('2> >(tee "$ERR_FILE" >&2)', content)


# ─────────────────── U2: codex vendor meter (rollout files) ──────────────────

class TestCodexRateLimits(_TempDb):
    def _write_rollout(self, rel, lines):
        p = self.tmp / "sessions" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines), encoding="utf-8")
        return p

    def _point_config_at_tmp_sessions(self):
        config.CONFIG_PATH.write_text(json.dumps(
            {"fusion": {"codex": {"sessions_dir": str(self.tmp / "sessions")}}}),
            encoding="utf-8")

    def test_reads_last_rate_limits_from_newest_rollout(self):
        self._point_config_at_tmp_sessions()
        rl = {"limit_id": "codex", "limit_name": None,
              "primary": {"used_percent": 71.0, "window_minutes": 10080,
                          "resets_at": 1785262626},
              "secondary": None, "plan_type": "plus"}
        stale = dict(rl, primary=dict(rl["primary"], used_percent=40.0))
        self._write_rollout("2026/07/24/rollout-a.jsonl", [
            "not json at all",
            json.dumps({"timestamp": "t1", "type": "event_msg",
                        "payload": {"type": "token_count", "rate_limits": stale}}),
            json.dumps({"timestamp": "t2", "type": "event_msg",
                        "payload": {"type": "token_count", "rate_limits": rl}}),
        ])
        out = usage.codex_rate_limits()
        self.assertEqual(out["used_percent"], 71.0)      # LAST line wins
        self.assertEqual(out["resets_at"], 1785262626)
        self.assertEqual(out["plan_type"], "plus")

    def test_absent_dir_or_no_rate_limits_is_none(self):
        self._point_config_at_tmp_sessions()
        self.assertIsNone(usage.codex_rate_limits())
        self._write_rollout("2026/07/24/rollout-b.jsonl",
                            [json.dumps({"type": "event_msg", "payload": {}})])
        self.assertIsNone(usage.codex_rate_limits())


# ───────────────────────── U3: page data + template ─────────────────────────

class TestUsagePage(_TempDb):
    def _seed(self):
        db.record_usage("glm", model="glm-4.6", role="seat", ok=True,
                        prompt_tokens=1200, completion_tokens=300)
        db.record_usage("kimi", role="seat", ok=False, raw_error=_KIMI_403,
                        error_class="limit", limit_hit=True,
                        reset_hint="next billing cycle")
        db.record_usage("deepseek", role="seat", ok=True)

    def test_page_data_shape(self):
        self._seed()
        with mock.patch.object(usage, "codex_rate_limits", return_value={
                "used_percent": 71.0, "window_minutes": 10080,
                "resets_at": 1785262626, "plan_type": "plus", "captured_at": "t"}):
            data = usage.usage_page_data()
        names = [c["name"] for c in data["primary"]]
        self.assertEqual(names, ["claude", "codex", "kimi", "glm", "gemini"])
        by = {c["name"]: c for c in data["primary"]}
        self.assertEqual(by["kimi"]["badge"]["state"], "limited")
        self.assertIn("next billing cycle", by["kimi"]["badge"]["detail"])
        self.assertEqual(by["glm"]["badge"]["state"], "ok")
        self.assertTrue(by["glm"]["has_tokens"])
        self.assertEqual(by["codex"]["codex_rl"]["pct"], 71.0)
        self.assertEqual(by["codex"]["codex_rl"]["left"], 29.0)
        self.assertEqual(by["codex"]["codex_rl"]["state"], "ok")
        self.assertEqual(len(by["claude"]["spark"]), 14)
        self.assertIn("deepseek", [o["name"] for o in data["others"]])
        self.assertEqual(data["recent_errors"][0]["engine"], "kimi")
        self.assertEqual(data["recent_errors"][0]["cls"], "limit")

    def test_meter_state_thresholds(self):
        for pct, state in ((10, "ok"), (75, "warn"), (95, "bad")):
            with mock.patch.object(usage, "codex_rate_limits", return_value={
                    "used_percent": pct, "window_minutes": 10080,
                    "resets_at": 1, "plan_type": "plus", "captured_at": "t"}):
                data = usage.usage_page_data()
            codex = next(c for c in data["primary"] if c["name"] == "codex")
            self.assertEqual(codex["codex_rl"]["state"], state)

    def test_template_renders_with_real_data(self):
        # No httpx in this venv (TestClient unavailable) — render the Jinja
        # template directly with real page data; the route itself is pinned by
        # TestUsageEngines.test_app_wiring_uses_config_not_literals.
        import jinja2
        self._seed()
        with mock.patch.object(usage, "codex_rate_limits", return_value=None):
            data = usage.usage_page_data()
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(
            str(REPO / "orchestrator" / "templates")))
        html = env.get_template("usage.html").render(request=None, **data)
        for needle in ("engine usage", "LIMITED", "claude", "codex", "kimi",
                       "glm", "gemini", "recent failures", 'hx-get="/usage"'):
            self.assertIn(needle, html)
        self.assertIn("usage limit for this billing cycle", html)   # last error


if __name__ == "__main__":
    unittest.main()
