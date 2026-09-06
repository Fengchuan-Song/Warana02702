"""Run one integrated detector over an isolated offline evaluation dataset."""

from __future__ import annotations

from datetime import datetime, timezone
import traceback

from django.core.management.base import BaseCommand, CommandError

from performance_tests.ais_runner import AISRunner
from performance_tests.dataset import DatasetError, load_dataset
from performance_tests.fusion_runner import FusionRunner
from performance_tests.metrics import calculate_metrics
from performance_tests.registry import SUPPORTED_DETECTORS, detector_kind
from performance_tests.result_writer import (
    detector_configuration,
    git_commit,
    prepare_result_directory,
    write_results,
)
from performance_tests.schemas import PredictionRecord
from performance_tests.state_reset import isolated_sample_state


class Command(BaseCommand):
    help = (
        "Evaluate one of the 16 integrated warning detectors with accelerated, "
        "source-time replay and isolated side effects."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--feature-id",
            required=True,
            choices=sorted(SUPPORTED_DETECTORS),
        )
        parser.add_argument(
            "--dataset",
            required=True,
            help="Dataset manifest or directory containing samples.json/jsonl/csv.",
        )
        parser.add_argument(
            "--output",
            required=True,
            help="Root directory for performance result artifacts.",
        )
        parser.add_argument(
            "--test-id",
            default="",
            help="Optional stable run identifier; generated when omitted.",
        )
        parser.add_argument(
            "--fail-fast",
            action="store_true",
            help="Stop after the first sample error.",
        )

    def handle(self, *args, **options):
        feature_id = options["feature_id"]
        try:
            dataset = load_dataset(options["dataset"], feature_id=feature_id)
            result_dir = prepare_result_directory(
                options["output"],
                feature_id,
                options["test_id"] or None,
            )
        except (DatasetError, OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc

        kind = detector_kind(feature_id)
        runner = AISRunner(feature_id) if kind == "ais" else FusionRunner(feature_id)
        started_at = datetime.now(timezone.utc)
        records = []
        self.stdout.write(
            self.style.NOTICE(
                f"Evaluating {feature_id}: samples={len(dataset.samples)}, "
                f"runner={runner.__class__.__name__}"
            )
        )

        for index, sample in enumerate(dataset.samples, 1):
            try:
                with isolated_sample_state(include_database=kind == "ais"):
                    record = runner.run(sample)
            except Exception as exc:
                record = PredictionRecord(
                    sample_id=sample.sample_id,
                    track_id=sample.track_id,
                    feature_id=feature_id,
                    ground_truth=sample.ground_truth,
                    prediction=None,
                    result="ERROR",
                    error="".join(
                        traceback.format_exception(type(exc), exc, exc.__traceback__)
                    ),
                )
                self.stderr.write(
                    self.style.ERROR(
                        f"[{index}/{len(dataset.samples)}] {sample.sample_id}: {exc}"
                    )
                )
                records.append(record)
                if options["fail_fast"]:
                    break
                continue
            records.append(record)
            self.stdout.write(
                f"[{index}/{len(dataset.samples)}] {sample.sample_id}: "
                f"prediction={record.prediction}, result={record.result}"
            )

        metrics = calculate_metrics(records, feature_id=feature_id)
        finished_at = datetime.now(timezone.utc)
        config = {
            "feature_id": feature_id,
            "runner": runner.__class__.__name__,
            "replay": "as-fast-as-possible source-time replay",
            "side_effect_isolation": {
                "cache": "per-sample LocMemCache",
                "database": (
                    "per-sample forced transaction rollback"
                    if kind == "ais"
                    else "not used by fusion detector core"
                ),
                "websocket": False,
                "operational_detection_source": False,
            },
            "git_commit": git_commit(),
            "dataset": {
                "manifest": str(dataset.source),
                "version": dataset.version,
                "sha256": dataset.fingerprint,
                "metadata": dataset.metadata,
            },
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_seconds": (finished_at - started_at).total_seconds(),
            **detector_configuration(feature_id),
        }
        write_results(result_dir, records, metrics, config)

        summary = (
            f"TP={metrics['tp']} FP={metrics['fp']} "
            f"TN={metrics['tn']} FN={metrics['fn']} "
            f"errors={metrics['error_count']}"
        )
        self.stdout.write(self.style.SUCCESS(f"Evaluation complete: {summary}"))
        self.stdout.write(str(result_dir))
