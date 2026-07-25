import json
from datetime import timedelta

from django.core.cache import cache
from django.urls import resolve
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.utils import timezone

from AISData.detection import DETECTORS
from AISData.views import cached_detection_result

from .views import detect_illegal_anchored
from .zones import (
    AUTHORIZED_ANCHORAGES,
    PROHIBITED_ANCHOR_ZONES,
    classify_location,
)


def polygon_centroid(points):
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


class AnchorageGeometryTests(SimpleTestCase):
    def test_detector_is_registered_and_legacy_url_is_cache_only(self):
        self.assertEqual(
            DETECTORS["detect-illegalAnchored"],
            "IllegalAnchored.views.detect_illegal_anchored",
        )
        match = resolve("/IllegalAnchored/detectIllegalAnchored/")
        self.assertIs(match.func, cached_detection_result)
        self.assertEqual(
            match.kwargs["feature_id"],
            "detect-illegalAnchored",
        )

    def test_authorized_anchorage_is_recognised(self):
        anchorage = next(
            zone
            for zone in AUTHORIZED_ANCHORAGES
            if zone["name"] == "大鹏湾LNG专用锚地"
        )
        lon, lat = polygon_centroid(anchorage["points"])

        result = classify_location(lon, lat)

        self.assertEqual(result["state"], "authorized")
        self.assertEqual(result["zone_name"], "大鹏湾LNG专用锚地")

    def test_prohibited_route_is_recognised(self):
        zone = PROHIBITED_ANCHOR_ZONES[0]
        lon, lat = polygon_centroid(zone["points"])

        result = classify_location(lon, lat)

        self.assertEqual(result["state"], "prohibited")
        self.assertEqual(result["zone_name"], "深圳西部公共航路")

    def test_unassigned_shenzhen_water_is_screened(self):
        result = classify_location(114.10, 22.50)
        self.assertEqual(result["state"], "outside_coverage")

        result = classify_location(114.60, 22.50)
        self.assertEqual(result["state"], "unassigned")

    def test_guangzhou_supplement_is_legal_zone_only(self):
        anchorage = next(
            zone
            for zone in AUTHORIZED_ANCHORAGES
            if zone["name"] == "45SJA锚地"
        )
        result = classify_location(*anchorage["center"])
        self.assertEqual(result["state"], "authorized")


class IllegalAnchoredDetectorTests(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        prohibited = PROHIBITED_ANCHOR_ZONES[0]
        self.prohibited_point = polygon_centroid(prohibited["points"])
        anchorage = next(
            zone
            for zone in AUTHORIZED_ANCHORAGES
            if zone["name"] == "大鹏湾LNG专用锚地"
        )
        self.authorized_point = polygon_centroid(anchorage["points"])

    def tearDown(self):
        cache.clear()

    def _detect(self, timestamp, point=None, **overrides):
        lon, lat = point or self.prohibited_point
        ship = {
            "timestamp": timestamp.isoformat(),
            "mmsi": "123456789",
            "name": "测试船",
            "lon": lon,
            "lat": lat,
            "course": 0,
            "speed": 0.1,
            "nav_status": 1,
            "at_dock": False,
        }
        ship.update(overrides)
        request = self.factory.get("/internal/detection/")
        request.ais_ship_list = [ship]
        response = detect_illegal_anchored(request)
        return json.loads(response.content)

    def test_empty_snapshot_is_safe(self):
        request = self.factory.get("/internal/detection/")
        request.ais_ship_list = []

        payload = json.loads(detect_illegal_anchored(request).content)

        self.assertTrue(payload["success"])
        self.assertEqual(payload["count"], 0)

    def test_stationary_ship_in_prohibited_zone_alerts_after_five_minutes(self):
        start = timezone.now()
        self.assertEqual(self._detect(start)["count"], 0)
        self.assertEqual(
            self._detect(start + timedelta(minutes=2))["count"],
            0,
        )

        payload = self._detect(start + timedelta(minutes=5))

        self.assertEqual(payload["count"], 1)
        self.assertEqual(
            payload["results"][0]["zone"],
            "深圳西部公共航路",
        )
        self.assertIn("AIS规则预警", payload["results"][0]["details"])

    def test_authorized_anchorage_does_not_alert(self):
        start = timezone.now()
        for minutes in (0, 3, 6):
            payload = self._detect(
                start + timedelta(minutes=minutes),
                point=self.authorized_point,
            )
        self.assertEqual(payload["count"], 0)

    def test_moving_or_docked_ship_resets_episode(self):
        start = timezone.now()
        self._detect(start)
        self._detect(start + timedelta(minutes=3))
        self._detect(start + timedelta(minutes=4), speed=3.0, nav_status=0)

        payload = self._detect(start + timedelta(minutes=8))
        self.assertEqual(payload["count"], 0)

        payload = self._detect(
            start + timedelta(minutes=20),
            at_dock=True,
        )
        self.assertEqual(payload["count"], 0)

    def test_low_speed_non_anchor_navigation_status_does_not_alert(self):
        start = timezone.now()
        for nav_status in (0, 3, 5, 8):
            cache.clear()
            for minutes in (0, 3, 6):
                payload = self._detect(
                    start + timedelta(minutes=minutes),
                    speed=0.0,
                    nav_status=nav_status,
                )
            self.assertEqual(payload["count"], 0)

    def test_port_matched_anchor_does_not_alert(self):
        start = timezone.now()
        for minutes in (0, 3, 6):
            payload = self._detect(
                start + timedelta(minutes=minutes),
                point=(114.60, 22.50),
                speed=0.0,
                nav_status=1,
                matched_port_name="深圳港",
            )
        self.assertEqual(payload["count"], 0)

    def test_port_match_does_not_override_explicit_no_anchor_zone(self):
        start = timezone.now()
        for minutes in (0, 3, 6):
            payload = self._detect(
                start + timedelta(minutes=minutes),
                speed=0.0,
                nav_status=1,
                matched_port_name="深圳港",
            )
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["risk"], "高风险")

    def test_outside_known_anchor_in_covered_water_is_screened(self):
        start = timezone.now()
        point = (114.60, 22.50)
        for minutes in (0, 3, 6):
            payload = self._detect(
                start + timedelta(minutes=minutes),
                point=point,
                nav_status=15,
            )

        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["risk"], "待核查")
