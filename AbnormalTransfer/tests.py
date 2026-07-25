import json

from django.core.cache import cache
from django.test import (
    Client,
    RequestFactory,
    TestCase,
    override_settings,
)
from django.urls import resolve, reverse
from django.utils.dateparse import parse_datetime

from AISData.detection import DETECTORS, detection_cache_key
from AISData.views import cached_detection_result

from .models import TransferOperationPlan
from .views import (
    ABNORMAL_TRANSFER_STATE_CACHE_KEY,
    detect_abnormal_transfer,
)


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "abnormal-transfer-tests",
    }
}

TEST_CONFIG = {
    "base_contact_distance_metres": 100,
    "maximum_contact_distance_metres": 200,
    "length_distance_factor": 0.5,
    "contact_buffer_metres": 25,
    "maximum_candidate_speed_knots": 20,
    "maximum_relative_speed_knots": 1,
    "maximum_course_difference_degrees": 30,
    "course_check_minimum_speed_knots": 1,
    "minimum_duration_seconds": 120,
    "minimum_observations": 3,
    "maximum_gap_seconds": 90,
    "max_position_age_seconds": 120,
    "max_valid_speed_knots": 102.2,
    "speed_tolerance_knots": 0.1,
    "state_retention_minutes": 30,
}


@override_settings(
    CACHES=TEST_CACHES,
    ABNORMAL_TRANSFER_DETECTION=TEST_CONFIG,
)
class AbnormalTransferDetectionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()

    def tearDown(self):
        cache.clear()

    @staticmethod
    def ship(
        mmsi,
        longitude,
        timestamp,
        speed=1,
        course=90,
        latitude=22.5,
        name="",
        length=100,
    ):
        return {
            "mmsi": mmsi,
            "name": name,
            "longitude": longitude,
            "latitude": latitude,
            "speed": speed,
            "course": course,
            "length": length,
            "timestamp": timestamp,
        }

    def pair(
        self,
        timestamp,
        first_speed=1,
        second_speed=1,
        separation=0.0005,
        second_course=90,
    ):
        return [
            self.ship(
                "412123456",
                114,
                timestamp,
                speed=first_speed,
                name="甲船",
            ),
            self.ship(
                "477123456",
                114 + separation,
                timestamp,
                speed=second_speed,
                course=second_course,
                name="乙船",
            ),
        ]

    def detect(self, ships):
        request = self.factory.get("/internal/abnormal-transfer/")
        request.ais_ship_list = ships
        response = detect_abnormal_transfer(request)
        return json.loads(response.content)

    def create_plan(
        self,
        starts_at="2026-07-24T00:00:00Z",
        ends_at="2026-07-24T01:00:00Z",
        max_speed=2,
        active=True,
    ):
        return TransferOperationPlan.objects.create(
            vessel_a_mmsi="412123456",
            vessel_b_mmsi="477123456",
            operation_name="测试接驳",
            approval_number="航通-001",
            starts_at=parse_datetime(starts_at),
            ends_at=parse_datetime(ends_at),
            max_speed_knots=max_speed,
            is_active=active,
        )

    def run_three_frames(self, speed=1):
        payload = None
        for timestamp in (
            "2026-07-24T00:00:00Z",
            "2026-07-24T00:01:00Z",
            "2026-07-24T00:02:00Z",
        ):
            payload = self.detect(
                self.pair(
                    timestamp,
                    first_speed=speed,
                    second_speed=speed,
                )
            )
        return payload

    def test_detector_is_registered_and_url_is_cache_only(self):
        self.assertEqual(
            DETECTORS["detect-abnormalTransfer"],
            "AbnormalTransfer.views.detect_abnormal_transfer",
        )
        match = resolve(
            "/AbnormalTransfer/detectAbnormalTransfer/"
        )
        self.assertIs(match.func, cached_detection_result)
        self.assertEqual(
            match.kwargs["feature_id"],
            "detect-abnormalTransfer",
        )

    def test_single_close_frame_is_not_enough(self):
        payload = self.detect(
            self.pair("2026-07-24T00:00:00Z")
        )

        self.assertEqual(payload["candidate_pair_count"], 1)
        self.assertEqual(payload["confirmed_pair_count"], 0)
        self.assertEqual(payload["count"], 0)

    def test_approved_time_and_speed_do_not_alert(self):
        self.create_plan()

        payload = self.run_three_frames(speed=1.5)

        self.assertEqual(payload["confirmed_pair_count"], 1)
        self.assertEqual(payload["count"], 0)

    def test_outside_approved_time_alerts(self):
        self.create_plan(
            starts_at="2026-07-23T00:00:00Z",
            ends_at="2026-07-23T01:00:00Z",
        )

        payload = self.run_three_frames()

        self.assertEqual(payload["count"], 1)
        result = payload["results"][0]
        self.assertEqual(
            result["violation_codes"],
            ["outside_operation_time"],
        )
        self.assertEqual(result["approval_number"], "航通-001")
        self.assertIn("不在批准的作业时间内", result["details"])

    def test_speed_above_plan_limit_alerts(self):
        self.create_plan(max_speed=2)

        payload = self.run_three_frames(speed=3)

        self.assertEqual(payload["count"], 1)
        result = payload["results"][0]
        self.assertEqual(result["violation_codes"], ["speed_exceeded"])
        self.assertEqual(result["maximum_pair_speed_knots"], 3)
        self.assertEqual(result["max_allowed_speed_knots"], 2)

    def test_time_and_speed_can_both_be_violated(self):
        self.create_plan(
            starts_at="2026-07-23T00:00:00Z",
            ends_at="2026-07-23T01:00:00Z",
            max_speed=2,
        )

        result = self.run_three_frames(speed=3)["results"][0]

        self.assertEqual(
            result["violation_codes"],
            ["outside_operation_time", "speed_exceeded"],
        )

    def test_unregistered_sustained_transfer_alerts(self):
        payload = self.run_three_frames()

        self.assertEqual(payload["count"], 1)
        result = payload["results"][0]
        self.assertEqual(
            result["violation_codes"],
            ["unregistered_operation"],
        )
        self.assertIsNone(result["operation_plan_id"])

    def test_inactive_plan_is_treated_as_unregistered(self):
        self.create_plan(active=False)

        result = self.run_three_frames()["results"][0]

        self.assertEqual(
            result["violation_codes"],
            ["unregistered_operation"],
        )

    def test_non_co_moving_or_distant_ships_are_not_candidates(self):
        different_course = self.detect(
            self.pair(
                "2026-07-24T00:00:00Z",
                first_speed=3,
                second_speed=3,
                second_course=180,
            )
        )
        distant = self.detect(
            self.pair(
                "2026-07-24T00:01:00Z",
                separation=0.01,
            )
        )

        self.assertEqual(different_course["candidate_pair_count"], 0)
        self.assertEqual(distant["candidate_pair_count"], 0)

    def test_breaking_contact_resets_duration(self):
        self.detect(self.pair("2026-07-24T00:00:00Z"))
        self.detect(
            self.pair(
                "2026-07-24T00:01:00Z",
                separation=0.01,
            )
        )
        payload = self.detect(
            self.pair("2026-07-24T00:02:00Z")
        )

        self.assertEqual(payload["count"], 0)
        state = cache.get(ABNORMAL_TRANSFER_STATE_CACHE_KEY)
        self.assertEqual(
            state["412123456:477123456"]["observations"], 1
        )

    def test_stale_and_invalid_positions_are_excluded(self):
        payload = self.detect(
            [
                self.ship(
                    "412123456",
                    114,
                    "2026-07-24T00:00:00Z",
                ),
                self.ship(
                    "477123456",
                    114.0005,
                    "2026-07-24T00:03:00Z",
                ),
                self.ship(
                    "invalid",
                    114.0004,
                    "2026-07-24T00:03:00Z",
                ),
            ]
        )

        self.assertEqual(payload["candidate_pair_count"], 0)
        self.assertEqual(payload["stale_count"], 1)
        self.assertEqual(payload["skipped_count"], 1)

    def test_repeated_violation_is_not_new_and_recovery_is_new(self):
        first = self.run_three_frames()["results"][0]
        repeated = self.detect(
            self.pair("2026-07-24T00:03:00Z")
        )["results"][0]

        self.assertTrue(first["is_new"])
        self.assertFalse(repeated["is_new"])
        self.assertEqual(first["event_id"], repeated["event_id"])

        self.create_plan(
            starts_at="2026-07-24T00:04:00Z",
            ends_at="2026-07-24T00:05:00Z",
        )
        normal = self.detect(
            self.pair("2026-07-24T00:04:00Z")
        )
        self.assertEqual(normal["count"], 0)
        still_normal = self.detect(
            self.pair("2026-07-24T00:05:00Z")
        )
        self.assertEqual(still_normal["count"], 0)

        later = self.detect(
            self.pair("2026-07-24T00:06:00Z")
        )["results"][0]
        self.assertTrue(later["is_new"])
        self.assertNotEqual(first["event_id"], later["event_id"])


