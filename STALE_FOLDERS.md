# Stale `data/analysis_output/` siblings audit (2026-05-25)

Generated 2026-05-25 as part of Phase 3b documentation cleanup. This file
lists scratch sibling folders under `data/analysis_output/` that match the
naming patterns called out as "iteration snapshots" in
[data/AGENT_CONTEXT.md](data/AGENT_CONTEXT.md): `_check`, `_step*_check`,
`_review_<date>`, `_day2_<date>`, `_apr30_check`, dated `_2026_05_*`
variants of canonical folders.

Each entry was grepped across `scripts/`, `tests/`, `cache/`, and all
top-level `*.md` docs. **Zero are referenced by any Python script or
test.** The only hits are data/AGENT_CONTEXT.md's own catalog of "scratch
siblings to ignore," which itself is the doc proposing this cleanup.

This is a report, not an action. No folders have been deleted. After
operator review, run the cleanup command at the bottom of this file
(after copying any analysis you still want).

---

## Findings (29 folders, ~55 MB total)

| Folder | Size | Age | Recommendation |
|---|---:|---:|---|
| `side_neutral_opportunities_check_2026_05_08` | 16M | 16d | Delete |
| `side_neutral_opportunities_review_2026_05_09` | 16M | 15d | Delete |
| `calibration_opportunity_training_review_2026_05_08` | 9.2M | 16d | Delete |
| `market_anchored_alpha_check_2026_05_08` | 4.0M | 16d | Delete |
| `calibration_opportunity_training_check_all` | 3.4M | 17d | Delete |
| `candidate_universe_review_2026_04_22` | 3.2M | 32d | Delete (>30d) |
| `log_audit_no_score_drift_paper_ledger_2026_05_01_05` | 232K | 19d | Delete |
| `log_audit_no_score_drift_2026_05_01_05` | 216K | 19d | Delete |
| `unified_signals_step2_check` | 220K | 33d | Delete (>30d) |
| `under_paper_ledger_check_2026_05_08` | 200K | 16d | Delete |
| `under_paper_ledger_review_2026_05_09` | 192K | 15d | Delete |
| `no_score_drift_walk_forward_2026_05_02` | 184K | 22d | Delete |
| `no_score_drift_paper_ledger_patch_check_2026_05_08` | 148K | 16d | Delete |
| `no_score_drift_paper_ledger_touch_check` | 144K | 23d | Delete |
| `no_score_drift_paper_ledger_all_to_2026_05_01` | 140K | 23d | Delete |
| `no_score_drift_paper_ledger_review_2026_05_09` | 136K | 15d | Delete |
| `unified_signals_review_2026_04_22` | 132K | 32d | Delete (>30d) |
| `candidate_universe_day2_2026_04_23` | 120K | 31d | Delete (>30d) |
| `state_value_transition_check` | 88K | 25d | Delete |
| `fair_value_stage_ablation_review_2026_05_09` | 76K | 15d | Delete |
| `no_score_drift_paper_ledger_2026_05_02` | 68K | 22d | Delete |
| `training_tables_step2_check` | 60K | 33d | Delete (>30d) |
| `queue_execution_replay_2026_05_02` | 60K | 22d | Delete |
| `model_maturity_review_2026_05_08` | 60K | 16d | Delete |
| `walk_forward_apr30_check` | 28K | 25d | Delete |
| `walk_forward_daily_check_short` | 28K | 23d | Delete |
| `execution_diagnostics_day2_2026_04_23` | 16K | 31d | Delete (>30d) |
| `candidate_universe_step3_check` | 9.0K | 33d | Delete (>30d) |
| `walk_forward_daily_check` | 4.0K | 23d | Delete |

## Files preserved (intentionally pinned snapshots)

These are not folders but loose JSON/JSONL files at the root of
`analysis_output/`. data/AGENT_CONTEXT.md explicitly calls them out as
"pinned overreaction snapshots from early studies" — leave alone:

- `day1_review_2026_04_22_candidate_labels.jsonl`
- `day1_review_2026_04_22_candidate_labels_from_schedule.jsonl`
- `overreact_*.json`, `overreact_full_*.json`, `overreactions_*.json`

## Retention policy proposal

Add to data/AGENT_CONTEXT.md (Safe-Edit Checklist):

> Scratch sibling folders (suffixes `_check`, `_step*_check`,
> `_review_<date>`, `_day2_<date>`, dated `_YYYY_MM_DD` variants) should
> be considered tombstoned 30 days after creation. The next operator
> can delete any that pass the grep test in `STALE_FOLDERS.md`. The
> canonical (unsuffixed) folder remains authoritative; any analysis
> still needed from a sibling should be promoted back to the canonical
> folder or pinned to `model_improvements/`.

## Cleanup command (after operator review)

**DO NOT RUN WITHOUT REVIEWING THIS LIST FIRST.** Total reclaim: ~55 MB.

```powershell
# PowerShell (matches the repo's shell context)
$folders = @(
  'side_neutral_opportunities_check_2026_05_08',
  'side_neutral_opportunities_review_2026_05_09',
  'calibration_opportunity_training_review_2026_05_08',
  'market_anchored_alpha_check_2026_05_08',
  'calibration_opportunity_training_check_all',
  'candidate_universe_review_2026_04_22',
  'log_audit_no_score_drift_paper_ledger_2026_05_01_05',
  'log_audit_no_score_drift_2026_05_01_05',
  'unified_signals_step2_check',
  'under_paper_ledger_check_2026_05_08',
  'under_paper_ledger_review_2026_05_09',
  'no_score_drift_walk_forward_2026_05_02',
  'no_score_drift_paper_ledger_patch_check_2026_05_08',
  'no_score_drift_paper_ledger_touch_check',
  'no_score_drift_paper_ledger_all_to_2026_05_01',
  'no_score_drift_paper_ledger_review_2026_05_09',
  'unified_signals_review_2026_04_22',
  'candidate_universe_day2_2026_04_23',
  'state_value_transition_check',
  'fair_value_stage_ablation_review_2026_05_09',
  'no_score_drift_paper_ledger_2026_05_02',
  'training_tables_step2_check',
  'queue_execution_replay_2026_05_02',
  'model_maturity_review_2026_05_08',
  'walk_forward_apr30_check',
  'walk_forward_daily_check_short',
  'execution_diagnostics_day2_2026_04_23',
  'candidate_universe_step3_check',
  'walk_forward_daily_check'
)
foreach ($f in $folders) {
  $path = "data\analysis_output\$f"
  if (Test-Path $path) {
    Remove-Item -Recurse -Force $path
    Write-Host "Removed $path"
  }
}
```

After running, also delete this file (`STALE_FOLDERS.md`) and the
scratch-sibling catalog entries in `data/AGENT_CONTEXT.md`.
