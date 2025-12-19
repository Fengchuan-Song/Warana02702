from django.urls import path
from . import views

urlpatterns = [
    path('detectAbnormalWandering/', views.receive_realtime_point, name='detectAbnormalWandering'),
]