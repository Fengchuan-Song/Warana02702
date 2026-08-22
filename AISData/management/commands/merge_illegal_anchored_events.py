import json
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone

from AISData.models import (
    ViolationAISTrajectoryPoint,
    ViolationEventRecord,
)


FEATURE_ID = "detect-illegalAnchored"
DEFAULT_GAP_MINUTES = 5


def _episode_start(record):
    raw_data = record.raw_data
    if not isinstance(raw_data, dict):
        return ""
    return str(raw_data.get("episode_started_at") or "").strip()


def _mergeable(previous_end, episode_starts, record, gap):
    if record.first_detected_at > previous_end + gap:
        return False
    incoming_start = _episode_start(record)
    return not (
        incoming_start
        and episode_starts
        and incoming_start not in episode_starts
    )


def find_merge_groups(queryset, gap):
    groups = []
    current = []
    current_fingerprint = None
    current_end = None
    episode_starts = set()

    for record in queryset.iterator(chunk_size=1000):
        same_fingerprint = record.fingerprint == current_fingerprint
        if (
            current
            and same_fingerprint
            and _mergeable(current_end, episode_starts, record, gap)
        ):
            current.append(record)
            current_end = max(current_end, record.last_detected_at)
            episode_start = _episode_start(record)
            if episode_start:
                episode_starts.add(episode_start)
            continue

        if len(current) > 1:
            groups.append(current)
        current = [record]
        current_fingerprint = record.fingerprint
        current_end = record.last_detected_at
        episode_start = _episode_start(record)
        episode_starts = {episode_start} if episode_start else set()

    if len(current) > 1:
        groups.append(current)
    return groups


def _model_data(instance):
    return {
        field.attname: getattr(instance, field.attname)
        for field in instance._meta.concrete_fields
    }


def write_backup(groups, path, gap_minutes):
    payload = {
        "created_at": timezone.now(),
        "feature_id": FEATURE_ID,
        "gap_minutes": gap_minutes,
        "groups": [],
    }
    for group in groups:
        payload["groups"].append(
            {
                "event_ids": [record.id for record in group],
                "events": [_model_data(record) for record in group],
                "trajectory_points": [
                    _model_data(point)
                    for record in group
                    for point in record.ais_trajectory.all()
                ],
            }
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise CommandError(f"Backup already exists: {path}")
    with path.open("w", encoding="utf-8") as destination:
        json.dump(
            payload,
            destination,
            cls=DjangoJSONEncoder,
            ensure_ascii=False,
            indent=2,
        )


def merge_group(records):
    survivor = min(records, key=lambda record: record.id)
    latest = max(
        records,
        key=lambda record: (record.last_detected_at, record.id),
    )
    fixed_records = [record for record in records if _episode_start(record)]
    stable_key_record = (
        max(
            fixed_records,
            key=lambda record: (record.last_detected_at, record.id),
        )
        if fixed_records
        else survivor
    )
    losers = [record for record in records if record.id != survivor.id]

    copied_points = []
    for record in losers:
        for point in record.ais_trajectory.all():
            copied_points.append(
                ViolationAISTrajectoryPoint(
                    event=survivor,
                    mmsi=point.mmsi,
                    observed_at=point.observed_at,
                    longitude=point.longitude,
                    latitude=point.latitude,
                    speed=point.speed,
                    course=point.course,
                    raw_data=point.raw_data,
                )
            )
    if copied_points:
        ViolationAISTrajectoryPoint.objects.bulk_create(
            copied_points,
            ignore_conflicts=True,
            batch_size=1000,
        )

    loser_ids = [record.id for record in losers]
    ViolationEventRecord.objects.filter(id__in=loser_ids).delete()

    latest_fields = (
        "fingerprint",
        "feature_id",
        "event_type",
        "target_id",
        "target_name",
        "status",
        "risk_level",
        "longitude",
        "latitude",
        "event_time",
        "details",
        "raw_data",
    )
    for field in latest_fields:
        setattr(survivor, field, getattr(latest, field))
    survivor.event_key = stable_key_record.event_key
    survivor.first_detected_at = min(
        record.first_detected_at for record in records
    )
    survivor.last_detected_at = max(
        record.last_detected_at for record in records
    )
    survivor.occurrence_count = sum(
        record.occurrence_count for record in records
    )
    survivor.save(
        update_fields=(
            "event_key",
            *latest_fields,
            "first_detected_at",
            "last_detected_at",
            "occurrence_count",
        )
    )
    return len(losers), len(copied_points)


class Command(BaseCommand):
    help = (
        "Safely merge duplicate historical illegal-anchoring records. "
        "The command is dry-run unless --apply is provided."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the merge after creating a JSON backup.",
        )
        parser.add_argument(
            "--target-id",
            help="Limit cleanup to one MMSI/target id.",
        )
        parser.add_argument(
            "--gap-minutes",
            type=int,
            default=DEFAULT_GAP_MINUTES,
            help="Maximum adjacent detection gap, default: 5.",
        )
        parser.add_argument(
            "--backup-path",
            help="Backup JSON path used with --apply.",
        )

    def handle(self, *args, **options):
        gap_minutes = options["gap_minutes"]
        if gap_minutes < 0:
            raise CommandError("--gap-minutes cannot be negative")
        gap = timedelta(minutes=gap_minutes)

        queryset = ViolationEventRecord.objects.filter(feature_id=FEATURE_ID)
        target_id = str(options.get("target_id") or "").strip()
        if target_id:
            queryset = queryset.filter(target_id=target_id)
        queryset = queryset.order_by(
            "fingerprint",
            "first_detected_at",
            "last_detected_at",
            "id",
        ).prefetch_related("ais_trajectory")
        groups = find_merge_groups(queryset, gap)
        duplicate_count = sum(len(group) - 1 for group in groups)
        self.stdout.write(
            "Merge plan: "
            f"groups={len(groups)}, duplicate_records={duplicate_count}, "
            f"gap={gap_minutes}m, target={target_id or 'ALL'}"
        )

        if not options["apply"]:
            self.stdout.write("Dry-run only; database was not changed.")
            return
        if not groups:
            self.stdout.write(self.style.SUCCESS("Nothing to merge."))
            return

        raw_backup_path = options.get("backup_path")
        if raw_backup_path:
            backup_path = Path(raw_backup_path).expanduser().resolve()
        else:
            timestamp = timezone.now().strftime("%Y%m%dT%H%M%S%fZ")
            backup_path = (
                Path(settings.BASE_DIR)
                / "outputs"
                / "backups"
                / f"illegal_anchored_events_{timestamp}.json"
            ).resolve()
        write_backup(groups, backup_path, gap_minutes)
        self.stdout.write(f"Backup created: {backup_path}")

        removed = 0
        copied_points = 0
        with transaction.atomic():
            for planned_group in groups:
                ids = [record.id for record in planned_group]
                locked_records = list(
                    ViolationEventRecord.objects.select_for_update()
                    .filter(id__in=ids)
                    .order_by("id")
                    .prefetch_related("ais_trajectory")
                )
                if len(locked_records) != len(ids):
                    raise CommandError(
                        "Records changed after planning; transaction aborted."
                    )
                group_removed, group_points = merge_group(locked_records)
                removed += group_removed
                copied_points += group_points

        self.stdout.write(
            self.style.SUCCESS(
                "Merge complete: "
                f"groups={len(groups)}, removed={removed}, "
                f"copied_trajectory_candidates={copied_points}, "
                f"backup={backup_path}"
            )
        )
