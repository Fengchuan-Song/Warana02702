"""Deterministic local AIS broadcast and receiver simulation helpers."""

import csv
import heapq
import itertools
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8 in the Predict environment.
    from backports.zoneinfo import ZoneInfo

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from AISData.normalization import (
    ais_record_kind,
    normalise_dynamic_ais_record,
    normalise_static_ais_record,
)


EARTH_RADIUS_METRES = 6_371_000.0
KNOTS_TO_METRES_PER_SECOND = 0.514444


@dataclass(frozen=True)
class SimulationConfig:
    mode: str = "received"
    max_segment_gap_seconds: float = 30 * 60
    max_segment_speed_knots: float = 80.0
    gps_noise_metres: float = 5.0
    loss_rate: float = 0.05
    duplicate_rate: float = 0.005
    out_of_order_rate: float = 0.01
    minimum_latency_ms: float = 50.0
    maximum_latency_ms: float = 500.0
    out_of_order_extra_ms: float = 3000.0
    outage_rate_per_hour: float = 0.02
    outage_minimum_seconds: float = 30.0
    outage_maximum_seconds: float = 300.0

    def __post_init__(self):
        if self.mode not in {"observed", "broadcast", "received"}:
            raise ValueError("mode must be observed, broadcast or received")
        for field_name in ("loss_rate", "duplicate_rate", "out_of_order_rate"):
            value = getattr(self, field_name)
            if not 0 <= value <= 1:
                raise ValueError(f"{field_name} must be between 0 and 1")
        for field_name in (
            "max_segment_gap_seconds",
            "max_segment_speed_knots",
            "gps_noise_metres",
            "minimum_latency_ms",
            "maximum_latency_ms",
            "out_of_order_extra_ms",
            "outage_rate_per_hour",
            "outage_minimum_seconds",
            "outage_maximum_seconds",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} cannot be negative")
        if self.maximum_latency_ms < self.minimum_latency_ms:
            raise ValueError("maximum_latency_ms cannot be less than minimum_latency_ms")
        if self.outage_maximum_seconds < self.outage_minimum_seconds:
            raise ValueError(
                "outage_maximum_seconds cannot be less than outage_minimum_seconds"
            )


@dataclass(order=True)
class ScheduledAISRecord:
    delivery_time: datetime
    sequence: int
    kind: str
    record: dict


@dataclass(frozen=True)
class LoadedAISData:
    tracks: dict
    static_records: list
    selected_mmsis: tuple
    source_rows: int
    invalid_rows: int
    source_files: tuple = ()


def discover_ais_csv_paths(path):
    """Resolve one CSV or every nested CSV below a replay directory."""
    source_path = Path(path)
    if source_path.is_file():
        if source_path.suffix.lower() != ".csv":
            raise ValueError(f"AIS source file is not CSV: {source_path}")
        return (source_path,)
    if not source_path.exists():
        raise FileNotFoundError(f"AIS source does not exist: {source_path}")
    if not source_path.is_dir():
        raise ValueError(f"AIS source is not a file or directory: {source_path}")
    csv_paths = tuple(
        sorted(
            (
                candidate
                for candidate in source_path.rglob("*")
                if candidate.is_file() and candidate.suffix.lower() == ".csv"
            ),
            key=lambda candidate: candidate.relative_to(source_path)
            .as_posix()
            .casefold(),
        )
    )
    if not csv_paths:
        raise ValueError(f"AIS source directory contains no CSV files: {source_path}")
    return csv_paths


def _aware_timestamp(value, source_timezone="UTC"):
    parsed = parse_datetime(value) if isinstance(value, str) else value
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=ZoneInfo(source_timezone))
    return parsed


def _prepare_source_row(row, source_timezone):
    prepared = dict(row)
    for field in ("timestamp", "event_time", "DateTime", "datetime", "time"):
        if prepared.get(field) not in (None, ""):
            parsed = _aware_timestamp(prepared[field], source_timezone)
            if parsed is not None:
                prepared[field] = parsed.isoformat()
            break
    return prepared


