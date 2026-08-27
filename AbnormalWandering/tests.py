import json
from datetime import datetime, timedelta, timezone as datetime_timezone
from math import atan2, cos, degrees, radians

from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from IllegalAnchored.zones import AUTHORIZED_ANCHORAGES

from .models import MonitorRegion, TrajectoryBuffer
from .utils import (
    detect_loitering_events,
    displacement_metrics,
    get_monitored_area,
    haversine_metres,
    heading_change,
)
from .views import receive_realtime_point


TEST_CONFIG = {
    "analysis_window_minutes": 30,
    "retention_window_minutes": 60,
    "min_points": 10,
    "min_duration_minutes": 30,
    "max_range_metres": 3000,
    "min_path_distance_metres": 300,
    "min_leg_distance_metres": 20,
    "min_turn_angle_degrees": 30,
    "min_turn_count": 4,
    "max_displacement_ratio": 0.3,
    "min_revisit_ratio": 0.4,
    "grid_size_metres": 200,
    "revisit_enabled": True,
    "min_speed_knots": 0.5,
    "max_valid_speed_knots": 102.2,
    "max_jump_speed_knots": 80,
    "min_jump_distance_metres": 500,
    "min_turn_interval_seconds": 30,
    "max_gap_minutes": 5,
    "monitored_only": True,
    "min_area_point_ratio": 0.5,
    "normal_anchorage_max_speed_knots": 0.5,
    "normal_anchorage_max_range_metres": 200,
    "normal_port_max_speed_knots": 3,
    "normal_port_max_range_metres": 1000,
    "normal_area_point_ratio": 0.8,
    "legal_operation_areas": [],
    "monitored_areas": [
        {
            "name": "测试徘徊监控区",
            "bounds": (114.59, 22.49, 114.61, 22.51),
        }
    ],
}

TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "abnormal-wandering-tests",
    }
}

LOITERING_POSITIONS = [
    (114.6000, 22.5000),
    (114.6030, 22.5000),
    (114.6030, 22.5030),
    (114.6000, 22.5030),
    (114.6000, 22.5000),
    (114.6030, 22.5000),
    (114.6030, 22.5030),
    (114.6000, 22.5030),
    (114.6000, 22.5000),
    (114.6030, 22.5000),
    (114.6030, 22.5030),
    (114.6000, 22.5030),
    (114.6000, 22.5000),
    (114.6030, 22.5000),
    (114.6030, 22.5030),
    (114.6000, 22.5030),
    (114.6000, 22.5000),
]


def course_between(start, end):
    lon_delta = radians(end[0] - start[0]) * cos(radians(start[1]))
    lat_delta = radians(end[1] - start[1])
    return (degrees(atan2(lon_delta, lat_delta)) + 360) % 360


def make_points(positions, start=None, gap_minutes=2, speed=5.0):
    start = start or datetime(
        2020,
        12,
        27,
        tzinfo=datetime_timezone.utc,
    )
    courses = [
        course_between(point, positions[index + 1])
        for index, point in enumerate(positions[:-1])
    ]
    courses.append(courses[-1] if courses else 0)
    return [
        {
            "longitude": lon,
            "latitude": lat,
            "timestamp": start + timedelta(minutes=index * gap_minutes),
            "speed": speed,
            "course": courses[index],
        }
        for index, (lon, lat) in enumerate(positions)
    ]


LOITERING_COURSES = [
    point["course"] for point in make_points(LOITERING_POSITIONS)
]


def polygon_centroid(points):
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


