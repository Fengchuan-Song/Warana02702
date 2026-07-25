from django.urls import path
from AISData.views import cached_detection_result

urlpatterns = [
    path(
        'detectAbnormalWandering/',
        cached_detection_result,
        {'feature_id': 'detect-abnormalWandering'},
        name='detectAbnormalWandering',
    ),
]
