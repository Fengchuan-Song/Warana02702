"""Decode JSON Kafka target payloads into canonical AIS source rows."""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
import json
import math


TIMESTAMP_DIVISORS = {
    "seconds": 1,
    "milliseconds": 1_000,
    "microseconds": 1_000_000,
    "nanoseconds": 1_000_000_000,
}

DYNAMIC_AIS_CLASSES = {"AIS_A", "AIS_B"}
STATIC_AIS_CLASSES = {"STATIC"}
SIM_AIS_CLASSES = {"SIM"}

# Retain the numeric values used by the former protobuf contract.  Accepting
# them costs nothing and makes a producer-side string migration less brittle.
CLASS_NAMES = {
    0: "AIS_A",
    1: "AIS_B",
    2: "RADAR",
    3: "SIM",
    4: "NORMAL_AIS_A_RADAR",
    5: "NORMAL_AIS_B_RADAR",
    6: "LOST_AIS_A_RADAR",
    7: "LOST_AIS_B_RADAR",
    8: "SIM_RADAR",
    9: "AID",
    10: "STATIC",
}


@dataclass(frozen=True)
class ParsedAISBatch:
    """AIS rows decoded from one Kafka message plus observability counters."""

    rows: list
    class_counts: dict
    ignored_counts: dict
    invalid_targets: int = 0


def kafka_timestamp_to_iso(value, unit="milliseconds"):
    """Convert an epoch value (or an ISO string) to an aware ISO timestamp."""
    try:
        divisor = TIMESTAMP_DIVISORS[unit]
    except KeyError as exc:
        supported = ", ".join(sorted(TIMESTAMP_DIVISORS))
        raise ValueError(
            f"Unsupported Kafka timestamp unit {unit!r}; expected {supported}"
        ) from exc

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            raw_value = int(stripped)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("Kafka target timestamp is invalid") from exc
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt_timezone.utc)
            return parsed.astimezone(dt_timezone.utc).isoformat()
    else:
        try:
            raw_value = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Kafka target timestamp must be an integer or ISO string") from exc

    if raw_value <= 0:
        return None
    try:
        return datetime.fromtimestamp(
            raw_value / divisor,
            tz=dt_timezone.utc,
        ).isoformat()
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError(f"Kafka target timestamp is out of range: {raw_value}") from exc


def decode_target_json(payload):
    """Return targets from a JSON object, array, or common envelope object."""
    if isinstance(payload, (bytes, bytearray, memoryview)):
        try:
            payload = bytes(payload).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("Kafka target payload must be UTF-8 JSON") from exc

    if isinstance(payload, str):
        try:
            document = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid Kafka target JSON: {exc.msg}") from exc
    elif isinstance(payload, (dict, list)):
        document = payload
    else:
        raise TypeError("Kafka target payload must be UTF-8 JSON bytes, text, object or array")

    if isinstance(document, list):
        return document
    if not isinstance(document, dict):
        raise ValueError("Kafka target JSON root must be an object or array")

    for envelope_key in ("list", "data", "targets"):
        if envelope_key in document:
            targets = document[envelope_key]
            if isinstance(targets, dict):
                return [targets]
            if not isinstance(targets, list):
                raise ValueError(
                    f"Kafka target JSON field {envelope_key!r} "
                    "must be an object or array"
                )
            return targets
    return [document]


def class_name(value):
    """Normalise a JSON target class to the canonical uppercase name."""
    if isinstance(value, bool):
        return f"UNKNOWN_{value}"
    if isinstance(value, (int, float)) and math.isfinite(value):
        numeric = int(value)
        if numeric == value:
            return CLASS_NAMES.get(numeric, f"UNKNOWN_{numeric}")
    text = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    return text or "UNKNOWN"


def first_value(source, *names, default=None):
    if not isinstance(source, dict):
        return default
    for name in names:
        value = source.get(name)
        if value not in (None, ""):
            return value
    return default


def target_body(target):
    """Support both the new flat JSON object and a legacy-style ``pos`` body."""
    if not isinstance(target, dict):
        return None
    body = target.get("pos")
    return body if isinstance(body, dict) else target


def target_position(body):
    if not isinstance(body, dict):
        return None
    for name in ("position", "geoPtn", "geoPoint"):
        value = body.get(name)
        if isinstance(value, dict):
            return value
    return body


def extension_values(body):
    values = {}
    extensions = first_value(body, "aisExtInfos", "extensions", default=[])
    if isinstance(extensions, dict):
        extensions = extensions.items()
    elif isinstance(extensions, list):
        extensions = (
            (item.get("key"), item.get("value"))
            for item in extensions
            if isinstance(item, dict)
        )
    else:
        return values
    for key, value in extensions:
        key_text = str(key or "").strip().casefold()
        if key_text:
            values[key_text] = str(value or "").strip()
    return values


def extension(extensions, *names, default=None):
    for name in names:
        value = extensions.get(name.casefold())
        if value not in (None, ""):
            return value
    return default


def finite_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_ais_json(
    payload,
    *,
    timestamp_unit="milliseconds",
    accept_sim=False,
    received_at=None,
):
    """Parse one Kafka JSON message and retain AIS/STATIC targets only."""
    targets = decode_target_json(payload)
    received_value = received_at or datetime.now(dt_timezone.utc).isoformat()
    allowed_dynamic = set(DYNAMIC_AIS_CLASSES)
    if accept_sim:
        allowed_dynamic.update(SIM_AIS_CLASSES)

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
        is_static = target_class in STATIC_AIS_CLASSES
        is_dynamic = target_class in allowed_dynamic
        if not is_static and not is_dynamic:
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
        if not timestamp:
            invalid_targets += 1
            continue

        extensions = extension_values(body)
        row = {
            "MMSI": str(first_value(body, "mmsi", "MMSI", default="")),
            "timestamp": timestamp,
            "received_at": received_value,
            "Name": str(first_value(body, "vesselName", "name", "Name", default="")),
            "IMO": str(first_value(body, "imo", "IMO", default="")),
            "status": first_value(body, "status"),
            "course": first_value(body, "course", "cog"),
            "speed": first_value(body, "speed", "sog"),
            "heading": first_value(body, "heading"),
            "length": first_value(body, "len", "length"),
            "width": first_value(body, "wid", "width"),
            "ship_type": first_value(body, "shipType", "shiptype", "ship_type"),
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
            "collection_type": target_class,
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
            "call_sign": first_value(body, "callSign", "call_sign", default=""),
            "rot": extension(extensions, "rot", "rate_of_turn"),
            "draught": extension(extensions, "draught", "draft"),
            "flag": extension(extensions, "flag", default=""),
            "iso3": extension(extensions, "iso3", default=""),
            "at_dock": extension(extensions, "at_dock"),
            "matched_port_name": extension(
                extensions, "matched_port_name", "matchedPortName", default=""
            ),
        }

        if is_static:
            row["msg_type"] = 5
            rows.append(row)
            continue

        position = target_position(body)
        longitude = finite_float(first_value(position, "longitude", "lon", "lng"))
        latitude = finite_float(first_value(position, "latitude", "lat"))
        if (
            longitude is None
            or latitude is None
            or not -180 <= longitude <= 180
            or not -90 <= latitude <= 90
        ):
            invalid_targets += 1
            continue
        row.update({"longitude": longitude, "latitude": latitude})
        rows.append(row)

    return ParsedAISBatch(
        rows=rows,
        class_counts=dict(class_counts),
        ignored_counts=dict(ignored_counts),
        invalid_targets=invalid_targets,
    )
