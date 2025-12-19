from django.urls import path
from .views import detect_crossing_boundary

urlpatterns = [
    path('detectCrossingBoundary/', detect_crossing_boundary, name='detect_crossing_boundary'),
]
