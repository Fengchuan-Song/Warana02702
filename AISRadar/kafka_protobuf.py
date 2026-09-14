"""Decode Kafka ``TargetProtoListZ`` payloads into Radar observations."""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
import math

from google.protobuf.message import DecodeError

from AISData.kafka_protobuf import protobuf_timestamp_to_iso
from AISData.protos import UnionTargsZV1_pb2 as union_targets


RADAR_CLASSES = {union_targets.RADAR}
SIM_RADAR_CLASSES = {union_targets.SIM_RADAR}


@dataclass(frozen=True)
class ParsedRadarBatch:
    """Radar rows decoded from one Kafka envelope plus counters."""

    rows: list
    class_counts: dict
    ignored_counts: dict
    invalid_targets: int = 0


def _finite_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _extension_values(position):
    return {
        str(item.key or "").strip().casefold(): str(item.value or "").strip()
        for item in position.aisExtInfos
        if str(item.key or "").strip()
    }


def _extension(extensions, *names, default=""):
    for name in names:
        value = extensions.get(name.casefold())
        if value not in (None, ""):
            return value
    return default


def _source_name(position):
    return str(
        position.srcSiteID
        or position.SrcrDeviceID
        or position.vendorId
        or ""
    ).strip()


def _target_id(position):
    """Choose the most specific stable Radar track identifier available."""
    for value in (
        position.srcTargetKey,
        position.displayId,
        position.srcIndex,
        position.mmsi,
    ):
        text = str(value or "").strip()
        if text and text != "0":
            return text
    return ""


def parse_radar_protobuf(
    payload,
    *,
    timestamp_unit="milliseconds",
    accept_sim=False,
    received_at=None,
):
    """Parse one raw Protobuf envelope and retain Radar targets only.

    Values must be raw ``TargetProtoListZ.SerializeToString()`` bytes without
    a Schema Registry or application-specific framing header.
    """
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("Kafka Radar payload must be bytes-like")

    envelope = union_targets.TargetProtoListZ()
    try:
        envelope.ParseFromString(bytes(payload))
    except DecodeError:
        raise

    received_value = received_at or datetime.now(dt_timezone.utc).isoformat()
    allowed_classes = set(RADAR_CLASSES)
    if accept_sim:
        allowed_classes.update(SIM_RADAR_CLASSES)

    rows = []
    class_counts = Counter()
    ignored_counts = Counter()
    invalid_targets = 0

    for target in envelope.list:
        if not target.HasField("pos"):
            invalid_targets += 1
            continue

        position = target.pos
        try:
            class_name = union_targets.ShipClass.Name(position.sclass)
        except ValueError:
            class_name = f"UNKNOWN_{position.sclass}"
        class_counts[class_name] += 1

        if position.sclass not in allowed_classes:
            ignored_counts[class_name] += 1
            continue

        try:
            timestamp = protobuf_timestamp_to_iso(
                target.lastTm or position.lastTm,
                timestamp_unit,
            )
        except ValueError:
            invalid_targets += 1
            continue

        target_id = _target_id(position)
        if not timestamp or not target_id or not position.HasField("geoPtn"):
            invalid_targets += 1
            continue

        longitude = _finite_float(position.geoPtn.longitude)
        latitude = _finite_float(position.geoPtn.latitude)
        if (
            longitude is None
            or latitude is None
            or not -180 <= longitude <= 180
            or not -90 <= latitude <= 90
        ):
            invalid_targets += 1
            continue

        extensions = _extension_values(position)
        row = {
            "timestamp": timestamp,
            "received_at": received_value,
            "id": target_id,
            "lon": longitude,
            "lat": latitude,
            "course": _finite_float(position.course),
            "speed": _finite_float(position.speed),
            "heading": _finite_float(position.heading),
            "collection_type": class_name,
            "source": _source_name(position),
            "device_owner_id": position.deviceOwnerID,
            "display_id": position.displayId,
            "source_type": position.srcType,
            "source_index": position.srcIndex,
            "source_target_key": position.srcTargetKey,
        }
        gtid = _extension(extensions, "gtid", "ground_truth_id")
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
