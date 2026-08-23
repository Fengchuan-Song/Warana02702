"""Shared normalisation rules for AIS records."""

import math

from django.utils import timezone
from django.utils.dateparse import parse_datetime


UNKNOWN_AIS_TARGET_NAME = "未知目标"

_MISSING_NAME_MARKERS = {
    "",
    "-",
    "--",
    "<na>",
    "n/a",
    "na",
    "nan",
    "none",
    "null",
    "unknown",
    "unknown target",
    "未命名",
    "未知",
    "未知船名",
    "未知船舶",
}


def normalise_ais_name(value):
    """Return one consistent display name for missing AIS ship names."""
    if value is None:
        return UNKNOWN_AIS_TARGET_NAME
    if isinstance(value, float) and math.isnan(value):
        return UNKNOWN_AIS_TARGET_NAME

    name = str(value).strip()
    if name.casefold() in _MISSING_NAME_MARKERS:
        return UNKNOWN_AIS_TARGET_NAME
    return name


def normalise_ais_snapshot(ship_list):
    """Normalise names without mutating the caller's AIS snapshot."""
    if not isinstance(ship_list, list):
        return ship_list

    normalised = []
    for ship in ship_list:
        if not isinstance(ship, dict):
            normalised.append(ship)
            continue
        item = ship.copy()
        item["name"] = normalise_ais_name(item.get("name"))
        normalised.append(item)
    return normalised


_DYNAMIC_MESSAGE_TYPES = {1, 2, 3, 18, 19, 27}
_STATIC_MESSAGE_TYPES = {5, 24}


def _first_value(source, *field_names):
    for field_name in field_names:
        value = source.get(field_name)
        if value not in (None, ""):
            return value
    return None


def _finite_float(source, *field_names):
    value = _first_value(source, *field_names)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(source, *field_names):
    value = _finite_float(source, *field_names)
    return int(value) if value is not None else None


def _text(source, *field_names):
    value = _first_value(source, *field_names)
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.casefold() in {"nan", "none", "null"} else text


def _boolean(source, *field_names):
    value = _first_value(source, *field_names)
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    return str(value).strip().casefold() in {
        "1", "true", "yes", "y", "是",
    }


def _timestamp(source):
    value = _first_value(
        source,
        "timestamp",
        "event_time",
        "DateTime",
        "datetime",
        "time",
    )
    timestamp = parse_datetime(value) if isinstance(value, str) else value
    if timestamp is None:
        return None
    if timezone.is_naive(timestamp):
        timestamp = timezone.make_aware(
            timestamp,
            timezone.get_current_timezone(),
        )
    return timestamp


def _received_timestamp(source):
    value = _first_value(
        source,
        "received_at",
        "received_time",
        "ingested_at",
        "collection_time",
    )
    received_at = parse_datetime(value) if isinstance(value, str) else value
    if received_at is None:
        received_at = timezone.now()
    if timezone.is_naive(received_at):
        received_at = timezone.make_aware(
            received_at,
            timezone.get_current_timezone(),
        )
    return received_at


def _mmsi(source):
    value = _first_value(source, "MMSI", "mmsi", "ais_id", "ID")
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    return text if len(text) == 9 and text.isdigit() else ""


def ais_record_kind(source):
    """Classify one decoded AIS record for the operational ship pipeline."""
    message_type = _integer(source, "msg_type", "message_type", "type")
    if message_type in _STATIC_MESSAGE_TYPES:
        return "static"
    if message_type in _DYNAMIC_MESSAGE_TYPES:
        return "dynamic"
    if message_type is None and (
        _first_value(source, "longitude", "lon", "Y") is not None
        and _first_value(source, "latitude", "lat", "X") is not None
    ):
        # Existing replay CSV files predate msg_type propagation.
        return "dynamic"
    return "unsupported"


def normalise_dynamic_ais_record(source):
    """Return one validated dynamic ship position or ``None``."""
    if not isinstance(source, dict) or ais_record_kind(source) != "dynamic":
        return None
    mmsi = _mmsi(source)
    timestamp = _timestamp(source)
    longitude = _finite_float(source, "longitude", "lon", "Y")
    latitude = _finite_float(source, "latitude", "lat", "X")
    if (
        not mmsi
        or timestamp is None
        or longitude is None
        or latitude is None
        or not -180 <= longitude <= 180
        or not -90 <= latitude <= 90
    ):
        return None

    quality_flags = []
    speed = _finite_float(source, "speed", "sog")
    if speed is None or speed < 0 or speed > 102.2:
        speed = None
        quality_flags.append("speed_unavailable")
    course = _finite_float(source, "course", "cog")
    if course is None or not 0 <= course < 360:
        course = None
        quality_flags.append("course_unavailable")
    heading = _finite_float(source, "heading", "true_heading")
    if heading is None or not 0 <= heading < 360:
        heading = None
        quality_flags.append("heading_unavailable")
    rot = _finite_float(source, "rot", "rate_of_turn")
    if rot is None or rot == -128:
        rot = None
        quality_flags.append("rot_unavailable")

    return {
        "timestamp": timestamp.isoformat(),
        "received_at": _received_timestamp(source).isoformat(),
        "mmsi": mmsi,
        "msg_type": _integer(source, "msg_type", "message_type", "type"),
        "name": normalise_ais_name(_first_value(source, "Name", "name")),
        "lon": longitude,
        "lat": latitude,
        "course": course,
        "speed": speed,
        "heading": heading,
        "rot": rot,
        "imo": _text(source, "IMO", "imo"),
        "flag": _text(source, "flag"),
        "iso3": _text(source, "iso3").upper(),
        "draught": _finite_float(source, "draught", "draft"),
        "ship_type": _text(
            source,
            "ship_type",
            "ship_and_cargo_type",
            "vessel_type",
        ),
        "length": _finite_float(source, "length"),
        "width": _finite_float(source, "width"),
        "accuracy": _finite_float(source, "accuracy", "position_accuracy"),
        "nav_status": _integer(source, "status", "nav_status"),
        "at_dock": _boolean(source, "at_dock"),
        "matched_port_name": _text(
            source,
            "matchedPortName",
            "matched_port_name",
        ),
        "collection_type": _text(source, "collection_type"),
        "source": _text(source, "source", "receiver_id"),
        "channel": _text(source, "channel", "radio_channel"),
        "quality_flags": quality_flags,
    }


def normalise_static_ais_record(source):
    """Return one validated type 5/24 static update or ``None``."""
    if not isinstance(source, dict) or ais_record_kind(source) != "static":
        return None
    mmsi = _mmsi(source)
    timestamp = _timestamp(source)
    if not mmsi or timestamp is None:
        return None
    return {
        "timestamp": timestamp.isoformat(),
        "received_at": _received_timestamp(source).isoformat(),
        "mmsi": mmsi,
        "msg_type": _integer(source, "msg_type", "message_type", "type"),
        "name": normalise_ais_name(_first_value(source, "Name", "name")),
        "imo": _text(source, "IMO", "imo"),
        "flag": _text(source, "flag"),
        "iso3": _text(source, "iso3").upper(),
        "draught": _finite_float(source, "draught", "draft"),
        "ship_type": _text(
            source,
            "ship_type",
            "ship_and_cargo_type",
            "vessel_type",
        ),
        "length": _finite_float(source, "length"),
        "width": _finite_float(source, "width"),
        "to_bow": _finite_float(source, "to_bow"),
        "to_stern": _finite_float(source, "to_stern"),
        "to_port": _finite_float(source, "to_port"),
        "to_starboard": _finite_float(source, "to_starboard"),
        "source": _text(source, "source", "receiver_id"),
    }