@override_settings(
    CACHES=TEST_CACHES,
    ABNORMAL_TRANSFER_DETECTION=TEST_CONFIG,
)
class TransferOperationApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()

    def tearDown(self):
        cache.clear()

    def create_plan(self):
        response = self.client.post(
            reverse("transfer_operation_collection"),
            data=json.dumps(
                {
                    "vessel_a_mmsi": "477123456",
                    "vessel_b_mmsi": "412123456",
                    "operation_name": "原油过驳",
                    "approval_number": "渝航通-001",
                    "starts_at": "2026-07-24T00:00:00+08:00",
                    "ends_at": "2026-07-24T12:00:00+08:00",
                    "max_speed_knots": 2,
                    "notes": "经批准作业",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        return response.json()["result"]

    def test_create_list_patch_and_delete_plan(self):
        created = self.create_plan()
        self.assertEqual(created["vessel_a_mmsi"], "412123456")
        self.assertEqual(created["vessel_b_mmsi"], "477123456")

        listed = self.client.get(
            reverse("transfer_operation_collection"),
            {"q": "渝航通"},
        )
        self.assertEqual(listed.json()["count"], 1)

        detail_url = reverse(
            "transfer_operation_detail",
            args=[created["id"]],
        )
        patched = self.client.patch(
            detail_url,
            data=json.dumps(
                {"max_speed_knots": 1.5, "is_active": False}
            ),
            content_type="application/json",
        )
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(
            patched.json()["result"]["max_speed_knots"], 1.5
        )
        self.assertFalse(patched.json()["result"]["is_active"])

        deleted = self.client.delete(detail_url)
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse(TransferOperationPlan.objects.exists())

    def test_invalid_pair_time_and_speed_are_rejected(self):
        endpoint = reverse("transfer_operation_collection")
        base = {
            "vessel_a_mmsi": "412123456",
            "vessel_b_mmsi": "477123456",
            "starts_at": "2026-07-24T00:00:00Z",
            "ends_at": "2026-07-24T01:00:00Z",
            "max_speed_knots": 2,
        }
        for override in (
            {"vessel_b_mmsi": "412123456"},
            {"ends_at": "2026-07-23T01:00:00Z"},
            {"max_speed_knots": -1},
            {"vessel_a_mmsi": "123"},
        ):
            payload = {**base, **override}
            response = self.client.post(
                endpoint,
                data=json.dumps(payload),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 400)

    def test_mutation_invalidates_detection_cache_and_state(self):
        cache.set(
            detection_cache_key("detect-abnormalTransfer"),
            {"count": 99},
        )
        cache.set(ABNORMAL_TRANSFER_STATE_CACHE_KEY, {"pair": {}})

        self.create_plan()

        self.assertIsNone(
            cache.get(
                detection_cache_key("detect-abnormalTransfer")
            )
        )
        self.assertIsNone(
            cache.get(ABNORMAL_TRANSFER_STATE_CACHE_KEY)
        )

    def test_home_page_exposes_transfer_plan_crud_panel(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'id="transfer-panel-container"',
        )
        self.assertContains(response, 'id="transfer-save"')
        self.assertContains(response, "function loadTransferPlans()")
        self.assertContains(response, "function saveTransferPlan()")
        self.assertContains(response, "function editTransferPlan(")
        self.assertContains(response, "function toggleTransferPlan(")
        self.assertContains(response, "function deleteTransferPlan(")
