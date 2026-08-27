"""Cross-process handoff from sensor replay to the JPDA fusion worker."""

from copy import deepcopy
import logging
import threading
from typing import Any, Dict, List

from django.core.cache import cache
from django.utils import timezone


LOGGER = logging.getLogger(__name__)
CACHE_KEY = "ais_radar:replay_input:v1"
_LOCK = threading.RLock()


def _empty_state() -> Dict[str, Any]:
    return {
        "available": False,
        "reset": False,
        "updated_at": None,
        "run_id": None,
        "scene_id": None,
        "sensor_timestamp": None,
        "ais_rows": [],
        "radar_rows": [],
        "radar_event": False,
    }


_local_state = _empty_state()


def _store(state: Dict[str, Any]) -> Dict[str, Any]:
    global _local_state
    with _LOCK:
        _local_state = deepcopy(state)
    try:
        cache.set(CACHE_KEY, state, timeout=None)
    except Exception:
        LOGGER.warning("Could not publish AIS/Radar replay input", exc_info=True)
    return deepcopy(state)


def reset_fusion_input(run_id: str, scene_id: str) -> Dict[str, Any]:
    state = _empty_state()
    state.update(
        {
            "reset": True,
            "updated_at": timezone.now().isoformat(),
            "run_id": str(run_id),
            "scene_id": str(scene_id),
        }
    )
    return _store(state)


def publish_fusion_input(
    *,
    run_id: str,
    scene_id: str,
    sensor_timestamp: str,
    ais_rows: List[Dict[str, Any]],
    radar_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Publish causal AIS history and the Radar frame for one sensor event."""
    return _store(
        {
            "available": True,
            "reset": False,
            "updated_at": timezone.now().isoformat(),
            "run_id": str(run_id),
            "scene_id": str(scene_id),
            "sensor_timestamp": str(sensor_timestamp),
            "ais_rows": deepcopy(ais_rows),
            "radar_rows": deepcopy(radar_rows),
            "radar_event": bool(radar_rows),
        }
    )


def get_fusion_input() -> Dict[str, Any]:
    try:
        state = cache.get(CACHE_KEY)
    except Exception:
        LOGGER.warning("Could not read AIS/Radar replay input", exc_info=True)
        state = None
    if isinstance(state, dict):
        return deepcopy(state)
    with _LOCK:
        return deepcopy(_local_state)
