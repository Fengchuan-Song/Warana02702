from django.urls import path

from AISData.views import cached_detection_result

from . import views


urlpatterns = [
    path(
        "detectAbnormalTransfer/",
        cached_detection_result,
        {"feature_id": "detect-abnormalTransfer"},
        name="detect_abnormal_transfer",
    ),
    path(
        "operations/",
        views.operation_collection,
        name="transfer_operation_collection",
    ),
    path(
        "operations/<int:plan_id>/",
        views.operation_detail,
        name="transfer_operation_detail",
    ),
]
