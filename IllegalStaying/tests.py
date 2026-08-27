import json
from datetime import datetime, timedelta, timezone

from django.core.cache import cache
from django.test import (
    RequestFactory,
    SimpleTestCase,
    TestCase,
    override_settings,
)
from django.urls import resolve

from AISData.detection import DETECTORS
from AISData.views import cached_detection_result
from IllegalAnchored.views import (
    HISTORY_CACHE_KEY as ANCHOR_HISTORY_CACHE_KEY,
    detect_illegal_anchored,
)
from IllegalAnchored.zones import PROHIBITED_ANCHOR_ZONES

from .models import IllegalStayingMonitorArea, StayingBuffer
from .utils import get_forbidden_area
from .views import (
    ILLEGAL_STAYING_EVENT_CACHE_KEY,
    detectIllegalStaying,
)


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "illegal-staying-tests",
    }
}

TEST_CONFIG = {
    "analysis_window_minutes": 10,
    "retention_window_minutes": 20,
    "max_speed_knots": 0.5,
    "distance_threshold_metres": 50,
    "min_duration_minutes": 2,
    "min_points": 3,
    "maximum_gap_seconds": 40,
    "max_position_age_seconds": 60,
    "future_tolerance_seconds": 60,
    "event_retention_minutes": 10,
    "forbidden_areas": [
        {
            "name": "测试禁停区",
            "bounds": [9, 9, 11, 11],
            "reason": "测试水域禁止驻留",
        }
    ],
}


class IllegalStayingRegistrationTests(SimpleTestCase):
    def test_detector_registration_and_cache_only_url(self):
        self.assertEqual(
            DETECTORS["detect-illegalStaying"],
            "IllegalStaying.views.detectIllegalStaying",
        )
        match = resolve("/IllegalStaying/detectIllegalStaying/")
        self.assertIs(match.func, cached_detection_result)
        self.assertEqual(
            match.kwargs["feature_id"],
            "detect-illegalStaying",
        )

    def test_polygon_forbidden_area_is_supported(self):
        areas = [
            {
                "name": "多边形禁停区",
                "bounds": [9, 9, 11, 11],
                "vertices": [[9, 9], [11, 9], [10, 11]],
            }
        ]
        area = get_forbidden_area(
            10,
            10,
            areas,
        )

        self.assertEqual(area["name"], "多边形禁停区")
        self.assertIsNone(get_forbidden_area(10.9, 10.9, areas))


