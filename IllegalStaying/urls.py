from django.urls import path
from AISData.views import cached_detection_result

urlpatterns = [
    path(
        'detectIllegalStaying/',
        cached_detection_result,
        {'feature_id': 'detect-illegalStaying'},
        name='detectIllegalStaying',
    ),
]
