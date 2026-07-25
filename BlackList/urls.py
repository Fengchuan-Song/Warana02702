from django.urls import path
from AISData.views import cached_detection_result
from . import views

urlpatterns = [
    path(
        'ships/',
        views.blacklist_collection,
        name='blacklist_collection',
    ),
    path(
        'ships/<int:entry_id>/',
        views.blacklist_detail,
        name='blacklist_detail',
    ),
    path(
        'detectBlackList/',
        cached_detection_result,
        {'feature_id': 'detect-blackList'},
        name='detect_black_list',
    ),
]
