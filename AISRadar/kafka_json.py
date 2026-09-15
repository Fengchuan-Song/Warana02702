"""Decode JSON Kafka target payloads into Radar observations."""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone

from AISData.kafka_json import (
    class_name,
    decode_target_json,
    extension,
    extension_values,
    finite_float,
    first_value,
    kafka_timestamp_to_iso,
    target_body,
    target_position,
)


RADAR_CLASSES = {"RADAR"}
SIM_RADAR_CLASSES = {"SIM_RADAR"}


@dataclass(frozen=True)
class ParsedRadarBatch:
    """Radar rows decoded from one Kafka message plus counters."""

    rows: list
    class_counts: dict
    ignored_counts: dict
    invalid_targets: int = 0


def _identifier_text(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return ""
    text = str(value or "").strip()
    return "" if text == "0" else text


def _target_id(body, fallback=None):
    """Choose a stable Radar track identifier supplied by the producer."""
    for name in (
        "id",
        "targetId",
        "target_id",
        "trackId",
        "track_id",
        "srcTargetKey",
        "source_target_key",
        "displayId",
        "display_id",
        "srcIndex",
        "source_index",
        "mmsi",
    ):
        text = _identifier_text(first_value(body, name))
        if text:
            return text
    return _identifier_text(fallback)


def parse_radar_json(
    payload,
    *,
    timestamp_unit="milliseconds",
    accept_sim=False,
    received_at=None,
    fallback_target_id=None,
):
    """Parse one Kafka JSON message and retain Radar targets only.

    ``fallback_target_id`` is intended for the Kafka message key and is used
    only when the message contains a single target.
    """
    targets = decode_target_json(payload)
    received_value = received_at or datetime.now(dt_timezone.utc).isoformat()
    allowed_classes = set(RADAR_CLASSES)
    if accept_sim:
        allowed_classes.update(SIM_RADAR_CLASSES)

    rows = []
    class_counts = Counter()
    ignored_counts = Counter()
    invalid_targets = 0

    for target in targets:
        body = target_body(target)
        if body is None:
            invalid_targets += 1
            continue

        target_class = class_name(first_value(body, "sclass", "shipClass", "collection_type"))
        class_counts[target_class] += 1
        if target_class not in allowed_classes:
            ignored_counts[target_class] += 1
            continue

        raw_timestamp = first_value(
            target,
            "lastTm",
            "timestamp",
            default=first_value(body, "lastTm", "timestamp"),
        )
        try:
            timestamp = kafka_timestamp_to_iso(raw_timestamp, timestamp_unit)
        except ValueError:
            invalid_targets += 1
            continue

        target_id = _target_id(
            body,
            fallback=fallback_target_id if len(targets) == 1 else None,
        )
        position = target_position(body)
        longitude = finite_float(first_value(position, "longitude", "lon", "lng"))
        latitude = finite_float(first_value(position, "latitude", "lat"))
        if (
            not timestamp
            or not target_id
            or longitude is None
            or latitude is None
            or not -180 <= longitude <= 180
            or not -90 <= latitude <= 90
        ):
            invalid_targets += 1
            continue

        extensions = extension_values(body)
        row = {
            "timestamp": timestamp,
            "received_at": received_value,
            "id": target_id,
            "lon": longitude,
            "lat": latitude,
            "course": finite_float(first_value(body, "course", "cog")),
            "speed": finite_float(first_value(body, "speed", "sog")),
            "heading": finite_float(first_value(body, "heading")),
            "collection_type": target_class,
            "source": str(
                first_value(
                    body,
                    "srcSiteID",
                    "SrcrDeviceID",
                    "vendorId",
                    "source",
                    default="",
                )
            ).strip(),
            "device_owner_id": first_value(body, "deviceOwnerID", "device_owner_id"),
            "display_id": first_value(body, "displayId", "display_id"),
            "source_type": first_value(body, "srcType", "source_type"),
            "source_index": first_value(body, "srcIndex", "source_index"),
            "source_target_key": first_value(
                body,
                "srcTargetKey",
                "source_target_key",
                default="",
            ),
            "mmsi": str(first_value(body, "mmsi", default="")),
            "status": first_value(body, "status"),
            "length": first_value(body, "len", "length"),
            "width": first_value(body, "wid", "width"),
            "ship_type": first_value(body, "shipType", "shiptype", "ship_type"),
            "vessel_name": str(first_value(body, "vesselName", "name", default="")),
        }
        gtid = extension(extensions, "gtid", "ground_truth_id", default="")
        if gtid:
            row["gtid"] = gtid
        rows.append(row)

    return ParsedRadarBatch(
        rows=rows,
        class_counts=dict(class_counts),
        ignored_counts=dict(ignored_counts),
        invalid_targets=invalid_targets,
    )


def radar_rows_for_fusion(rows):
    """Convert normalised Radar observations to the matcher table contract."""
    result = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        item = {
            "DateTime": row.get("timestamp"),
            "ID": row.get("id"),
            "X": row.get("lat"),
            "Y": row.get("lon"),
        }
        for source_name, target_name in (
            ("speed", "speed"),
            ("course", "course"),
            ("gtid", "GTID"),
        ):
            value = row.get(source_name)
            if value not in (None, ""):
                item[target_name] = value
        if all(item.get(field) not in (None, "") for field in ("DateTime", "ID", "X", "Y")):
            result.append(item)
    return result
