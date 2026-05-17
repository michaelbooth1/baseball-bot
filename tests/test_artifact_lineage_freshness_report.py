import json
import os
import time
from pathlib import Path

from scripts.analysis import build_artifact_lineage_freshness_report as lineage


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_report_flags_downstream_older_by_mtime_and_max_date(tmp_path):
    upstream_manifest = tmp_path / "upstream" / "manifest.json"
    upstream_rows = tmp_path / "upstream" / "rows.jsonl"
    downstream_report = tmp_path / "downstream" / "report.json"

    _write_json(
        upstream_manifest,
        {
            "generated_at_utc": "2026-05-15T01:00:00Z",
            "config": {"max_date": "2026-05-14"},
            "counts": {
                "training_rows_total": 2,
                "rows_by_family": {
                    "score_event_transition": 1,
                    "no_score_drift": 1,
                },
            },
        },
    )
    _write_jsonl(
        upstream_rows,
        [
            {"session_date": "2026-05-13", "signal_model_family": "score_event_transition"},
            {"session_date": "2026-05-14", "signal_model_family": "no_score_drift"},
        ],
    )
    _write_json(
        downstream_report,
        {
            "generated_at_utc": "2026-05-14T23:00:00Z",
            "config": {"max_date": "2026-05-13"},
            "source_rows": 2,
        },
    )

    now = time.time()
    os.utime(downstream_report, (now - 100, now - 100))
    os.utime(upstream_rows, (now, now))
    os.utime(upstream_manifest, (now, now))

    specs = [
        lineage.ArtifactSpec(
            "upstream_table",
            "table",
            upstream_manifest,
            row_path=upstream_rows,
        ),
        lineage.ArtifactSpec(
            "downstream_model",
            "model",
            downstream_report,
            input_paths=(upstream_rows,),
        ),
    ]

    report = lineage.build_report(
        project_dir=tmp_path,
        specs=specs,
        max_hash_bytes=1024 * 1024,
        max_jsonl_scan_bytes=1024 * 1024,
    )

    downstream = next(art for art in report["artifacts"] if art["name"] == "downstream_model")
    assert downstream["health"]["status"] == "warning"
    assert downstream["health"]["stale_by_mtime"] is True
    assert downstream["health"]["stale_by_max_date"] is True
    assert "output_older_than_newest_input_mtime" in downstream["health"]["warnings"]
    assert "artifact_max_date_lags_upstream_max_date" in downstream["health"]["warnings"]
    assert downstream["inputs"][0]["upstream_artifact_name"] == "upstream_table"
    assert len(downstream["inputs"][0]["content_sha256"]) == 64
    assert report["summary"]["stale_by_mtime"] == 1
    assert report["summary"]["stale_by_max_date"] == 1


def test_jsonl_scan_supplies_row_family_and_date_stats(tmp_path):
    primary = tmp_path / "report.json"
    rows = tmp_path / "rows.jsonl"
    _write_json(primary, {"generated_at_utc": "2026-05-15T02:00:00Z"})
    _write_jsonl(
        rows,
        [
            {"session_date": "2026-05-11", "signal_model_family": "score_event_transition"},
            {"session_date": "2026-05-12", "signal_model_family": "score_event_transition"},
            {"session_date": "2026-05-13", "signal_model_family": "no_score_drift"},
        ],
    )

    report = lineage.build_report(
        project_dir=tmp_path,
        specs=[
            lineage.ArtifactSpec(
                "row_scanned_report",
                "report",
                primary,
                row_path=rows,
            )
        ],
        max_jsonl_scan_bytes=1024 * 1024,
    )

    artifact = report["artifacts"][0]
    assert artifact["row_count"] == 3
    assert artifact["max_date"] == "2026-05-13"
    assert artifact["family_counts"] == {
        "no_score_drift": 1,
        "score_event_transition": 2,
    }
    assert artifact["health"]["status"] == "ok"


def test_write_report_outputs_json_markdown_and_csv(tmp_path):
    primary = tmp_path / "source.json"
    _write_json(primary, {"generated_at_utc": "2026-05-15T02:00:00Z", "max_date": "2026-05-14"})
    report = lineage.build_report(
        project_dir=tmp_path,
        specs=[lineage.ArtifactSpec("source_report", "report", primary)],
    )

    paths = lineage.write_report(report, tmp_path / "out", "lineage")

    assert Path(paths["json_path"]).exists()
    assert Path(paths["markdown_path"]).exists()
    assert Path(paths["csv_path"]).exists()
    md = Path(paths["markdown_path"]).read_text(encoding="utf-8")
    assert "Artifact Lineage/Freshness Report" in md
    assert "source_report" in md