@override_settings(ABNORMAL_WANDERING=TEST_CONFIG)
class LoiteringAlgorithmTests(SimpleTestCase):
    def test_haversine_distance_and_displacement_efficiency(self):
        distance = haversine_metres((114.6, 22.5), (114.601, 22.5))
        self.assertAlmostEqual(distance, 102.7, delta=1.0)
        points = [
            {
                "lon": lon,
                "lat": lat,
                "timestamp": datetime(2020, 1, 1, tzinfo=datetime_timezone.utc),
                "speed": 5.0,
                "course": 0.0,
            }
            for lon, lat in [
                (114.6, 22.5),
                (114.601, 22.5),
                (114.6, 22.5),
            ]
        ]
        net_distance, path_distance, ratio = displacement_metrics(points)
        self.assertAlmostEqual(net_distance, 0.0)
        self.assertGreater(path_distance, 200)
        self.assertAlmostEqual(ratio, 0.0)

    def test_cog_change_wraps_at_north(self):
        self.assertEqual(heading_change(350, 10), 20)
        self.assertEqual(heading_change(10, 350), 20)

    def test_polygon_vertices_take_precedence_over_bounding_rectangle(self):
        area = {
            "name": "三角形徘徊监控区",
            "bounds": [9, 9, 11, 11],
            "vertices": [[9, 9], [11, 9], [10, 11]],
        }

        self.assertIsNotNone(get_monitored_area(10, 10, [area]))
        self.assertIsNone(get_monitored_area(10.9, 10.9, [area]))

    def test_repeated_turning_is_detected(self):
        events = detect_loitering_events(
            make_points(LOITERING_POSITIONS),
        )

        self.assertEqual(len(events), 1)
        self.assertGreaterEqual(events[0]["turn_count"], 4)
        self.assertLessEqual(events[0]["displacement_ratio"], 0.3)
        self.assertGreaterEqual(events[0]["revisit_ratio"], 0.4)
        self.assertGreaterEqual(events[0]["trajectory_abnormal_count"], 2)

    def test_straight_route_is_not_detected(self):
        positions = [
            (114.59 + index * 0.001, 22.5)
            for index in range(17)
        ]

        self.assertEqual(
            detect_loitering_events(make_points(positions)),
            [],
        )

    def test_stationary_jitter_is_not_wandering(self):
        positions = [(114.6, 22.5)] * 17

        self.assertEqual(
            detect_loitering_events(make_points(positions)),
            [],
        )

    def test_low_speed_position_jitter_cannot_accumulate_into_wandering(self):
        offsets = [
            (-0.00015, -0.00010),
            (0.00015, 0.00010),
            (-0.00015, 0.00010),
            (0.00015, -0.00010),
        ]
        positions = [
            (114.6 + offsets[index % 4][0], 22.5 + offsets[index % 4][1])
            for index in range(17)
        ]
        points = make_points(positions, speed=0.1)

        self.assertGreater(displacement_metrics([
            {
                "lon": point["longitude"],
                "lat": point["latitude"],
                "timestamp": point["timestamp"],
                "speed": point["speed"],
                "course": point["course"],
            }
            for point in points
        ])[1], TEST_CONFIG["min_path_distance_metres"])
        self.assertEqual(detect_loitering_events(points), [])

    def test_low_displacement_and_revisit_without_turning_is_not_wandering(self):
        points = make_points(LOITERING_POSITIONS, speed=5.0)
        for point in points:
            point["course"] = 0.0

        self.assertEqual(detect_loitering_events(points), [])

    def test_large_time_gap_splits_trajectory(self):
        points = make_points(LOITERING_POSITIONS)
        for index in range(8, len(points)):
            points[index]["timestamp"] += timedelta(minutes=10)

        self.assertEqual(detect_loitering_events(points), [])

    def test_normal_curved_route_is_not_detected(self):
        positions = [
            (114.59 + index * 0.0008, 22.5 + (index ** 2) * 0.00001)
            for index in range(17)
        ]
        self.assertEqual(detect_loitering_events(make_points(positions)), [])

    def test_repeated_backtracking_is_detected(self):
        positions = [
            (114.6 if index % 2 == 0 else 114.604, 22.5)
            for index in range(17)
        ]
        self.assertEqual(len(detect_loitering_events(make_points(positions))), 1)

    def test_turning_while_progressing_is_not_detected(self):
        positions = [
            (114.59 + index * 0.001, 22.5 + (0.0003 if index % 2 else 0))
            for index in range(17)
        ]
        self.assertEqual(detect_loitering_events(make_points(positions)), [])

    def test_single_position_jump_does_not_create_event(self):
        positions = [(114.59 + index * 0.001, 22.5) for index in range(17)]
        positions[8] = (115.5, 23.5)
        self.assertEqual(detect_loitering_events(make_points(positions)), [])

    def test_monitor_scope_can_be_disabled_for_all_waters(self):
        outside_positions = [
            (115.6 + lon - 114.6, 23.5 + lat - 22.5)
            for lon, lat in LOITERING_POSITIONS
        ]
        points = make_points(outside_positions)

        self.assertEqual(detect_loitering_events(points), [])
        events = detect_loitering_events(
            points,
            {**TEST_CONFIG, "monitored_only": False},
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["area"], "全水域")

    def test_low_speed_small_drift_in_authorized_anchorage_is_excluded(self):
        anchorage = next(
            zone
            for zone in AUTHORIZED_ANCHORAGES
            if zone["name"] == "大鹏湾LNG专用锚地"
        )
        lon, lat = polygon_centroid(anchorage["points"])
        offsets = [(0, 0.0005), (0.0005, 0), (0, -0.0005), (-0.0005, 0)]
        positions = [
            (lon + offsets[index % 4][0], lat + offsets[index % 4][1])
            for index in range(17)
        ]
        points = make_points(positions, speed=0.3)
        config = {
            **TEST_CONFIG,
            "min_speed_knots": 0,
            "grid_size_metres": 20,
            "monitored_areas": [
                {
                    "name": "锚地测试区",
                    "bounds": (lon - 0.01, lat - 0.01, lon + 0.01, lat + 0.01),
                }
            ],
        }

        without_exclusion = {
            **config,
            "normal_anchorage_max_speed_knots": 0,
        }
        self.assertEqual(len(detect_loitering_events(points, without_exclusion)), 1)
        self.assertEqual(detect_loitering_events(points, config), [])


