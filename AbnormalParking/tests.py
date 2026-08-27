import json
from datetime import datetime, timedelta, timezone as datetime_timezone
from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from AISData.detection import DETECTORS
from IllegalAnchored.zones import AUTHORIZED_ANCHORAGES
from IllegalAnchored.views import detect_illegal_anchored

from .models import ParkingBuffer
from .utils import (
    circular_heading_variation,
    detect_parking_events,
)
from .views import detectAbnormalParking


TEST_CONFIG = {
    "analysis_window_minutes": 120,
    "retention_window_minutes": 240,
    "max_speed_knots": 0.5,
    "exit_speed_knots": 1.0,
    "distance_threshold_metres": 50,
    "position_exit_radius_metres": 100,
    "min_duration_minutes": 30,
    "min_points": 3,
    "max_gap_minutes": 20,
    "near_shore_distance_metres": 100,
    "max_heading_change_degrees": 25,
    "min_heading_observations": 2,
    "anchor_swing_heading_degrees": 45,
    "legal_max_duration_minutes": 0,
    "berthing_facility_areas": [
        {
            "name": "测试岸线设施",
            "bounds": (114.59, 22.49, 114.61, 22.51),
        }
    ],
    "legal_berthing_areas": [],
}


def polygon_centroid(points):
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


class ParkingEventAlgorithmTests(SimpleTestCase):
    def test_detector_is_registered(self):
        self.assertEqual(
            DETECTORS["detect-abnormalStaying"],
            "AbnormalParking.views.detectAbnormalParking",
        )

    def test_heading_variation_uses_circular_angles(self):
        self.assertEqual(circular_heading_variation([359, 1, 2]), 3)

    def test_exact_point_and_duration_thresholds_are_inclusive(self):
        start = datetime(2020, 12, 27, tzinfo=datetime_timezone.utc)
        points = [
            (22.5, 114.6, start, 0.1),
            (22.5, 114.6, start + timedelta(minutes=15), 0.1),
            (22.5, 114.6, start + timedelta(minutes=30), 0.1),
        ]

        events = detect_parking_events(
            points,
            config=TEST_CONFIG,
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["point_count"], 3)

    def test_moving_point_breaks_stationary_episode(self):
        start = datetime(2020, 12, 27, tzinfo=datetime_timezone.utc)
        points = [
            (22.5, 114.6, start, 0.1),
            (22.5, 114.6, start + timedelta(minutes=15), 0.1),
            (22.5, 114.6, start + timedelta(minutes=20), 3.0),
            (22.5, 114.6, start + timedelta(minutes=30), 0.1),
            (22.5, 114.6, start + timedelta(minutes=60), 0.1),
        ]

        events = detect_parking_events(
            points,
            config=TEST_CONFIG,
        )

        self.assertEqual(events, [])

    def test_missing_heading_is_auxiliary_not_mandatory(self):
        start = datetime(2020, 12, 27, tzinfo=datetime_timezone.utc)
        points = [
            (22.5, 114.6, start + timedelta(minutes=minutes), 0.1)
            for minutes in (0, 15, 30)
        ]
        self.assertEqual(
            len(detect_parking_events(points, config=TEST_CONFIG)),
            1,
        )

    def test_unstable_heading_is_not_moored_state(self):
        start = datetime(2020, 12, 27, tzinfo=datetime_timezone.utc)
        headings = (0, 90, 180)
        points = [
            (22.5, 114.6, start + timedelta(minutes=index * 15), 0.1, heading)
            for index, heading in enumerate(headings)
        ]
        self.assertEqual(detect_parking_events(points, config=TEST_CONFIG), [])

    def test_anchor_navigation_status_alone_does_not_override_physical_evidence(self):
        start = datetime(2020, 12, 27, tzinfo=datetime_timezone.utc)
        points = [
            {
                "latitude": 22.5,
                "longitude": 114.6,
                "timestamp": start + timedelta(minutes=minutes),
                "speed": 0.1,
                "heading": 90,
                "nav_status": 1,
            }
            for minutes in (0, 15, 30)
        ]
        self.assertEqual(
            len(detect_parking_events(points, config=TEST_CONFIG)),
            1,
        )

    def test_illegal_anchor_swing_away_from_port_is_not_abnormal_berthing(self):
        start = datetime(2020, 12, 27, tzinfo=datetime_timezone.utc)
        headings = (0, 90, 180, 270)
        points = [
            {
                "latitude": 22.55,
                "longitude": 114.65,
                "timestamp": start + timedelta(minutes=index * 10),
                "speed": 0.1,
                "heading": heading,
                "nav_status": 1,
            }
            for index, heading in enumerate(headings)
        ]
        config = {**TEST_CONFIG, "berthing_facility_areas": []}
        self.assertEqual(detect_parking_events(points, config=config), [])

    def test_natural_coastline_without_port_basin_is_not_berthing_evidence(self):
        start = datetime(2020, 12, 27, tzinfo=datetime_timezone.utc)
        points = [
            {
                "latitude": 22.55,
                "longitude": 114.65,
                "timestamp": start + timedelta(minutes=minutes),
                "speed": 0.1,
                "heading": 90,
                "nav_status": 5,
            }
            for minutes in (0, 15, 30)
        ]
        config = {**TEST_CONFIG, "berthing_facility_areas": []}

        self.assertEqual(detect_parking_events(points, config=config), [])

    def test_moored_status_while_underway_is_abnormal_parking_state(self):
        start = datetime(2020, 12, 27, tzinfo=datetime_timezone.utc)
        points = [
            {
                "latitude": 22.5,
                "longitude": 114.59 + index * 0.01,
                "timestamp": start + timedelta(minutes=index * 3),
                "speed": 5.0,
                "heading": 90,
                "nav_status": 5,
            }
            for index in range(3)
        ]

        events = detect_parking_events(points, config=TEST_CONFIG)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["behavior"], "underway_with_moored_status")
        self.assertGreater(events[0]["path_distance_metres"], 100)

    def test_low_speed_forward_motion_is_not_abnormal_berthing(self):
        start = datetime(2020, 12, 27, tzinfo=datetime_timezone.utc)
        points = [
            {
                "latitude": 22.5,
                "longitude": 114.595 + index * 0.001,
                "timestamp": start + timedelta(minutes=index * 5),
                "speed": 0.4,
                "heading": 90,
            }
            for index in range(7)
        ]
        self.assertEqual(detect_parking_events(points, config=TEST_CONFIG), [])

    def test_large_ais_gap_resets_candidate_duration(self):
        start = datetime(2020, 12, 27, tzinfo=datetime_timezone.utc)
        points = [
            (22.5, 114.6, start, 0.1, 90),
            (22.5, 114.6, start + timedelta(minutes=15), 0.1, 90),
            (22.5, 114.6, start + timedelta(minutes=60), 0.1, 90),
        ]
        self.assertEqual(detect_parking_events(points, config=TEST_CONFIG), [])