@override_settings(
    CACHES=TEST_CACHES,
    ILLEGAL_STAYING=TEST_CONFIG,
)
class IllegalStayingDetectorTests(TestCase):
    base_time = datetime(2020, 12, 28, tzinfo=timezone.utc)

    def setUp(self):
        cache.delete(ILLEGAL_STAYING_EVENT_CACHE_KEY)
        self.factory = RequestFactory()
        IllegalStayingMonitorArea.objects.all().delete()
        self.monitor_area = IllegalStayingMonitorArea.objects.create(
            name="测试禁停区",
            reason="测试水域禁止驻留",
            min_lon=9,
            min_lat=9,
            max_lon=11,
            max_lat=11,
            is_active=True,
        )

    def ship(
        self,
        seconds,
        speed=0.1,
        mmsi="123456789",
        lon=10,
        lat=10,
        **extra,
    ):
        ship = {
            "timestamp": (
                self.base_time + timedelta(seconds=seconds)
            ).isoformat(),
            "mmsi": mmsi,
            "name": None,
            "lon": lon,
            "lat": lat,
            "speed": speed,
        }
        ship.update(extra)
        return ship

    def call_view(self, ship_list, method="get"):
        request = getattr(self.factory, method)("/internal/detection/")
        request.ais_ship_list = ship_list
        response = detectIllegalStaying(request)
        payload = None
        if response.headers.get("Content-Type", "").startswith(
            "application/json"
        ):
            payload = json.loads(response.content)
        return response, payload

    def feed(
        self,
        seconds_list=(0, 30, 60, 90, 120),
        **overrides,
    ):
        payload = None
        latest_ship = None
        for seconds in seconds_list:
            latest_ship = self.ship(seconds, **overrides)
            _, payload = self.call_view([latest_ship])
        return payload, latest_ship

    def test_empty_snapshot_and_post_are_safe(self):
        response, payload = self.call_view([])
        post_response, _ = self.call_view([], method="post")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["count"], 0)
        self.assertEqual(post_response.status_code, 405)

    def test_invalid_records_are_skipped_instead_of_stored_as_zero_speed(self):
        _, payload = self.call_view(
            [
                self.ship(0, speed=None, mmsi="1"),
                self.ship(0, speed=102.3, mmsi="2"),
                self.ship(0, lon=999, mmsi="3"),
                {"mmsi": "4", "speed": 0.1},
            ]
        )

        self.assertEqual(payload["skipped_count"], 4)
        self.assertEqual(StayingBuffer.objects.count(), 0)

    def test_inactive_monitor_area_disables_detection_and_clears_history(self):
        self.feed((0, 30, 60))
        self.monitor_area.is_active = False
        self.monitor_area.save()

        _, payload = self.call_view([self.ship(90)])

        self.assertEqual(payload["count"], 0)
        self.assertEqual(StayingBuffer.objects.count(), 0)

    def test_database_polygon_excludes_points_only_inside_its_bounds(self):
        self.monitor_area.vertices = [[9, 9], [11, 9], [10, 11]]
        self.monitor_area.save(update_fields=("vertices", "updated_at"))

        _, payload = self.call_view([self.ship(0, lon=10.9, lat=10.9)])

        self.assertEqual(payload["count"], 0)
        self.assertEqual(StayingBuffer.objects.count(), 0)

    def test_monitor_area_crud_and_validation(self):
        list_response = self.client.get("/IllegalStaying/monitor-areas/")
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.json()["count"], 1)
        self.assertEqual(
            list_response.json()["results"][0]["reason"],
            "测试水域禁止驻留",
        )

        create_response = self.client.post(
            "/IllegalStaying/monitor-areas/",
            data=json.dumps(
                {
                    "name": "第二禁停区",
                    "reason": "测试原因",
                    "min_lon": 12,
                    "min_lat": 12,
                    "max_lon": 13,
                    "max_lat": 13,
                    "vertices": [[12, 12], [13, 12], [12, 13]],
                    "is_active": True,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(create_response.status_code, 201)
        created = create_response.json()["result"]
        self.assertEqual(len(created["vertices"]), 3)

        update_response = self.client.patch(
            f"/IllegalStaying/monitor-areas/{created['id']}/",
            data=json.dumps({"is_active": False, "reason": "更新原因"}),
            content_type="application/json",
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertFalse(update_response.json()["result"]["is_active"])
        self.assertEqual(update_response.json()["result"]["reason"], "更新原因")

        delete_response = self.client.delete(
            f"/IllegalStaying/monitor-areas/{created['id']}/"
        )
        self.assertEqual(delete_response.status_code, 200)
        last_delete_response = self.client.delete(
            f"/IllegalStaying/monitor-areas/{self.monitor_area.id}/"
        )
        self.assertEqual(last_delete_response.status_code, 409)

        invalid_response = self.client.post(
            "/IllegalStaying/monitor-areas/",
            data=json.dumps(
                {
                    "name": "错误区域",
                    "reason": "",
                    "min_lon": 13,
                    "min_lat": 12,
                    "max_lon": 12,
                    "max_lat": 13,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(invalid_response.status_code, 400)

    def test_historical_ais_time_drives_duration_and_cleanup(self):
        payload, _ = self.feed()

        self.assertEqual(payload["count"], 1)
        result = payload["results"][0]
        self.assertEqual(result["duration_minutes"], 2)
        self.assertEqual(result["point_count"], 5)
        self.assertEqual(result["name"], "未知目标")
        self.assertEqual(result["zone"], "测试禁停区")
        self.assertTrue(result["is_new"])
        self.assertEqual(StayingBuffer.objects.count(), 5)

    def test_large_ais_gap_breaks_the_episode(self):
        payload, _ = self.feed((0, 30, 180, 210, 240))

        self.assertEqual(payload["count"], 0)

    def test_movement_ends_episode_instead_of_being_filtered_out(self):
        self.feed((0, 30, 60))
        _, moving = self.call_view([self.ship(90, speed=3)])
        payload, _ = self.feed((120, 150, 180, 210))

        self.assertEqual(moving["count"], 0)
        self.assertEqual(payload["count"], 0)
        self.assertEqual(StayingBuffer.objects.count(), 4)

    def test_leaving_zone_resets_and_port_behavior_does_not_alert(self):
        self.feed((0, 30, 60))
        self.call_view([self.ship(90, lon=12)])
        self.assertEqual(StayingBuffer.objects.count(), 0)

        self.feed((120, 150, 180))
        self.call_view(
            [self.ship(210, matched_port_name="测试港")]
        )
        self.assertGreater(StayingBuffer.objects.count(), 0)

        self.feed((240, 270, 300))
        self.call_view([self.ship(330, at_dock=True)])
        self.assertGreater(StayingBuffer.objects.count(), 0)

    def test_anchoring_in_forbidden_area_emits_independent_staying_alert(self):
        payload, _ = self.feed(nav_status=1, heading=90)

        self.assertEqual(payload["count"], 1)
        result = payload["results"][0]
        self.assertEqual(result["behavior"], "anchoring")
        self.assertEqual(
            result["related_primary_feature_id"],
            "detect-illegalAnchored",
        )
        self.assertIn("同时命中非法驻留", result["details"])

    @override_settings(
        ILLEGAL_ANCHORED_DETECTION={
            "min_duration_seconds": 120,
            "history_window_seconds": 600,
            "max_gap_seconds": 60,
        }
    )
    def test_forbidden_area_anchor_produces_two_feature_alerts(self):
        cache.delete(ANCHOR_HISTORY_CACHE_KEY)
        zone = PROHIBITED_ANCHOR_ZONES[0]
        lon = sum(point[0] for point in zone["points"]) / len(zone["points"])
        lat = sum(point[1] for point in zone["points"]) / len(zone["points"])
        self.monitor_area.min_lon = lon - 0.01
        self.monitor_area.max_lon = lon + 0.01
        self.monitor_area.min_lat = lat - 0.01
        self.monitor_area.max_lat = lat + 0.01
        self.monitor_area.vertices = []
        self.monitor_area.save()

        staying_payload = None
        anchor_payload = None
        for seconds in (0, 30, 60, 90, 120):
            ship = self.ship(
                seconds,
                lon=lon,
                lat=lat,
                nav_status=1,
                heading=90,
            )
            _, staying_payload = self.call_view([ship])
            request = self.factory.get("/internal/detection/")
            request.ais_ship_list = [ship]
            anchor_payload = json.loads(
                detect_illegal_anchored(request).content
            )

        self.assertEqual(staying_payload["count"], 1)
        self.assertEqual(anchor_payload["count"], 1)
        self.assertEqual(
            staying_payload["results"][0]["behavior"],
            "anchoring",
        )
        self.assertEqual(
            anchor_payload["results"][0]["behavior"],
            "anchoring",
        )
        self.assertNotEqual(
            staying_payload["results"][0]["event_id"],
            anchor_payload["results"][0]["event_id"],
        )

    def test_drift_beyond_radius_starts_a_new_episode(self):
        self.feed((0, 30, 60))
        payload, _ = self.feed(
            (90, 120, 150, 180),
            lon=10.001,
        )

        self.assertEqual(payload["count"], 0)

    def test_repeated_frames_use_upsert_and_keep_one_event(self):
        first, latest_ship = self.feed()
        _, repeated = self.call_view([latest_ship])
        _, continuing = self.call_view([self.ship(150)])

        self.assertFalse(repeated["results"][0]["is_new"])
        self.assertFalse(continuing["results"][0]["is_new"])
        self.assertEqual(
            first["results"][0]["event_id"],
            continuing["results"][0]["event_id"],
        )
        self.assertEqual(StayingBuffer.objects.count(), 6)

    def test_stale_ship_is_not_joined_to_old_history(self):
        self.feed((0, 30, 60))
        _, payload = self.call_view(
            [
                self.ship(90),
                self.ship(300, mmsi="987654321", lon=12),
            ]
        )

        self.assertEqual(payload["stale_count"], 1)
        self.assertFalse(
            StayingBuffer.objects.filter(mmsi="123456789").exists()
        )

    def test_future_replay_points_are_removed(self):
        StayingBuffer.objects.create(
            mmsi="123456789",
            name="测试船",
            longitude=10,
            latitude=10,
            speed=0.1,
            zone_name="测试禁停区",
            timestamp=self.base_time + timedelta(days=1),
        )

        self.call_view([self.ship(0)])

        self.assertFalse(
            StayingBuffer.objects.filter(
                timestamp=self.base_time + timedelta(days=1)
            ).exists()
        )
