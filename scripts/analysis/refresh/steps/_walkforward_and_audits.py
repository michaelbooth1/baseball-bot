"""Step cluster: walk-forward retrains + audit reports + final rollup steps.

Everything after the canonical_tables block in the legacy file:
- walk-forward retrains (gated on config.run_walk_forward)
- stake-scaling, walk-forward certification, shadow-override
- cell loss attribution, gate counterfactual, over-gate EV audit
- under-gate bottleneck audit, feed enrichment, quote engine shadow
- under walk-forward certification, weekly drift rollup
- auto-promote/demote daemon, retrospective, artifact lineage
- refresh_health_rollup (always last)
"""
from __future__ import annotations

from typing import List

from .. import config as _config
from ..config import (
    RefreshConfig,
    RefreshStep,
    StalenessCheck,
)
from ..helpers import _python, _script


def build_walk_forward_steps(
    config: RefreshConfig,
    max_date: str,
) -> List[RefreshStep]:
    """Gated on config.run_walk_forward."""
    strict_flag = ["--strict"] if config.strict else []
    max_date_args = ["--max-date", max_date]
    if not config.run_walk_forward:
        return []
    return [
        RefreshStep(
            name="walk_forward_score_event",
            description="Refresh score-event walk-forward research output.",
            command=[
                _python(),
                _script("scripts/analysis/walk_forward_runner.py"),
                "--mode",
                "live",
                "--end-date",
                max_date,
                *strict_flag,
            ],
            staleness_check=StalenessCheck(
                output_path=_config.PROJECT_DIR / "data" / "analysis_output"
                / "walk_forward" / "summary.json",
                input_paths=(
                    _config.PROJECT_DIR / "data" / "analysis_output"
                    / "training_tables" / "signal_training_table.jsonl",
                ),
            ),
        ),
        RefreshStep(
            name="walk_forward_no_score_drift",
            description="Refresh no-score drift walk-forward research output.",
            command=[
                _python(),
                _script("scripts/analysis/no_score_drift_walk_forward.py"),
                "--mode",
                "live",
                *max_date_args,
                "--end-date",
                max_date,
                *strict_flag,
            ],
        ),
        RefreshStep(
            name="walk_forward_market_anchored_alpha",
            description=(
                "Refresh family-separated market-anchored alpha "
                "walk-forward research output with ask/no-vig "
                "baselines and clustered policy-P&L intervals."
            ),
            command=[
                _python(),
                _script("scripts/analysis/calibration_market_anchored_alpha_walk_forward.py"),
                "--mode",
                "live",
                *max_date_args,
                "--end-date",
                max_date,
                *strict_flag,
            ],
            staleness_check=StalenessCheck(
                output_path=_config.PROJECT_DIR / "data" / "analysis_output"
                / "calibration_market_anchored_alpha_walk_forward" / "summary.json",
                input_paths=(
                    _config.PROJECT_DIR / "data" / "analysis_output"
                    / "calibration_opportunity_training"
                    / "calibration_opportunity_training_table.jsonl",
                ),
            ),
        ),
        RefreshStep(
            name="under_walk_forward",
            description=(
                "Phase A4 (2026-05-16): UNDER walk-forward "
                "(signal_win only, flipped labels). No "
                "execution_fill: no UNDER orders have ever "
                "been posted, so no fill history to learn "
                "from. Sibling to walk_forward_score_event."
            ),
            command=[
                _python(),
                _script("scripts/analysis/under_walk_forward_runner.py"),
                "--mode",
                "live",
                "--end-date",
                max_date,
                *strict_flag,
            ],
            staleness_check=StalenessCheck(
                output_path=_config.PROJECT_DIR / "data" / "analysis_output"
                / "under_walk_forward" / "summary.json",
                input_paths=(
                    _config.PROJECT_DIR / "data" / "analysis_output"
                    / "training_tables" / "signal_training_table.jsonl",
                ),
            ),
        ),
        RefreshStep(
            name="walk_forward_fv_disagreement_quality",
            description=(
                "Refresh FV-vs-market disagreement bucket "
                "walk-forward validation: train/validation-selected "
                "trust buckets applied out of sample."
            ),
            command=[
                _python(),
                _script("scripts/analysis/fv_disagreement_quality_walk_forward.py"),
                "--mode",
                "live",
                *max_date_args,
                "--end-date",
                max_date,
                *strict_flag,
            ],
            staleness_check=StalenessCheck(
                output_path=_config.PROJECT_DIR / "data" / "analysis_output"
                / "fv_disagreement_quality_walk_forward" / "summary.json",
                input_paths=(
                    _config.PROJECT_DIR
                    / "data" / "analysis_output"
                    / "calibration_opportunity_training"
                    / "calibration_opportunity_training_table.jsonl",
                    _config.PROJECT_DIR / "data" / "analysis_output" / "clv" / "clv_rows.jsonl",
                ),
            ),
        ),
    ]


