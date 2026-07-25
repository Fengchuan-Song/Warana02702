from django.urls import path

from AISData.views import cached_detection_result

from . import views


urlpatterns = [
    path(
        "detectIllegalBerthing/",
        cached_detection_result,
        {"feature_id": "detect-illegalBerthing"},
        name="detect_illegal_berthing",
    ),
    path(
        "permits/",
        views.permit_collection,
        name="illegal_berthing_permit_collection",
    ),
    path(
        "permits/<int:permit_id>/",
        views.permit_detail,
        name="illegal_berthing_permit_detail",
    ),
]
