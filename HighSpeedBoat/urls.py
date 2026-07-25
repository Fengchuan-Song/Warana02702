from django.contrib import admin
from django.urls import path
from AISData.views import cached_detection_result

urlpatterns = [
    path(
        "detectHighSpeedBoat/",
        cached_detection_result,
        {"feature_id": "detect-highSpeedBoat"},
        name='detect_high_speed',
    ),
]
