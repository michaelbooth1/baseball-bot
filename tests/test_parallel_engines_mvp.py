import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace


PROJECT_DIR = Path(__file__).resolve().parents[1]
TRADING_DIR = PROJECT_DIR / "scripts" / "trading"
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
for path in (TRADING_DIR, ANALYSIS_DIR, PROJECT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import aggregate_parallel_engines as ape  # noqa: E402
import build_walk_forward_certification as bwfc  # noqa: E402
import candidate_schema_enrichment as cse  # noqa: E402
import capture_helpers as ch  # noqa: E402
import launch_parallel_engines as lpe  # noqa: E402
import paper_engine_consumer as pec  # noqa: E402
import signal_config as sc  # noqa: E402
import shared_capture as scp  # noqa: E402
import shared_market_data as smd  # noqa: E402
import walk_forward_runner as wfr  # noqa: E402
from models import BetRecord  # noqa: E402
from monitor_models import GameMarketMatch, OUMarket, ScheduledGame, ScheduleScore  # noqa: E402
from scripts.analysis.unified_signal_table.loaders import load_captures_for_mode  # noqa: E402


def _base_bet(**overrides):
    payload = {
        "bet_id": "bet_1",
        "placed_at": "2026-05-24T12:00:00Z",
        "game_pk": 1,
        "away_abbrev": "AWY",
        "home_abbrev": "HOM",
        "line": "8.5",
        "side": "over",
        "entry_ask": 0.75,
        "fair_value": 0.86,
        "base_fair_value": 0.84,
        "stage2_run_env_delta": 0.0,
        "team_offense_delta": 0.0,
        "edge": 0.11,
        "inferred_runs": 1,
        "inning": 6,
        "inning_state": "Top",
        "outs": 1,
        "away_score_before": 3,
        "home_score_before": 3,
        "inferred_away_after": 4,
        "inferred_home_after": 3,
        "stake": 10.0,
        "runners_on": 0,
    }
    payload.update(overrides)
    return payload


class ParallelEnginesMvpTests(unittest.TestCase):
    def test_config_label_parses_and_reaches_candidate_rows_and_bet_json(self):
        trade_args, _ = sc.parse_trade_args(["--config-label", "A_current"])
        self.assertEqual(trade_args.config_label, "A_current")

        row = {
            "session_date": "2026-05-24",
            "game_pk": 1,
            "line": "8.5",
            "decision_ask": 0.74,
            "edge": 0.12,
        }
        engine = SimpleNamespace(
            date_str="2026-05-24",
            trade_args=SimpleNamespace(config_label="A_current", extreme_edge_max=0.22),
        )
        cse.attach_modeling_observability_fields(engine, row)
        self.assertEqual(row["config_label"], "A_current")

        bet = BetRecord(**_base_bet(config_label="B_cal_only"))
        self.assertEqual(asdict(bet)["config_label"], "B_cal_only")

    def test_five_preset_2x2_factorial_plus_tight_edge(self):
        # 2026-05-25 expansion: D_scope_only completes the scope x
        # calibrator 2x2 (A: cal+scope, B: cal only, C: neither,
        # D: scope only). E_tight_edge tests the edge-threshold lever
        # at +5pp tighter than A_current.
        d_flags = lpe.PRESETS["D_scope_only"]
        self.assertIn("--prob-calibration-mode", d_flags)
        self.assertIn("--stage1-alt-a-scope-mode", d_flags)
        # cal=shadow, scope=enforce
        cal_idx = d_flags.index("--prob-calibration-mode")
        scope_idx = d_flags.index("--stage1-alt-a-scope-mode")
        self.assertEqual(d_flags[cal_idx + 1], "shadow")
        self.assertEqual(d_flags[scope_idx + 1], "enforce")

        e_flags = lpe.PRESETS["E_tight_edge"]
        self.assertIn("--edge-threshold", e_flags)
        et_idx = e_flags.index("--edge-threshold")
        self.assertEqual(e_flags[et_idx + 1], "0.20")
        eth_idx = e_flags.index("--edge-threshold-high-line")
        self.assertEqual(e_flags[eth_idx + 1], "0.21")
        # E should still enforce both cal and scope (it's A_current
        # plus a tighter edge gate); test that.
        e_cal_idx = e_flags.index("--prob-calibration-mode")
        e_scope_idx = e_flags.index("--stage1-alt-a-scope-mode")
        self.assertEqual(e_flags[e_cal_idx + 1], "enforce")
        self.assertEqual(e_flags[e_scope_idx + 1], "enforce")

    def test_fourteen_preset_default_includes_k_l_m_n(self):
        """2026-05-26: K_line5p5_block. 2026-05-28: L_enforce_min_raw_095 +
        M_under_paper. 2026-06-01: N_extreme_edge_022 (A/B vs the
        gate_extreme_edge-disabled live default). Default = 14 configs."""
        self.assertEqual(
            set(lpe.PRESETS.keys()),
            {
                "A_current", "B_cal_only", "C_raw", "D_scope_only", "E_tight_edge",
                "F_no_dedup", "G_loose_edge", "H_late_innings",
                "I_extreme_018", "J_no_phantom_filter", "K_line5p5_block",
                "L_enforce_min_raw_095", "M_under_paper", "N_extreme_edge_022",
            },
        )

    def test_n_extreme_edge_022_preset_flags(self):
        """N_extreme_edge_022: A_current baseline + --extreme-edge-max 0.22
        (re-enables the TR17/TR18 cap disabled in live since 2026-05-28).
        No live-only flags; pure paper A/B."""
        flags = lpe.PRESETS["N_extreme_edge_022"]
        self.assertEqual(flags[flags.index("--extreme-edge-max") + 1], "0.22")
        # Sanity: must mirror A_current's other lever choices.
        self.assertEqual(flags[flags.index("--prob-calibration-mode") + 1], "enforce")
        self.assertEqual(flags[flags.index("--stage1-alt-a-scope-mode") + 1], "enforce")
        self.assertEqual(flags[flags.index("--under-emission-mode") + 1], "shadow")
        # Must NOT carry any live-only flag.
        for f in flags:
            if not f.startswith("--"):
                continue
            self.assertNotIn(
                f, lpe.LIVE_ONLY_ENGINE_FLAGS,
                f"N_extreme_edge_022 contains LIVE_ONLY flag {f}",
            )

    def test_m_under_paper_preset_flags(self):
        """M_under_paper: A_current baseline + --under-mode paper (paper
        mirror of live UNDER; no live-only flags)."""
        flags = lpe.PRESETS["M_under_paper"]
        self.assertEqual(flags[flags.index("--under-mode") + 1], "paper")
        self.assertEqual(flags[flags.index("--prob-calibration-mode") + 1], "enforce")
        for f in flags:
            if not f.startswith("--"):
                continue
            self.assertNotIn(
                f, lpe.LIVE_ONLY_ENGINE_FLAGS,
                f"M_under_paper contains LIVE_ONLY flag {f}",
            )

    def test_l_enforce_min_raw_095_preset_flags(self):
        """L_enforce_min_raw_095: A_current baseline + enforce_min_raw raised
        0.90 -> 0.95 (single varying knob vs A, mirrors I/J/K pattern)."""
        flags = lpe.PRESETS["L_enforce_min_raw_095"]
        self.assertEqual(
            flags[flags.index("--prob-calibration-enforce-min-raw") + 1], "0.95"
        )
        # Inherits A's enforce/enforce baseline so the only varying dimension
        # vs A_current is the band-gate threshold.
        self.assertEqual(
            flags[flags.index("--prob-calibration-mode") + 1], "enforce"
        )
        self.assertEqual(
            flags[flags.index("--stage1-alt-a-scope-mode") + 1], "enforce"
        )
        # Must pass the live-only safety check (paper-safe flag only).
        for f in flags:
            if not f.startswith("--"):
                continue
            self.assertNotIn(
                f, lpe.LIVE_ONLY_ENGINE_FLAGS,
                f"L_enforce_min_raw_095 contains LIVE_ONLY flag {f}",
            )

    def test_k_line5p5_block_preset_flags(self):
        """K_line5p5_block: A_current baseline + line-5.5 high-FV guard
        enforced. Mirrors I/J pattern (single varying knob vs A)."""
        flags = lpe.PRESETS["K_line5p5_block"]
        self.assertEqual(
            flags[flags.index("--line-high-fv-block-mode") + 1], "enforce"
        )
        self.assertEqual(
            flags[flags.index("--line-high-fv-block-min-raw-fv") + 1], "0.90"
        )
        self.assertEqual(
            flags[flags.index("--line-high-fv-block-lines") + 1], "5.5"
        )
        # Inherits A's enforce/enforce baseline so the only varying
        # dimension vs A is the line-5.5 guard.
        self.assertEqual(
            flags[flags.index("--prob-calibration-mode") + 1], "enforce"
        )
        self.assertEqual(
            flags[flags.index("--stage1-alt-a-scope-mode") + 1], "enforce"
        )
        # K must also pass the live-only safety check.
        for f in flags:
            if not f.startswith("--"):
                continue
            self.assertNotIn(
                f, lpe.LIVE_ONLY_ENGINE_FLAGS,
                f"K_line5p5_block contains LIVE_ONLY flag {f}",
            )

    def test_f_no_dedup_strips_every_dedup_knob(self):
        """F_no_dedup: zero per-event cooldown, zero inning gap,
        correlated-line cap effectively off. Edge/ask floors remain."""
        flags = lpe.PRESETS["F_no_dedup"]
        # Dedup knobs all zero/off.
        self.assertEqual(flags[flags.index("--event-dedup-secs") + 1], "0")
        self.assertEqual(flags[flags.index("--inning-dedup-gap") + 1], "0")
        self.assertEqual(
            flags[flags.index("--max-correlated-over-lines-per-game") + 1],
            "999",
        )
        self.assertEqual(
            flags[flags.index("--min-correlated-line-gap") + 1], "0.0"
        )
        # Calibrator + scope still enforce (operator wants only dedup loosened).
        self.assertEqual(flags[flags.index("--prob-calibration-mode") + 1], "enforce")
        self.assertEqual(flags[flags.index("--stage1-alt-a-scope-mode") + 1], "enforce")
        # No edge override -> default floor stays in play.
        self.assertNotIn("--edge-threshold", flags)

    def test_g_loose_edge_lowers_both_edge_floors_5pp(self):
        flags = lpe.PRESETS["G_loose_edge"]
        self.assertEqual(flags[flags.index("--edge-threshold") + 1], "0.10")
        self.assertEqual(
            flags[flags.index("--edge-threshold-high-line") + 1], "0.11"
        )

    def test_h_late_innings_sets_min_inning_6_on_both_tiers(self):
        flags = lpe.PRESETS["H_late_innings"]
        self.assertEqual(flags[flags.index("--min-inning") + 1], "6")
        self.assertEqual(flags[flags.index("--min-inning-high-line") + 1], "6")

    def test_i_and_j_form_extreme_edge_sweep(self):
        """I (0.18 tightened) and J (1.0 = off) pair with A's 0.22 to
        form a clean 3-point sweep of the TR19 extreme-edge knob.
        Phase 6 prep for the 2026-06-07 recalibration deadline."""
        i_flags = lpe.PRESETS["I_extreme_018"]
        j_flags = lpe.PRESETS["J_no_phantom_filter"]
        self.assertEqual(
            i_flags[i_flags.index("--extreme-edge-max") + 1], "0.18"
        )
        self.assertEqual(
            j_flags[j_flags.index("--extreme-edge-max") + 1], "1.0"
        )
        # Both inherit A's enforce/enforce baseline so the only varying
        # dimension is the extreme_edge_max knob.
        for flags in (i_flags, j_flags):
            self.assertEqual(flags[flags.index("--prob-calibration-mode") + 1], "enforce")
            self.assertEqual(flags[flags.index("--stage1-alt-a-scope-mode") + 1], "enforce")

    def test_no_f_through_j_uses_live_only_flag(self):
        """F-J must only use paper-safe flags (none in LIVE_ONLY_ENGINE_FLAGS).
        Catches regressions where someone adds a Kelly / daily-budget /
        stake-mode flag to a preset thinking paper supports it."""
        for label in ("F_no_dedup", "G_loose_edge", "H_late_innings",
                      "I_extreme_018", "J_no_phantom_filter"):
            flags = lpe.PRESETS[label]
            for f in flags:
                if not f.startswith("--"):
                    continue
                self.assertNotIn(
                    f, lpe.LIVE_ONLY_ENGINE_FLAGS,
                    f"Preset {label} contains LIVE_ONLY flag {f}; "
                    "paper_trader.py will reject the run.",
                )

    def test_launcher_builds_isolated_commands_from_presets(self):
        with tempfile.TemporaryDirectory() as td:
            args = lpe.parse_args(
                [
                    "--config", "A_current",
                    "--config", "B:enforce_shadow",
                    "--paper-root-prefix", str(Path(td) / "paper_"),
                    "--stake", "10",
                    "--dry-launch",
                ]
            )
            configs = [lpe._resolve_config(raw, Path(args.paper_root_prefix)) for raw in args.config]
            cmds = [lpe.build_engine_command(args, cfg) for cfg in configs]

        self.assertIn("--config-label", cmds[0])
        self.assertIn("A_current", cmds[0])
        self.assertIn("--paper-root", cmds[1])
        self.assertIn("B", cmds[1])
        self.assertIn("--stage1-alt-a-scope-mode", cmds[0])
        self.assertIn("enforce", cmds[0])
        self.assertIn("shadow", cmds[1])
        self.assertIn("--no-startup-refresh", cmds[0])

    def test_launcher_builds_shared_watcher_and_consumer_commands(self):
        with tempfile.TemporaryDirectory() as td:
            args = lpe.parse_args(
                [
                    "--config", "A_current",
                    "--paper-root-prefix", str(Path(td) / "paper_"),
                    "--stake", "10",
                    "--dry-launch",
                ]
            )
            cfg = lpe._resolve_config("A_current", Path(args.paper_root_prefix))
            watcher = lpe.build_watcher_command(
                args,
                bus_host="127.0.0.1",
                bus_port=12345,
                bus_authkey="secret",
                ready_file=Path(td) / "ready.json",
                expected_consumers=1,
                capture_bus_host="127.0.0.1",
                capture_bus_port=12346,
                capture_bus_authkey="capture_secret",
            )
            consumer = lpe.build_consumer_command(
                args,
                cfg,
                bus_host="127.0.0.1",
                bus_port=12345,
                bus_authkey="secret",
                capture_bus_host="127.0.0.1",
                capture_bus_port=12346,
                capture_bus_authkey="capture_secret",
                watcher_pid=111,
            )
        self.assertIn("shared_market_watcher.py", " ".join(watcher))
        self.assertIn("--output-root", watcher)
        self.assertIn("--expected-consumers", watcher)
        self.assertIn("--capture-bus-port", watcher)
        self.assertIn("paper_engine_consumer.py", " ".join(consumer))
        self.assertIn("--bus-port", consumer)
        self.assertIn("--capture-bus-port", consumer)
        self.assertIn("--config-label", consumer)

    def test_shared_capture_depth_cache_reuses_bucket(self):
        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "last_trade_price": "0.71",
                    "bids": [{"price": "0.69", "size": "12"}],
                    "asks": [{"price": "0.72", "size": "8"}],
                }

        class FakeSession:
            def __init__(self):
                self.calls = 0

            def get(self, *_args, **_kwargs):
                self.calls += 1
                return FakeResponse()

        class FakeBookClient:
            timeout = 1.0

            def __init__(self):
                self.session = FakeSession()

            def _session(self):
                return self.session

        with tempfile.TemporaryDirectory() as td:
            book_client = FakeBookClient()
            port = lpe._pick_free_port()
            server = scp.SharedCaptureServer(
                host="127.0.0.1",
                port=port,
                authkey="secret",
                book_client=book_client,
                output_root=Path(td),
                date_str="2026-05-24",
            )
            server.start()
            client = scp.SharedCaptureClient(
                host="127.0.0.1",
                port=port,
                authkey="secret",
                timeout_secs=2.0,
            )
            try:
                first_snapshot = client.fetch_depth_snapshot(token_id="tok", depth=5, bucket_ms=10000)
                second_snapshot = client.fetch_depth_snapshot(token_id="tok", depth=5, bucket_ms=10000)
                stats = client.stats()
            finally:
                server.close()
        self.assertIsNotNone(first_snapshot)
        self.assertIsNotNone(second_snapshot)
        self.assertEqual(stats["responses_ok"], 2)
        self.assertEqual(stats["cache_hits"], 1)
        self.assertEqual(book_client.session.calls, 1)
        self.assertEqual(second_snapshot["best_ask"], 0.72)

    def test_capture_helpers_use_shared_depth_and_write_book_pointer(self):
        class FakeSharedClient:
            def __init__(self, shared_path: Path):
                self.shared_path = shared_path
                self.depth_calls = 0
                self.book_calls = 0

            def stats(self):
                return {"requests": self.depth_calls + self.book_calls}

            def fetch_depth_snapshot(self, **_kwargs):
                self.depth_calls += 1
                return {"ok": True, "best_bid": 0.68, "best_ask": 0.71, "top_bids": [], "top_asks": []}

            def start_book_capture(self, **_kwargs):
                self.book_calls += 1
                return {
                    "ok": True,
                    "cache_hit": self.book_calls > 1,
                    "shared_capture_id": "shared_abc",
                    "shared_capture_path": str(self.shared_path),
                }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shared_path = root / "shared.jsonl"
            engine = SimpleNamespace(
                date_str="2026-05-24",
                trade_args=SimpleNamespace(
                    paper_root=root / "paper",
                    capture_duration=2.0,
                    capture_interval=1.0,
                    capture_depth=5,
                ),
                _shared_capture_client=FakeSharedClient(shared_path),
                _market_data_health={},
            )
            depth = ch.fetch_depth_snapshot(engine, "tok", 5)
            self.assertEqual(depth["best_ask"], 0.71)
            self.assertEqual(depth["shared_capture_source"], "watcher")

            bet = SimpleNamespace(
                bet_id="bet1",
                placed_at="2026-05-24T12:00:00Z",
                game_pk=1,
                away_abbrev="AWY",
                home_abbrev="HOM",
                line="8.5",
                inning=6,
                inning_state="Top",
                outs=1,
                away_score_before=3,
                home_score_before=3,
                entry_ask=0.72,
                fair_value=0.82,
                base_fair_value=0.80,
                stage2_run_env_delta=0.0,
                team_offense_delta=0.0,
                edge=0.10,
            )
            ch.start_book_capture(
                engine,
                bet=bet,
                token_id="tok",
                initial_book={"ok": True, "best_bid": 0.68, "best_ask": 0.72},
                signal_ts=1000.0,
            )
            pointer = root / "paper" / "book_captures" / "2026-05-24" / "bet1.jsonl"
            rows = [json.loads(line) for line in pointer.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(rows[0]["shared_capture_pointer"])
        self.assertEqual(rows[1]["type"], "shared_capture_pointer")
        self.assertEqual(rows[1]["shared_capture_id"], "shared_abc")

    def test_unified_loader_follows_shared_capture_pointer(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shared = root / "shared_book_captures" / "2026-05-24"
            shared.mkdir(parents=True)
            shared_file = shared / "cap1.jsonl"
            shared_file.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "signal", "bet_id": "shared_cap1"}),
                        json.dumps({
                            "type": "snapshot",
                            "seq": 0,
                            "elapsed_s": 0.0,
                            "ts": "2026-05-24T12:00:00Z",
                            "book": {"ok": True, "best_bid": 0.68, "best_ask": 0.72},
                        }),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            local = root / "paper" / "book_captures" / "2026-05-24"
            local.mkdir(parents=True)
            (local / "bet1.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({
                            "type": "signal",
                            "bet_id": "bet1",
                            "shared_capture_pointer": True,
                            "shared_capture_path": str(shared_file),
                        }),
                        json.dumps({
                            "type": "shared_capture_pointer",
                            "bet_id": "bet1",
                            "shared_capture_path": str(shared_file),
                        }),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            warnings = []
            hard_errors = []
            captures = load_captures_for_mode(
                "paper",
                root / "paper" / "book_captures",
                "2026-05-24",
                "2026-05-24",
                warnings,
                hard_errors,
            )
        self.assertEqual(warnings, [])
        self.assertEqual(hard_errors, [])
        self.assertIn("bet1", captures)
        self.assertEqual(len(captures["bet1"].snapshots), 1)
        self.assertEqual(captures["bet1"].t0_book["best_ask"], 0.72)

    def test_launcher_rejects_live_only_flags(self):
        with self.assertRaises(SystemExit) as ctx:
            lpe.parse_args(["--config", "A_current", "--daily-budget", "80"])
        self.assertIn("live-execution-only", str(ctx.exception))

    def test_post_session_aggregate_flag_defaults_on(self):
        """Default is on so post-session reports never go stale."""
        args = lpe.parse_args(["--config", "A_current"])
        self.assertTrue(args.post_session_aggregate)

    def test_post_session_aggregate_flag_opts_out(self):
        """Explicit --no-post-session-aggregate flips the flag for debug runs."""
        args = lpe.parse_args(
            ["--config", "A_current", "--no-post-session-aggregate"]
        )
        self.assertFalse(args.post_session_aggregate)

    def test_post_session_aggregator_skips_when_disabled(self):
        """Helper is a no-op when post_session_aggregate=False (or dry_launch)."""
        import argparse as _argparse

        # The real helper writes to stdout; capture and assert it returned
        # without spawning the aggregator subprocess.
        from io import StringIO
        import contextlib

        # Disabled: no run, no print.
        args = _argparse.Namespace(
            post_session_aggregate=False,
            dry_launch=False,
            post_session_aggregate_script=Path("nonexistent.py"),
        )
        buf = StringIO()
        with contextlib.redirect_stdout(buf):
            lpe._run_post_session_aggregator(args, [], "2026-05-25")
        self.assertEqual(buf.getvalue(), "")

        # Dry-launch: same -- no run, no print.
        args.post_session_aggregate = True
        args.dry_launch = True
        buf = StringIO()
        with contextlib.redirect_stdout(buf):
            lpe._run_post_session_aggregator(args, [], "2026-05-25")
        self.assertEqual(buf.getvalue(), "")

    def test_post_session_aggregator_handles_missing_script(self):
        """Fail-open when the aggregator script path doesn't exist."""
        import argparse as _argparse
        from io import StringIO
        import contextlib

        args = _argparse.Namespace(
            post_session_aggregate=True,
            dry_launch=False,
            post_session_aggregate_script=Path("definitely_does_not_exist.py"),
        )
        buf = StringIO()
        with contextlib.redirect_stdout(buf):
            lpe._run_post_session_aggregator(args, [], "2026-05-25")
        output = buf.getvalue()
        self.assertIn("script not found", output)
        # Crucially: did NOT raise. Launcher exit code stays unchanged.

    def test_shared_market_data_round_trip_and_gap_stats(self):
        game = ScheduledGame(
            game_pk=123,
            game_date="2026-05-24T19:00:00Z",
            start_time_utc="2026-05-24T19:00:00Z",
            away_abbrev="AWY",
            home_abbrev="HOM",
            away_name="Away",
            home_name="Home",
            status_abstract="Live",
            status_detailed="In Progress",
            score=ScheduleScore(
                away=2,
                home=1,
                inning=5,
                inning_state="Top",
                outs=1,
                balls=0,
                strikes=0,
                runners_on=1,
                away_inning_runs=[1, 0, 1],
                home_inning_runs=[0, 1],
            ),
        )
        market = OUMarket(
            market_id="m1",
            question="Over 8.5",
            line="8.5",
            over_token_id="over",
            under_token_id="under",
        )
        match = GameMarketMatch(
            game_pk=123,
            event_slug="awy-hom",
            event_title="AWY at HOM",
            markets=[market],
        )
        payload = smd.encode_batch(
            sequence=3,
            date_str="2026-05-24",
            games={123: game},
            matches={123: match},
            active_games={123: True},
            tick_batch=[(game, market, "over_yes", {"book": {"ok": True, "best_ask": 0.71}})],
            health={"watcher_pid": 111},
        )
        games, matches, active = smd.decode_state(payload)
        ticks = smd.decode_tick_batch(payload)
        self.assertEqual(games[123].score.away, 2)
        self.assertEqual(matches[123].markets[0].line, "8.5")
        self.assertTrue(active[123])
        self.assertEqual(ticks[0][2], "over_yes")
        self.assertEqual(ticks[0][3]["book"]["best_ask"], 0.71)

        engine = SimpleNamespace(_market_data_health={"last_market_data_sequence": 1})
        pec._update_market_data_stats(engine, payload)
        self.assertEqual(engine._market_data_health["market_data_gap_count"], 1)
        self.assertEqual(engine._market_data_health["last_market_data_sequence"], 3)

    def test_aggregator_groups_roots_and_detects_split_decision(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            roots = []
            for label, decision, won, profit in [
                ("A_current", "trade", True, 3.33),
                ("B_cal_only", "skip", False, 0.0),
            ]:
                paper_root = root / f"paper_{label}"
                roots.append(paper_root)
                (paper_root / "sessions").mkdir(parents=True)
                (paper_root / "candidate_universe").mkdir(parents=True)
                session = {
                    "date": "2026-05-24",
                    "mode": "paper",
                    "params": {"config_label": label},
                    "summary": {
                        "market_data_health": {
                            "shutdown_received": True,
                            "last_market_data_sequence": 12,
                            "market_data_gap_count": 0,
                        }
                    },
                    "bets": [
                        _base_bet(
                            bet_id=f"{label}_bet",
                            settled=True,
                            won=won,
                            profit=profit,
                            config_label=label,
                        )
                    ] if decision == "trade" else [],
                }
                (paper_root / "sessions" / "2026-05-24_session.json").write_text(
                    json.dumps(session),
                    encoding="utf-8",
                )
                candidate = {
                    "session_date": "2026-05-24",
                    "game_pk": 1,
                    "line": "8.5",
                    "config_label": label,
                    "decision": decision,
                    "decision_reason": "placed" if decision == "trade" else "gate_min_edge",
                    "outcome_join_key": "2026-05-24|1|8.5",
                }
                with (paper_root / "candidate_universe" / "2026-05-24_candidates.jsonl").open("w", encoding="utf-8") as f:
                    f.write(json.dumps(candidate) + "\n")

            report = ape.build_report(roots, "2026-05-24", "2026-05-24")
            self.assertEqual(report["configs"]["A_current"]["headline"]["n_bets"], 1)
            self.assertIn("daily_read", report)
            self.assertEqual(report["daily_read"]["best_roi_config"], "A_current")
            self.assertTrue(report["configs"]["A_current"]["completeness"]["complete"])
            self.assertAlmostEqual(
                report["configs"]["A_current"]["headline"]["edge_over_market_stake_weighted_actual_minus_ask"],
                0.25,
            )
            self.assertEqual(
                report["shared_candidate_disagreement"]["game_line"]["counts"]["split"],
                1,
            )

    def test_aggregator_normalization_volume_index_and_profit_per_bet(self):
        """2026-05-26: F-J normalization. Build a 2-config report where
        config_F places 4x more bets than config_A on the same stake,
        and verify the headline carries:
          - profit_per_settled_bet (per-bet quality)
          - n_unique_game_lines + bets_per_unique_game_line (cohort breadth)
          - volume_index_vs_baseline (F's bet count / A's bet count)
        Baseline defaults to A_current when present.
        """
        with tempfile.TemporaryDirectory() as td:
            # A_current: 2 bets, 1 settled win at +$5 -> $5/bet, $5 ROI/stake
            # F_no_dedup: 8 bets, 4 settled wins at +$5 each -> $5/bet, $20 P&L
            # Both configs place 2 bets per unique game-line for F (cohort
            # breadth check: F's bets_per_unique_game_line = 2.0, A's = 1.0).
            def _make_root(label, bets):
                root = Path(td) / f"paper_{label}"
                (root / "sessions").mkdir(parents=True)
                (root / "candidate_universe").mkdir(parents=True)
                session = {
                    "date": "2026-05-24",
                    "mode": "paper",
                    "params": {"config_label": label},
                    "summary": {
                        "market_data_health": {
                            "shutdown_received": True,
                            "last_market_data_sequence": 1,
                            "market_data_gap_count": 0,
                        },
                    },
                    "bets": bets,
                }
                (root / "sessions" / "2026-05-24_session.json").write_text(
                    json.dumps(session), encoding="utf-8",
                )
                return root

            a_bets = [
                _base_bet(
                    bet_id="A_1", game_pk=1, line="7.5",
                    settled=True, won=True, profit=5.0, config_label="A_current",
                ),
                _base_bet(
                    bet_id="A_2", game_pk=2, line="8.5",
                    settled=True, won=False, profit=-10.0,
                    placed_at="2026-05-24T13:00:00Z",
                    config_label="A_current",
                ),
            ]
            # F: 8 bets across 2 unique game-lines (4 per line, mirroring the
            # bet-multiple-times-on-same-line pattern F_no_dedup enables).
            f_bets = []
            for i in range(4):
                f_bets.append(_base_bet(
                    bet_id=f"F_g1_{i}", game_pk=1, line="7.5",
                    settled=True, won=True, profit=5.0,
                    placed_at=f"2026-05-24T1{i}:00:00Z",
                    config_label="F_no_dedup",
                ))
            for i in range(4):
                f_bets.append(_base_bet(
                    bet_id=f"F_g2_{i}", game_pk=2, line="8.5",
                    settled=True, won=False, profit=-10.0,
                    placed_at=f"2026-05-24T1{i + 4}:00:00Z",
                    config_label="F_no_dedup",
                ))
            a_root = _make_root("A_current", a_bets)
            f_root = _make_root("F_no_dedup", f_bets)

            report = ape.build_report([a_root, f_root], "2026-05-24", "2026-05-24")

            self.assertEqual(report["baseline_config_label"], "A_current")

            a_head = report["configs"]["A_current"]["headline"]
            f_head = report["configs"]["F_no_dedup"]["headline"]

            # Per-bet quality.
            self.assertAlmostEqual(a_head["profit_per_settled_bet"], -2.5)
            self.assertAlmostEqual(f_head["profit_per_settled_bet"], -2.5)

            # Cohort breadth.
            self.assertEqual(a_head["n_unique_game_lines"], 2)
            self.assertEqual(f_head["n_unique_game_lines"], 2)
            self.assertAlmostEqual(a_head["bets_per_unique_game_line"], 1.0)
            self.assertAlmostEqual(f_head["bets_per_unique_game_line"], 4.0)

            # Volume index: F bet 4x as much as A.
            self.assertAlmostEqual(a_head["volume_index_vs_baseline"], 1.0)
            self.assertAlmostEqual(f_head["volume_index_vs_baseline"], 4.0)
            self.assertAlmostEqual(a_head["settled_index_vs_baseline"], 1.0)
            self.assertAlmostEqual(f_head["settled_index_vs_baseline"], 4.0)

            # Daily-read surfaces per-bet leader (ties broken by sort order;
            # both configs tie here at -$2.50/bet, so just verify the field
            # exists and references one of them).
            read = report["daily_read"]
            self.assertIn(
                read["best_profit_per_settled_bet_config"],
                {"A_current", "F_no_dedup"},
            )
            self.assertAlmostEqual(
                read["best_profit_per_settled_bet"], -2.5,
            )

    def test_aggregator_baseline_falls_back_to_first_alpha_when_no_a_current(self):
        """If no A_current root is present, baseline = first config
        alphabetically (so volume index still computes)."""
        with tempfile.TemporaryDirectory() as td:
            def _make(label):
                root = Path(td) / f"paper_{label}"
                (root / "sessions").mkdir(parents=True)
                (root / "candidate_universe").mkdir(parents=True)
                session = {
                    "date": "2026-05-24",
                    "mode": "paper",
                    "params": {"config_label": label},
                    "summary": {"market_data_health": {"shutdown_received": True}},
                    "bets": [_base_bet(bet_id=f"{label}_1", settled=True, won=True,
                                       profit=2.0, config_label=label)],
                }
                (root / "sessions" / "2026-05-24_session.json").write_text(
                    json.dumps(session), encoding="utf-8",
                )
                return root

            roots = [_make("X_custom"), _make("Y_custom"), _make("Z_custom")]
            report = ape.build_report(roots, "2026-05-24", "2026-05-24")
            # First alpha = X_custom.
            self.assertEqual(report["baseline_config_label"], "X_custom")
            for label in ("X_custom", "Y_custom", "Z_custom"):
                self.assertEqual(
                    report["configs"][label]["headline"]["volume_index_vs_baseline"],
                    1.0,
                )

    def test_aggregator_marks_shared_consumer_gaps_incomplete(self):
        with tempfile.TemporaryDirectory() as td:
            paper_root = Path(td) / "paper_A_current"
            (paper_root / "sessions").mkdir(parents=True)
            session = {
                "date": "2026-05-24",
                "mode": "paper",
                "params": {"config_label": "A_current", "market_data_mode": "shared_consumer"},
                "summary": {
                    "market_data_gap_count": 2,
                    "consumer_disconnects": 0,
                    "max_market_data_lag_ms": 100.0,
                    "last_market_data_sequence": 9,
                    "market_data_health": {"shutdown_received": True},
                },
                "bets": [],
            }
            (paper_root / "sessions" / "2026-05-24_session.json").write_text(json.dumps(session), encoding="utf-8")
            report = ape.build_report([paper_root], "2026-05-24", "2026-05-24")
            completeness = report["configs"]["A_current"]["completeness"]
            self.assertFalse(completeness["complete"])
            self.assertIn("market_data_sequence_gaps", completeness["reasons"])

    def test_launcher_reports_early_startup_failure(self):
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "launch_log.txt"
            log_path.write_text("startup boom\n", encoding="utf-8")
            proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(7)"])
            cfg = lpe.EngineConfig(
                label="A_current",
                flags=[],
                source="test",
                paper_root=Path(td),
            )
            running = [
                lpe.RunningEngine(
                    config=cfg,
                    process=proc,
                    log_path=log_path,
                    start_time=0.0,
                )
            ]
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                rc = lpe._wait_for_engines(running, startup_health_secs=9999999999.0)
            self.assertEqual(rc, 7)
            self.assertIn("Early startup failure detected", stdout.getvalue())
            self.assertIn("startup boom", stdout.getvalue())

    def test_walk_forward_config_label_filter_changes_plan_dates(self):
        with tempfile.TemporaryDirectory() as td:
            input_path = Path(td) / "signals_master.jsonl"
            with input_path.open("w", encoding="utf-8") as f:
                for idx, d in enumerate(["2026-05-01", "2026-05-02", "2026-05-03"], start=1):
                    f.write(json.dumps({"mode": "paper", "config_label": "A", "session_date": d, "bet_id": f"a{idx}"}) + "\n")
                f.write(json.dumps({"mode": "paper", "config_label": "B", "session_date": "2026-05-04", "bet_id": "b1"}) + "\n")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                wfr.main(
                    [
                        "--input-path", str(input_path),
                        "--mode", "paper",
                        "--config-label-filter", "A",
                        "--train-days", "1",
                        "--val-days", "1",
                        "--test-days", "1",
                        "--min-train-dates", "1",
                        "--plan-only",
                    ]
                )
            plans = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
            self.assertEqual(plans[-1]["test_start"], "2026-05-03")

    def test_certification_config_label_filter_limits_rows(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            table = root / "training.jsonl"
            rows = [
                {
                    "config_label": "A",
                    "session_date": "2026-05-24",
                    "decision_ask": 0.70,
                    "edge_at_ask": 0.12,
                    "target_filled": 1,
                    "target_win": 1,
                    "target_profit": 4.29,
                    "line": "8.5",
                    "inning": 6,
                    "runs_needed": 2.5,
                    "fair_value": 0.82,
                    "limit_price": 0.70,
                },
                {
                    "config_label": "B",
                    "session_date": "2026-05-24",
                    "decision_ask": 0.70,
                    "edge_at_ask": 0.12,
                    "target_filled": 1,
                    "target_win": 0,
                    "target_profit": -10.0,
                    "line": "8.5",
                    "inning": 6,
                    "runs_needed": 2.5,
                    "fair_value": 0.82,
                    "limit_price": 0.70,
                },
            ]
            with table.open("w", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row) + "\n")

            out = root / "out"
            rc = bwfc.main([
                "--training-table", str(table),
                "--output-dir", str(out),
                "--config-label-filter", "A",
            ])
            payload = json.loads((out / "walk_forward_certification.json").read_text(encoding="utf-8"))
            self.assertEqual(rc, 0)
            self.assertEqual(payload["config_label_filter"], "A")
            self.assertEqual(payload["overall"]["n_bets"], 1)
            self.assertEqual(payload["overall"]["n_filled_wins"], 1)


if __name__ == "__main__":
    unittest.main()