@override_settings(ABNORMAL_PARKING=TEST_CONFIG)
class AbnormalParkingDetectorTests(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        self.start = datetime(
            2020,
            12,
            27,
            tzinfo=datetime_timezone.utc,
        )

    def tearDown(self):
        cache.clear()

    def _detect(self, timestamp, **overrides):
        ship = {
            "timestamp": timestamp.isoformat(),
            "mmsi": "123456789",
            "name": "测试船",
            "lon": 114.60,
            "lat": 22.50,
            "speed": 0.1,
            "heading": 90,
            "nav_status": 5,
            "at_dock": False,
            "matched_port_name": "",
        }
        ship.update(overrides)
        request = self.factory.get("/internal/detection/")
        request.ais_ship_list = [ship]
        return json.loads(detectAbnormalParking(request).content)

    def test_empty_snapshot_is_safe(self):
        request = self.factory.get("/internal/detection/")
        request.ais_ship_list = []

        payload = json.loads(detectAbnormalParking(request).content)

        self.assertTrue(payload["success"])
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["timestamp"], None)

    def test_historical_replay_uses_ais_time_window(self):
        self.assertEqual(self._detect(self.start)["count"], 0)
        self.assertEqual(
            self._detect(self.start + timedelta(minutes=15))["count"],
            0,
        )

        payload = self._detect(self.start + timedelta(minutes=30))

        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["facility"], "测试岸线设施")
        self.assertEqual(payload["results"][0]["duration_minutes"], 30)
        self.assertIn("异常停泊", payload["results"][0]["details"])
        self.assertNotIn("异常徘徊", payload["results"][0]["details"])

    def test_duplicate_snapshot_does_not_duplicate_buffer_point(self):
        self._detect(self.start)
        self._detect(self.start)

        self.assertEqual(ParkingBuffer.objects.count(), 1)

    def test_moving_sample_resets_episode(self):
        self._detect(self.start)
        self._detect(self.start + timedelta(minutes=15))
        self._detect(self.start + timedelta(minutes=20), speed=4.0)
        self._detect(self.start + timedelta(minutes=30))

        payload = self._detect(self.start + timedelta(minutes=60))

        self.assertEqual(payload["count"], 0)

    def test_realtime_moored_status_while_sailing_uses_parking_feature(self):
        for index in range(3):
            payload = self._detect(
                self.start + timedelta(minutes=index * 3),
                lon=114.59 + index * 0.01,
                speed=5.0,
                nav_status=5,
            )

        self.assertEqual(payload["count"], 1)
        result = payload["results"][0]
        self.assertEqual(result["behavior"], "underway_with_moored_status")
        self.assertGreaterEqual(result["mean_speed_knots"], 1.0)
        self.assertGreaterEqual(result["path_distance_metres"], 100.0)

    def test_normal_legal_berth_for_one_hour_does_not_alert(self):
        for minutes in (0, 15, 30, 45, 60):
            payload = self._detect(
                self.start + timedelta(minutes=minutes),
                matched_port_name="深圳港",
            )
        self.assertEqual(payload["count"], 0)
        self.assertTrue(ParkingBuffer.objects.filter(mmsi="123456789").exists())

    def test_at_dock_outside_legal_area_can_be_abnormal(self):
        for minutes in (0, 15, 30):
            payload = self._detect(
                self.start + timedelta(minutes=minutes),
                at_dock=True,
            )
        self.assertEqual(payload["count"], 1)

    def test_same_offshore_stop_is_only_reported_by_illegal_anchor_detector(self):
        positions = [
            (114.6000, 22.5000, 0),
            (114.6001, 22.5000, 2),
            (114.6000, 22.5001, 5),
        ]
        parking_points = [
            {
                "longitude": lon,
                "latitude": lat,
                "timestamp": self.start + timedelta(minutes=minutes),
                "speed": 0.1,
                "heading": heading,
                "nav_status": 1,
            }
            for (lon, lat, minutes), heading in zip(
                positions,
                (0, 90, 180),
            )
        ]
        parking_config = {
            **TEST_CONFIG,
            "berthing_facility_areas": [],
        }
        self.assertEqual(
            detect_parking_events(parking_points, config=parking_config),
            [],
        )

        for lon, lat, minutes in positions:
            request = self.factory.get("/internal/detection/")
            request.ais_ship_list = [
                {
                    "timestamp": (
                        self.start + timedelta(minutes=minutes)
                    ).isoformat(),
                    "mmsi": "987654321",
                    "name": "锚泊测试船",
                    "lon": lon,
                    "lat": lat,
                    "speed": 0.1,
                    "nav_status": 1,
                    "at_dock": False,
                    "matched_port_name": "",
                }
            ]
            anchor_payload = json.loads(
                detect_illegal_anchored(request).content
            )
        self.assertEqual(anchor_payload["count"], 1)

    def test_authorized_anchorage_does_not_alert_or_buffer(self):
        anchorage = next(
            zone
            for zone in AUTHORIZED_ANCHORAGES
            if zone["name"] == "大鹏湾LNG专用锚地"
        )
        lon, lat = polygon_centroid(anchorage["points"])

        for minutes in (0, 15, 30):
            payload = self._detect(
                self.start + timedelta(minutes=minutes),
                lon=lon,
                lat=lat,
            )

        self.assertEqual(payload["count"], 0)
        self.assertTrue(ParkingBuffer.objects.exists())

    def test_short_stop_does_not_alert_and_speed_recovery_ends_candidate(self):
        self._detect(self.start)
        self._detect(self.start + timedelta(minutes=5))
        payload = self._detect(self.start + timedelta(minutes=10), speed=2.0)
        self.assertEqual(payload["count"], 0)

    def test_single_speed_excursion_below_exit_threshold_does_not_reset(self):
        speeds = (0.1, 0.2, 0.0, 0.8, 0.2, 0.1)
        for index, speed in enumerate(speeds):
            payload = self._detect(
                self.start + timedelta(minutes=index * 6),
                speed=speed,
            )
        self.assertEqual(payload["count"], 1)

    @override_settings(
        ABNORMAL_PARKING={
            **TEST_CONFIG,
            "legal_max_duration_minutes": 20,
        }
    )
    def test_legal_berth_can_alert_after_configured_time_limit(self):
        for minutes in (0, 15, 30):
            payload = self._detect(
                self.start + timedelta(minutes=minutes),
                matched_port_name="测试合法泊位",
            )
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["reason"], "超过合法停泊允许时长")

    def test_cleanup_uses_replay_timestamp(self):
        ParkingBuffer.objects.create(
            mmsi="old",
            name="旧轨迹",
            longitude=114.6,
            latitude=22.5,
            speed=0,
            timestamp=self.start - timedelta(hours=5),
        )

        self._detect(self.start)

        self.assertFalse(ParkingBuffer.objects.filter(mmsi="old").exists())

    def test_invalid_rows_are_skipped(self):
        request = self.factory.get("/internal/detection/")
        request.ais_ship_list = [
            {"mmsi": "bad", "timestamp": "invalid", "lon": 1, "lat": 2},
            {
                "mmsi": "123456789",
                "timestamp": self.start.isoformat(),
                "lon": 114.6,
                "lat": 22.5,
                "speed": 0.1,
            },
        ]

        payload = json.loads(detectAbnormalParking(request).content)

        self.assertEqual(payload["skipped_count"], 1)
        self.assertEqual(payload["count"], 0)

    @override_settings(
        ABNORMAL_PARKING={
            **TEST_CONFIG,
            "berthing_facility_areas": [
                {
                    "name": "远端测试设施",
                    "bounds": (119.99, 29.99, 120.01, 30.01),
                }
            ],
        }
    )
    def test_detection_is_not_limited_by_a_monitor_area(self):
        for minutes in (0, 15, 30):
            payload = self._detect(
                self.start + timedelta(minutes=minutes),
                lon=120.0,
                lat=30.0,
            )

        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["facility"], "远端测试设施")

    def test_monitor_area_api_is_removed(self):
        response = self.client.get("/AbnormalParking/monitor-areas/")

        self.assertEqual(response.status_code, 404)
