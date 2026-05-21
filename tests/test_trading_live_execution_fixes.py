import json
import sys
import tempfile
import types
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
TRADING_DIR = PROJECT_DIR / "scripts" / "trading"
if str(TRADING_DIR) not in sys.path:
    sys.path.insert(0, str(TRADING_DIR))

import live_engine as le  # noqa: E402
import live_session_loading as lsl  # noqa: E402
import live_engine_session_io as lesio  # noqa: E402
import candidate_schema_enrichment as cse  # noqa: E402
import ev_policy as ep  # noqa: E402
import polymarket_client as pc  # noqa: E402
import signal_config as sc  # noqa: E402
import signal_pipeline_capture as spc  # noqa: E402
import signal_engine as se  # noqa: E402
from models import LiveBetRecord  # noqa: E402


def _live_bet(**overrides):
    base = {
        "bet_id": "bet_1",
        "placed_at": "2026-04-27T12:00:00Z",
        "game_pk": 1,
        "away_abbrev": "AWY",
        "home_abbrev": "HOM",
        "line": "8.5",
        "side": "over",
        "entry_ask": 0.75,
        "fair_value": 0.90,
        "base_fair_value": 0.88,
        "stage2_run_env_delta": 0.0,
        "team_offense_delta": 0.0,
        "edge": 0.15,
        "inferred_runs": 1,
        "inning": 6,
        "inning_state": "Top",
        "outs": 1,
        "away_score_before": 4,
        "home_score_before": 4,
        "inferred_away_after": 5,
        "inferred_home_after": 4,
        "stake": 15.0,
        "runners_on": 0,
        "limit_price": 0.75,
    }
    base.update(overrides)
    return LiveBetRecord(**base)


class _FakeClob:
    def __init__(self):
        self.order_args = None

    def create_order(self, order_args):
        self.order_args = order_args
        return {"signed": True}

    def post_order(self, signed):
        return {"orderID": "order_123", "status": "live"}


class _FakeFinalGame:
    def __init__(self, away, home):
        self.score = SimpleNamespace(away=away, home=home)

    def is_final(self):
        return True


class _FakeOrderArgsV2:
    def __init__(self, token_id, price, size, side):
        self.token_id = token_id
        self.price = price
        self.size = size
        self.side = side


def _fake_clob_v2_modules():
    sdk = types.ModuleType("py_clob_client_v2")
    sdk.OrderArgsV2 = _FakeOrderArgsV2
    order_builder = types.ModuleType("py_clob_client_v2.order_builder")
    order_builder.BUY = "BUY"
    return {
        "py_clob_client_v2": sdk,
        "py_clob_client_v2.order_builder": order_builder,
    }


