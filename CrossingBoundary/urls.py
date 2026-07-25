from django.urls import path
from AISData.views import cached_detection_result
from . import views

urlpatterns = [
    path('fences/', views.fence_collection, name='fence_collection'),
    path('fences/<int:fence_id>/', views.fence_detail, name='fence_detail'),
    path(
        'detectCrossingBoundary/',
        cached_detection_result,
        {'feature_id': 'detect-CrossingBoundary'},
        name='detect_crossing_boundary',
    ),
]