def _record_time(record):
    return _aware_timestamp(record.get("timestamp"), "UTC")


def load_ais_csv(
    path,
    max_ships=200,
    mmsis=None,
    start_at=None,
    end_at=None,
    duration_minutes=60.0,
    source_timezone="UTC",
):
    """Load and time-bound dynamic tracks plus type 5/24 static records."""
    csv_paths = discover_ais_csv_paths(path)
    requested = {str(value).strip() for value in (mmsis or []) if str(value).strip()}
    selected = set()
    tracks = {}
    static_records = []
    source_rows = invalid_rows = 0

    for csv_path in csv_paths:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as source_file:
            reader = csv.DictReader(source_file)
            if not reader.fieldnames:
                raise ValueError(f"AIS CSV has no header: {csv_path}")
            for row in reader:
                source_rows += 1
                prepared = _prepare_source_row(row, source_timezone)
                raw_mmsi = prepared.get("MMSI", prepared.get("mmsi", ""))
                mmsi = str(raw_mmsi or "").strip()
                if len(mmsi) != 9 or not mmsi.isdigit():
                    invalid_rows += 1
                    continue
                if requested and mmsi not in requested:
                    continue
                if mmsi not in selected:
                    if not requested and len(selected) >= max(1, int(max_ships)):
                        continue
                    selected.add(mmsi)

                kind = ais_record_kind(prepared)
                if kind == "static":
                    item = normalise_static_ais_record(prepared)
                    if item is not None:
                        static_records.append(item)
                    else:
                        invalid_rows += 1
                    continue
                if kind != "dynamic":
                    invalid_rows += 1
                    continue
                item = normalise_dynamic_ais_record(prepared)
                if item is None:
                    invalid_rows += 1
                    continue
                tracks.setdefault(item["mmsi"], []).append(item)

    for mmsi, points in list(tracks.items()):
        by_timestamp = {}
        for point in points:
            by_timestamp[point["timestamp"]] = point
        ordered = sorted(by_timestamp.values(), key=_record_time)
        if ordered:
            tracks[mmsi] = ordered
        else:
            tracks.pop(mmsi, None)

    start_time = _aware_timestamp(start_at, source_timezone) if start_at else None
    end_time = _aware_timestamp(end_at, source_timezone) if end_at else None
    all_times = [
        _record_time(point)
        for points in tracks.values()
        for point in points
    ]
    all_times = [value for value in all_times if value is not None]
    if start_time is None and all_times:
        start_time = min(all_times)
    if end_time is None and start_time is not None and duration_minutes > 0:
        end_time = start_time + timedelta(minutes=duration_minutes)

    for mmsi, points in list(tracks.items()):
        bounded = [
            point
            for point in points
            if (start_time is None or _record_time(point) >= start_time)
            and (end_time is None or _record_time(point) <= end_time)
        ]
        if bounded:
            tracks[mmsi] = bounded
        else:
            tracks.pop(mmsi, None)
    static_records = [
        item
        for item in static_records
        if item["mmsi"] in tracks
        and (start_time is None or _record_time(item) >= start_time)
        and (end_time is None or _record_time(item) <= end_time)
    ]
    static_records.sort(key=_record_time)
    return LoadedAISData(
        tracks=tracks,
        static_records=static_records,
        selected_mmsis=tuple(sorted(tracks)),
        source_rows=source_rows,
        invalid_rows=invalid_rows,
        source_files=tuple(str(csv_path) for csv_path in csv_paths),
    )


def nominal_reporting_interval(record):
    """Return the nominal autonomous AIS dynamic reporting period in seconds."""
    message_type = record.get("msg_type")
    speed = record.get("speed")
    status = record.get("nav_status")
    rot = record.get("rot")
    turning = rot is not None and abs(float(rot)) > 1e-9
    if message_type == 27:
        return 180.0
    if message_type in {18, 19}:
        if speed is None or speed <= 2:
            return 180.0
        if speed <= 14:
            return 30.0
        if speed <= 23:
            return 5.0 if turning else 15.0
        return 5.0
    if status in {1, 5} and (speed is None or speed <= 3):
        return 180.0
    if speed is None or speed <= 14:
        return 10.0 / 3.0 if turning else 10.0
    if speed <= 23:
        return 2.0 if turning else 6.0
    return 2.0