def build_audit_research_steps(
    config: RefreshConfig,
) -> List[RefreshStep]:
    """Unconditional audit + research steps that run after walk-forward."""
    steps: List[RefreshStep] = []

    steps.append(RefreshStep(
        name="stake_scaling_promotion_analyzer",
        description=(
            "Rebuild the Active #6 stake-scaling promotion analyzer from "
            "all session JSONs that carry calibrated_stake_multiplier."
        ),
        command=[
            _python(),
            _script("scripts/analysis/analyze_stake_scaling_promotion.py"),
        ],
    ))

    steps.append(RefreshStep(
        name="walk_forward_certification",
        description=(
            "Rebuild the Active #1 walk-forward certification report "
            "(per-cohort + per-gate scorecard with READY/PRELIMINARY/"
            "INSUFFICIENT verdict) from signal_training_table.jsonl."
        ),
        command=[
            _python(),
            _script("scripts/analysis/build_walk_forward_certification.py"),
        ],
    ))

    steps.append(RefreshStep(
        name="stage1_shadow_override_report",
        description=(
            "Replay two candidate Stage-1 fixes (empirical-when-"
            "available + block-deep-fallback) against actual bet "
            "outcomes; surface counterfactual bias deltas + P&L "
            "delta + recommendation verdicts so Active #8's runtime "
            "change has shadow evidence before promotion."
        ),
        command=[
            _python(),
            _script(
                "scripts/analysis/build_stage1_shadow_override_report.py"
            ),
        ],
    ))

    steps.append(RefreshStep(
        name="stage1_cell_loss_attribution",
        description=(
            "Drill today's Active #10 finding (Stage-1 owns the "
            "aggregate bias) into Stage-1-internal cohort cuts: "
            "fallback level, line fallback mode, used_fallback, "
            "cell sample size, Poisson-vs-empirical gap. Surfaces "
            "the cohort culprits that narrow Active #8's retrain "
            "surface from 'rebuild Stage-1' to 'fix THIS Stage-1 "
            "cohort.'"
        ),
        command=[
            _python(),
            _script(
                "scripts/analysis/build_stage1_cell_loss_attribution.py"
            ),
        ],
    ))

    steps.append(RefreshStep(
        name="loss_attribution_report",
        description=(
            "Rebuild the Active #10 bet-level loss attribution "
            "report -- per-stage decomposition of each filled+settled "
            "bet's FV chain into Stage-1 / Stage-2 / Stage-3 / "
            "calibration contributions, plus aggregate culprit "
            "ranking by share of bias direction."
        ),
        command=[
            _python(),
            _script("scripts/analysis/build_loss_attribution_report.py"),
        ],
    ))

    steps.append(RefreshStep(
        name="gate_counterfactual_report",
        description=(
            "Rebuild the Active #11 gate counterfactual report -- "
            "for each enforced gate, each sweep threshold, each "
            "time window (all / trailing_30d / trailing_7d), compute "
            "the realized-$ counterfactual P&L delta vs. current and "
            "rank the top tightening recommendations."
        ),
        command=[
            _python(),
            _script("scripts/analysis/build_gate_counterfactual_report.py"),
        ],
    ))

    steps.append(RefreshStep(
        name="over_gate_ev_audit",
        description=(
            "Per-OVER-gate counterfactual EV audit (2026-05-28). Unlike "
            "walk_forward_certification / gate_counterfactual (which run on "
            "the ~227 FILLED bets and can't see pre-FV gates that block 0 "
            "filled bets), this reads candidate-universe SKIP rows -- the "
            "bets each gate actually blocked -- dedupes to unique game "
            "states, joins over_hit outcomes, and computes each blocked "
            "cohort's taker ROI vs breakeven (Wilson-bounded) -> per-gate "
            "+EV / marginal / -EV verdict. Keeps the case for re-enabling "
            "gate_extreme_edge / gate_stage2_suppression (disabled "
            "2026-05-28) fresh as live data accumulates. Outputs to "
            "data/analysis_output/over_gate_ev_audit/."
        ),
        command=[
            _python(),
            _script("scripts/analysis/audit_over_gate_ev.py"),
        ],
    ))

    steps.append(RefreshStep(
        name="under_gate_bottleneck_audit",
        description=(
            "UNDER single-gate-bottleneck guardrail (2026-05-30). Scans "
            "recent session JSON files and flags any session where >=95%% "
            "of UNDER skip rows (n>=100) hit a single gate -- the "
            "fingerprint of a misconfigured UNDER threshold like the "
            "2026-05-29 gate_under_min_entry_ask incident (877/877 UNDER "
            "candidates blocked by one mirrored OVER default). Cheap, "
            "fail-open; outputs to "
            "data/analysis_output/under_gate_bottleneck_audit/ and prints "
            "WARN to stderr when triggered."
        ),
        command=[
            _python(),
            _script("scripts/analysis/audit_under_gate_bottleneck.py"),
        ],
    ))

    steps.append(RefreshStep(
        name="fleet_correlation_diagnostic",
        description=(
            "Fleet correlation diagnostic (2026-06-01). For each recent "
            "day, computes (a) split_density: the fraction of compared "
            "game-line keys where the parallel paper presets disagreed "
            "(the actual A/B signal density), and (b) "
            "max_correlated_loss_share: the worst-day concentration of "
            "losing bets onto a single game (e.g. 2026-05-27 had 11/12 "
            "models lose on 824270 OVER 4.5 -> 4, a 92% correlated "
            "loss). Surfaces both as single numbers per day so the "
            "operator can read the multi-engine A/B's actual evidence "
            "density at a glance. Outputs to "
            "data/analysis_output/fleet_correlation/ and prints WARN to "
            "stderr when max_correlated_loss_share crosses the threshold."
        ),
        command=[
            _python(),
            _script("scripts/analysis/fleet_correlation_diagnostic.py"),
        ],
    ))

    steps.append(RefreshStep(
        name="feed_enrichment",
        description=(
            "Tier-3 offline feed enrichment (2026-05-29). Joins each "
            "model-bearing candidate to its scraped MLB live-feed JSON by ts "
            "and reconstructs decision-time pitch count, times-through-order, "
            "bullpen depth, handedness matchup, catcher, and velocity/exit-velo "
            "trends. No live polling -- reads data/games we already scrape; "
            "backfills history. Outputs data/analysis_output/feed_enrichment/ "
            "keyed by candidate_id for join into calibration / walk-forward / "
            "the gate-EV audit."
        ),
        command=[
            _python(),
            _script("scripts/analysis/build_feed_enrichment.py"),
        ],
    ))

    steps.append(RefreshStep(
        name="quote_engine_shadow_report",
        description=(
            "Phase C shadow (2026-05-17): summarise the two-sided "
            "quote engine's shadow ledger. Reads per-date "
            "quote_engine_shadow/<date>_quotes.jsonl files written "
            "by the live engine running --quote-engine-mode shadow. "
            "Pure observability; no order placement. Outputs to "
            "data/analysis_output/quote_engine_shadow/."
        ),
        command=[
            _python(),
            _script("scripts/analysis/build_quote_engine_shadow_report.py"),
        ],
    ))

    steps.append(RefreshStep(
        name="under_walk_forward_certification",
        description=(
            "Phase A4 (2026-05-16): UNDER walk-forward certification "
            "report. Mirrors the Active #1 cert but for the UNDER side "
            "with flipped outcome, under-side ROI math, and no "
            "per-gate scorecard (no UNDER gates are enforced today)."
        ),
        command=[
            _python(),
            _script("scripts/analysis/build_under_walk_forward_certification.py"),
        ],
    ))

    steps.append(RefreshStep(
        name="weekly_drift_rollup",
        description=(
            "Render the trailing 7-day drift / health HTML rollup from the "
            "per-date daily-review JSONs."
        ),
        command=[
            _python(),
            _script("scripts/analysis/build_weekly_drift_rollup.py"),
        ],
    ))

    steps.append(RefreshStep(
        name="auto_promote_demote_daemon",
        description=(
            "Auto promote/demote daemon (default: preview mode). Reads "
            "stability-gate verdicts + promote_events log; for stage2 and "
            "stage3-v2 invokes promote.py when verdict says go AND "
            "cooldown has elapsed. Skip-decisions logged to stdout, "
            "actions logged to promote_events.jsonl with operator=auto_daemon. "
            "Switch to --auto-daemon-mode act after reviewing preview output."
        ),
        command=[
            _python(),
            _script("scripts/analysis/auto_promote_demote_daemon.py"),
            "--mode", config.auto_daemon_mode,
            "--cooldown-days", str(config.auto_daemon_cooldown_days),
            "--active-date", config.active_date,
        ],
    ))

    steps.append(RefreshStep(
        name="daemon_retrospective",
        description=(
            "Replay daemon decisions against per-date history snapshots; "
            "report per-day MATCH/DAEMON_ONLY/OPERATOR_ONLY/DISAGREE "
            "agreement vs the audit log. Builds operator confidence to "
            "flip --auto-daemon-mode preview -> act."
        ),
        command=[
            _python(),
            _script("scripts/analysis/daemon_retrospective.py"),
            "--cooldown-days", str(config.auto_daemon_cooldown_days),
        ],
    ))

    steps.append(RefreshStep(
        name="artifact_lineage_freshness",
        description=(
            "Build canonical artifact lineage/freshness report with input "
            "hashes, mtimes, row/family counts, and downstream staleness flags."
        ),
        command=[
            _python(),
            _script("scripts/analysis/build_artifact_lineage_freshness_report.py"),
        ],
    ))

    steps.append(RefreshStep(
        name="refresh_health_rollup",
        kind="inline",
        description=(
            "End-of-refresh health rollup. Reads the latest daily review + "
            "walk-forward summary + per-step results and prints one "
            "consolidated 'is the project healthy?' summary."
        ),
        command=[],
    ))

    return steps