@override_settings(
    ABNORMAL_WANDERING=TEST_CONFIG,
    CACHES=TEST_CACHES,
)
class AbnormalWanderingDetectorTests(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        MonitorRegion.objects.all().delete()
        self.monitor_region = MonitorRegion.objects.create(
            name="测试徘徊监控区",
            min_lon=114.59,
            min_lat=22.49,
            max_lon=114.61,
            max_lat=22.51,
            vertices=[
                [114.59, 22.49],
                [114.61, 22.49],
                [114.61, 22.51],
                [114.59, 22.51],
            ],
            is_active=True,
        )
        self.start = datetime(
            2020,
            12,
            27,
            tzinfo=datetime_timezone.utc,
        )

    def tearDown(self):
        cache.clear()

    def _detect(self, index, **overrides):
        lon, lat = LOITERING_POSITIONS[index]
        ship = {
            "timestamp": (
                self.start + timedelta(minutes=index * 2)
            ).isoformat(),
            "mmsi": "123456789",
            "name": "测试船",
            "lon": lon,
            "lat": lat,
            "speed": 5.0,
            "course": LOITERING_COURSES[index],
            "at_dock": False,
            "matched_port_name": "",
        }
        ship.update(overrides)
        request = self.factory.get("/internal/detection/")
        request.ais_ship_list = [ship]
        return json.loads(receive_realtime_point(request).content)

    def test_empty_snapshot_is_safe(self):
        request = self.factory.get("/internal/detection/")
        request.ais_ship_list = []

        payload = json.loads(receive_realtime_point(request).content)

        self.assertTrue(payload["success"])
        self.assertEqual(payload["count"], 0)
        self.assertIsNone(payload["timestamp"])

    def test_historical_replay_detects_loitering(self):
        for index in range(len(LOITERING_POSITIONS)):
            payload = self._detect(index)

        self.assertEqual(payload["count"], 1)
        result = payload["results"][0]
        self.assertEqual(result["area"], "测试徘徊监控区")
        self.assertGreaterEqual(result["turn_count"], 4)
        self.assertLessEqual(result["range_metres"], 3000)
        self.assertGreaterEqual(result["trajectory_abnormal_count"], 2)
        self.assertIn("明显转向", result["details"])
        self.assertNotIn("待补充", result["details"])

    def test_one_incremental_batch_preserves_all_loitering_points(self):
        ships = []
        for index, (lon, lat) in enumerate(LOITERING_POSITIONS):
            ships.append(
                {
                    "timestamp": (
                        self.start + timedelta(minutes=index * 2)
                    ).isoformat(),
                    "mmsi": "123456789",
                    "name": "测试船",
                    "lon": lon,
                    "lat": lat,
                    "speed": 5.0,
                    "course": LOITERING_COURSES[index],
                    "at_dock": False,
                    "matched_port_name": "",
                }
            )
        request = self.factory.get("/internal/detection/")
        request.ais_ship_list = ships

        payload = json.loads(receive_realtime_point(request).content)

        self.assertEqual(payload["count"], 1)
        self.assertEqual(TrajectoryBuffer.objects.count(), len(ships))

    def test_unmodified_active_vessel_is_retained_until_incremental_gap(self):
        for index in range(len(LOITERING_POSITIONS)):
            active = self._detect(index)
        self.assertEqual(active["count"], 1)

        last_minutes = (len(LOITERING_POSITIONS) - 1) * 2
        base_ship = {
            "mmsi": "987654321",
            "name": "其他船",
            "lon": 114.60,
            "lat": 22.50,
            "speed": 5,
            "course": 90,
            "at_dock": False,
            "matched_port_name": "",
        }
        request = self.factory.get("/internal/detection/")
        request.ais_ship_list = [
            {
                **base_ship,
                "timestamp": (
                    self.start + timedelta(minutes=last_minutes + 2)
                ).isoformat(),
            }
        ]
        retained = json.loads(receive_realtime_point(request).content)
        request.ais_ship_list = [
            {
                **base_ship,
                "timestamp": (
                    self.start + timedelta(minutes=last_minutes + 6)
                ).isoformat(),
            }
        ]
        expired = json.loads(receive_realtime_point(request).content)

        self.assertEqual(retained["count"], 1)
        self.assertEqual(retained["results"][0]["mmsi"], "123456789")
        self.assertEqual(expired["count"], 0)

    @override_settings(
        ABNORMAL_WANDERING={**TEST_CONFIG, "monitored_only": False}
    )
    def test_realtime_detection_can_run_outside_monitor_regions(self):
        for index, (lon, lat) in enumerate(LOITERING_POSITIONS):
            payload = self._detect(
                index,
                lon=lon + 1,
                lat=lat + 1,
            )

        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["area"], "全水域")
        self.assertIs(payload["rule"]["monitored_only"], False)

    def test_duplicate_snapshot_does_not_duplicate_buffer_point(self):
        self._detect(0)
        self._detect(0)

        self.assertEqual(TrajectoryBuffer.objects.count(), 1)

    def test_low_speed_port_operation_is_excluded(self):
        for index in range(len(LOITERING_POSITIONS)):
            payload = self._detect(
                index,
                matched_port_name="深圳港",
                speed=0.3,
            )
        self.assertEqual(payload["count"], 0)
        self.assertTrue(TrajectoryBuffer.objects.exists())

    def test_explicit_docked_state_resets_history(self):
        for index in range(5):
            self._detect(index)
        payload = self._detect(5, at_dock=True)
        self.assertEqual(payload["count"], 0)
        self.assertFalse(TrajectoryBuffer.objects.exists())

    def test_authorized_anchorage_is_excluded(self):
        anchorage = next(
            zone
            for zone in AUTHORIZED_ANCHORAGES
            if zone["name"] == "大鹏湾LNG专用锚地"
        )
        lon, lat = polygon_centroid(anchorage["points"])
        MonitorRegion.objects.create(
            name="合法锚地测试区",
            min_lon=lon - 0.01,
            max_lon=lon + 0.01,
            min_lat=lat - 0.01,
            max_lat=lat + 0.01,
            is_active=True,
        )

        offsets = [
            (0.0, 0.0003),
            (0.0003, 0.0),
            (0.0, -0.0003),
            (-0.0003, 0.0),
        ]
        anchorage_positions = [
            (lon + offsets[index % 4][0], lat + offsets[index % 4][1])
            for index in range(len(LOITERING_POSITIONS))
        ]
        anchorage_courses = [
            point["course"] for point in make_points(anchorage_positions)
        ]
        for index, (point_lon, point_lat) in enumerate(anchorage_positions):
            payload = self._detect(
                index,
                lon=point_lon,
                lat=point_lat,
                course=anchorage_courses[index],
                speed=0.3,
            )

        self.assertEqual(payload["count"], 0)
        self.assertTrue(TrajectoryBuffer.objects.exists())

    def test_active_database_region_overrides_default_region(self):
        self.monitor_region.name = "其他监控区"
        self.monitor_region.min_lon = 115.0
        self.monitor_region.max_lon = 115.1
        self.monitor_region.min_lat = 23.0
        self.monitor_region.max_lat = 23.1
        self.monitor_region.vertices = [
            [115.0, 23.0],
            [115.1, 23.0],
            [115.1, 23.1],
            [115.0, 23.1],
        ]
        self.monitor_region.save()

        payload = self._detect(0)

        self.assertEqual(payload["count"], 0)
        self.assertFalse(TrajectoryBuffer.objects.exists())

    def test_inactive_database_region_disables_monitoring(self):
        self.monitor_region.is_active = False
        self.monitor_region.save(update_fields=("is_active",))

        payload = self._detect(0)

        self.assertEqual(payload["count"], 0)
        self.assertFalse(TrajectoryBuffer.objects.exists())

    def test_polygon_excludes_point_inside_its_bounding_rectangle(self):
        self.monitor_region.vertices = [
            [114.59, 22.49],
            [114.61, 22.49],
            [114.59, 22.51],
        ]
        self.monitor_region.save(update_fields=("vertices",))

        payload = self._detect(0, lon=114.609, lat=22.509)

        self.assertEqual(payload["count"], 0)
        self.assertFalse(TrajectoryBuffer.objects.exists())

    def test_monitor_area_crud_api(self):
        list_response = self.client.get(
            "/AbnormalWandering/monitor-areas/"
        )
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.json()["count"], 1)

        create_response = self.client.post(
            "/AbnormalWandering/monitor-areas/",
            data=json.dumps(
                {
                    "name": "第二徘徊监控区",
                    "vertices": [
                        [113.60, 22.10],
                        [113.80, 22.10],
                        [113.70, 22.20],
                    ],
                    "is_active": True,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(create_response.status_code, 201)
        created = create_response.json()["result"]
        self.assertEqual(len(created["vertices"]), 3)
        self.assertAlmostEqual(created["min_lon"], 113.60)
        self.assertAlmostEqual(created["max_lat"], 22.20)

        patch_response = self.client.patch(
            f"/AbnormalWandering/monitor-areas/{created['id']}/",
            data=json.dumps({"is_active": False}),
            content_type="application/json",
        )
        self.assertEqual(patch_response.status_code, 200)
        self.assertFalse(patch_response.json()["result"]["is_active"])

        delete_response = self.client.delete(
            f"/AbnormalWandering/monitor-areas/{created['id']}/"
        )
        self.assertEqual(delete_response.status_code, 200)

    def test_monitor_area_rejects_invalid_polygon_and_last_delete(self):
        invalid_response = self.client.post(
            "/AbnormalWandering/monitor-areas/",
            data=json.dumps(
                {
                    "name": "无效区域",
                    "vertices": [[113.6, 22.1], [113.7, 22.1]],
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(invalid_response.status_code, 400)

        delete_response = self.client.delete(
            f"/AbnormalWandering/monitor-areas/{self.monitor_region.id}/"
        )
        self.assertEqual(delete_response.status_code, 409)

    def test_home_page_exposes_wandering_polygon_management(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="wandering-panel-container"')
        self.assertContains(response, 'id="wandering-area-draw"')
        self.assertContains(response, "function loadWanderingAreas()")
        self.assertContains(response, "function startPickingWanderingArea()")

    def test_cleanup_uses_replay_timestamp(self):
        TrajectoryBuffer.objects.create(
            mmsi="old",
            name="旧轨迹",
            longitude=114.6,
            latitude=22.5,
            course=0,
            speed=1,
            timestamp=self.start - timedelta(minutes=61),
        )

        self._detect(0)

        self.assertFalse(
            TrajectoryBuffer.objects.filter(mmsi="old").exists()
        )

    def test_invalid_rows_are_skipped(self):
        request = self.factory.get("/internal/detection/")
        request.ais_ship_list = [
            {
                "mmsi": "bad",
                "timestamp": "invalid",
                "lon": 1,
                "lat": 2,
            },
            {
                "mmsi": "123456789",
                "timestamp": self.start.isoformat(),
                "lon": 114.6,
                "lat": 22.5,
                "speed": 1,
                "course": 0,
            },
        ]

        payload = json.loads(receive_realtime_point(request).content)

        self.assertEqual(payload["skipped_count"], 1)
        self.assertEqual(payload["count"], 0)