def _distance_metres(first, second):
    first_lat = math.radians(first["lat"])
    second_lat = math.radians(second["lat"])
    delta_lat = second_lat - first_lat
    delta_lon = math.radians(second["lon"] - first["lon"])
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(first_lat) * math.cos(second_lat) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_METRES * math.asin(min(1.0, math.sqrt(value)))


def _interpolate_angle(first, second, fraction):
    if first is None:
        return second
    if second is None:
        return first
    delta = (second - first + 180) % 360 - 180
    return (first + delta * fraction) % 360


def _add_position_noise(record, metres, rng):
    if metres <= 0:
        return record
    noisy = record.copy()
    east = rng.gauss(0.0, metres)
    north = rng.gauss(0.0, metres)
    latitude_radians = math.radians(record["lat"])
    noisy["lat"] += math.degrees(north / EARTH_RADIUS_METRES)
    noisy["lon"] += math.degrees(
        east / (EARTH_RADIUS_METRES * max(abs(math.cos(latitude_radians)), 1e-9))
    )
    return noisy


def _interpolate_record(first, second, event_time, fraction, config, rng):
    item = first.copy()
    delta_lon = (second["lon"] - first["lon"] + 180) % 360 - 180
    item["lat"] = first["lat"] + (second["lat"] - first["lat"]) * fraction
    item["lon"] = ((first["lon"] + delta_lon * fraction + 180) % 360) - 180
    for field in ("speed", "draught"):
        if first.get(field) is not None and second.get(field) is not None:
            item[field] = first[field] + (second[field] - first[field]) * fraction
    for field in ("course", "heading"):
        item[field] = _interpolate_angle(
            first.get(field),
            second.get(field),
            fraction,
        )
    item["timestamp"] = event_time.isoformat()
    item["synthetic"] = True
    item["simulation_method"] = "segment_interpolation"
    return _add_position_noise(item, config.gps_noise_metres, rng)


def _valid_segment(first, second, config):
    start = _record_time(first)
    end = _record_time(second)
    if start is None or end is None or end <= start:
        return False
    gap = (end - start).total_seconds()
    if gap > config.max_segment_gap_seconds:
        return False
    implied_speed = _distance_metres(first, second) / gap
    return (
        implied_speed / KNOTS_TO_METRES_PER_SECOND
        <= config.max_segment_speed_knots
    )


def _iter_observed_track(track):
    for point in track:
        item = point.copy()
        item["synthetic"] = False
        item["simulation_method"] = "observed"
        yield item


def _iter_broadcast_track(track, config, rng):
    if not track:
        return
    if len(track) == 1:
        item = track[0].copy()
        item["synthetic"] = False
        item["simulation_method"] = "observed_anchor"
        yield item
        return
    for index in range(len(track) - 1):
        first = track[index]
        second = track[index + 1]
        anchor = first.copy()
        anchor["synthetic"] = False
        anchor["simulation_method"] = "observed_anchor"
        yield anchor
        if not _valid_segment(first, second, config):
            continue
        start = _record_time(first)
        end = _record_time(second)
        gap = (end - start).total_seconds()
        period = nominal_reporting_interval(first)
        event_time = start + timedelta(seconds=period)
        while event_time < end:
            fraction = (event_time - start).total_seconds() / gap
            yield _interpolate_record(
                first,
                second,
                event_time,
                fraction,
                config,
                rng,
            )
            event_time += timedelta(seconds=period)
    final = track[-1].copy()
    final["synthetic"] = False
    final["simulation_method"] = "observed_anchor"
    yield final


