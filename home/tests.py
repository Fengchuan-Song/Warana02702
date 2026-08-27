from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase
from django.urls import reverse

from .typhoon_service import (
    TyphoonDataError,
    _download_xml,
    _validate_track_url,
    parse_typhoon_list,
    parse_typhoon_track,
)


TYPHOON_LIST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<TropicalCycloneList>
  <TropicalCyclone>
    <TropicalCycloneID>2609</TropicalCycloneID>
    <TropicalCycloneChineseName>巴威</TropicalCycloneChineseName>
    <TropicalCycloneEnglishName>BAVI</TropicalCycloneEnglishName>
    <TropicalCycloneURL>
      https://www.weather.gov.hk/wxinfo/currwx/hko_tctrack_2609.xml
    </TropicalCycloneURL>
  </TropicalCyclone>
</TropicalCycloneList>
"""

TYPHOON_TRACK_XML = """<?xml version="1.0" encoding="UTF-8"?>
<TropicalCycloneTrack xmlns="urn:hko:typhoon">
  <BulletinHeader>
    <BulletinTime>2026-07-10T11:00:00+08:00</BulletinTime>
  </BulletinHeader>
  <WeatherReport>
    <TropicalCycloneName>BAVI</TropicalCycloneName>
    <PastInformation>
      <Index>1</Index>
      <Intensity>Typhoon</Intensity>
      <MaximumWind>145km/h</MaximumWind>
      <Time>2026-07-10T00:00:00+00:00</Time>
      <Latitude>18.50N</Latitude>
      <Longitude>121.20E</Longitude>
    </PastInformation>
    <AnalysisInformation>
      <Intensity>Severe Typhoon</Intensity>
      <MaximumWind>165km/h</MaximumWind>
      <Time>2026-07-10T03:00:00+00:00</Time>
      <Latitude>19.10N</Latitude>
      <Longitude>120.50E</Longitude>
    </AnalysisInformation>
    <ForecastInformation>
      <Index>3</Index>
      <Intensity>Typhoon</Intensity>
      <MaximumWind>150km/h</MaximumWind>
      <Time>2026-07-11T03:00:00+00:00</Time>
      <Latitude>20.20N</Latitude>
      <Longitude>119.00E</Longitude>
    </ForecastInformation>
  </WeatherReport>
