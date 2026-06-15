# Bidirectional Trading → Market-Making (long-horizon)

Carved out of **[ROADMAP.md](ROADMAP.md)** on 2026-06-13. This is the
multi-quarter pivot from the current Over-only directional strategy to a
two-sided "smart market maker"; Phases A–E. It changes slowly and is read
when planning the pivot, not every session. Its structural premise — the
Edge Atlas "market overprices Over" finding (RF1) — lives in the Research
findings section of **[ROADMAP.md](ROADMAP.md#research-findings)**.

The bot today is **Over-only**: it evaluates one side of every game,
quotes one direction, and books P&L on directional accuracy. The
long-horizon ambition is to become a **two-sided "smart market maker"**
on Polymarket MLB OU markets -- quote both bid and ask on every game,
turn a small profit per side through high volume + spread capture, and
use inventory management to stay roughly delta-neutral on outcomes
where our edge is in *spread* not *direction*.

**Structural validation (2026-05-27)**: the Edge Atlas (see Research
findings RF1 in ROADMAP.md) measured Polymarket OVER ask vs 10y MLB empirical
across 7,609 (cell × line) pairs and found a +2-5pp Over premium in
EVERY cohort. That's exactly the inefficiency a two-sided quoter
captures structurally — the directional Over-only strategy pays the
premium on every fill; a market-maker collects it on every UNDER fill.
The atlas is descriptive (and 10y baseline may not reflect today's
scoring environment), but the cohort universality makes it the
strongest pre-pivot evidence we have for the strategy.

This is a multi-quarter pivot, not a one-week task. Five phases (A-E).
Each phase has a meaningful checkpoint where we can stop and re-evaluate;
nothing past Phase A goes live until the prior phase has produced
durable evidence the same way Active #1 / walk-forward-certify gates
the current Over-only model.

**Strategic reason to do this**: at our current 3.4 fills/day rate the
self-improvement loop is starved of data; the model is technically
sound but underfed. Doubling the addressable signal universe (Over +
Under) and then quoting two-sided (taking spread, not just direction)
moves us from ~50 candidates/day to ~500+ -- enough volume that every
diagnostic, drift alert, calibration retrain, and walk-forward
certification matures in days instead of weeks.

### Phase A -- Symmetric UNDER signals (foundation, ~6 weeks)

**Shipped 2026-05-16** (offline / shadow only; live trading remains
Over-only until Phase C). See the Recently Completed "Phase A
foundation: UNDER offline pipeline" entry for full details. The
high-level state of each item:

A1. **UNDER-side book ingestion.** *Shipped* -- audit confirmed
    the monitor already polls both sides and signal_engine attaches
    `under_best_bid/ask/...` to the book payload; the original
    roadmap text was based on a stale assumption. Daily-review now
    surfaces `under_pair_available_rate` (~49% in production --
    Phase C needs to raise this for live UNDER quoting).

A2. **UNDER FV inference + candidate emission.** *Partially
    shipped*: separate UNDER calibrator artifact ships
    (`signal_win_calibration_under.json`, refreshed daily) with
    flipped labels + raw probs, separate stability gate. UNDER
    candidate emission in the LIVE signal pipeline is deferred to
    Phase B (it would touch the live trading runtime; foundation
    is meaningful without it because the candidate_universe table
    already carries the OVER candidate's full FV + ask + outcome
    which is what UNDER offline analysis needs).

A3. **UNDER state-value transition modeling.** *Shipped* --
    `build_under_state_value_transition_report.py` mirrors the
    Over report with flipped outcome, under-ask ROI math, and
    inverted regime classifiers.

A4. **UNDER walk-forward certification.** *Shipped (narrower
    scope than originally specced)*. Per-gate scorecard
    intentionally omitted: no UNDER gates are enforced today, so
    "what threshold works" is premature. Per-gate piece lands in
    Phase C with the first UNDER gate. Sample-readiness +
    per-cohort scorecard + per-week drift all ship.

**Remaining for the Phase A -> B transition**:

A5. **Live UNDER candidate emission.** *Shipped 2026-05-19.* See
    the Recently Completed "Phase A5: live UNDER candidate
    emission (shadow mode)" entry for full details. Shipped as a
    `--under-emission-mode {off, shadow}` CLI flag on
    live_engine_cli.py. When `shadow`, the engine emits a sibling
    UNDER candidate row alongside every OVER candidate that
    reaches the FV phase, with its own calibrated FV
    (= UNDER_calibrator(1 - over_fv_raw)), its own UNDER-side
    market data (ask, bid, pair_available), and its own gate
    evaluation (`decision=shadow_under` when gates pass;
    `gate_min_edge` / `gate_no_under_liquidity` skip reasons
    otherwise). NO UNDER bets are placed in either mode (paper
    or live); pure observability so the paper-mode runway
    accumulates UNDER signal-quality data that the daily-review
    `by_side` block, training table, loss-attribution, and
    shadow-override reports all pick up automatically. The
    eventual UNDER paper-bet flip is a separate ship gated by
    B4 60-session validation.
    - Files: `scripts/trading/signal_config.py` (CLI flag +
      constants), `scripts/trading/signal_engine.py` (UNDER
      calibrator load), `scripts/trading/signal_pipeline.py`
      (`_maybe_emit_under_candidate` helper + wire site),
      `scripts/trading/live_engine_cli.py` + `live_engine.py`
      (CLI flag + trade_args bridge).

### Phase B -- Symmetric infrastructure (~3 weeks, after A)

**Shipped 2026-05-17** (foundation + structure; UNDER trading is
still NOT enabled in the live engine -- that's Phase C). See the
Recently Completed "Phase B foundation: symmetric infra" entry for
full details. The high-level state of each item:

B1. **Side-aware drift alerts.** *Shipped (calibration_health
    only)*. `calibration_health` now reads the UNDER calibrator
    artifact and exposes a parallel `under` sub-block plus
    side-prefixed alerts (`under: ...`). The other six drift
    dimensions (fill_rate, signal_quality, regime_mix, cohort_roi,
    concept_drift, drift_in_drift) defer their splits to Phase C
    when real UNDER trading produces UNDER fill/outcome data;
    until then the splits would be all-zero / no-data and add
    noise without value. Concept_drift + drift_in_drift stay
    side-agnostic by design (input-feature drift).

B2. **Side-aware audit log + promote CLI.** *Shipped*.
    `PromotionEvent` carries a `side` field (default "both" for
    side-symmetric levers like stage2/stage3-v2). `promote.py`
    accepts `--side {over,under,both}` on stake-scaling and
    gate-threshold subcommands (default `over` -- today's live
    engine is Over-only). `latest_promotion_event_for_lever` gained
    an optional `side` filter that matches same-side OR `both`
    rows so daemon retrospective + drift attribution can filter
    correctly without losing side-symmetric promotions.

B3. **Side-aware session JSON + reports.** *Shipped (foundation)*.
    `BetRecord` already had a `side` field (defaulted "over").
    Daily review's `bet_totals` now exposes a `by_side: {over: {...},
    under: {...}}` sub-block with per-side count, filled, wins,
    losses, profit, ROI, win_rate. Today UNDER subtotals are
    all-zero (no UNDER bets placed); Phase C populates them
    naturally without further plumbing. Each compact bet row
    surfaces `side` for the markdown table.

B4. **UNDER paper-mode validation period.** *🟡 DORMANT as of
    2026-06-15 (operator decision, T5).* Was 8 UNDER bets in ~2 weeks
    against a 150-settled / 60-session bar — ~a year away at that pace.
    **The limiter is UNDER signal QUALITY, not session count:**
    score_event UNDER FV is near-flat (~0.30), per-line UNDER overfits
    (deferred 2026-06-11), and under-pair liquidity is ~49%, so the
    honest enforce-mode (2026-06-11 fix) correctly emits few defensible
    bets. Forcing volume can't clear B4's ROI/calibration conditions —
    it would only churn the verdict ladder — and loosening emission
    would either re-break FV honesty or add low-quality bets that fail
    the quality gates anyway. So B4 is marked **DORMANT**:
    `_under_paper_b4_milestone_health` now reports `status=DORMANT`
    (preserving the underlying ladder + per-condition progress in the
    JSON) and suppresses the verdict-ladder Notes alerts
    (`B4_MILESTONE_DORMANT=True`). `M_under_paper` keeps running so
    honest UNDER data accrues passively. **Re-activate** (flip the flag
    to `False`) when UNDER signal quality improves — a discriminating
    UNDER calibrator (e.g. the no_score_drift market-anchored alpha the
    2026-06-14 audit found OOS-positive) or higher under-pair liquidity.
    Evidence had accumulated via the `M_under_paper` fleet preset
    (running `--under-mode paper` daily since 2026-05-30). Status notes:
    - **DONE**: Phase C-paper ships `--under-mode paper` + the 5
      symmetric UNDER gate stack (extreme_edge, fv_ask_gap,
      max_base_fv, min_inning, min_entry_ask).
    - **DONE**: B4 milestone dashboard shipped
      (`_under_paper_b4_milestone_health`, verdict ladder
      `NOT_EMITTING → INSUFFICIENT_SESSIONS → ... → READY`).
    - **DONE 2026-06-10**: B4 scanner blind spot fixed — the block
      now also walks fleet roots (`B4_EXTRA_PAPER_SESSION_ROOTS`);
    - **DONE**: Phase C-paper ships `--under-mode paper` + the 5
      symmetric UNDER gate stack (extreme_edge, fv_ask_gap,
      max_base_fv, min_inning, min_entry_ask).
    - **DONE**: B4 milestone dashboard shipped
      (`_under_paper_b4_milestone_health`, verdict ladder
      `NOT_EMITTING → INSUFFICIENT_SESSIONS → ... → READY`).
    - **DONE 2026-06-10**: B4 scanner blind spot fixed — the block
      now also walks fleet roots (`B4_EXTRA_PAPER_SESSION_ROOTS`);
      before the fix it read 0/60 while M accumulated invisibly.
    - **DONE 2026-06-11**: M flipped back to
      `--under-calibration-mode enforce` (off-mode produced honest
      volume but dishonest FVs: 1W/7L, −39pp calibration delta —
      could never clear B4's ROI/calibration conditions). Expect
      fewer but defensible UNDER paper bets; the early-window
      numbers (taker ROI −69%) are dominated by pre-fix off-mode
      bets + the 06-02 dedup-leak multi-fires and will dilute as
      enforce-mode sessions accumulate.
    - Operator runs the live engine with `--under-mode paper` for
      **>= 60 daily sessions**. Threshold matches the OVER
      walk-forward `READY` verdict's date floor (30 dates) doubled,
      because UNDER is in less-validated territory than OVER was
      when OVER promoted.
    - Across those 60 sessions, the daily review's `by_side.under`
      block must show:
      * `n_under_outcomes >= 150` (matches A4 walk-forward READY)
      * UNDER win rate within 5pp of the UNDER calibrator's
        predicted win rate (calibration is honoring outcomes)
      * UNDER taker ROI > 0% (positive expected value)
      * No persistent UNDER-side drift alerts from B1 (calibrator
        stable; cohort_roi will fire if outcomes diverge)
    - Only after the verdict clears: the operator can ship the
      `--under-mode live` value (currently rejected by the CLI
      parser) AND the `--quote-engine-mode act` flip. Both remain
      structurally unbuilt today by design — B4 protects against
      shipping them on unvalidated UNDER signal quality.

### Phase C -- Market-maker foundation (~6 weeks, after B)

**Shadow shipped 2026-05-17** (compute + log only; no order
placement). See the Recently Completed "Phase C shadow: two-sided
quote engine foundation" entry. The live-engine flip (the part that
actually places two-sided orders) is gated by the B4 60-session
paper-mode validation milestone.

C1. **Two-sided quote engine.** *Shadow shipped*. New
    `scripts/trading/live_quote_engine.py` computes a per-tick
    `QuoteDecision` (bid + ask + skip reasons + hedge opportunity)
    when `--quote-engine-mode shadow` is passed. Writes to
    `data/{live,paper}_trading/quote_engine_shadow/<date>_quotes.jsonl`.
    The live-engine flip (`--quote-engine-mode act`) is deferred to
    after B4 paper-validation. Existing `_place_bet` path is
    untouched.

C2. **Inventory tracking.** *Shipped*. New
    `scripts/trading/inventory_tracker.py` aggregates
    `live_orders_ledger.jsonl` into per-game `GameInventoryRow`
    with `filled_over/under_shares` + `open_over/under_shares` +
    `net_over_shares`. Read-only by design: shadow quotes do NOT
    mutate the snapshot, so "what shadow quoted vs what really
    happened" stays uncontaminated. In Phase C shadow, the
    snapshot is cached once per session; Phase D adds tick-level
    refresh.

C3. **Inventory-aware quote shading.** *Shadow shipped (inside
    C1)*. The quote engine computes a signed shade =
    `(net_inventory / max_inventory) * max_shade` and applies it to
    BOTH bid and ask anchors. Positive when long Over (shifts both
    quotes DOWN to discourage adding + encourage flattening);
    negative when short. Clamped to ±max_shade. Default cfg
    `max_inventory_per_game=50, max_shade=0.05`.

C4. **Hedging on opposite-side opportunities.** *Shadow shipped
    (inside C1)*. The quote decision carries
    `hedge_opportunity: bool` + `hedge_side: "buy_under" | "buy_over"`
    + `hedge_target_price` + `hedge_max_price` + `hedge_reason`. Fires
    when net inventory exceeds 1 share AND opposite-side ask is
    at-or-below fair + hedge_premium (default 1c). Shadow only --
    no hedge order is placed. The shadow report aggregates triggers
    by side + inventory-at-trigger distribution.

### Phase D -- High-volume scaling (~4 weeks, after C)

D1. **Multi-game concurrent quoting at scale.** The current
    engine handles ~10 concurrent active games comfortably.
    Two-sided quoting on each ~doubles the order count. Profile
    the tick loop, the CLOB SDK rate limits, and the order-
    lifecycle bookkeeping for the new load. Likely requires
    batching order updates and a per-game tick-scheduling
    heuristic so we don't blow rate limits on a 15-game evening
    slate.

D2. **Per-game profit target tracking.** A market maker measures
    P&L per game, not per bet. New session JSON field:
    `per_game_pnl: {game_pk: {realized, unrealized, spread_captured,
    inventory_marked}}`. Daily review surfaces "X of N games
    were profitable today" as the headline KPI alongside the
    existing per-bet ROI. Target metric:
    `profitable_games_share` (% of games with realized P&L > 0).

D3. **Position-sizing across correlated games.** When inventory
    on multiple games correlates (e.g. all Over positions on a
    high-scoring-weather day), apply a portfolio-level cap. New
    `portfolio_risk_check` step in the candidate pipeline checks
    summed correlated exposure against a daily limit.

D4. **Risk limits (per game / per day / per cohort).** Three
    explicit caps the market-maker mode respects:
    `max_inventory_per_game` (absolute share count),
    `max_daily_drawdown` (kill-switch at -$X session loss),
    `max_cohort_inventory` (e.g. no more than $K exposure to
    high-ask cohort even across games). Mirrors the existing
    correlated-line cap pattern.

### Phase E -- Smart quoting (continuous improvement, after D)

E1. **Adverse-selection / toxic-flow detection.** When the same
    counterparty consistently hits our bids right before the
    market moves against us, that's toxic flow. Log fills
    against counterparty (where Polymarket exposes it) + against
    immediate post-fill book moves; build a "toxic counterparty"
    model that shades quotes wider when those counterparties
    are active. Defensive complement to C3 (inventory shading).

E2. **Dynamic spread sizing.** Today's spread is implicit
    (fixed offsets in `live_pricing.py`). A smart MM widens
    spread when volatility is high (high innings, big score
    changes) and tightens when volatility is low (early
    innings, quiet game). Spread function:
    `f(realized_volatility, time_to_resolution, inventory)`.

E3. **Time-decay-aware quoting.** A game in the 9th has minutes
    to resolution; a game in the 3rd has hours. The
    information-decay rate is different, and so is the
    inventory cost-of-carry. Tighten ask / loosen bid as
    resolution approaches and inventory needs to clear.

E4. **Cross-game model A/B harness.** Once volume supports it,
    run two variants of the quote engine on disjoint game
    cohorts (deterministic hash on `game_pk`). The existing
    self-improvement loop (drift alerts, daemon retrospective,
    promote/demote) is generalized to compare cohort-A vs
    cohort-B P&L per refresh. This is the natural follow-on to
    the daemon retrospective once paired-cohort data is
    available.

**Phase gating rule**: nothing past the current phase goes live
until the prior phase has accumulated 30+ sessions of clean
evidence the same way Active #1 / walk-forward gates the
current Over-only stack. The drift-alert family, calibration
stability gate, and daemon retrospective all generalize to
two-sided / market-maker mode -- we are extending the
self-improvement loop, not building a parallel one.
