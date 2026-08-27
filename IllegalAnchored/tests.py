import json
from datetime import timedelta

from django.core.cache import cache
from django.urls import resolve
from django.test import (
    RequestFactory,
    SimpleTestCase,
    TestCase,
    override_settings,
)
from django.utils import timezone

from AISData.detection import DETECTORS
from AISData.maritime_zones import (
    ANCHORAGE_ZONE_TYPES,
    PORT_ZONE_TYPES,
    get_maritime_zones,
    point_in_polygon,
)
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


def polygon_interior_point(zone):
    candidate = polygon_centroid(zone.points)
    if point_in_polygon(*candidate, zone.points):
        return candidate
    min_lon, min_lat, max_lon, max_lat = zone.bounds
    for x_step in range(1, 40):
        for y_step in range(1, 40):
            candidate = (
                min_lon + (max_lon - min_lon) * x_step / 40,
                min_lat + (max_lat - min_lat) * y_step / 40,
            )
            if point_in_polygon(*candidate, zone.points):
                return candidate
    raise AssertionError(f"No interior test point found for {zone.name}")


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

    def test_bundled_hong_kong_anchorage_is_recognised(self):
        anchorage = next(
            zone
            for zone in get_maritime_zones({"ANC"})
            if zone.locode == "HKABD"
        )

        result = classify_location(*polygon_interior_point(anchorage))

        self.assertEqual(result["state"], "authorized")
        self.assertEqual(result["zone_name"], anchorage.name)
        self.assertIn("HKABD", result["reason"])

    def test_bundled_hong_kong_and_macau_zones_are_available(self):
        zones = get_maritime_zones(
            ANCHORAGE_ZONE_TYPES | PORT_ZONE_TYPES
        )

        self.assertEqual(
            sum(
                zone.region == "香港"
                and zone.zone_type in ANCHORAGE_ZONE_TYPES
                for zone in zones
            ),
            12,
        )
        self.assertEqual(
            sum(
                zone.region == "香港"
                and zone.zone_type in PORT_ZONE_TYPES
                for zone in zones
            ),
            8,
        )
        self.assertEqual(
            sum(
                zone.region == "澳门"
                and zone.zone_type in ANCHORAGE_ZONE_TYPES
                for zone in zones
            ),
            1,
        )
        self.assertEqual(
            sum(
                zone.region == "澳门"
                and zone.zone_type in PORT_ZONE_TYPES
                for zone in zones
            ),
            1,
        )


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

    @override_settings(
        ILLEGAL_ANCHORED_DETECTION={
            "min_duration_seconds": 60,
            "history_window_seconds": 600,
        }
    )
    def test_detector_reads_runtime_parameter_settings(self):
        start = timezone.now()
        self.assertEqual(self._detect(start)["count"], 0)
        self.assertEqual(
            self._detect(start + timedelta(seconds=30))["count"],
            0,
        )

        payload = self._detect(start + timedelta(seconds=60))

        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["rule"]["min_duration_seconds"], 60)
        self.assertEqual(payload["rule"]["history_window_seconds"], 600)

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

    def test_explicit_anchor_status_still_requires_low_speed(self):
        start = timezone.now()
        self._detect(start)
        self._detect(start + timedelta(minutes=3))
        self._detect(
            start + timedelta(minutes=4),
            speed=3.5,
            nav_status=1,
        )

        payload = self._detect(start + timedelta(minutes=8))

        self.assertEqual(payload["count"], 0)

    def test_separate_anchor_episodes_have_different_event_ids(self):
        start = timezone.now()
        for minutes in (0, 3, 6):
            first = self._detect(start + timedelta(minutes=minutes))

        self._detect(
            start + timedelta(minutes=7),
            speed=3.5,
            nav_status=1,
        )
        for minutes in (8, 11, 14):
            second = self._detect(start + timedelta(minutes=minutes))

        self.assertEqual(first["count"], 1)
        self.assertEqual(second["count"], 1)
        self.assertNotEqual(
            first["results"][0]["event_id"],
            second["results"][0]["event_id"],
        )

    def test_event_id_stays_stable_when_history_window_advances(self):
        start = timezone.now()
        alerts = []

        for minutes in range(0, 37, 3):
            payload = self._detect(start + timedelta(minutes=minutes))
            if payload["count"]:
                alerts.append(payload["results"][0])

        self.assertGreater(len(alerts), 2)
        self.assertEqual(
            {alert["event_id"] for alert in alerts},
            {alerts[0]["event_id"]},
        )
        self.assertEqual(
            alerts[-1]["episode_started_at"],
            start.isoformat(),
        )
        self.assertEqual(alerts[-1]["duration_seconds"], 36 * 60)

    def test_hong_kong_and_macau_port_polygons_do_not_alert(self):
        ports = get_maritime_zones(PORT_ZONE_TYPES)
        for region in ("香港", "澳门"):
            cache.clear()
            port = next(zone for zone in ports if zone.region == region)
            point = polygon_interior_point(port)
            for minutes in (0, 3, 6):
                payload = self._detect(
                    timezone.now() + timedelta(minutes=minutes),
                    point=point,
                )
            self.assertEqual(payload["count"], 0)

    def test_low_speed_underway_status_is_staying_not_anchoring(self):
        start = timezone.now()
        for minutes in (0, 3, 6):
            payload = self._detect(
                start + timedelta(minutes=minutes),
                speed=0.0,
                nav_status=0,
            )

        self.assertEqual(payload["count"], 0)

    def test_status_zero_large_displacement_resets_episode(self):
        start = timezone.now()
        first_point = (113.9634, 22.3049)
        moved_point = (113.9664, 22.3049)
        self._detect(
            start,
            point=first_point,
            speed=0.0,
            nav_status=0,
        )
        self._detect(
            start + timedelta(minutes=3),
            point=first_point,
            speed=0.0,
            nav_status=0,
        )

        payload = self._detect(
            start + timedelta(minutes=6),
            point=moved_point,
            speed=0.0,
            nav_status=0,
        )

        self.assertEqual(payload["count"], 0)

    def test_low_speed_excluded_navigation_status_does_not_alert(self):
        start = timezone.now()
        for nav_status in (3, 5, 8):
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

    def test_undefined_status_without_swing_is_not_anchoring(self):
        start = timezone.now()
        point = (114.60, 22.50)
        for minutes in (0, 3, 6):
            payload = self._detect(
                start + timedelta(minutes=minutes),
                point=point,
                nav_status=15,
            )

        self.assertEqual(payload["count"], 0)

    def test_heading_swing_can_confirm_anchor_without_anchor_status(self):
        start = timezone.now()
        for minutes, heading in zip((0, 3, 6), (0, 90, 180)):
            payload = self._detect(
                start + timedelta(minutes=minutes),
                nav_status=15,
                heading=heading,
            )

        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["behavior"], "anchoring")

    def test_position_swing_can_confirm_anchor_when_heading_is_missing(self):
        start = timezone.now()
        lon, lat = self.prohibited_point
        positions = [
            (lon, lat),
            (lon + 0.0006, lat),
            (lon, lat),
            (lon + 0.0006, lat),
        ]
        for minutes, point in zip((0, 2, 4, 6), positions):
            payload = self._detect(
                start + timedelta(minutes=minutes),
                point=point,
                nav_status=15,
                heading=None,
            )

        self.assertEqual(payload["count"], 1)
        self.assertTrue(payload["results"][0]["position_swing"])
