from django.urls import path
from AISData.views import cached_detection_result
from . import views

urlpatterns = [
    path(
        'detectAbnormalParking/',
        cached_detection_result,
        {'feature_id': 'detect-abnormalStaying'},
        name='detectAbnormalParking',
    ),
]
