"""Write reproducible detector evaluation artifacts outside business tables."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import uuid

from django.conf import settings


CSV_FIELDS = (
    "sample_id",
    "track_id",
    "feature_id",
    "ground_truth",
    "prediction",
    "result",
    "prediction_id",
    "alert_time",
    "detail",
)


def make_test_id():
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def prepare_result_directory(output_root, feature_id, test_id=None):
    test_id = str(test_id or make_test_id()).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", test_id):
        raise ValueError("test_id may contain only letters, numbers, '.', '_' and '-'")
    result_dir = Path(output_root).expanduser().resolve() / feature_id / test_id
    if result_dir.exists():
        raise FileExistsError(f"Evaluation result directory already exists: {result_dir}")
    result_dir.mkdir(parents=True)
    return result_dir


def git_commit():
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=settings.BASE_DIR,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return completed.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def detector_configuration(feature_id):
    from AISData.maritime_zone_registry import get_zone_snapshot
    from AISData.model_parameters import serialize_configuration

    try:
        parameters = serialize_configuration(feature_id)
    except Exception as exc:
        parameters = {"unavailable": str(exc)}
    try:
        zone_snapshot = get_zone_snapshot()
        zone_revision = zone_snapshot.get("revision")
    except Exception as exc:
        zone_revision = f"unavailable: {exc}"

    result = {
        "parameters": parameters,
        "region_version": zone_revision,
    }
    if feature_id in {"detect-ais-off", "detect-spoofing"}:
        from AISRadar.views import _options, checkpoint_sha256

        options = _options()
        weights = Path(options["weights"])
        result["fusion_parameters"] = {
            key: str(value) if isinstance(value, Path) else value
            for key, value in options.items()
        }
        result["model_weights"] = {
            "path": str(weights),
            "sha256": checkpoint_sha256(weights) if weights.is_file() else None,
        }
    return result


def write_results(result_dir, records, metrics, config):
    result_dir = Path(result_dir)
    with (result_dir / "predictions.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(record.as_csv_row() for record in records)

    (result_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    (result_dir / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    errors = [
        {
            "sample_id": record.sample_id,
            "track_id": record.track_id,
            "error": record.error,
        }
        for record in records
        if record.error
    ]
    (result_dir / "errors.log").write_text(
        "".join(
            json.dumps(error, ensure_ascii=False, default=str) + "\n"
            for error in errors
        ),
        encoding="utf-8",
    )