</TropicalCycloneTrack>
"""


class TyphoonParserTests(SimpleTestCase):
    def test_parse_typhoon_list(self):
        result = parse_typhoon_list(TYPHOON_LIST_XML)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "2609")
        self.assertEqual(result[0]["name_zh"], "巴威")
        self.assertEqual(result[0]["name_en"], "BAVI")

    def test_parse_empty_typhoon_list(self):
        self.assertEqual(
            parse_typhoon_list("<TropicalCycloneList />"),
            [],
        )

    def test_hong_kong_typhoon_name_is_converted_to_simplified_chinese(self):
        traditional_xml = TYPHOON_LIST_XML.replace(
            "巴威",
            "熱帶低氣壓",
        )

        result = parse_typhoon_list(traditional_xml)

        self.assertEqual(result[0]["name_zh"], "热带低气压")

    def test_parse_namespaced_typhoon_track(self):
        summary = parse_typhoon_list(TYPHOON_LIST_XML)[0]

        result = parse_typhoon_track(TYPHOON_TRACK_XML, summary)

        self.assertEqual(result["display_name"], "巴威")
        self.assertEqual(result["bulletin_time"], "2026-07-10T11:00:00+08:00")
        self.assertEqual(len(result["past"]), 1)
        self.assertEqual(len(result["forecast"]), 1)
        self.assertEqual(result["current"]["latitude"], 19.1)
        self.assertEqual(result["current"]["longitude"], 120.5)
        self.assertEqual(result["current"]["maximum_wind"], 165.0)

    def test_invalid_track_coordinate_raises_data_error(self):
        summary = parse_typhoon_list(TYPHOON_LIST_XML)[0]
        invalid_xml = TYPHOON_TRACK_XML.replace("19.10N", "unknown")

        with self.assertRaises(TyphoonDataError):
            parse_typhoon_track(invalid_xml, summary)

    def test_official_http_track_url_is_upgraded_to_https(self):
        result = _validate_track_url(
            "http://www.weather.gov.hk/wxinfo/currwx/"
            "hko_tctrack_2617.xml"
        )

        self.assertEqual(
            result,
            "https://www.weather.gov.hk/wxinfo/currwx/"
            "hko_tctrack_2617.xml",
        )

    @patch("home.typhoon_service.requests.Session")
    @patch("home.typhoon_service.requests.get")
    def test_download_retries_directly_when_proxy_fails(
        self,
        requests_get,
        session_class,
    ):
        requests_get.side_effect = requests.exceptions.ProxyError(
            "proxy unavailable"
        )
        direct_response = MagicMock()
        direct_response.content = b"<TropicalCycloneList />"
        direct_session = session_class.return_value.__enter__.return_value
        direct_session.get.return_value = direct_response

        result = _download_xml(
            "https://www.weather.gov.hk/wxinfo/currwx/tc_list.xml"
        )

        self.assertEqual(result, b"<TropicalCycloneList />")
        self.assertFalse(direct_session.trust_env)
        direct_response.raise_for_status.assert_called_once_with()

    @patch("home.typhoon_service.requests.Session")
    @patch("home.typhoon_service.requests.get")
    def test_download_reports_when_proxy_and_direct_connection_fail(
        self,
        requests_get,
        session_class,
    ):
        requests_get.side_effect = requests.exceptions.ProxyError(
            "proxy unavailable"
        )
        direct_session = session_class.return_value.__enter__.return_value
        direct_session.get.side_effect = requests.exceptions.ConnectionError(
            "direct unavailable"
        )

        with self.assertRaisesRegex(
            TyphoonDataError,
            "代理连接失败，自动直连也失败",
        ):
            _download_xml(
                "https://www.weather.gov.hk/wxinfo/currwx/tc_list.xml"
            )


class CurrentTyphoonViewTests(SimpleTestCase):
    @patch("home.views.get_current_typhoons")
    def test_current_typhoon_endpoint(self, get_current_typhoons):
        get_current_typhoons.return_value = {
            "source": {
                "name": "香港天文台",
                "url": "https://www.weather.gov.hk/",
            },
            "typhoons": [],
            "partial_errors": [],
            "stale": False,
        }

        response = self.client.get(reverse("current_typhoons"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(response.json()["typhoons"], [])

    @patch("home.views.get_current_typhoons")
    def test_current_typhoon_endpoint_reports_source_error(
        self,
        get_current_typhoons,
    ):
        get_current_typhoons.side_effect = TyphoonDataError("数据源不可用")

        response = self.client.get(reverse("current_typhoons"))

        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json()["success"])
        self.assertEqual(response.json()["message"], "数据源不可用")


class ModelParameterPageTests(SimpleTestCase):
    def test_model_parameter_editor_is_a_standalone_page(self):
        response = self.client.get(reverse("model_parameter_page"))
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertIn('id="model-parameter-form"', content)
        self.assertIn('id="model-parameter-fields"', content)
        self.assertIn("/AISData/model-parameters/", content)
        self.assertIn("method: 'PUT'", content)
        self.assertIn("method: 'DELETE'", content)
        self.assertIn("该模型暂无可配置参数", content)
        self.assertIn("!isConfigurable", content)
        self.assertNotIn("side-panel-container", content)

    def test_dashboard_links_to_full_model_parameter_page(self):
        response = self.client.get(reverse("index"))
        content = response.content.decode("utf-8")

        self.assertIn('href="/model-parameters/"', content)
        self.assertNotIn('id="model-parameters-panel-container"', content)
        self.assertNotIn("toggleModelParametersPanel", content)


class AisNavigationStatusTemplateTests(SimpleTestCase):
    def test_dashboard_displays_ais_navigation_status_as_status(self):
        response = self.client.get(reverse("index"))
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn("AIS_NAVIGATION_STATUS_LABELS", content)
        self.assertIn("normaliseAisNavigationStatus", content)
        self.assertIn("normaliseAisShipType", content)
        self.assertIn(
            "船舶类型: ${escapeHtml(normaliseAisShipType(ship.shipType))}",
            content,
        )
        self.assertIn("ship.ship_type ?? ship.shipType ?? ship.vessel_type", content)
        self.assertIn("状态: ${ship.navStatusLabel}", content)
        self.assertIn("航速: ${ship.speed.toFixed(1)}节", content)
        self.assertIn("航向: ${ship.course.toFixed(2)}°", content)
        self.assertNotIn("航速/航向:", content)
        self.assertIn("ship-navigation-status-label", content)
        self.assertNotIn("运动状态:", content)
        self.assertNotIn("AIS航行状态:", content)


class ShipTrajectoryTemplateIntegrationTests(SimpleTestCase):
    def test_detection_detail_mmsi_locates_ship_and_loads_history(self):
        response = self.client.get(reverse("index"))
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertIn("locateShipWithHistory", content)
        self.assertIn("/AISData/ship-trajectories/", content)
        self.assertIn("定位船舶并显示历史轨迹", content)
        self.assertIn("showShipTrajectoryByMmsi", content)
        self.assertIn('data-mmsi="${mmsi}"', content)
        self.assertIn('data-end-at="${escapeHtml(endAt)}"', content)
        self.assertIn("button.dataset.mmsi", content)
        self.assertIn("button.dataset.endAt", content)
        self.assertIn("parameters.set('end', endAt)", content)
        self.assertNotIn("AMap.event.addListener(shipInfoWindow", content)
        self.assertIn('id="violation-trajectory-status"', content)
