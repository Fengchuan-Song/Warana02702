from django.urls import path
from AISData.views import cached_detection_result
from . import views

urlpatterns = [
    path(
        'monitor-areas/',
        views.monitor_area_collection,
        name='parking_monitor_area_collection',
    ),
    path(
        'monitor-areas/<int:area_id>/',
        views.monitor_area_detail,
        name='parking_monitor_area_detail',
    ),
    path(
        'detectAbnormalParking/',
        cached_detection_result,
        {'feature_id': 'detect-abnormalStaying'},
        name='detectAbnormalParking',
    ),
]
