from django.urls import path
from AISData.views import cached_detection_result

urlpatterns = [
    path(
        'detectCollision/',
        cached_detection_result,
        {'feature_id': 'detect-collision'},
        name='detect_collision',
    ),
]
