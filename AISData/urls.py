from django.urls import path
from .maritime_zone_views import zone_export, zone_import_preview, zone_management

from .views import (
    cached_detection_result,
    detection_model_parameter_collection,
    detection_model_parameter_detail,
    maritime_zone_collection,
    ship_trajectory,
    violation_record_detail,
    violation_record_list,
)


urlpatterns = [
    path("maritime-zones/manage/", zone_management, name="maritime_zone_management"),
    path("maritime-zones/import-preview/", zone_import_preview, name="maritime_zone_import_preview"),
    path("maritime-zones/export/", zone_export, name="maritime_zone_export"),
    path(
        "model-parameters/",
        detection_model_parameter_collection,
        name="detection_model_parameter_collection",
    ),
    path(
        "model-parameters/<str:feature_id>/",
        detection_model_parameter_detail,
        name="detection_model_parameter_detail",
    ),
    path(
        "maritime-zones/",
        maritime_zone_collection,
        name="maritime_zone_collection",
    ),
    path(
        "violation-records/",
        violation_record_list,
        name="violation_record_list",
    ),
    path(
        "violation-records/<int:record_id>/",
        violation_record_detail,
        name="violation_record_detail",
    ),
    path(
        "ship-trajectories/",
        ship_trajectory,
        name="ship_trajectory",
    ),
    path(
        "ship-trajectories/<str:mmsi>/",
        ship_trajectory,
        name="ship_trajectory_by_mmsi",
    ),
    path(
        "detection-results/<str:feature_id>/",
        cached_detection_result,
        name="cached_detection_result",
    ),
]
