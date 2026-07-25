from django.urls import path

from .views import cached_detection_result


urlpatterns = [
    path(
        "detection-results/<str:feature_id>/",
        cached_detection_result,
        name="cached_detection_result",
    ),
]
