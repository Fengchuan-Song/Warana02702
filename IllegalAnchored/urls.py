from django.urls import path
from AISData.views import cached_detection_result

urlpatterns = [
    path(
        "detectIllegalAnchored/",
        cached_detection_result,
        {"feature_id": "detect-illegalAnchored"},
        name="detect_illegal_anchored",
    ),
]