def _track_seed(seed, mmsi):
    digits = "".join(character for character in str(mmsi) if character.isdigit())
    return int(seed) + int((digits or "0")[-8:])


def _iter_dynamic_source_events(tracks, config, seed):
    heap = []
    sequence = itertools.count()
    for mmsi in sorted(tracks):
        rng = random.Random(_track_seed(seed, mmsi))
        iterator = iter(
            _iter_observed_track(tracks[mmsi])
            if config.mode == "observed"
            else _iter_broadcast_track(tracks[mmsi], config, rng)
        )
        first = next(iterator, None)
        if first is not None:
            heapq.heappush(
                heap,
                (_record_time(first), next(sequence), first, iterator),
            )
    while heap:
        _event_time, _order, record, iterator = heapq.heappop(heap)
        yield "dynamic", record
        following = next(iterator, None)
        if following is not None:
            heapq.heappush(
                heap,
                (_record_time(following), next(sequence), following, iterator),
            )


def _iter_source_events(tracks, static_records, config, seed):
    dynamic = _iter_dynamic_source_events(tracks, config, seed)
    static = (("static", item.copy()) for item in static_records)
    return heapq.merge(
        dynamic,
        static,
        key=lambda pair: _record_time(pair[1]),
    )


def _schedule_without_reception(events):
    for sequence, (kind, record) in enumerate(events):
        yield ScheduledAISRecord(
            delivery_time=_record_time(record),
            sequence=sequence,
            kind=kind,
            record=record,
        )


def _apply_reception(events, config, rng):
    pending = []
    sequence = itertools.count()
    last_event_time = {}
    outage_ends = {}
    for kind, source_record in events:
        event_time = _record_time(source_record)
        while pending and pending[0].delivery_time <= event_time:
            yield heapq.heappop(pending)

        mmsi = source_record["mmsi"]
        previous_time = last_event_time.get(mmsi)
        elapsed = (
            max(0.0, (event_time - previous_time).total_seconds())
            if previous_time is not None
            else 0.0
        )
        last_event_time[mmsi] = event_time
        outage_end = outage_ends.get(mmsi)
        if outage_end is None or event_time >= outage_end:
            probability = 1 - math.exp(
                -config.outage_rate_per_hour * elapsed / 3600.0
            )
            if rng.random() < probability:
                outage_end = event_time + timedelta(
                    seconds=rng.uniform(
                        config.outage_minimum_seconds,
                        config.outage_maximum_seconds,
                    )
                )
                outage_ends[mmsi] = outage_end
        if outage_end is not None and event_time < outage_end:
            continue
        if rng.random() < config.loss_rate:
            continue

        latency_ms = rng.uniform(
            config.minimum_latency_ms,
            config.maximum_latency_ms,
        )
        if rng.random() < config.out_of_order_rate:
            latency_ms += rng.uniform(0.0, config.out_of_order_extra_ms)
        delivery_time = event_time + timedelta(milliseconds=latency_ms)
        record = source_record.copy()
        record["simulation_received"] = True
        heapq.heappush(
            pending,
            ScheduledAISRecord(
                delivery_time=delivery_time,
                sequence=next(sequence),
                kind=kind,
                record=record,
            ),
        )
        if rng.random() < config.duplicate_rate:
            duplicate = record.copy()
            duplicate["simulation_duplicate"] = True
            heapq.heappush(
                pending,
                ScheduledAISRecord(
                    delivery_time=delivery_time
                    + timedelta(milliseconds=rng.uniform(1.0, 100.0)),
                    sequence=next(sequence),
                    kind=kind,
                    record=duplicate,
                ),
            )
    while pending:
        yield heapq.heappop(pending)


def iter_simulated_records(loaded, config, seed=42):
    """Yield delivery-ordered records without sleeping or mutating shared state."""
    events = _iter_source_events(
        loaded.tracks,
        loaded.static_records,
        config,
        seed,
    )
    if config.mode == "received":
        yield from _apply_reception(events, config, random.Random(seed))
    else:
        yield from _schedule_without_reception(events)
