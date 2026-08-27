import json
from datetime import datetime, timedelta, timezone

from django.core.cache import cache
from django.test import (
    Client,
    RequestFactory,
    TestCase,
    override_settings,
)
from django.urls import reverse

from .models import ElectronicFence
from .views import (
    CROSSING_EVENT_CACHE_KEY,
    CROSSING_STATE_CACHE_KEY,
    classify_point,
    detect_crossing_boundary,
    point_in_polygon,
    segment_crosses_polygon,
)


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "crossing-boundary-tests",
    }
}

TEST_CONFIG = {
    "max_position_age_seconds": 60,
    "boundary_tolerance_metres": 10,
    "state_retention_hours": 1,
    "event_retention_minutes": 30,
    "max_vertices": 20,
}


class ElectronicFenceApiTests(TestCase):
    def setUp(self):
        ElectronicFence.objects.all().delete()
        self.client = Client()
        self.vertices = [
            [113.68, 22.39],
            [113.70, 22.39],
            [113.70, 22.41],
            [113.68, 22.41],
        ]

    def create_fence(self, name="测试围栏", crossing_mode="enter"):
        response = self.client.post(
            reverse("fence_collection"),
            data=json.dumps(
                {
                    "name": name,
                    "description": "测试",
                    "vertices": self.vertices,
                    "crossing_mode": crossing_mode,
                    "is_active": True,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        return response.json()["result"]

    def test_create_list_retrieve_update_and_delete(self):
        created = self.create_fence(crossing_mode="exit")
        self.assertEqual(created["crossing_mode"], "exit")
        self.assertEqual(created["crossing_mode_label"], "禁止驶出")

        list_response = self.client.get(reverse("fence_collection"))
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.json()["count"], 1)

        detail_url = reverse("fence_detail", args=[created["id"]])
        detail_response = self.client.get(detail_url)
        self.assertEqual(
            detail_response.json()["result"]["name"], "测试围栏"
        )

        update_response = self.client.put(
            detail_url,
            data=json.dumps(
                {
                    "name": "修改后的围栏",
                    "description": "",
                    "vertices": self.vertices,
                    "crossing_mode": "both",
                    "is_active": False,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertFalse(update_response.json()["result"]["is_active"])
        self.assertEqual(
            update_response.json()["result"]["crossing_mode"], "both"
        )

        delete_response = self.client.delete(detail_url)
        self.assertEqual(delete_response.status_code, 200)
        self.assertFalse(ElectronicFence.objects.exists())

    def test_patch_can_toggle_active_state(self):
        created = self.create_fence()
        response = self.client.patch(
            reverse("fence_detail", args=[created["id"]]),
            data=json.dumps({"is_active": False}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["result"]["is_active"])

    def test_invalid_vertices_are_rejected(self):
        invalid_shapes = (
            [[113.68, 22.39], [113.70, 22.39]],
            [
                [113.68, 22.39],
                [113.69, 22.40],
                [113.70, 22.41],
            ],
            [
                [113.68, 22.39],
                [113.70, 22.41],
                [113.68, 22.41],
                [113.70, 22.39],
            ],
        )
        for index, vertices in enumerate(invalid_shapes):
            with self.subTest(vertices=vertices):
                response = self.client.post(
                    reverse("fence_collection"),
                    data=json.dumps(
                        {
                            "name": f"无效围栏{index}",
                            "vertices": vertices,
                        }
                    ),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 400)

    def test_invalid_crossing_mode_is_rejected(self):
        response = self.client.post(
            reverse("fence_collection"),
            data=json.dumps(
                {
                    "name": "无效规则",
                    "vertices": self.vertices,
                    "crossing_mode": "unknown",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("crossing_mode", response.json()["message"])

    def test_duplicate_name_is_rejected(self):
        self.create_fence()
        response = self.client.post(
            reverse("fence_collection"),
            data=json.dumps(
                {
                    "name": "测试围栏",
                    "vertices": self.vertices,
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409)

    def test_mutation_requires_csrf_token(self):
        csrf_client = Client(enforce_csrf_checks=True)
        response = csrf_client.post(
            reverse("fence_collection"),
            data=json.dumps(
                {
                    "name": "CSRF 测试",
                    "vertices": self.vertices,
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)

    def test_home_page_exposes_fence_panel_and_mode_control(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("csrftoken", response.cookies)
        self.assertContains(response, 'id="fence-panel-container"')
        self.assertContains(response, 'id="fence-crossing-mode"')
        self.assertContains(response, 'value="enter"')
        self.assertContains(response, 'value="exit"')
        self.assertContains(response, 'value="both"')
        self.assertContains(response, "function viewFence(fenceId)")


@override_settings(
    CACHES=TEST_CACHES,
    CROSSING_BOUNDARY_DETECTION=TEST_CONFIG,
)
class CrossingBoundaryDetectionTests(TestCase):
    base_time = datetime(2026, 7, 23, 8, tzinfo=timezone.utc)

    def setUp(self):
        cache.delete(CROSSING_STATE_CACHE_KEY)
        cache.delete(CROSSING_EVENT_CACHE_KEY)
        ElectronicFence.objects.all().delete()
        self.factory = RequestFactory()
        self.fence = ElectronicFence.objects.create(
            name="检测围栏",
            vertices=[
                [113.68, 22.39],
                [113.70, 22.39],
                [113.70, 22.41],
                [113.68, 22.41],
            ],
            crossing_mode="enter",
        )

    def ship(self, lon, lat, seconds=0, mmsi="123456789"):
        return {
            "timestamp": (
                self.base_time + timedelta(seconds=seconds)
            ).isoformat(),
            "mmsi": mmsi,
            "name": "测试船",
            "lon": lon,
            "lat": lat,
        }

    def call_detector(self, ships):
        request = self.factory.get("/internal/crossing-boundary/")
        request.ais_ship_list = ships
        response = detect_crossing_boundary(request)
        return response, json.loads(response.content)

    def test_geometry_includes_exact_edge_and_detects_transit(self):
        self.assertTrue(
            point_in_polygon(113.68, 22.40, self.fence.vertices)
        )
        self.assertFalse(
            point_in_polygon(113.75, 22.40, self.fence.vertices)
        )
        self.assertEqual(
            classify_point(
                113.68,
                22.40,
                self.fence.vertices,
                boundary_tolerance_metres=1,
            ),
            "boundary",
        )
        self.assertTrue(
            segment_crosses_polygon(
                [113.67, 22.40],
                [113.71, 22.40],
                self.fence.vertices,
            )
        )

    def test_initial_position_only_initialises_state(self):
        _, payload = self.call_detector(
            [self.ship(113.69, 22.40)]
        )

        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["initialised_state_count"], 1)

    def test_outside_to_inside_generates_one_enter_event(self):
        self.call_detector([self.ship(113.67, 22.40)])
        _, entered = self.call_detector(
            [self.ship(113.69, 22.40, seconds=30)]
        )
        _, duplicate = self.call_detector(
            [self.ship(113.69, 22.40, seconds=30)]
        )
        _, remains_inside = self.call_detector(
            [self.ship(113.69, 22.40, seconds=60)]
        )

        self.assertEqual(entered["count"], 1)
        self.assertEqual(entered["new_count"], 1)
        result = entered["results"][0]
        self.assertEqual(result["crossing_direction"], "enter")
        self.assertEqual(result["fence_id"], self.fence.id)
        self.assertEqual(result["previous_location"], [113.67, 22.40])
        self.assertTrue(result["is_new"])
        self.assertIn("event_id", result)
        self.assertEqual(duplicate["count"], 1)
        self.assertEqual(duplicate["new_count"], 0)
        self.assertFalse(duplicate["results"][0]["is_new"])
        self.assertEqual(remains_inside["count"], 1)
        self.assertEqual(remains_inside["new_count"], 0)

    def test_enter_only_fence_does_not_alert_on_exit(self):
        self.call_detector([self.ship(113.69, 22.40)])
        _, payload = self.call_detector(
            [self.ship(113.71, 22.40, seconds=30)]
        )

        self.assertEqual(payload["count"], 0)

    def test_rule_changed_to_enter_while_inside_does_not_emit_exit(self):
        self.fence.crossing_mode = "both"
        self.fence.save()
        self.call_detector([self.ship(113.67, 22.40)])
        _, entered = self.call_detector(
            [self.ship(113.69, 22.40, seconds=30)]
        )
        self.assertEqual(entered["new_count"], 1)

        self.fence.crossing_mode = "enter"
        self.fence.save()
        _, exited = self.call_detector(
            [self.ship(113.71, 22.40, seconds=60)]
        )

        self.assertEqual(exited["new_count"], 0)
        self.assertTrue(
            all(
                result["crossing_direction"] != "exit"
                for result in exited["results"]
            )
        )

    def test_incompatible_retained_event_is_removed_after_rule_change(self):
        self.fence.crossing_mode = "both"
        self.fence.save()
        self.call_detector([self.ship(113.69, 22.40)])
        _, exited = self.call_detector(
            [self.ship(113.71, 22.40, seconds=30)]
        )
        self.assertEqual(exited["results"][0]["crossing_direction"], "exit")

        self.fence.crossing_mode = "enter"
        self.fence.save()
        _, after_change = self.call_detector(
            [self.ship(113.72, 22.40, seconds=60)]
        )

        self.assertEqual(after_change["count"], 0)
        self.assertEqual(after_change["new_count"], 0)

    def test_exit_mode_alerts_only_when_ship_leaves(self):
        self.fence.crossing_mode = "exit"
        self.fence.save()
        self.call_detector([self.ship(113.69, 22.40)])
        _, payload = self.call_detector(
            [self.ship(113.71, 22.40, seconds=30)]
        )

        self.assertEqual(payload["count"], 1)
        self.assertEqual(
            payload["results"][0]["crossing_direction"], "exit"
        )

    def test_two_outside_points_crossing_fence_generate_transit(self):
        self.call_detector([self.ship(113.67, 22.40)])
        _, payload = self.call_detector(
            [self.ship(113.71, 22.40, seconds=30)]
        )

        self.assertEqual(payload["count"], 1)
        self.assertEqual(
            payload["results"][0]["crossing_direction"], "transit"
        )

    def test_boundary_point_does_not_create_jitter_event(self):
        self.call_detector([self.ship(113.6798, 22.40)])
        _, boundary = self.call_detector(
            [self.ship(113.68, 22.40, seconds=30)]
        )
        _, entered = self.call_detector(
            [self.ship(113.6802, 22.40, seconds=60)]
        )

        self.assertEqual(boundary["count"], 0)
        self.assertEqual(entered["count"], 1)

    def test_non_geometry_fence_edit_does_not_hide_next_crossing(self):
        self.call_detector([self.ship(113.67, 22.40)])
        self.fence.description = "已修改"
        self.fence.save()
        _, payload = self.call_detector(
            [self.ship(113.69, 22.40, seconds=30)]
        )

        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["new_count"], 1)
        self.assertEqual(payload["initialised_state_count"], 0)

    def test_geometry_edit_resets_state_without_false_alert(self):
        self.call_detector([self.ship(113.67, 22.40)])
        self.fence.vertices = [
            [113.675, 22.39],
            [113.70, 22.39],
            [113.70, 22.41],
            [113.675, 22.41],
        ]
        self.fence.save()

        _, payload = self.call_detector(
            [self.ship(113.69, 22.40, seconds=30)]
        )

        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["initialised_state_count"], 1)

    def test_multiple_points_for_one_ship_are_processed_in_time_order(self):
        _, payload = self.call_detector(
            [
                self.ship(113.71, 22.40, seconds=60),
                self.ship(113.67, 22.40),
                self.ship(113.69, 22.40, seconds=30),
            ]
        )

        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["new_count"], 1)
        self.assertEqual(
            payload["results"][0]["crossing_direction"],
            "enter",
        )

    def test_boundary_points_do_not_erase_last_stable_location(self):
        self.call_detector([self.ship(113.67, 22.40)])
        self.call_detector(
            [self.ship(113.68, 22.40, seconds=30)]
        )
        self.call_detector(
            [self.ship(113.70, 22.40, seconds=60)]
        )
        _, payload = self.call_detector(
            [self.ship(113.71, 22.40, seconds=90)]
        )

        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["new_count"], 1)
        self.assertEqual(
            payload["results"][0]["crossing_direction"],
            "transit",
        )

    def test_first_boundary_fix_then_inside_is_an_entry(self):
        self.call_detector([self.ship(113.68, 22.40)])
        _, payload = self.call_detector(
            [self.ship(113.69, 22.40, seconds=30)]
        )

        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["new_count"], 1)
        self.assertEqual(
            payload["results"][0]["crossing_direction"],
            "enter",
        )

    def test_inactive_fence_is_not_used_for_detection(self):
        self.fence.is_active = False
        self.fence.save()
        _, payload = self.call_detector(
            [self.ship(113.69, 22.40)]
        )

        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["initialised_state_count"], 0)

    def test_empty_invalid_and_stale_snapshots_are_safe(self):
        _, empty = self.call_detector([])
        _, invalid = self.call_detector(
            [{"mmsi": None, "lon": "bad", "lat": 22.4}]
        )
        _, stale = self.call_detector(
            [
                self.ship(113.67, 22.40),
                self.ship(
                    113.71,
                    22.40,
                    seconds=300,
                    mmsi="987654321",
                ),
            ]
        )

        self.assertEqual(empty["count"], 0)
        self.assertIsNone(empty["timestamp"])
        self.assertEqual(invalid["skipped_count"], 1)
        self.assertEqual(stale["stale_count"], 1)
