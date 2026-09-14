"""Confirm AIS/Radar association anomalies across consecutive fusion frames."""

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import logging
import math
import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime


LOGGER = logging.getLogger(__name__)
CACHE_PREFIX = "ais_radar:alert_confirmation:v1"
DEFAULT_CONFIRMATION_FRAMES = 3
DEFAULT_MAX_GAP_SECONDS = 30.0
DEFAULT_STATE_TTL_SECONDS = 10 * 60
_LOCK = threading.RLock()


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) and parsed > 0 else default


def confirmation_config(feature_id: str) -> Dict[str, Any]:
    """Return validated per-detector confirmation settings."""
    configured = getattr(settings, "AIS_RADAR_ALERT_CONFIRMATION", {}) or {}
    common = configured.get("default") or {}
    feature = configured.get(feature_id) or {}
    merged = {**common, **feature}
    return {
        "confirmation_frames": _positive_int(
            merged.get("confirmation_frames"),
            DEFAULT_CONFIRMATION_FRAMES,
        ),
        "max_gap_seconds": _positive_float(
            merged.get("max_gap_seconds"),
            DEFAULT_MAX_GAP_SECONDS,
        ),
        "state_ttl_seconds": _positive_int(
            merged.get("state_ttl_seconds"),
            DEFAULT_STATE_TTL_SECONDS,
        ),
    }


def _cache_key(feature_id: str) -> str:
    return f"{CACHE_PREFIX}:{feature_id}"


def _normalise_id(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value or "").strip()


def _time_seconds(value: Any) -> Optional[float]:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        parsed = parse_datetime(text)
        if parsed is None:
            try:
                numeric = float(text)
            except (TypeError, ValueError):
                return None
            return numeric if math.isfinite(numeric) else None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_default_timezone())
    return parsed.timestamp()


def _frame_identity(fusion_state: Dict[str, Any]) -> str:
    """Use the Kafka event id when present, with a deterministic fallback."""
    event_id = _normalise_id(fusion_state.get("fusion_event_id"))
    if event_id:
        return f"event:{event_id}"

    source_time = fusion_state.get("source_end_time")
    if source_time not in (None, ""):
        return f"time:{source_time}"

    # Offline callers may not provide a source time. Hash only stable fusion
    # fields, rather than updated_at, so retrying the same snapshot is idempotent.
    stable = {
        "matches": fusion_state.get("matches") or [],
        "unmatched_ais_targets": fusion_state.get("unmatched_ais_targets") or [],
        "unmatched_radar_targets": fusion_state.get("unmatched_radar_targets") or [],
    }
    encoded = json.dumps(
        stable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"hash:{hashlib.sha256(encoded).hexdigest()}"


def _frame_time(fusion_state: Dict[str, Any]) -> Tuple[float, str]:
    for field in ("source_end_time", "updated_at"):
        value = fusion_state.get(field)
        parsed = _time_seconds(value)
        if parsed is not None:
            return parsed, str(value)
    now = timezone.now()
    return now.timestamp(), now.isoformat()


def _read_state(feature_id: str) -> Dict[str, Any]:
    value = cache.get(_cache_key(feature_id))
    if not isinstance(value, dict):
        return {"last_frame_identity": None, "candidates": {}}
    if not isinstance(value.get("candidates"), dict):
        value["candidates"] = {}
    return value


def confirm_consecutive_targets(
    feature_id: str,
    fusion_state: Dict[str, Any],
    targets: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Advance one detector's per-target counters and return confirmed targets.

    A repeated fusion frame is read idempotently. On a new frame, a target that
    is matched again, disappears, arrives out of order, or exceeds max_gap is
    reset instead of inheriting an old streak.
    """
    config = confirmation_config(feature_id)
    required = config["confirmation_frames"]
    frame_identity = _frame_identity(fusion_state)
    frame_seconds, frame_time = _frame_time(fusion_state)
    current_targets = {}
    for target in targets or []:
        if not isinstance(target, dict):
            continue
        target_id = _normalise_id(target.get("id"))
        if target_id:
            current_targets[target_id] = deepcopy(target)

    with _LOCK:
        state = _read_state(feature_id)
        candidates = state["candidates"]
        repeated_frame = state.get("last_frame_identity") == frame_identity

        if not repeated_frame:
            next_candidates = {}
            for target_id, target in current_targets.items():
                previous = candidates.get(target_id) or {}
                previous_seconds = previous.get("last_seen_seconds")
                consecutive = 1
                first_seen = frame_time
                if isinstance(previous_seconds, (int, float)):
                    gap = frame_seconds - float(previous_seconds)
                    if 0 <= gap <= config["max_gap_seconds"]:
                        consecutive = int(previous.get("consecutive_frames", 0)) + 1
                        first_seen = previous.get("first_seen") or frame_time
                next_candidates[target_id] = {
                    "target": target,
                    "consecutive_frames": consecutive,
                    "first_seen": first_seen,
                    "last_seen": frame_time,
                    "last_seen_seconds": frame_seconds,
                }
            candidates = next_candidates
            state = {
                "last_frame_identity": frame_identity,
                "last_frame_time": frame_time,
                "candidates": candidates,
            }
            cache.set(
                _cache_key(feature_id),
                state,
                timeout=config["state_ttl_seconds"],
            )

        confirmed = []
        for target_id, candidate in candidates.items():
            consecutive = int(candidate.get("consecutive_frames", 0))
            if consecutive < required:
                continue
            confirmed.append(
                {
                    "target_id": target_id,
                    "target": deepcopy(candidate.get("target") or {}),
                    "consecutive_frames": consecutive,
                    "first_seen": candidate.get("first_seen"),
                    "last_seen": candidate.get("last_seen"),
                }
            )

    return {
        "confirmation_frames_required": required,
        "max_gap_seconds": config["max_gap_seconds"],
        "candidate_count": len(candidates),
        "confirmed": confirmed,
        "repeated_frame": repeated_frame,
    }


def clear_confirmation_state(feature_id: Optional[str] = None) -> None:
    """Clear confirmation counters for tests and operational resets."""
    feature_ids: List[str]
    if feature_id:
        feature_ids = [feature_id]
    else:
        feature_ids = ["detect-ais-off", "detect-spoofing"]
    with _LOCK:
        for item in feature_ids:
            try:
                cache.delete(_cache_key(item))
            except Exception:
                LOGGER.warning(
                    "Could not clear %s alert confirmation state",
                    item,
                    exc_info=True,
                )
