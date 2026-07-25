from django.urls import path
from AISData.views import cached_detection_result

urlpatterns = [
    path(
        'detectLowSpeed/',
        cached_detection_result,
        {'feature_id': 'detect-lowSpeedBoat'},
        name='detect_low_speed',
    ),
]
