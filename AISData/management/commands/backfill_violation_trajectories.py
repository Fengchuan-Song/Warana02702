"""Backfill pre-detection AIS tracks from time-partitioned CSV files."""

import csv
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from AISData.models import ViolationEventRecord
from AISData.violation_records import save_trajectory_sources


FILE_TIME_FORMAT = "%Y-%m-%d %H-%M-%S"
MMSI_PATTERN = re.compile(r"(?<!\d)\d{9}(?!\d)")


def _file_time(path):
    try:
        value = datetime.strptime(path.stem, FILE_TIME_FORMAT)
    except ValueError:
        return None
    return timezone.make_aware(value, timezone.get_current_timezone())


def _floor_half_minute(value):
    return value.replace(second=30 if value.second >= 30 else 0, microsecond=0)


def _finite_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _row_datetime(value):
    observed_at = parse_datetime(str(value).strip())
    if observed_at is None:
        return None
    if timezone.is_naive(observed_at):
        observed_at = timezone.make_aware(
            observed_at,
            timezone.get_current_timezone(),
        )
    return observed_at


class Command(BaseCommand):
    help = (
        "Backfill each violation event's pre-detection AIS trajectory from "
        "Data/AIS/timeDivision_v2_30s without rerunning detectors."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--data-dir",
            default=str(
                Path(settings.BASE_DIR)
                / "Data"
                / "AIS"
                / "timeDivision_v2_30s"
            ),
        )
        parser.add_argument(
            "--window-minutes",
            type=int,
            default=30,
        )
        parser.add_argument(
            "--record-id",
            action="append",
            type=int,
            dest="record_ids",
        )
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--progress-every",
            type=int,
            default=250,
        )

    def handle(self, *args, **options):
        data_directory = Path(options["data_dir"]).resolve()
        if not data_directory.is_dir():
            raise CommandError(f"AIS data directory does not exist: {data_directory}")
        if options["window_minutes"] < 1:
            raise CommandError("--window-minutes must be at least 1")

        available_files = {}
        for path in data_directory.glob("*.csv"):
            timestamp = _file_time(path)
            if timestamp is not None:
                available_files[timestamp] = path
        if not available_files:
            raise CommandError(f"No time-partitioned AIS CSV files found in {data_directory}")

        records = ViolationEventRecord.objects.exclude(target_id="").only(
            "id",
            "target_id",
            "first_detected_at",
            "raw_data",
        )
        if options.get("record_ids"):
            records = records.filter(id__in=options["record_ids"])
        records_by_id = {record.id: record for record in records}
        if not records_by_id:
            self.stdout.write(self.style.WARNING("No matching violation records."))
            return

        window_size = timedelta(minutes=options["window_minutes"])
        associations = defaultdict(lambda: defaultdict(list))
        matched_records = set()
        for record in records_by_id.values():
            mmsis = sorted(set(MMSI_PATTERN.findall(record.target_id)))
            if not mmsis:
                continue
            start_at = record.first_detected_at - window_size
            if isinstance(record.raw_data, dict):
                trajectory_started_at = _row_datetime(
                    record.raw_data.get("trajectory_started_at")
                    or record.raw_data.get("episode_started_at")
                    or ""
                )
                if trajectory_started_at is not None:
                    start_at = max(start_at, trajectory_started_at)
            end_at = record.first_detected_at
            bucket = _floor_half_minute(start_at)
            while bucket <= end_at:
                if bucket in available_files:
                    for mmsi in mmsis:
                        associations[bucket][mmsi].append(
                            (record.id, start_at, end_at)
                        )
                    matched_records.add(record.id)
                bucket += timedelta(seconds=30)

        relevant_times = sorted(associations)
        self.stdout.write(
            "Backfill plan: "
            f"records={len(matched_records)}, files={len(relevant_times)}, "
            f"window={options['window_minutes']}m, dry_run={options['dry_run']}"
        )
        points_by_record = defaultdict(list)
        matched_rows = 0
        invalid_rows = 0
        progress_every = max(1, options["progress_every"])
        for file_index, file_timestamp in enumerate(relevant_times, start=1):
            path = available_files[file_timestamp]
            active = associations[file_timestamp]
            with path.open("r", encoding="utf-8", newline="") as source_file:
                reader = csv.reader(source_file)
                try:
                    header = next(reader)
                    columns = {name.strip(): index for index, name in enumerate(header)}
                    required = {
                        name: columns[name]
                        for name in ("timestamp", "MMSI", "latitude", "longitude")
                    }
                except (StopIteration, KeyError):
                    invalid_rows += 1
                    continue
                speed_index = columns.get("speed")
                course_index = columns.get("course")
                for row in reader:
                    try:
                        mmsi = str(row[required["MMSI"]]).strip()
                    except IndexError:
                        invalid_rows += 1
                        continue
                    windows = active.get(mmsi)
                    if not windows:
                        continue
                    try:
                        observed_at = _row_datetime(row[required["timestamp"]])
                        latitude = _finite_float(row[required["latitude"]])
                        longitude = _finite_float(row[required["longitude"]])
                    except IndexError:
                        invalid_rows += 1
                        continue
                    if (
                        observed_at is None
                        or latitude is None
                        or longitude is None
                        or not -90 <= latitude <= 90
                        or not -180 <= longitude <= 180
                    ):
                        invalid_rows += 1
                        continue
                    speed = (
                        _finite_float(row[speed_index])
                        if speed_index is not None and speed_index < len(row)
                        else None
                    )
                    course = (
                        _finite_float(row[course_index])
                        if course_index is not None and course_index < len(row)
                        else None
                    )
                    point = (
                        mmsi,
                        observed_at,
                        longitude,
                        latitude,
                        speed,
                        course,
                        path.name,
                    )
                    for record_id, start_at, end_at in windows:
                        if start_at <= observed_at < end_at:
                            points_by_record[record_id].append(point)
                            matched_rows += 1
            if file_index % progress_every == 0 or file_index == len(relevant_times):
                self.stdout.write(
                    f"Scanned {file_index}/{len(relevant_times)} files; "
                    f"matched point-record pairs={matched_rows}"
                )

        if options["dry_run"]:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Dry run complete: records_with_points={len(points_by_record)}, "
                    f"matched_pairs={matched_rows}, invalid_rows={invalid_rows}"
                )
            )
            return

        inserted = 0
        completed = 0
        total_records = len(points_by_record)
        for record_id, raw_points in points_by_record.items():
            sources = []
            for (
                mmsi,
                observed_at,
                longitude,
                latitude,
                speed,
                course,
                source_name,
            ) in raw_points:
                sources.append(
                    (
                        mmsi,
                        {
                            "mmsi": mmsi,
                            "timestamp": observed_at.isoformat(),
                            "lon": longitude,
                            "lat": latitude,
                            "speed": speed,
                            "course": course,
                            "backfilled": True,
                            "source_file": source_name,
                        },
                    )
                )
            inserted += save_trajectory_sources(
                records_by_id[record_id],
                sources,
            )
            completed += 1
            if completed % 250 == 0 or completed == total_records:
                self.stdout.write(
                    f"Saved {completed}/{total_records} records; inserted={inserted}"
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Backfill complete: records={total_records}, inserted={inserted}, "
                f"matched_pairs={matched_rows}, invalid_rows={invalid_rows}"
            )
        )
