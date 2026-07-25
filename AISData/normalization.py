"""Shared normalisation rules for AIS records."""

import math


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
