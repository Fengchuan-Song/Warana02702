import json
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from AISData.models import ViolationAISTrajectoryPoint


def is_crossing_result_artifact(raw_data):
    """Return true for detector results that were stored as if they were AIS."""
    if not isinstance(raw_data, dict):
        return False
    return (
        raw_data.get("event") == "CrossingBoundary"
        and not raw_data.get("timestamp")
        and not raw_data.get("time")
    )


class Command(BaseCommand):
    help = (
        "Audit or remove CrossingBoundary detector-result locations that "
        "were incorrectly persisted as AIS trajectory points."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Delete matched artifacts. The default is a read-only audit.",
        )
        parser.add_argument(
            "--mmsi",
            action="append",
            default=[],
            help="Limit the audit to one MMSI; may be supplied more than once.",
        )

    def handle(self, *args, **options):
        queryset = ViolationAISTrajectoryPoint.objects.filter(
            event__feature_id="detect-CrossingBoundary"
        ).only("id", "mmsi", "raw_data")
        mmsis = sorted(
            {
                str(value).strip()
                for value in options["mmsi"]
                if str(value).strip()
            }
        )
        if mmsis:
            queryset = queryset.filter(mmsi__in=mmsis)

        artifact_ids = [
            point.id
            for point in queryset.iterator(chunk_size=2000)
            if is_crossing_result_artifact(point.raw_data)
        ]
        mode = "apply" if options["apply"] else "dry-run"
        self.stdout.write(
            f"Crossing trajectory artifact audit: mode={mode}, "
            f"matched={len(artifact_ids)}, mmsis={mmsis or 'all'}"
        )
        if not options["apply"] or not artifact_ids:
            return

        backup_directory = Path(
            getattr(
                settings,
                "CROSSING_TRAJECTORY_CLEANUP_BACKUP_DIR",
                Path(settings.BASE_DIR)
                / ".runtime"
                / "crossing-trajectory-cleanup",
            )
        )
        backup_directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = backup_directory / f"crossing-artifacts-{timestamp}.jsonl"
        with backup_path.open("w", encoding="utf-8", newline="\n") as output:
            for start in range(0, len(artifact_ids), 1000):
                rows = ViolationAISTrajectoryPoint.objects.filter(
                    id__in=artifact_ids[start : start + 1000]
                ).values(
                    "id",
                    "event_id",
                    "mmsi",
                    "observed_at",
                    "longitude",
                    "latitude",
                    "speed",
                    "course",
                    "raw_data",
                )
                for row in rows:
                    output.write(
                        json.dumps(row, ensure_ascii=False, default=str)
                        + "\n"
                    )
        self.stdout.write(f"Backup created: {backup_path}")

        deleted = 0
        with transaction.atomic():
            for start in range(0, len(artifact_ids), 1000):
                count, _ = ViolationAISTrajectoryPoint.objects.filter(
                    id__in=artifact_ids[start : start + 1000]
                ).delete()
                deleted += count
        self.stdout.write(self.style.SUCCESS(f"Deleted artifacts: {deleted}"))