class LiveExecutionFixTests(unittest.TestCase):
    def test_clob_buy_converts_usdc_notional_to_share_size(self):
        client = pc.CLOBOrderClient(private_key="0x" + "1" * 64)
        fake = _FakeClob()
        client._initialized = True
        client._client = fake

        with patch.dict(sys.modules, _fake_clob_v2_modules()):
            result = client.place_limit_buy("token", price=0.75, size_usdc=15.0)

        self.assertTrue(result.success)
        self.assertAlmostEqual(fake.order_args.size, 20.0)
        self.assertAlmostEqual(result.size_shares, 20.0)
        self.assertAlmostEqual(result.notional_usdc, 15.0)

    def test_live_settlement_uses_fill_shares_and_cost(self):
        engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
        bet = _live_bet(
            order_status="filled",
            fill_price=0.75,
            actual_fill_price=0.75,
            fill_size=20.0,
            fill_cost=None,
        )
        engine._bets = [bet]
        engine.games = {1: _FakeFinalGame(5, 4)}
        engine.matches = {}
        engine._line_states = {}
        engine._outcome_games_written = set()
        engine._save_session = lambda *args, **kwargs: None

        se.SignalEngine._settle_finished_games(engine)

        self.assertTrue(bet.settled)
        self.assertTrue(bet.won)
        self.assertAlmostEqual(bet.fill_cost, 15.0)
        self.assertAlmostEqual(bet.filled_shares, 20.0)
        self.assertAlmostEqual(bet.fill_cost_usdc, 15.0)
        self.assertAlmostEqual(bet.payout, 20.0)
        self.assertAlmostEqual(bet.payout_usdc, 20.0)
        self.assertAlmostEqual(bet.profit, 5.0)

    def test_cancelled_order_releases_line_state_with_cooldown(self):
        engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
        state = SimpleNamespace(
            bet_open=True,
            cooldown_remaining=0,
            baseline_ask=0.70,
            baseline_candidate=0.70,
            stable_count=99,
            pending_signal=True,
            pending_ticks_remaining=2,
            pending_jump_ask=0.80,
        )
        engine._line_states = {(1, "8.5"): state}
        bet = _live_bet()

        engine._release_line_after_unfilled_order(bet, reason="fv_decay")

        self.assertFalse(state.bet_open)
        self.assertEqual(state.cooldown_remaining, le.DEFAULT_COOLDOWN_TICKS)
        self.assertIsNone(state.baseline_ask)
        self.assertFalse(state.pending_signal)

    def test_live_ledger_writes_orders_and_master_streams(self):
        engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
        bet = _live_bet(order_status="filled", fill_price=0.75, fill_size=20.0)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            engine._live_orders_path = root / "live_orders_ledger.jsonl"
            engine._live_ledger_path = root / "master_ledger.jsonl"

            engine._append_to_live_ledger(bet)

            for path in (engine._live_orders_path, engine._live_ledger_path):
                rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["bet_id"], "bet_1")
                self.assertEqual(rows[0]["_event"], "filled")

    def test_live_ledger_deduplicates_repeated_lifecycle_events(self):
        engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
        bet = _live_bet(order_id="order_1", order_status="live")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            engine._live_orders_path = root / "live_orders_ledger.jsonl"
            engine._live_ledger_path = root / "master_ledger.jsonl"

            engine._append_to_live_ledger(bet)
            bet.ask_5s = 0.74
            engine._append_to_live_ledger(bet)
            bet.order_status = "filled"
            bet.fill_price = 0.72
            engine._append_to_live_ledger(bet)

            for path in (engine._live_orders_path, engine._live_ledger_path):
                rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                self.assertEqual([row["_event"] for row in rows], ["live", "filled"])

    def test_model_maturity_report_refreshes_inputs_through_session_date(self):
        engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
        engine.date_str = "2026-05-08"
        engine._model_maturity_report_written = False

        with patch("live_engine._refresh_calibration_opportunity_training_table") as refresh_mock, patch(
            "live_engine._build_model_maturity_report"
        ) as build_mock, patch("live_engine._write_model_maturity_report") as write_mock:
            build_mock.return_value = {"date_range": {"max_date": "2026-05-08"}}
            write_mock.return_value = (
                Path("data/analysis_output/model_maturity/model_maturity_report.json"),
                Path("data/analysis_output/model_maturity/model_maturity_report.md"),
            )

            engine._write_model_maturity_report()

        refresh_mock.assert_called_once_with(["--mode", "live", "--max-date", "2026-05-08"])
        build_mock.assert_called_once_with(mode="live", max_date="2026-05-08")
        write_mock.assert_called_once()
        self.assertTrue(engine._model_maturity_report_written)

    def test_shutdown_scrapes_active_date_before_reports(self):
        engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
        engine._open_orders = {}
        calls = []
        engine._settle_finished_games = lambda: calls.append("settle")
        engine._save_session = lambda *args, **kwargs: calls.append("save")
        engine._scrape_active_date_final_games = lambda: calls.append("scrape")
        engine._write_daily_human_review_report = lambda: calls.append("human_review")
        engine._write_model_maturity_report = lambda: calls.append("maturity")

        engine._shutdown_gracefully()

        self.assertEqual(calls, ["settle", "save", "scrape", "human_review", "maturity"])

    def test_normal_run_exit_scrapes_active_date_before_reports(self):
        engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
        calls = []
        engine._settle_finished_games = lambda: calls.append("settle")
        engine._save_session = lambda *args, **kwargs: calls.append("save")
        engine._scrape_active_date_final_games = lambda: calls.append("scrape")
        engine._write_daily_human_review_report = lambda: calls.append("human_review")
        engine._write_model_maturity_report = lambda: calls.append("maturity")

        le._finalize_after_run_exit(engine)

        self.assertEqual(calls, ["settle", "save", "scrape", "human_review", "maturity"])

    def test_session_resume_dedup_ignores_failed_and_unfilled_orders(self):
        filled = _live_bet(
            bet_id="filled",
            placed_at="2026-05-14T18:00:00Z",
            order_status="filled",
            order_id="filled_order",
            edge=0.12,
            inning=4,
        )
        failed = _live_bet(
            bet_id="failed",
            placed_at="2026-05-14T19:00:00Z",
            order_status="error",
            order_id=None,
            edge=0.99,
            inning=9,
        )
        cancelled = _live_bet(
            bet_id="cancelled",
            placed_at="2026-05-14T20:00:00Z",
            order_status="cancelled",
            cancel_reason="fv_decay",
            order_id="cancelled_order",
            edge=0.88,
            inning=8,
        )

        with tempfile.TemporaryDirectory() as td:
            session_path = Path(td) / "session.json"
            session_path.write_text(
                json.dumps({"bets": [asdict(filled), asdict(failed), asdict(cancelled)]}),
                encoding="utf-8",
            )
            engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
            engine._live_session_path = session_path
            engine.live_args = SimpleNamespace(daily_budget=100.0)
            engine._bets = []
            engine._last_bet_ts = {}
            engine._last_bet_edge = {}
            engine._last_bet_inning = {}
            engine._last_bet_edge_by_line = {}
            engine._open_orders = {}
            engine._line_states = {}
            engine._filled_notional = lambda bet: float(bet.stake)
            engine._check_open_orders = lambda: None

            lsl.load_existing_session(engine)

        self.assertEqual(len(engine._bets), 3)
        self.assertEqual(engine._last_bet_edge[1], 0.12)
        self.assertEqual(engine._last_bet_inning[(1, "8.5")], 4)
        self.assertEqual(engine._last_bet_edge_by_line[(1, "8.5")], 0.12)
        self.assertEqual(engine._open_orders, {})
        self.assertEqual(engine._line_states, {})

    def test_session_resume_restores_open_order_line_lock(self):
        live_bet = _live_bet(
            bet_id="open",
            placed_at="2026-05-14T18:00:00Z",
            order_placed_at="2026-05-14T18:00:05Z",
            order_status="live",
            order_id="order_live",
            edge=0.16,
            inning=6,
        )
        calls = []

        with tempfile.TemporaryDirectory() as td:
            session_path = Path(td) / "session.json"
            session_path.write_text(json.dumps({"bets": [asdict(live_bet)]}), encoding="utf-8")
            engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
            engine._live_session_path = session_path
            engine.live_args = SimpleNamespace(daily_budget=100.0)
            engine._bets = []
            engine._last_bet_ts = {}
            engine._last_bet_edge = {}
            engine._last_bet_inning = {}
            engine._last_bet_edge_by_line = {}
            engine._open_orders = {}
            engine._line_states = {}
            engine._filled_notional = lambda bet: float(bet.stake)
            engine._check_open_orders = lambda: calls.append("poll")

            lsl.load_existing_session(engine)

        self.assertIn("order_live", engine._open_orders)
        self.assertEqual(calls, ["poll"])
        state = engine._line_states[(1, "8.5")]
        self.assertTrue(state.bet_open)
        self.assertGreater(state.cooldown_remaining, 0)
        self.assertEqual(engine._last_bet_edge[1], 0.16)
        self.assertEqual(engine._last_bet_inning[(1, "8.5")], 6)

    def test_skip_feature_limit_uses_live_spread_factor(self):
        engine = SimpleNamespace(
            live_args=SimpleNamespace(spread_factor=0.25),
            trade_args=SimpleNamespace(spread_factor=0.65, capture_depth=5),
            _start_tape_capture=lambda **kwargs: None,
            _start_family_b_capture=lambda **kwargs: None,
            _start_family_c_capture=lambda **kwargs: None,
        )
        ctx = SimpleNamespace(
            best_bid=0.60,
            ask=0.80,
            now=123.0,
            market=SimpleNamespace(over_token_id="token"),
            state=SimpleNamespace(),
            book={"ltp": 0.70},
            inning=6,
            runners_on=0,
        )
        payload = {
            "candidate_id": "abc",
            "fair_value": 0.90,
            "runs_needed": 2.5,
        }

        spc.attach_skip_features(engine, ctx, payload, reason="gate_min_edge")

        self.assertAlmostEqual(payload["hypothetical_limit_price"], 0.65)
        self.assertAlmostEqual(payload["family_d_features"]["spread_normalized_limit"], 0.25)

    def test_candidate_enrichment_limit_uses_live_spread_factor(self):
        engine = SimpleNamespace(
            live_args=SimpleNamespace(spread_factor=0.25),
            trade_args=SimpleNamespace(spread_factor=0.65, extreme_edge_max=0.22),
        )
        row = {
            "session_date": "2026-05-15",
            "game_pk": 1,
            "line": "8.5",
            "best_bid": 0.60,
            "decision_ask": 0.80,
            "fair_value": 0.90,
            "edge": 0.10,
        }

        cse.attach_modeling_observability_fields(engine, row)

        self.assertAlmostEqual(row["hypothetical_limit_price"], 0.65)
        self.assertAlmostEqual(row["execution_policy_current_limit_price"], 0.65)

    def test_parse_trade_args_restores_sys_argv_on_monitor_parse_error(self):
        original_argv = list(sys.argv)
        sentinel_argv = ["pytest", "--original"]
        sys.argv = list(sentinel_argv)
        try:
            with patch("signal_config.monitor_parse_args", side_effect=RuntimeError("boom")):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    sc.parse_trade_args(["--stake", "10"])
            self.assertEqual(sys.argv, sentinel_argv)
        finally:
            sys.argv = original_argv

    def test_zero_trade_summary_counts_listed_lines_not_games(self):
        class FinalGame:
            def __init__(self, game_pk, away, home, away_score, home_score):
                self.game_pk = game_pk
                self.away_abbrev = away
                self.home_abbrev = home
                self.score = SimpleNamespace(away=away_score, home=home_score)

            def is_final(self):
                return True

        engine = SimpleNamespace(
            date_str="2026-05-15",
            _bets=[],
            _open_orders={},
            games={
                1: FinalGame(1, "AWY", "HOM", 5, 4),
                2: FinalGame(2, "BOS", "NYY", 3, 2),
            },
            matches={
                1: SimpleNamespace(markets=[
                    SimpleNamespace(line="7.5"),
                    SimpleNamespace(line="8.5"),
                    SimpleNamespace(line="9.5"),
                ]),
                2: SimpleNamespace(markets=[
                    SimpleNamespace(line="6.5"),
                    SimpleNamespace(line="8.5"),
                ]),
            },
        )

        with self.assertLogs("live_engine", level="INFO") as cm:
            lesio._emit_post_save_summary_logs(
                engine,
                filled_settled=[],
                missed_settled=[],
            )

        joined = "\n".join(cm.output)
        self.assertIn("2 games final | 2/5 listed lines went over", joined)

    def test_active_date_final_scrape_uses_active_date(self):
        engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
        engine.date_str = "2026-05-09"
        engine._active_date_final_scrape_done = False

        completed = SimpleNamespace(returncode=0, stdout="done\n", stderr="")
        with patch("live_engine.subprocess.run", return_value=completed) as run_mock:
            engine._scrape_active_date_final_games()
            engine._scrape_active_date_final_games()

        run_mock.assert_called_once()
        command = run_mock.call_args.args[0]
        self.assertIn("scripts\\scraping\\scrape_mlb_history.py", command[1])
        self.assertIn("--start-date", command)
        self.assertEqual(command[command.index("--start-date") + 1], "2026-05-09")
        self.assertEqual(command[command.index("--end-date") + 1], "2026-05-09")
        self.assertIn("--overwrite", command)

    def test_paper_bet_records_state_value_diagnostics(self):
        engine = se.SignalEngine.__new__(se.SignalEngine)
        engine.date_str = "2026-04-29"
        engine._bet_counter = 0
        engine._bets = []
        engine.trade_args = SimpleNamespace(stake=10.0)
        engine._save_session = lambda *args, **kwargs: None
        game = SimpleNamespace(
            game_pk=123,
            away_abbrev="AWY",
            home_abbrev="HOM",
            venue_name="Test Park",
        )
        market = SimpleNamespace(line="8.5")

        bet = engine._place_bet(
            game=game,
            market=market,
            best_ask=0.74,
            fair_value=0.91,
            base_fair_value=0.89,
            stage2_run_env_delta=0.01,
            team_offense_delta=0.02,
            edge=0.17,
            inferred_runs=1,
            inning=6,
            inning_state="Top",
            outs=1,
            away_score_before=4,
            home_score_before=3,
            batting_is_away=True,
            runners_on=1,
            state_value_diagnostics={
                "state_value_strategy": "score_event_transition",
                "current_state_value_edge": -0.08,
                "current_state_value_fv_raw": 0.66,
                "current_state_value_empirical_edge": -0.03,
                "shadow_fv_inferred_lift": 0.25,
                "shadow_no_event_edge": -0.08,
                "shadow_after_event_edge": 0.17,
                "shadow_p_score_event_proxy": 0.20,
                "shadow_phantom_risk_score": 0.80,
                "shadow_phantom_risk_band": "high",
                "shadow_transition_model": "score_event_vs_no_event_proxy_v1",
            },
        )

        self.assertIsNotNone(bet)
        self.assertEqual(bet.state_value_strategy, "score_event_transition")
        self.assertAlmostEqual(bet.current_state_value_edge, -0.08)
        self.assertAlmostEqual(bet.shadow_fv_inferred_lift, 0.25)
        self.assertEqual(bet.shadow_phantom_risk_band, "high")

    def test_live_session_save_includes_no_score_drift_params(self):
        live_args, trade_args, _ = le.parse_live_args(
            [
                "--dry-run",
                "--stake-mode", "flat",
                "--stake", "10",
                "--daily-budget", "80",
                "--shadow-no-score-drift-enabled",
            ]
        )
        engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
        engine.date_str = "2026-04-29"
        engine.trade_args = trade_args
        engine.live_args = live_args
        engine._dry_run = True
        engine._bets = []
        engine._open_orders = {}
        engine.games = {}
        engine._ev_policy_mode = "shadow"
        engine._ev_policy_stats = {
            "scored": 0,
            "shadow_allow": 0,
            "shadow_block": 0,
            "enforce_allow": 0,
            "enforce_block": 0,
        }
        engine._prob_calibration_stats = {
            "scored": 0,
            "applied": 0,
            "shadow_scored": 0,
            "disabled_or_missing": 0,
        }
        engine._candidate_rows_written = 0
        engine._candidate_rows_dedup_suppressed = 0
        engine._candidate_null_fields_omitted = 0
        engine._last_session_save_ts = 0.0
        engine._session_save_pending = False
        engine._session_save_min_interval_secs = 0.0
        engine._shadow_order_summary_logged = False
        engine._shadow_feature_summary_logged = False

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "2026-04-29_session.json"
            engine._live_session_path = path

            engine._save_session(force=True)

            self.assertTrue(path.exists())
            session = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(session["params"]["shadow_no_score_drift_enabled"])
            self.assertEqual(session["params"]["shadow_no_score_drift_min_inning"], 3)
            self.assertEqual(session["params"]["stake"], 10.0)
            self.assertEqual(session["params"]["ev_policy_mode"], "shadow")
            self.assertIn("current_state_edge_band_diagnostics", session["summary"])
            self.assertIn("shadow_feature_diagnostics", session["summary"])

    def test_daily_human_review_written_once(self):
        engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
        engine.date_str = "2026-05-08"
        engine._daily_human_review_written = False

        with patch("live_engine._build_daily_human_review_report") as build_mock, patch(
            "live_engine._write_daily_human_review_report"
        ) as write_mock:
            build_mock.return_value = {"session_date": "2026-05-08"}
            write_mock.return_value = (
                Path("data/analysis_output/daily_human_review/2026-05-08_human_review.json"),
                Path("data/analysis_output/daily_human_review/2026-05-08_human_review.md"),
            )

            engine._write_daily_human_review_report()
            engine._write_daily_human_review_report()

        build_mock.assert_called_once_with(session_date="2026-05-08")
        write_mock.assert_called_once()
        self.assertTrue(engine._daily_human_review_written)

    def test_kelly_stake_does_not_floor_to_min_by_default(self):
        engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
        engine.live_args = SimpleNamespace(
            stake_mode="kelly",
            daily_budget=100.0,
            kelly_fraction=0.25,
            kelly_max_bet_fraction=0.33,
            kelly_max_edge=0.25,
            min_order_size=5.0,
            kelly_floor_to_min=False,
        )

        stake = engine._compute_stake(fair_value=0.61, limit_price=0.60)

        self.assertAlmostEqual(stake, 0.63)
        self.assertLess(stake, engine.live_args.min_order_size)

    def test_kelly_floor_to_min_is_explicit_opt_in(self):
        engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
        engine.live_args = SimpleNamespace(
            stake_mode="kelly",
            daily_budget=100.0,
            kelly_fraction=0.25,
            kelly_max_bet_fraction=0.33,
            kelly_max_edge=0.25,
            min_order_size=5.0,
            kelly_floor_to_min=True,
        )

        stake = engine._compute_stake(fair_value=0.61, limit_price=0.60)

        self.assertAlmostEqual(stake, 5.0)

    def test_accepted_order_statuses_are_exposure_counted(self):
        self.assertEqual(le._normalize_accepted_order_status("matched"), "live")
        self.assertEqual(le._normalize_accepted_order_status("delayed"), "live")
        self.assertEqual(le._normalize_accepted_order_status("filled"), "live")
        self.assertTrue(le._is_exposure_counted_status("matched"))
        self.assertTrue(le._is_exposure_counted_status("delayed"))
        self.assertEqual(le._normalize_order_status("canceled"), "cancelled")
        self.assertFalse(le._is_exposure_counted_status("canceled"))
        self.assertEqual(lesio.live_bet_event_type(_live_bet(order_status="canceled")), "cancelled")

        bets = [
            _live_bet(order_status="matched", stake=15.0),
            _live_bet(order_status="delayed", stake=20.0),
        ]
        reserved = sum(
            b.stake for b in bets
            if le._is_exposure_counted_status(getattr(b, "order_status", ""))
        )

        self.assertAlmostEqual(reserved, 35.0)

    def test_ev_policy_schema_mismatch_raises(self):
        payload = {
            "preprocessor": {
                "numeric_cols": ["x"],
                "categorical_cols": ["side"],
                "medians": {"x": 0.0},
                "means": {"x": 0.0},
                "stds": {"x": 1.0},
                "categories": {"side": ["A", "B"]},
                "feature_names": ["x", "side_A"],
            },
            "model": {"bias": 0.0},
            "weights": [
                {"feature": "x", "weight": 1.0},
                {"feature": "side_A", "weight": 1.0},
            ],
        }

        with self.assertRaisesRegex(ValueError, "schema mismatch"):
            ep.LogisticJsonScorer(payload)

    def test_ev_policy_enforce_blocks_when_runtime_unavailable(self):
        engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
        engine._ev_policy_mode = "enforce"
        engine._ev_policy_runtime = None
        engine._ev_policy_stats = {
            "scored": 0,
            "shadow_allow": 0,
            "shadow_block": 0,
            "enforce_allow": 0,
            "enforce_block": 0,
        }

        allow, diag = engine._evaluate_ev_policy({}, stake=15.0, price=0.75)

        self.assertFalse(allow)
        self.assertEqual(diag["reason"], "ev_policy_unavailable")
        self.assertEqual(engine._ev_policy_stats["enforce_block"], 1)

    def test_ev_policy_load_error_fails_closed_in_enforce_mode(self):
        bad_payload = {
            "preprocessor": {
                "numeric_cols": ["x"],
                "categorical_cols": ["side"],
                "medians": {"x": 0.0},
                "means": {"x": 0.0},
                "stds": {"x": 1.0},
                "categories": {"side": ["A", "B"]},
                "feature_names": ["x", "side_A"],
            },
            "model": {"bias": 0.0},
            "weights": [{"feature": "x", "weight": 1.0}],
        }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            report_path = root / "report.json"
            win_path = root / "win.json"
            fill_path = root / "fill.json"
            report_path.write_text(
                json.dumps({"best_config": {"min_ev_per_stake": 0.01, "min_p_fill": 0.20}}),
                encoding="utf-8",
            )
            win_path.write_text(json.dumps(bad_payload), encoding="utf-8")
            fill_path.write_text(json.dumps(bad_payload), encoding="utf-8")

            engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
            engine.live_args = SimpleNamespace(
                ev_policy_report_path=report_path,
                ev_policy_win_model_path=win_path,
                ev_policy_fill_model_path=fill_path,
            )
            engine._ev_policy_mode = "enforce"
            engine._ev_policy_runtime = None

            engine._load_ev_policy_runtime()

            self.assertEqual(engine._ev_policy_mode, "enforce")
            self.assertIn("load_error", engine._ev_policy_runtime)

    def test_ev_policy_runtime_rejects_post_signal_fill_artifact(self):
        safe_payload = {
            "runtime_safe": True,
            "preprocessor": {
                "numeric_cols": ["current_state_value_edge"],
                "categorical_cols": [],
                "medians": {"current_state_value_edge": 0.0},
                "means": {"current_state_value_edge": 0.0},
                "stds": {"current_state_value_edge": 1.0},
                "categories": {},
                "feature_names": ["num::current_state_value_edge"],
            },
            "model": {"bias": 0.0},
            "weights": [
                {"feature": "num::current_state_value_edge", "weight": 1.0},
            ],
        }
        unsafe_fill_payload = {
            "runtime_safe": False,
            "preprocessor": {
                "numeric_cols": ["ask_1s"],
                "categorical_cols": [],
                "medians": {"ask_1s": 0.0},
                "means": {"ask_1s": 0.0},
                "stds": {"ask_1s": 1.0},
                "categories": {},
                "feature_names": ["num::ask_1s"],
            },
            "model": {"bias": 0.0},
            "weights": [
                {"feature": "num::ask_1s", "weight": 1.0},
            ],
        }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            report_path = root / "report.json"
            win_path = root / "win.json"
            fill_path = root / "fill.json"
            report_path.write_text(
                json.dumps(
                    {
                        "policy_selection": {
                            "best_validation_config": {
                                "min_ev_per_stake": 0.02,
                                "min_p_fill": 0.30,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            win_path.write_text(json.dumps(safe_payload), encoding="utf-8")
            fill_path.write_text(json.dumps(unsafe_fill_payload), encoding="utf-8")

            engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
            engine.live_args = SimpleNamespace(
                ev_policy_report_path=report_path,
                ev_policy_win_model_path=win_path,
                ev_policy_fill_model_path=fill_path,
            )
            engine._ev_policy_mode = "enforce"
            engine._ev_policy_runtime = None

            engine._load_ev_policy_runtime()

            self.assertIn("load_error", engine._ev_policy_runtime)
            self.assertIn("post-signal", engine._ev_policy_runtime["load_error"])

    def test_ev_policy_runtime_loads_current_report_schema_thresholds(self):
        payload = {
            "runtime_safe": True,
            "preprocessor": {
                "numeric_cols": ["current_state_value_edge"],
                "categorical_cols": [],
                "medians": {"current_state_value_edge": 0.0},
                "means": {"current_state_value_edge": 0.0},
                "stds": {"current_state_value_edge": 1.0},
                "categories": {},
                "feature_names": ["num::current_state_value_edge"],
            },
            "model": {"bias": 0.0},
            "weights": [
                {"feature": "num::current_state_value_edge", "weight": 1.0},
            ],
        }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            report_path = root / "report.json"
            win_path = root / "win.json"
            fill_path = root / "fill.json"
            report_path.write_text(
                json.dumps(
                    {
                        "policy_selection": {
                            "best_validation_config": {
                                "min_ev_per_stake": 0.02,
                                "min_p_fill": 0.30,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            win_path.write_text(json.dumps(payload), encoding="utf-8")
            fill_path.write_text(json.dumps(payload), encoding="utf-8")

            engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
            engine.live_args = SimpleNamespace(
                ev_policy_report_path=report_path,
                ev_policy_win_model_path=win_path,
                ev_policy_fill_model_path=fill_path,
            )
            engine._ev_policy_mode = "shadow"
            engine._ev_policy_runtime = None

            engine._load_ev_policy_runtime()

            self.assertEqual(engine._ev_policy_mode, "shadow")
            self.assertAlmostEqual(engine._ev_policy_runtime["min_ev_per_stake"], 0.02)
            self.assertAlmostEqual(engine._ev_policy_runtime["min_p_fill"], 0.30)
            self.assertEqual(engine._ev_policy_runtime["feature_policy"], "decision_time_runtime_reliable")

    def test_ev_policy_runtime_rejects_no_score_drift_artifact(self):
        payload = {
            "model_family": "no_score_drift",
            "runtime_safe": True,
            "preprocessor": {
                "numeric_cols": ["current_state_value_edge"],
                "categorical_cols": [],
                "medians": {"current_state_value_edge": 0.0},
                "means": {"current_state_value_edge": 0.0},
                "stds": {"current_state_value_edge": 1.0},
                "categories": {},
                "feature_names": ["num::current_state_value_edge"],
            },
            "model": {"bias": 0.0},
            "weights": [
                {"feature": "num::current_state_value_edge", "weight": 1.0},
            ],
        }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            report_path = root / "report.json"
            win_path = root / "win.json"
            fill_path = root / "fill.json"
            report_path.write_text(
                json.dumps({"best_config": {"min_ev_per_stake": 0.01, "min_p_fill": 0.20}}),
                encoding="utf-8",
            )
            win_path.write_text(json.dumps(payload), encoding="utf-8")
            fill_path.write_text(json.dumps(payload), encoding="utf-8")

            engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
            engine.live_args = SimpleNamespace(
                ev_policy_report_path=report_path,
                ev_policy_win_model_path=win_path,
                ev_policy_fill_model_path=fill_path,
            )
            engine._ev_policy_mode = "enforce"
            engine._ev_policy_runtime = None

            engine._load_ev_policy_runtime()

            self.assertIn("load_error", engine._ev_policy_runtime)
            self.assertIn("score_event_transition artifacts", engine._ev_policy_runtime["load_error"])

    def test_ev_feature_row_carries_state_value_and_training_aliases(self):
        engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
        engine.trade_args = SimpleNamespace(
            edge_threshold=0.15,
            edge_threshold_high_line=0.16,
            jump_threshold=0.06,
            min_inning=4,
            min_inning_high_line=5,
            high_line_cutoff=8.5,
            min_entry_ask=0.55,
            min_entry_ask_high_line=0.60,
            min_current_total=4,
            runs_needed_max=3.5,
            min_close_game_rn=4.0,
            inn5_rn_max=2.5,
            inn6_rn_max=2.5,
            confirmation_ticks=3,
            event_dedup_secs=60.0,
            fv_ask_gap_max=0.26,
            fv_ask_gap_min_inning=6,
            inning_dedup_edge_gap=0.05,
            inning_dedup_gap=2,
            lookback_ticks=5,
            max_base_fv=0.99,
            max_spread=0.20,
            spread_factor=0.65,
            stake_mode="flat",
        )
        engine.live_args = SimpleNamespace(
            ask_reversal_drop=0.08,
            ask_reversal_window=5,
            daily_budget=80.0,
            fv_cancel_min_edge=0.03,
            fv_decay_min_age_secs=90.0,
            fv_decay_min_ask_drop=0.03,
            kelly_fraction=None,
            kelly_max_bet_fraction=None,
            kelly_max_edge=None,
            max_open_orders=7,
            order_timeout_secs=10800.0,
            per_game_budget_fraction=0.40,
            spread_factor=0.65,
            stake_mode="flat",
        )
        game = SimpleNamespace(
            away_abbrev="AWY",
            home_abbrev="HOM",
            venue_name="Test Park",
        )
        market = SimpleNamespace(line="8.5", over_token_id="token_123")

        row = engine._build_ev_feature_row(
            game=game,
            market=market,
            line_val=8.5,
            best_ask=0.66,
            bid=0.61,
            fair_value=0.87,
            base_fair_value=0.83,
            stage2_run_env_delta=0.02,
            team_offense_delta=0.01,
            edge=0.21,
            inferred_runs=1,
            inning=6,
            inning_state="Top",
            outs=1,
            away_score_before=3,
            home_score_before=2,
            runners_on=3,
            limit_price=0.64,
            stake=10.0,
            ltp=0.63,
            execution_book={
                "ok": True,
                "latency_ms": 12.5,
                "total_bid_depth": 100.0,
                "total_ask_depth": 80.0,
            },
            state_value_diagnostics={
                "current_state_value_base_poisson": 0.71,
                "current_state_value_edge": 0.05,
                "current_state_value_empirical_edge": 0.03,
                "shadow_fv_inferred_lift": 0.11,
                "shadow_phantom_risk_score": 0.42,
                "shadow_phantom_risk_band": "medium",
                "shadow_transition_model": "score_event_vs_no_event_proxy_v1",
                "shadow_no_score_drift_trigger": "both",
                "score_segment_drawdown": 0.07,
            },
        )

        self.assertEqual(row["entry_ask"], 0.66)
        self.assertEqual(row["t0_best_bid"], 0.61)
        self.assertEqual(row["t0_best_ask"], 0.66)
        self.assertEqual(row["t0_ltp"], 0.63)
        self.assertEqual(row["param_edge_threshold"], 0.15)
        self.assertEqual(row["param_daily_budget"], 80.0)
        self.assertEqual(row["over_token_id"], "token_123")
        self.assertAlmostEqual(row["current_state_value_base_poisson"], 0.71)
        self.assertAlmostEqual(row["current_state_value_edge"], 0.05)
        self.assertAlmostEqual(row["shadow_fv_inferred_lift"], 0.11)
        self.assertEqual(row["shadow_phantom_risk_band"], "medium")
        self.assertEqual(row["shadow_no_score_drift_trigger"], "both")
        self.assertAlmostEqual(row["score_segment_drawdown"], 0.07)
        self.assertTrue(row["shadow_low_ask_high_edge"])
        self.assertTrue(row["shadow_runs_needed_exact_3p5"])
        self.assertEqual(row["shadow_current_state_edge_bucket"], "current_edge_0.03-0.08")
        self.assertEqual(row["shadow_phantom_risk_bucket"], "medium")
        self.assertEqual(
            row["shadow_current_phantom_combo_bucket"],
            "current_edge_0.03-0.08|phantom_medium",
        )
        self.assertEqual(row["shadow_inning_runs_needed_bucket"], "inn_6|rn_2.5-3.5")

    def test_ev_policy_enforce_blocks_missing_runtime_features(self):
        payload = {
            "preprocessor": {
                "numeric_cols": ["current_state_value_edge"],
                "categorical_cols": [],
                "medians": {"current_state_value_edge": 0.0},
                "means": {"current_state_value_edge": 0.0},
                "stds": {"current_state_value_edge": 1.0},
                "categories": {},
                "feature_names": ["num::current_state_value_edge"],
            },
            "model": {"bias": 0.0},
            "weights": [
                {"feature": "num::current_state_value_edge", "weight": 1.0},
            ],
        }
        engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
        engine._ev_policy_mode = "enforce"
        engine._ev_policy_runtime = {
            "win_scorer": ep.LogisticJsonScorer(payload),
            "fill_scorer": None,
            "min_ev_per_stake": 0.0,
            "min_p_fill": 0.0,
        }
        engine._ev_policy_stats = {
            "scored": 0,
            "shadow_allow": 0,
            "shadow_block": 0,
            "enforce_allow": 0,
            "enforce_block": 0,
        }

        allow, diag = engine._evaluate_ev_policy({}, stake=10.0, price=0.50)

        self.assertFalse(allow)
        self.assertEqual(diag["reason"], "ev_policy_missing_runtime_features")
        self.assertEqual(diag["missing_runtime_features"]["win"], ["current_state_value_edge"])
        self.assertEqual(engine._ev_policy_stats["enforce_block"], 1)
        self.assertEqual(engine._ev_policy_stats["missing_runtime_features"], 1)

    def test_ev_policy_shadow_warns_once_for_missing_runtime_features(self):
        payload = {
            "preprocessor": {
                "numeric_cols": ["current_state_value_edge"],
                "categorical_cols": [],
                "medians": {"current_state_value_edge": 0.0},
                "means": {"current_state_value_edge": 0.0},
                "stds": {"current_state_value_edge": 1.0},
                "categories": {},
                "feature_names": ["num::current_state_value_edge"],
            },
            "model": {"bias": 0.0},
            "weights": [
                {"feature": "num::current_state_value_edge", "weight": 1.0},
            ],
        }
        engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
        engine._ev_policy_mode = "shadow"
        engine._ev_policy_runtime = {
            "win_scorer": ep.LogisticJsonScorer(payload),
            "fill_scorer": None,
            "min_ev_per_stake": 0.0,
            "min_p_fill": 0.0,
        }
        engine._ev_policy_stats = {
            "scored": 0,
            "shadow_allow": 0,
            "shadow_block": 0,
            "enforce_allow": 0,
            "enforce_block": 0,
        }

        with self.assertLogs("live_engine", level="WARNING") as captured:
            engine._evaluate_ev_policy({}, stake=10.0, price=0.50)
            engine._evaluate_ev_policy({}, stake=10.0, price=0.50)

        self.assertEqual(len(captured.records), 1)
        self.assertIn("requires runtime features", captured.output[0])
        self.assertEqual(engine._ev_policy_stats["missing_runtime_features"], 2)

    def test_shadow_order_diagnostics_summarize_risk_regimes(self):
        engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
        engine.trade_args = SimpleNamespace(
            extreme_edge_max=0.30,
            ltp_ask_gap_max=0.50,
            max_base_fv=0.99,
        )
        high_edge_loss = _live_bet(
            bet_id="high_edge_loss",
            order_id="order_1",
            order_status="filled",
            edge=0.31,
            fill_price=0.56,
            actual_fill_price=0.56,
            fill_size=35.7143,
            fill_cost=20.0,
            profit=-20.0,
            won=False,
            ltp_at_signal=0.67,
            entry_ask=0.57,
            state_value_strategy="score_event_transition",
            current_state_value_edge=-0.12,
            shadow_phantom_risk_score=0.82,
            shadow_phantom_risk_band="high",
        )
        ltp_gap_miss = _live_bet(
            bet_id="ltp_gap_miss",
            order_id="order_2",
            order_status="cancelled",
            cancel_reason="game_final",
            edge=0.20,
            stake=20.0,
            won=True,
            ltp_at_signal=0.24,
            entry_ask=0.76,
        )
        saturated_win = _live_bet(
            bet_id="saturated_win",
            order_id="order_3",
            order_status="filled",
            base_fair_value=0.995,
            fill_price=0.75,
            fill_size=20.0,
            fill_cost=15.0,
            profit=5.0,
            won=True,
            state_value_strategy="score_event_transition",
            current_state_value_edge=0.08,
            shadow_phantom_risk_score=0.18,
            shadow_phantom_risk_band="low",
        )
        engine._bets = [high_edge_loss, ltp_gap_miss, saturated_win]

        diag = engine._build_shadow_order_diagnostics()

        self.assertEqual(diag["high_edge"]["placed"], 1)
        self.assertEqual(diag["high_edge"]["filled_losses"], 1)
        self.assertAlmostEqual(diag["high_edge"]["filled_profit"], -20.0)
        self.assertEqual(diag["ltp_ask_gap"]["placed"], 1)
        self.assertEqual(diag["ltp_ask_gap"]["missed"], 1)
        self.assertEqual(diag["ltp_ask_gap"]["signal_wins"], 1)
        self.assertAlmostEqual(diag["ltp_ask_gap"]["reserved_on_misses"], 20.0)
        self.assertEqual(diag["fv_saturation"]["placed"], 1)
        self.assertEqual(diag["fv_saturation"]["filled_wins"], 1)
        self.assertEqual(diag["phantom_high"]["placed"], 1)
        self.assertEqual(diag["phantom_high"]["filled_losses"], 1)
        self.assertEqual(diag["phantom_high_current_negative"]["placed"], 1)
        self.assertEqual(diag["phantom_high_current_negative"]["filled_losses"], 1)
        self.assertEqual(diag["current_state_positive"]["placed"], 1)
        self.assertEqual(diag["current_state_positive"]["filled_wins"], 1)

    def test_current_state_edge_band_diagnostics_are_score_event_only(self):
        engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
        low_edge_loss = _live_bet(
            bet_id="low_edge_loss",
            order_id="order_1",
            order_status="filled",
            decision_ask=0.72,
            fill_price=0.72,
            actual_fill_price=0.72,
            fill_size=13.8889,
            fill_cost=10.0,
            profit=-10.0,
            won=False,
            state_value_strategy="score_event_transition",
            current_state_value_edge=0.02,
            shadow_phantom_risk_score=0.55,
            shadow_fv_inferred_lift=0.20,
        )
        mid_edge_win = _live_bet(
            bet_id="mid_edge_win",
            order_id="order_2",
            order_status="filled",
            decision_ask=0.62,
            fill_price=0.62,
            actual_fill_price=0.62,
            fill_size=16.129,
            fill_cost=10.0,
            profit=6.13,
            won=True,
            state_value_strategy="score_event_transition",
            current_state_value_edge=0.05,
            shadow_phantom_risk_score=0.25,
            shadow_fv_inferred_lift=0.08,
        )
        strong_edge_miss = _live_bet(
            bet_id="strong_edge_miss",
            order_id="order_3",
            order_status="cancelled",
            cancel_reason="game_final",
            stake=10.0,
            decision_ask=0.58,
            won=True,
            state_value_strategy="score_event_transition",
            current_state_value_edge=0.10,
            shadow_phantom_risk_score=0.18,
            shadow_fv_inferred_lift=0.04,
        )
        no_score_row = _live_bet(
            bet_id="no_score_shadow_family",
            order_id="order_4",
            order_status="filled",
            decision_ask=0.50,
            fill_price=0.50,
            fill_size=20.0,
            fill_cost=10.0,
            profit=10.0,
            won=True,
            state_value_strategy="no_score_drift",
            current_state_value_edge=0.01,
        )
        engine._bets = [low_edge_loss, mid_edge_win, strong_edge_miss, no_score_row]

        diag = engine._build_current_state_edge_band_diagnostics()

        low = diag["current_edge_lt_0p03"]
        self.assertEqual(low["placed"], 1)
        self.assertEqual(low["filled"], 1)
        self.assertEqual(low["filled_losses"], 1)
        self.assertEqual(low["signal_losses"], 1)
        self.assertAlmostEqual(low["filled_profit"], -10.0)
        self.assertAlmostEqual(low["avg_current_state_value_edge"], 0.02)

        mid = diag["current_edge_0p03_to_0p08"]
        self.assertEqual(mid["placed"], 1)
        self.assertEqual(mid["filled_wins"], 1)
        self.assertAlmostEqual(mid["filled_roi"], 0.613)

        strong = diag["current_edge_gte_0p08"]
        self.assertEqual(strong["placed"], 1)
        self.assertEqual(strong["missed"], 1)
        self.assertEqual(strong["signal_wins"], 1)
        self.assertAlmostEqual(strong["reserved_on_misses"], 10.0)

        self.assertEqual(diag["missing"]["placed"], 0)
        self.assertEqual(
            sum(int(row["placed"]) for row in diag.values()),
            3,
            "no-score drift rows should not be counted in score-event diagnostics",
        )

    def test_shadow_feature_diagnostics_summarize_new_regimes(self):
        engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
        low_ask_high_edge_loss = _live_bet(
            bet_id="low_ask_high_edge_loss",
            order_id="order_1",
            order_status="filled",
            entry_ask=0.66,
            decision_ask=0.66,
            edge=0.24,
            fill_price=0.66,
            actual_fill_price=0.66,
            fill_size=15.1515,
            fill_cost=10.0,
            profit=-10.0,
            won=False,
            line="8.5",
            away_score_before=3,
            home_score_before=2,
            inning=6,
            state_value_strategy="score_event_transition",
            current_state_value_edge=0.02,
            shadow_phantom_risk_score=0.55,
            shadow_phantom_risk_band="medium",
        )
        runs_needed_win = _live_bet(
            bet_id="runs_needed_win",
            order_id="order_2",
            order_status="filled",
            entry_ask=0.74,
            decision_ask=0.74,
            edge=0.17,
            fill_price=0.74,
            actual_fill_price=0.74,
            fill_size=13.5135,
            fill_cost=10.0,
            profit=3.51,
            won=True,
            line="8.5",
            away_score_before=3,
            home_score_before=2,
            inning=4,
            state_value_strategy="score_event_transition",
            current_state_value_edge=0.10,
            shadow_phantom_risk_score=0.25,
            shadow_phantom_risk_band="low",
            home_leading_late=False,
            batting_team_is_home=False,
            expected_remaining_pa_bucket="19+",
            home_skip_bottom9_risk=0.0,
        )
        bottom9_miss = _live_bet(
            bet_id="bottom9_miss",
            order_id="order_3",
            order_status="cancelled",
            cancel_reason="game_final",
            stake=10.0,
            entry_ask=0.76,
            decision_ask=0.76,
            edge=0.18,
            line="8.5",
            away_score_before=3,
            home_score_before=5,
            inning=8,
            inning_state="Top",
            won=True,
            home_leading_late=True,
            batting_team_is_home=False,
            expected_remaining_pa_bucket="<=5",
            home_skip_bottom9_risk=1.0,
        )
        engine._bets = [low_ask_high_edge_loss, runs_needed_win, bottom9_miss]

        diag = engine._build_shadow_feature_diagnostics()

        regimes = diag["regimes"]
        self.assertEqual(regimes["low_ask_high_edge"]["placed"], 1)
        self.assertEqual(regimes["low_ask_high_edge"]["filled_losses"], 1)
        self.assertAlmostEqual(regimes["low_ask_high_edge"]["filled_profit"], -10.0)
        self.assertEqual(regimes["runs_needed_exact_3p5"]["placed"], 2)
        self.assertEqual(regimes["runs_needed_exact_3p5"]["filled"], 2)
        self.assertEqual(regimes["home_skip_bottom9_risk"]["placed"], 1)
        self.assertEqual(regimes["home_skip_bottom9_risk"]["missed"], 1)
        self.assertIn(
            "current_edge<0.03|phantom_medium",
            diag["current_phantom_combo"],
        )
        self.assertIn("inn_<=4|rn_2.5-3.5", diag["inning_runs_needed_combo"])
        self.assertIn(
            "home_leading_late_skip_bottom9_risk|pa_<=5",
            diag["bottom9_home_lead_context"],
        )


class DepositWalletInitTests(unittest.TestCase):
    """ERC-1271 / sig_type=3 opt-in path coverage.

    Uses dry_run=True so we exercise branch selection without hitting the
    SDK's on-chain derive_api_key flow. The legacy proxy (sig_type=2) path
    must remain the default whenever use_deposit_wallet is not set.
    """

    PRIV = "0x" + "1" * 64
    DEPOSIT = "0x00000000000000000000000000000000DEADBEEF"

    def test_constructor_rejects_use_flag_without_address(self):
        with self.assertRaises(ValueError):
            pc.CLOBOrderClient(private_key=self.PRIV, use_deposit_wallet=True)

    def test_dry_run_init_legacy_path_unchanged(self):
        # Default constructor (no flags) must still produce sig_type=0/2
        # behavior; explicitly verify dry-run keeps the legacy log shape.
        client = pc.CLOBOrderClient(private_key=self.PRIV, dry_run=True)
        client.initialize()
        self.assertTrue(client._initialized)
        self.assertEqual(client._sig_type, 0)
        self.assertFalse(client._use_deposit_wallet)

    def test_dry_run_init_deposit_wallet_path(self):
        client = pc.CLOBOrderClient(
            private_key=self.PRIV,
            dry_run=True,
            use_deposit_wallet=True,
            deposit_wallet=self.DEPOSIT,
        )
        client.initialize()
        self.assertTrue(client._initialized)
        self.assertEqual(client._sig_type, pc.SIG_TYPE_DEPOSIT_WALLET)
        self.assertEqual(client._sig_type, 3)
        self.assertEqual(client._maker_address, self.DEPOSIT)

    def test_from_env_deposit_wallet_via_env_vars(self):
        # POLY_USE_DEPOSIT_WALLET=1 + POLY_DEPOSIT_WALLET=0x... should
        # activate the path even when no explicit kwargs are passed.
        with patch.dict(
            "os.environ",
            {
                "POLY_PRIVATE_KEY": self.PRIV,
                "POLY_USE_DEPOSIT_WALLET": "1",
                "POLY_DEPOSIT_WALLET": self.DEPOSIT,
                "POLY_PUBLIC_KEY": "",
            },
            clear=False,
        ):
            client = pc.CLOBOrderClient.from_env(dry_run=True)
        self.assertTrue(client._use_deposit_wallet)
        self.assertEqual(client._deposit_wallet, self.DEPOSIT)

    def test_from_env_explicit_kwargs_override_env_vars(self):
        # Explicit kwargs from CLI should win over a stale POLY_USE_DEPOSIT_WALLET.
        with patch.dict(
            "os.environ",
            {
                "POLY_PRIVATE_KEY": self.PRIV,
                "POLY_USE_DEPOSIT_WALLET": "0",
                "POLY_DEPOSIT_WALLET": "",
                "POLY_PUBLIC_KEY": "",
            },
            clear=False,
        ):
            client = pc.CLOBOrderClient.from_env(
                dry_run=True,
                use_deposit_wallet=True,
                deposit_wallet=self.DEPOSIT,
            )
        self.assertTrue(client._use_deposit_wallet)
        self.assertEqual(client._deposit_wallet, self.DEPOSIT)


class CorrelatedLineCapTests(unittest.TestCase):
    """Active #6: cap exposure on correlated over lines on the same game.

    Historical evidence shows multiple over-side bets on the same game
    (e.g. O7.5 + O8.5) effectively double exposure on one trade idea.
    Verifies both the count cap and the spacing cap.
    """

    def _engine(self, *, max_lines=2, min_gap=1.5, existing_bets=None):
        engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
        engine._bets = list(existing_bets or [])
        engine.live_args = SimpleNamespace(
            max_correlated_over_lines_per_game=max_lines,
            min_correlated_line_gap=min_gap,
        )
        return engine

    def _game(self, game_pk=1, away="MIN", home="CLE"):
        return SimpleNamespace(
            game_pk=game_pk, away_abbrev=away, home_abbrev=home
        )

    def _market(self, line="8.5"):
        return SimpleNamespace(line=line)

    def test_no_cap_fires_when_no_prior_same_game_bets(self):
        engine = self._engine()
        result = engine._evaluate_correlated_line_cap(
            game=self._game(),
            market=self._market(line="7.5"),
        )
        self.assertIsNone(result)

    def test_spacing_cap_blocks_adjacent_over_line(self):
        # Existing O7.5 (filled), trying O8.5 (gap=1.0 < 1.5) -- block.
        prior = _live_bet(
            bet_id="prior", game_pk=1, line="7.5", side="over",
            order_status="filled", actual_fill_price=0.75,
            fill_size=20.0, fill_cost=15.0,
        )
        engine = self._engine(existing_bets=[prior])
        result = engine._evaluate_correlated_line_cap(
            game=self._game(),
            market=self._market(line="8.5"),
        )
        self.assertEqual(result, "correlated_line_gap_cap")

    def test_spacing_cap_allows_distant_over_line(self):
        # Existing O7.5, trying O9.5 (gap=2.0 >= 1.5) -- allow.
        prior = _live_bet(
            bet_id="prior", game_pk=1, line="7.5", side="over",
            order_status="filled",
        )
        engine = self._engine(existing_bets=[prior])
        result = engine._evaluate_correlated_line_cap(
            game=self._game(),
            market=self._market(line="9.5"),
        )
        self.assertIsNone(result)

    def test_count_cap_blocks_third_over_bet(self):
        # 2 existing over bets at distant lines -- count cap (max=2) blocks
        # any further over placement regardless of spacing.
        prior_a = _live_bet(
            bet_id="a", game_pk=1, line="7.5", side="over",
            order_status="filled",
        )
        prior_b = _live_bet(
            bet_id="b", game_pk=1, line="10.5", side="over",
            order_status="live",
        )
        engine = self._engine(existing_bets=[prior_a, prior_b])
        result = engine._evaluate_correlated_line_cap(
            game=self._game(),
            market=self._market(line="13.5"),  # far away, but count cap fires
        )
        self.assertEqual(result, "correlated_line_count_cap")

    def test_cap_ignores_bets_on_other_games(self):
        prior = _live_bet(
            bet_id="other-game", game_pk=999, line="8.5", side="over",
            order_status="filled",
        )
        engine = self._engine(existing_bets=[prior])
        result = engine._evaluate_correlated_line_cap(
            game=self._game(game_pk=1),
            market=self._market(line="8.5"),
        )
        self.assertIsNone(result)

    def test_cap_ignores_fully_cancelled_bets_with_no_exposure(self):
        # A bet that cancelled before reaching fill or open should not
        # count toward the cap -- no exposure was ever taken.
        prior = _live_bet(
            bet_id="bailed", game_pk=1, line="7.5", side="over",
            order_status="cancelled",
        )
        engine = self._engine(existing_bets=[prior])
        result = engine._evaluate_correlated_line_cap(
            game=self._game(),
            market=self._market(line="8.5"),
        )
        self.assertIsNone(result)

    def test_cap_counts_live_open_orders_toward_count(self):
        # Two open (live) orders should already trip the count cap even
        # before either has filled.
        a = _live_bet(bet_id="a", game_pk=1, line="7.5", side="over", order_status="live")
        b = _live_bet(bet_id="b", game_pk=1, line="10.5", side="over", order_status="live")
        engine = self._engine(existing_bets=[a, b])
        result = engine._evaluate_correlated_line_cap(
            game=self._game(),
            market=self._market(line="13.5"),
        )
        self.assertEqual(result, "correlated_line_count_cap")

    def test_disabling_both_caps_returns_none(self):
        # max=0 AND gap=0 -> caps fully disabled.
        prior = _live_bet(
            bet_id="prior", game_pk=1, line="7.5", side="over",
            order_status="filled",
        )
        engine = self._engine(
            max_lines=0, min_gap=0.0, existing_bets=[prior]
        )
        result = engine._evaluate_correlated_line_cap(
            game=self._game(),
            market=self._market(line="8.5"),
        )
        self.assertIsNone(result)

    def test_cli_args_flow_into_live_args(self):
        # Verify the new CLI flags get parsed and end up on the live_args
        # namespace under the names the gate looks for.
        live_args, _trade_args, _ = le.parse_live_args([
            "--dry-run", "--stake", "10",
            "--max-correlated-over-lines-per-game", "1",
            "--min-correlated-line-gap", "2.0",
        ])
        self.assertEqual(live_args.max_correlated_over_lines_per_game, 1)
        self.assertAlmostEqual(live_args.min_correlated_line_gap, 2.0)

    def test_defaults_block_the_historical_o7_5_plus_o8_5_pattern(self):
        # 2026-05-08 game 823466 was the canonical example: an O7.5 fill
        # at $10 followed by an O8.5 fill at $10. With defaults
        # (max=2, gap=1.5) the second placement should be blocked.
        live_args, _trade_args, _ = le.parse_live_args(["--dry-run"])
        engine = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
        engine.live_args = live_args
        engine._bets = [_live_bet(
            bet_id="historical_o7_5",
            game_pk=823466, line="7.5", side="over",
            order_status="filled",
        )]
        result = engine._evaluate_correlated_line_cap(
            game=self._game(game_pk=823466),
            market=self._market(line="8.5"),
        )
        self.assertEqual(result, "correlated_line_gap_cap")


class ScopedAltAEnforceTests(unittest.TestCase):
    """Active #17 (2026-05-21) -- _apply_stage1_alt_a_scope behaviors.

    Three modes (off / shadow / enforce) x two cohorts (inning>=8
    hold-poisson, default apply). When mode=enforce + cohort=apply +
    alt-empirical-FV available, FV gets swapped.
    """

    def _engine(self, scope_mode="enforce"):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "trading"))
        import signal_engine as se
        engine = se.SignalEngine.__new__(se.SignalEngine)
        engine._stage1_alt_a_scope_mode = scope_mode
        return engine

    def _call_scope(self, engine, *, payload, best_fv, inning):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "trading"))
        import signal_pipeline_gates_post_fv as gpf
        return gpf._apply_stage1_alt_a_scope(
            engine,
            candidate_payload=payload,
            current_best_fv=best_fv,
            inning=inning,
        )

    def test_mode_off_writes_only_mode_tag(self):
        engine = self._engine(scope_mode="off")
        payload = {}
        result = self._call_scope(
            engine, payload=payload, best_fv=0.98, inning=6,
        )
        self.assertEqual(result, 0.98)
        self.assertEqual(payload["stage1_alt_a_scope_mode"], "off")
        self.assertEqual(payload["stage1_alt_a_scope_decision"], "mode_off")
        # FV unchanged
        self.assertNotIn("fair_value", payload)

    def test_shadow_logs_decision_but_does_not_swap(self):
        engine = self._engine(scope_mode="shadow")
        payload = {
            "fair_value_alt_empirical": 0.88,
            "fair_value_alt_empirical_used_empirical": True,
        }
        result = self._call_scope(
            engine, payload=payload, best_fv=0.98, inning=6,
        )
        # No swap even though alt available
        self.assertEqual(result, 0.98)
        self.assertEqual(payload["stage1_alt_a_scope_decision"], "would_apply_in_shadow")

    def test_enforce_swaps_fv_on_apply_rule(self):
        engine = self._engine(scope_mode="enforce")
        payload = {
            "fair_value_alt_empirical": 0.88,
            "fair_value_alt_empirical_used_empirical": True,
        }
        result = self._call_scope(
            engine, payload=payload, best_fv=0.98, inning=6,
        )
        # Default rule = apply -> swap
        self.assertAlmostEqual(result, 0.88, places=6)
        self.assertEqual(payload["fair_value"], 0.88)
        self.assertEqual(payload["inferred_state_base_source"], "empirical_scoped_alt_a")
        self.assertEqual(payload["stage1_alt_a_scope_decision"], "applied")
        self.assertEqual(payload["stage1_alt_a_scope_action"], "apply")

    def test_enforce_holds_poisson_on_inning_gte_8_regression_cohort(self):
        engine = self._engine(scope_mode="enforce")
        payload = {
            "fair_value_alt_empirical": 0.88,
            "fair_value_alt_empirical_used_empirical": True,
        }
        # Inning 9 -> hold_poisson rule fires
        result = self._call_scope(
            engine, payload=payload, best_fv=0.98, inning=9,
        )
        self.assertEqual(result, 0.98)  # Poisson kept
        self.assertNotIn("fair_value", payload)  # not swapped
        self.assertEqual(payload["stage1_alt_a_scope_decision"], "held_poisson_enforce")
        self.assertEqual(payload["stage1_alt_a_scope_action"], "hold_poisson")
        self.assertEqual(
            payload["stage1_alt_a_scope_rule_matched"],
            "inning_gte_8_regression",
        )

    def test_enforce_holds_when_alt_fv_missing(self):
        # Apply-cohort but alt_fv wasn't computed (e.g., shadow mode off
        # OR boundary-empirical guard blocked). Must keep Poisson.
        engine = self._engine(scope_mode="enforce")
        payload = {
            "fair_value_alt_empirical": None,
            "fair_value_alt_empirical_used_empirical": False,
        }
        result = self._call_scope(
            engine, payload=payload, best_fv=0.98, inning=6,
        )
        self.assertEqual(result, 0.98)
        self.assertEqual(payload["stage1_alt_a_scope_decision"], "alt_fv_unavailable")


class PaperCorrelatedLineCapTests(unittest.TestCase):
    """2026-05-21 (P1c) -- correlated-line cap was previously live-only.
    Paper bets had no protection, so the 2026-05-20 paper session bet
    HOU@MIN twice (both lost), MIL@CHC twice (both lost), and BOS@KC
    three times. Lifted the cap into SignalEngine. These tests assert
    paper inherits the protection via the parent class.
    """

    def _paper_engine(self, *, max_lines=2, min_gap=1.5, existing_bets=None):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "trading"))
        import signal_engine as se
        engine = se.SignalEngine.__new__(se.SignalEngine)
        engine._bets = list(existing_bets or [])
        engine.trade_args = SimpleNamespace(
            max_correlated_over_lines_per_game=max_lines,
            min_correlated_line_gap=min_gap,
        )
        return engine

    def _paper_bet(self, *, game_pk, line, side="over"):
        return SimpleNamespace(game_pk=game_pk, line=line, side=side)

    def _game(self, game_pk=1, away="MIN", home="CLE"):
        return SimpleNamespace(game_pk=game_pk, away_abbrev=away, home_abbrev=home)

    def _market(self, line="8.5"):
        return SimpleNamespace(line=line)

    def test_paper_spacing_cap_blocks_adjacent_line(self):
        # Re-creates the 2026-05-20 HOU@MIN failure: O7.5 placed,
        # then O6.5 (gap=1.0 < 1.5) should now be blocked.
        prior = self._paper_bet(game_pk=1, line="7.5", side="over")
        engine = self._paper_engine(existing_bets=[prior])
        result = engine._evaluate_correlated_line_cap(
            game=self._game(game_pk=1),
            market=self._market(line="6.5"),
        )
        self.assertEqual(result, "correlated_line_gap_cap")

    def test_paper_count_cap_blocks_third_bet(self):
        # Re-creates the 2026-05-20 BOS@KC failure: 3 bets on the
        # same game. Third should be blocked by count cap (default 2).
        prior_a = self._paper_bet(game_pk=1, line="6.5", side="over")
        prior_b = self._paper_bet(game_pk=1, line="8.5", side="over")
        engine = self._paper_engine(existing_bets=[prior_a, prior_b])
        result = engine._evaluate_correlated_line_cap(
            game=self._game(game_pk=1),
            market=self._market(line="10.5"),  # spacing OK but count cap fires
        )
        self.assertEqual(result, "correlated_line_count_cap")

    def test_paper_caps_skip_when_disabled(self):
        prior = self._paper_bet(game_pk=1, line="7.5", side="over")
        engine = self._paper_engine(
            max_lines=0, min_gap=0.0, existing_bets=[prior],
        )
        result = engine._evaluate_correlated_line_cap(
            game=self._game(game_pk=1),
            market=self._market(line="8.5"),
        )
        self.assertIsNone(result)

    def test_paper_caps_no_cross_game_interference(self):
        # Bet on game 1 doesn't affect game 2.
        prior = self._paper_bet(game_pk=1, line="7.5", side="over")
        engine = self._paper_engine(existing_bets=[prior])
        result = engine._evaluate_correlated_line_cap(
            game=self._game(game_pk=2),
            market=self._market(line="7.5"),
        )
        self.assertIsNone(result)


class CalibratedStakeMultiplierTests(unittest.TestCase):
    """Active #6 part 2: pure-function tests for the calibrated-edge
    multiplier and the calibrated-edge resolution helper."""

    def setUp(self):
        # Import in setUp so we don't pay it at module load (the helper
        # lives next to compute_stake in live_pricing).
        import live_pricing as lp  # noqa: E402

        self.lp = lp

    def test_multiplier_at_zero_edge_clamps_to_min(self):
        m = self.lp.calibrated_stake_multiplier(
            calibrated_edge=0.0,
            min_multiplier=0.5,
            max_multiplier=1.5,
            ramp_top_edge=0.15,
        )
        self.assertEqual(m, 0.5)

    def test_multiplier_at_ramp_top_clamps_to_max(self):
        m = self.lp.calibrated_stake_multiplier(
            calibrated_edge=0.15,
            min_multiplier=0.5,
            max_multiplier=1.5,
            ramp_top_edge=0.15,
        )
        self.assertEqual(m, 1.5)

    def test_multiplier_at_half_ramp_is_midpoint(self):
        # At edge = 0.075, halfway between 0 and 0.15, multiplier should be 1.0.
        m = self.lp.calibrated_stake_multiplier(
            calibrated_edge=0.075,
            min_multiplier=0.5,
            max_multiplier=1.5,
            ramp_top_edge=0.15,
        )
        self.assertAlmostEqual(m, 1.0, places=6)

    def test_negative_edge_uses_min(self):
        m = self.lp.calibrated_stake_multiplier(
            calibrated_edge=-0.10,
            min_multiplier=0.5,
            max_multiplier=1.5,
            ramp_top_edge=0.15,
        )
        self.assertEqual(m, 0.5)

    def test_edge_above_ramp_clamps_to_max(self):
        m = self.lp.calibrated_stake_multiplier(
            calibrated_edge=0.50,
            min_multiplier=0.5,
            max_multiplier=1.5,
            ramp_top_edge=0.15,
        )
        self.assertEqual(m, 1.5)

    def test_resolve_calibrated_edge_returns_none_without_calibrator(self):
        engine = SimpleNamespace(
            _prob_calibrator=None,
            _prob_calibration_mode="off",
        )
        result = self.lp.resolve_calibrated_edge(
            engine, raw_or_final_fv=0.85, decision_ask=0.70,
            model_family="score_event_transition",
        )
        self.assertIsNone(result)

    def test_resolve_calibrated_edge_calls_calibrator_in_shadow_mode(self):
        # Mode = shadow -> raw_or_final_fv is RAW; we must call calibrate
        # to get the calibrated value before computing the edge.
        calls = []

        class FakeCal:
            def calibrate(self, prob, model_family=None):
                calls.append((prob, model_family))
                return 0.72  # shrunk from 0.85

        engine = SimpleNamespace(
            _prob_calibrator=FakeCal(),
            _prob_calibration_mode="shadow",
        )
        result = self.lp.resolve_calibrated_edge(
            engine, raw_or_final_fv=0.85, decision_ask=0.70,
            model_family="score_event_transition",
        )
        self.assertAlmostEqual(result, 0.02, places=6)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], (0.85, "score_event_transition"))

    def test_resolve_calibrated_edge_skips_calibration_in_enforce_mode(self):
        # Mode = enforce -> raw_or_final_fv IS the calibrated value
        # already; calling calibrate again would double-calibrate.
        class FakeCal:
            def calibrate(self, prob, model_family=None):
                raise AssertionError("must not call calibrate in enforce mode")

        engine = SimpleNamespace(
            _prob_calibrator=FakeCal(),
            _prob_calibration_mode="enforce",
        )
        result = self.lp.resolve_calibrated_edge(
            engine, raw_or_final_fv=0.72, decision_ask=0.70,
            model_family="score_event_transition",
        )
        self.assertAlmostEqual(result, 0.02, places=6)


class CalibratedStakeScalingPlacementTests(unittest.TestCase):
    """Active #6 part 2: integration test verifying the multiplier reaches
    the bet record (shadow mode logs only; enforce changes stake)."""

    def test_cli_args_flow_into_live_args(self):
        live_args, _trade_args, _ = le.parse_live_args([
            "--dry-run", "--stake", "10",
            "--calibrated-stake-scale-mode", "enforce",
            "--calibrated-stake-min-multiplier", "0.25",
            "--calibrated-stake-max-multiplier", "2.0",
            "--calibrated-stake-ramp-top-edge", "0.20",
        ])
        self.assertEqual(live_args.calibrated_stake_scale_mode, "enforce")
        self.assertAlmostEqual(live_args.calibrated_stake_min_multiplier, 0.25)
        self.assertAlmostEqual(live_args.calibrated_stake_max_multiplier, 2.0)
        self.assertAlmostEqual(live_args.calibrated_stake_ramp_top_edge, 0.20)

    def test_default_mode_is_shadow(self):
        live_args, _, _ = le.parse_live_args(["--dry-run", "--stake", "10"])
        self.assertEqual(live_args.calibrated_stake_scale_mode, "shadow")

    def test_invalid_min_max_inversion_errors(self):
        with self.assertRaises(SystemExit):
            le.parse_live_args([
                "--dry-run", "--stake", "10",
                "--calibrated-stake-min-multiplier", "1.5",
                "--calibrated-stake-max-multiplier", "0.5",
            ])


if __name__ == "__main__":
    unittest.main()
