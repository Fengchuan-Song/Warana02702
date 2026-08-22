from django.urls import path
from AISData.views import cached_detection_result
from . import views

urlpatterns = [
    path(
        'monitor-areas/',
        views.monitor_area_collection,
        name='illegal_staying_monitor_area_collection',
    ),
    path(
        'monitor-areas/<int:area_id>/',
        views.monitor_area_detail,
        name='illegal_staying_monitor_area_detail',
    ),
    path(
        'detectIllegalStaying/',
        cached_detection_result,
        {'feature_id': 'detect-illegalStaying'},
        name='detectIllegalStaying',
    ),
]
