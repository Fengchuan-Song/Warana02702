from django.http import JsonResponse
from django.views.decorators.http import require_GET

from .detection import (
    DETECTORS,
    EXTERNAL_DETECTION_FEATURES,
    get_detection_result,
)


@require_GET
def cached_detection_result(request, feature_id):
    """Return the most recent backend-produced result without running detection."""
    if (
        feature_id not in DETECTORS
        and feature_id not in EXTERNAL_DETECTION_FEATURES
    ):
        return JsonResponse(
            {
                "success": False,
                "feature_id": feature_id,
                "count": 0,
                "results": [],
                "message": "未知检测类型",
            },
            status=404,
        )

    return JsonResponse(get_detection_result(feature_id))
