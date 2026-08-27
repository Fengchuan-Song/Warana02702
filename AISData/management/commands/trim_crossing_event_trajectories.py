import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from AISData.models import ViolationAISTrajectoryPoint, ViolationEventRecord


def _event_identity(record):
    raw = record.raw_data if isinstance(record.raw_data, dict) else {}
    mmsi = str(raw.get("mmsi") or record.target_id or "").strip()
    fence_id = str(raw.get("fence_id") or "").strip()
    return (
        record.source_namespace,
        record.simulation_id,
        fence_id,
        mmsi,
    )


def crossing_trajectory_cutoffs(records, maximum_cycle_gap):
    """Return event-id cutoffs that can be proven from crossing transitions."""
    grouped = {}
    for record in records:
        raw = record.raw_data if isinstance(record.raw_data, dict) else {}
        direction = raw.get("crossing_direction")
        if direction not in {"enter", "exit", "transit"} or not record.event_time:
            continue
        grouped.setdefault(_event_identity(record), []).append(record)

    cutoffs = {}
    for events in grouped.values():
        events.sort(key=lambda item: (item.event_time, item.id))
        for index, record in enumerate(events):
            direction = record.raw_data.get("crossing_direction")
            if direction in {"exit", "transit"}:
                cutoffs[record.id] = record.event_time
                continue
            for later in events[index + 1 :]:
                gap = later.event_time - record.event_time
                if gap > maximum_cycle_gap:
                    break
                later_direction = later.raw_data.get("crossing_direction")
                if later_direction in {"exit", "transit"}:
                    cutoffs[record.id] = later.event_time
                    break
    return cutoffs


class Command(BaseCommand):
    help = (
        "Audit or remove CrossingBoundary trajectory points that occur after "
        "a provable exit/transit cutoff. The default is read-only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Delete matched points after writing a JSONL backup.",
        )
        parser.add_argument(
            "--mmsi",
            action="append",
            default=[],
            help="Limit the audit to one MMSI; may be supplied more than once.",
        )
        parser.add_argument(
            "--maximum-cycle-hours",
            type=float,
            default=24.0,
            help="Maximum enter-to-exit interval used to pair an event.",
        )

    def handle(self, *args, **options):
        mmsis = sorted(
            {
                str(value).strip()
                for value in options["mmsi"]
                if str(value).strip()
            }
        )
        records = ViolationEventRecord.objects.filter(
            feature_id="detect-CrossingBoundary"
        ).only(
            "id",
            "source_namespace",
            "simulation_id",
            "target_id",
            "event_time",
            "raw_data",
        )
        if mmsis:
            records = records.filter(target_id__in=mmsis)
        maximum_cycle_gap = timedelta(
            hours=max(0.0, options["maximum_cycle_hours"])
        )
        cutoffs = crossing_trajectory_cutoffs(
            list(records),
            maximum_cycle_gap,
        )

        point_ids = []
        for event_id, cutoff in cutoffs.items():
            point_ids.extend(
                ViolationAISTrajectoryPoint.objects.filter(
                    event_id=event_id,
                    observed_at__gt=cutoff,
                ).values_list("id", flat=True)
            )

        mode = "apply" if options["apply"] else "dry-run"
        self.stdout.write(
            f"Crossing trajectory trim audit: mode={mode}, "
            f"events={len(cutoffs)}, matched_points={len(point_ids)}, "
            f"mmsis={mmsis or 'all'}"
        )
        if not options["apply"] or not point_ids:
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
        backup_path = backup_directory / f"crossing-post-exit-{timestamp}.jsonl"
        with backup_path.open("w", encoding="utf-8", newline="\n") as output:
            for start in range(0, len(point_ids), 1000):
                rows = ViolationAISTrajectoryPoint.objects.filter(
                    id__in=point_ids[start : start + 1000]
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
                        json.dumps(row, ensure_ascii=False, default=str) + "\n"
                    )
        self.stdout.write(f"Backup created: {backup_path}")

        deleted = 0
        with transaction.atomic():
            for start in range(0, len(point_ids), 1000):
                count, _ = ViolationAISTrajectoryPoint.objects.filter(
                    id__in=point_ids[start : start + 1000]
                ).delete()
                deleted += count
        self.stdout.write(self.style.SUCCESS(f"Deleted post-exit points: {deleted}"))
