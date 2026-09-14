"""Detect AIS-only targets as suspected identity forgery events."""

import math

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET

from AISData.detection import get_detection_result
from AISRadar.alert_confirmation import confirm_consecutive_targets


FEATURE_ID = "detect-spoofing"


def _position(target):
    try:
        latitude = float(target.get("x"))
        longitude = float(target.get("y"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        return None
    return [longitude, latitude]


def detect_forgery(fusion_state):
    """Alert only after an AIS trajectory is unmatched in consecutive frames."""
    source_time = fusion_state.get("source_end_time")
    confirmation = confirm_consecutive_targets(
        FEATURE_ID,
        fusion_state,
        fusion_state.get("unmatched_ais_targets") or [],
    )
    results = []
    for confirmed in confirmation["confirmed"]:
        target = confirmed["target"]
        ais_id = confirmed["target_id"]
        if not ais_id:
            continue
        position = _position(target)
        consecutive = confirmed["consecutive_frames"]
        item = {
            "ais_id": ais_id,
            "mmsi": ais_id,
            "name": f"AIS目标 {ais_id}",
            "status": "疑似身份伪造",
            "risk": "高风险",
            "details": f"连续{consecutive}帧仅检测到AIS轨迹，未关联到雷达轨迹",
            "timestamp": source_time,
            "confirmation_frames": consecutive,
            "confirmation_frames_required": confirmation[
                "confirmation_frames_required"
            ],
            "first_unmatched_at": confirmed["first_seen"],
            "last_unmatched_at": confirmed["last_seen"],
        }
        if position:
            item.update(
                {
                    "location": position,
                    "lon": position[0],
                    "lat": position[1],
                }
            )
        results.append(item)

    return {
        "success": True,
        "feature_id": FEATURE_ID,
        "association_method": fusion_state.get("association_method"),
        "timestamp": source_time,
        "computed_at": timezone.now().isoformat(),
        "count": len(results),
        "candidate_count": confirmation["candidate_count"],
        "confirmation_frames_required": confirmation[
            "confirmation_frames_required"
        ],
        "results": results,
        "message": "AIS-only target detection completed",
    }


@require_GET
def latest_result(request):
    return JsonResponse(
        get_detection_result(FEATURE_ID),
        json_dumps_params={"ensure_ascii": False},
    )
