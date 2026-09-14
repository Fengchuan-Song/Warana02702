"""Decode Kafka ``TargetProtoListZ`` payloads into canonical AIS source rows."""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone

from google.protobuf.message import DecodeError

from AISData.protos import UnionTargsZV1_pb2 as union_targets


TIMESTAMP_DIVISORS = {
    "seconds": 1,
    "milliseconds": 1_000,
    "microseconds": 1_000_000,
    "nanoseconds": 1_000_000_000,
}

DYNAMIC_AIS_CLASSES = {
    union_targets.AIS_A,
    union_targets.AIS_B,
}
STATIC_AIS_CLASSES = {union_targets.STATIC}
SIM_AIS_CLASSES = {union_targets.SIM}


@dataclass(frozen=True)
class ParsedAISBatch:
    """AIS rows decoded from one Kafka envelope plus observability counters."""

    rows: list
    class_counts: dict
    ignored_counts: dict
    invalid_targets: int = 0


def protobuf_timestamp_to_iso(value, unit="milliseconds"):
    """Convert the producer's unsigned epoch value to an aware ISO timestamp."""
    try:
        divisor = TIMESTAMP_DIVISORS[unit]
    except KeyError as exc:
        supported = ", ".join(sorted(TIMESTAMP_DIVISORS))
        raise ValueError(
            f"Unsupported Kafka timestamp unit {unit!r}; expected {supported}"
        ) from exc

    try:
        raw_value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Kafka AIS timestamp must be an integer") from exc
    if raw_value <= 0:
        return None

    try:
        return datetime.fromtimestamp(
            raw_value / divisor,
            tz=dt_timezone.utc,
        ).isoformat()
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError(f"Kafka AIS timestamp is out of range: {raw_value}") from exc


def _extension_values(position):
    return {
        str(item.key or "").strip().casefold(): str(item.value or "").strip()
        for item in position.aisExtInfos
        if str(item.key or "").strip()
    }


def _extension(extensions, *names, default=None):
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


def _base_row(position, timestamp, class_name, received_at):
    extensions = _extension_values(position)
    row = {
        "MMSI": str(position.mmsi),
        "timestamp": timestamp,
        "received_at": received_at,
        "Name": position.vesselName,
        "IMO": str(position.imo) if position.imo else "",
        "status": position.status,
        "course": position.course,
        "speed": position.speed,
        "heading": position.heading,
        "length": position.len,
        "width": position.wid,
        "ship_type": position.shiptype,
        "source": _source_name(position),
        "collection_type": class_name,
        "device_owner_id": position.deviceOwnerID,
        "display_id": position.displayId,
        "source_type": position.srcType,
        "source_index": position.srcIndex,
        "source_target_key": position.srcTargetKey,
        "call_sign": position.callSign,
        "rot": _extension(extensions, "rot", "rate_of_turn"),
        "draught": _extension(extensions, "draught", "draft"),
        "flag": _extension(extensions, "flag", default=""),
        "iso3": _extension(extensions, "iso3", default=""),
        "at_dock": _extension(extensions, "at_dock"),
        "matched_port_name": _extension(
            extensions,
            "matched_port_name",
            "matchedPortName",
            default="",
        ),
    }
    return row


def parse_ais_protobuf(
    payload,
    *,
    timestamp_unit="milliseconds",
    accept_sim=False,
    received_at=None,
):
    """Parse one raw Protobuf envelope and retain AIS/STATIC targets only.

    ``payload`` must be the raw result of Protobuf ``SerializeToString``.  A
    Schema Registry or custom framing header must be removed upstream.
    """
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("Kafka Protobuf payload must be bytes-like")

    envelope = union_targets.TargetProtoListZ()
    try:
        envelope.ParseFromString(bytes(payload))
    except DecodeError:
        raise

    received_value = received_at or datetime.now(dt_timezone.utc).isoformat()
    allowed_dynamic = set(DYNAMIC_AIS_CLASSES)
    if accept_sim:
        allowed_dynamic.update(SIM_AIS_CLASSES)

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

        is_static = position.sclass in STATIC_AIS_CLASSES
        is_dynamic = position.sclass in allowed_dynamic
        if not is_static and not is_dynamic:
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
        if not timestamp:
            invalid_targets += 1
            continue

        row = _base_row(
            position,
            timestamp,
            class_name,
            received_value,
        )

        if is_static:
            # The operational normaliser recognises AIS type 5/24 as static.
            row["msg_type"] = 5
            rows.append(row)
            continue

        if not position.HasField("geoPtn"):
            invalid_targets += 1
            continue

        row.update(
            {
                "longitude": position.geoPtn.longitude,
                "latitude": position.geoPtn.latitude,
            }
        )
        rows.append(row)

    return ParsedAISBatch(
        rows=rows,
        class_counts=dict(class_counts),
        ignored_counts=dict(ignored_counts),
        invalid_targets=invalid_targets,
    )
