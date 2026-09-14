from django.contrib import admin
from django.urls import path
from .views import (
    camera_collection,
    camera_configuration,
    camera_detail,
    camera_video,
    current_typhoons,
    guangdong_marine_weather,
    index,
    model_parameter_page,
    offline_map_asset,
    offline_map_tile,
)

urlpatterns = [
    path("", index, name='index'),
    path(
        "maps/assets/<str:asset_name>",
        offline_map_asset,
        name="offline_map_asset",
    ),
    path(
        "maps/tiles/<int:zoom>/<int:tile_x>/<int:tile_y>.png",
        offline_map_tile,
        name="offline_map_tile",
    ),
    path(
        "model-parameters/",
        model_parameter_page,
        name="model_parameter_page",
    ),
    path(
        "api/cameras/",
        camera_collection,
        name="camera_collection",
    ),
    path(
        "api/cameras/<slug:camera_key>/",
        camera_detail,
        name="camera_detail",
    ),
    path(
        "api/cameras/harbor-01/video/",
        camera_video,
        name="camera_video",
    ),
    path(
        "api/cameras/harbor-01/config/",
        camera_configuration,
        name="camera_configuration",
    ),
    path(
        "api/typhoons/current/",
        current_typhoons,
        name="current_typhoons",
    ),
    path(
        "api/environment/guangdong-marine/",
        guangdong_marine_weather,
        name="guangdong_marine_weather",
    ),
]
