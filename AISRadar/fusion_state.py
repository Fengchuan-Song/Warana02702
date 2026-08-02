"""Publish the latest AIS/Radar matches for the live map UI."""

from copy import deepcopy
import logging
import threading
from typing import Any, Dict

from django.core.cache import cache
from django.utils import timezone


LOGGER = logging.getLogger(__name__)
CACHE_KEY = "ais_radar:latest_fused_targets:v1"
_LOCK = threading.RLock()


def _empty_state() -> Dict[str, Any]:
    return {
        "available": False,
        "updated_at": None,
        "source_start_time": None,
        "source_end_time": None,
        "count": 0,
        "ais_ids": [],
        "radar_ids": [],
        "matches": [],
    }


_local_state = _empty_state()


def _normalise_id(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def build_fusion_state(result: Dict[str, Any]) -> Dict[str, Any]:
    """Build a UI snapshot from the most recent inference window only."""
    windows = result.get("windows") or []
    latest_window = windows[-1] if windows else {}
    matches = []

    for match in latest_window.get("matches") or []:
        ais_id = _normalise_id(match.get("ais_id"))
        radar_id = _normalise_id(match.get("radar_id"))
        if not ais_id or ais_id == "None" or not radar_id or radar_id == "None":
            continue
        matches.append(
            {
                "ais_id": ais_id,
                "radar_id": radar_id,
                "confidence": match.get("confidence"),
            }
        )

    return {
        "available": bool(windows),
        "updated_at": timezone.now().isoformat(),
        "source_start_time": latest_window.get("start_time"),
        "source_end_time": latest_window.get("end_time"),
        "count": len(matches),
        "ais_ids": sorted({item["ais_id"] for item in matches}),
        "radar_ids": sorted({item["radar_id"] for item in matches}),
        "matches": matches,
    }


def publish_fusion_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Store a fresh fusion snapshot in-process and, when available, Redis."""
    global _local_state
    state = build_fusion_state(result)
    with _LOCK:
        _local_state = deepcopy(state)
    try:
        cache.set(CACHE_KEY, state, timeout=None)
    except Exception:
        LOGGER.warning("Could not publish AIS/Radar fusion state to cache", exc_info=True)
    return deepcopy(state)


def get_fusion_state() -> Dict[str, Any]:
    """Read the shared snapshot, falling back to this worker's local copy."""
    try:
        cached_state = cache.get(CACHE_KEY)
    except Exception:
        LOGGER.warning("Could not read AIS/Radar fusion state from cache", exc_info=True)
        cached_state = None
    if isinstance(cached_state, dict):
        return deepcopy(cached_state)
    with _LOCK:
        return deepcopy(_local_state)


def clear_fusion_state() -> None:
    """Clear state for tests and operational resets."""
    global _local_state
    with _LOCK:
        _local_state = _empty_state()
    try:
        cache.delete(CACHE_KEY)
    except Exception:
        LOGGER.warning("Could not clear AIS/Radar fusion state cache", exc_info=True)
