from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase
from django.urls import reverse

from .marine_weather_service import (
    GUANGDONG_COASTAL_POINTS,
    MarineWeatherDataError,
    _download_json,
    build_marine_weather_payload,
)


def _weather_fixture():
    return [
        {
            "current": {
                "time": "2026-07-24T10:30",
                "wind_speed_10m": 5.2,
                "wind_direction_10m": 135,
                "wind_gusts_10m": 8.1,
            }
        }
        for _ in GUANGDONG_COASTAL_POINTS
    ]


def _marine_fixture():
    return [
        {
            "current": {
                "time": "2026-07-24T10:30",
                "wave_height": 0.8,
                "wave_direction": 145,
                "ocean_current_velocity": 0.35,
                "ocean_current_direction": 70,
                "sea_surface_temperature": 29.1,
            }
        }
        for _ in GUANGDONG_COASTAL_POINTS
    ]


class MarineWeatherServiceTests(SimpleTestCase):
    def test_builds_guangdong_wind_wave_current_points(self):
        result = build_marine_weather_payload(
            _weather_fixture(),
            _marine_fixture(),
        )

        self.assertEqual(
            len(result["points"]),
            len(GUANGDONG_COASTAL_POINTS),
        )
        first = result["points"][0]
        self.assertEqual(first["wind"]["speed"], 5.2)
        self.assertEqual(first["wind"]["direction"], 135.0)
        self.assertEqual(first["wave"]["height"], 0.8)
        self.assertEqual(first["current"]["speed"], 0.35)
        self.assertEqual(first["sea_surface_temperature"], 29.1)

    def test_rejects_incomplete_location_response(self):
        with self.assertRaisesRegex(
            MarineWeatherDataError,
            "采样点数量不完整",
        ):
            build_marine_weather_payload(
                _weather_fixture()[:-1],
                _marine_fixture(),
            )

    @patch("home.marine_weather_service.requests.Session")
    @patch("home.marine_weather_service.requests.get")
    def test_download_retries_directly_when_proxy_fails(
        self,
        requests_get,
        session_class,
    ):
        requests_get.side_effect = requests.exceptions.ProxyError(
            "proxy unavailable"
        )
        direct_response = MagicMock()
        direct_response.json.return_value = {"current": {}}
        direct_session = session_class.return_value.__enter__.return_value
        direct_session.get.return_value = direct_response

        result = _download_json("https://api.open-meteo.com/", {})

        self.assertEqual(result, {"current": {}})
        self.assertFalse(direct_session.trust_env)
        direct_response.raise_for_status.assert_called_once_with()


class MarineWeatherViewTests(SimpleTestCase):
    @patch("home.views.get_guangdong_marine_weather")
    def test_environment_endpoint(self, get_marine_weather):
        get_marine_weather.return_value = {
            "source": {"name": "Open-Meteo"},
            "units": {},
            "points": [],
            "stale": False,
            "notice": "仅供参考",
        }

        response = self.client.get(reverse("guangdong_marine_weather"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])

    @patch("home.views.get_guangdong_marine_weather")
    def test_environment_endpoint_reports_source_error(
        self,
        get_marine_weather,
    ):
        get_marine_weather.side_effect = MarineWeatherDataError(
            "数据源不可用"
        )

        response = self.client.get(reverse("guangdong_marine_weather"))

        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json()["success"])
        self.assertEqual(response.json()["message"], "数据源不可用")
