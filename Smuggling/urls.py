from django.urls import path

from AISData.views import cached_detection_result

from .views import (
    permit_collection,
    permit_detail,
    zone_collection,
    zone_detail,
)

urlpatterns = [
    path(
        "detectSmuggling/",
        cached_detection_result,
        {"feature_id": "detect-smuggling"},
        name="detect_smuggling",
    ),
    path("zones/", zone_collection, name="smuggling_zone_collection"),
    path(
        "zones/<int:zone_id>/",
        zone_detail,
        name="smuggling_zone_detail",
    ),
    path(
        "permits/",
        permit_collection,
        name="smuggling_permit_collection",
    ),
    path(
        "permits/<int:permit_id>/",
        permit_detail,
        name="smuggling_permit_detail",
    ),
]
