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

from .mmsi import classify_vessel_mmsi
from .models import IllegalBerthingPermit
from .views import (
    ILLEGAL_BERTHING_STATE_CACHE_KEY,
    detect_illegal_berthing,
)


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "illegal-berthing-tests",
    }
}

TEST_CONFIG = {
    "chinese_mids": ["412", "413", "414"],
    "base_contact_distance_metres": 100,
    "maximum_contact_distance_metres": 200,
    "length_distance_factor": 0.5,
    "contact_buffer_metres": 25,
    "maximum_speed_knots": 2,
    "maximum_relative_speed_knots": 0.5,
    "minimum_duration_seconds": 120,
    "minimum_observations": 3,
    "maximum_gap_seconds": 90,
    "max_position_age_seconds": 120,
    "max_valid_speed_knots": 102.2,
    "state_retention_minutes": 30,
}


@override_settings(
    CACHES=TEST_CACHES,
    ILLEGAL_BERTHING_DETECTION=TEST_CONFIG,
)
class IllegalBerthingDetectionTests(TestCase):
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
        latitude=22.5,
        speed=0.5,
        name="",
        length=100,
    ):
        return {
            "mmsi": mmsi,
            "name": name,
            "longitude": longitude,
            "latitude": latitude,
            "speed": speed,
            "length": length,
            "timestamp": timestamp,
        }

    def detect(self, ships):
        request = self.factory.get("/internal/illegal-berthing/")
        request.ais_ship_list = ships
        response = detect_illegal_berthing(request)
        return json.loads(response.content)

    def close_pair(self, timestamp, chinese_speed=0.5, foreign_speed=0.6):
        return [
            self.ship(
                "412123456",
                114.0000,
                timestamp,
                speed=chinese_speed,
                name="中国船",
            ),
            self.ship(
                "477123456",
                114.0005,
                timestamp,
                speed=foreign_speed,
                name="外籍船",
            ),
        ]

    def complete_detection(self):
        payload = None
        for timestamp in (
            "2026-07-24T00:00:00Z",
            "2026-07-24T00:01:00Z",
            "2026-07-24T00:02:00Z",
        ):
            payload = self.detect(self.close_pair(timestamp))
        return payload

    def test_mmsi_classification_only_accepts_ship_stations(self):
        self.assertEqual(
            classify_vessel_mmsi("412123456"), "chinese"
        )
        self.assertEqual(
            classify_vessel_mmsi("477123456"), "foreign"
        )
        self.assertIsNone(classify_vessel_mmsi("004121234"))
        self.assertIsNone(classify_vessel_mmsi("123"))

    def test_detector_is_registered_and_url_is_cache_only(self):
        self.assertEqual(
            DETECTORS["detect-illegalBerthing"],
            "IllegalBerthing.views.detect_illegal_berthing",
        )
        match = resolve(
            "/IllegalBerthing/detectIllegalBerthing/"
        )
        self.assertIs(match.func, cached_detection_result)
        self.assertEqual(
            match.kwargs["feature_id"],
            "detect-illegalBerthing",
        )

    def test_chinese_foreign_pair_must_remain_alongside(self):
        first = self.detect(
            self.close_pair("2026-07-24T00:00:00Z")
        )
        second = self.detect(
            self.close_pair("2026-07-24T00:01:00Z")
        )
        third = self.detect(
            self.close_pair("2026-07-24T00:02:00Z")
        )

        self.assertEqual(first["count"], 0)
        self.assertEqual(second["count"], 0)
        self.assertEqual(third["count"], 1)
        result = third["results"][0]
        self.assertEqual(result["chinese_mmsi"], "412123456")
        self.assertEqual(result["foreign_mmsi"], "477123456")
        self.assertEqual(result["duration_seconds"], 120)
        self.assertEqual(result["observations"], 3)
        self.assertFalse(result["authorized"])
        self.assertIn("未经有效许可", result["details"])

    def test_same_nationality_pairs_do_not_trigger(self):
        timestamp = "2026-07-24T00:00:00Z"
        chinese_pair = [
            self.ship("412123456", 114, timestamp),
            self.ship("413123456", 114.0005, timestamp),
        ]
        foreign_pair = [
            self.ship("477123456", 114, timestamp),
            self.ship("538123456", 114.0005, timestamp),
        ]

        self.assertEqual(self.detect(chinese_pair)["candidate_pair_count"], 0)
        self.assertEqual(self.detect(foreign_pair)["candidate_pair_count"], 0)

    def test_active_permit_suppresses_detection(self):
        IllegalBerthingPermit.objects.create(
            chinese_mmsi="412123456",
            foreign_mmsi="477123456",
            permit_number="P-001",
            is_active=True,
        )

        for timestamp in (
            "2026-07-24T00:00:00Z",
            "2026-07-24T00:01:00Z",
            "2026-07-24T00:02:00Z",
        ):
            payload = self.detect(self.close_pair(timestamp))

        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["authorized_pair_count"], 1)
        self.assertIsNone(
            cache.get(ILLEGAL_BERTHING_STATE_CACHE_KEY).get(
                "412123456:477123456"
            )
        )

    def test_expired_permit_does_not_suppress_detection(self):
        IllegalBerthingPermit.objects.create(
            chinese_mmsi="412123456",
            foreign_mmsi="477123456",
            valid_until=parse_datetime("2026-07-23T23:59:59Z"),
        )

        payload = self.complete_detection()

        self.assertEqual(payload["authorized_pair_count"], 0)
        self.assertEqual(payload["count"], 1)

    def test_fast_or_far_frame_breaks_continuity(self):
        self.detect(self.close_pair("2026-07-24T00:00:00Z"))
        self.detect(
            self.close_pair(
                "2026-07-24T00:01:00Z",
                chinese_speed=3,
                foreign_speed=3,
            )
        )
        payload = self.detect(
            self.close_pair("2026-07-24T00:02:00Z")
        )

        self.assertEqual(payload["count"], 0)
        state = cache.get(ILLEGAL_BERTHING_STATE_CACHE_KEY)
        self.assertEqual(
            state["412123456:477123456"]["observations"], 1
        )

    def test_stale_position_is_not_compared(self):
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
            ]
        )

        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["candidate_pair_count"], 0)
        self.assertEqual(payload["stale_count"], 1)

    def test_repeated_alert_is_not_new_and_recovery_creates_new_event(self):
        first = self.complete_detection()["results"][0]
        repeated = self.detect(
            self.close_pair("2026-07-24T00:03:00Z")
        )["results"][0]

        self.assertTrue(first["is_new"])
        self.assertFalse(repeated["is_new"])
        self.assertEqual(first["event_id"], repeated["event_id"])

        self.detect(
            self.close_pair(
                "2026-07-24T00:04:00Z",
                chinese_speed=4,
                foreign_speed=4,
            )
        )
        for timestamp in (
            "2026-07-24T00:05:00Z",
            "2026-07-24T00:06:00Z",
            "2026-07-24T00:07:00Z",
        ):
            payload = self.detect(self.close_pair(timestamp))
        recovered = payload["results"][0]

        self.assertTrue(recovered["is_new"])
        self.assertNotEqual(first["event_id"], recovered["event_id"])

    def test_invalid_and_empty_snapshots_are_safe(self):
        self.assertEqual(self.detect([])["count"], 0)
        payload = self.detect(
            [
                self.ship(
                    "not-mmsi",
                    114,
                    "2026-07-24T00:00:00Z",
                )
            ]
        )
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["skipped_count"], 1)


@override_settings(
    CACHES=TEST_CACHES,
    ILLEGAL_BERTHING_DETECTION=TEST_CONFIG,
)
class IllegalBerthingPermitApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()

    def tearDown(self):
        cache.clear()

    def create_permit(self):
        response = self.client.post(
            reverse("illegal_berthing_permit_collection"),
            data=json.dumps(
                {
                    "chinese_mmsi": "412123456",
                    "foreign_mmsi": "477123456",
                    "permit_number": "P-001",
                    "valid_from": "2026-07-24T00:00:00Z",
                    "valid_until": "2026-07-25T00:00:00Z",
                    "notes": "临时作业许可",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        return response.json()["result"]

    def test_permit_create_list_update_and_delete(self):
        created = self.create_permit()
        listed = self.client.get(
            reverse("illegal_berthing_permit_collection"),
            {"q": "P-001"},
        )
        self.assertEqual(listed.json()["count"], 1)

        detail_url = reverse(
            "illegal_berthing_permit_detail",
            args=[created["id"]],
        )
        patched = self.client.patch(
            detail_url,
            data=json.dumps({"is_active": False}),
            content_type="application/json",
        )
        self.assertEqual(patched.status_code, 200)
        self.assertFalse(patched.json()["result"]["is_active"])

        deleted = self.client.delete(detail_url)
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse(IllegalBerthingPermit.objects.exists())

    def test_permit_requires_correct_nationalities_and_period(self):
        invalid_nationality = self.client.post(
            reverse("illegal_berthing_permit_collection"),
            data=json.dumps(
                {
                    "chinese_mmsi": "477123456",
                    "foreign_mmsi": "412123456",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(invalid_nationality.status_code, 400)

        invalid_period = self.client.post(
            reverse("illegal_berthing_permit_collection"),
            data=json.dumps(
                {
                    "chinese_mmsi": "412123456",
                    "foreign_mmsi": "477123456",
                    "valid_from": "2026-07-25T00:00:00Z",
                    "valid_until": "2026-07-24T00:00:00Z",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(invalid_period.status_code, 400)

    def test_permit_mutation_invalidates_detection_state(self):
        cache.set(
            detection_cache_key("detect-illegalBerthing"),
            {"count": 99},
        )
        cache.set(ILLEGAL_BERTHING_STATE_CACHE_KEY, {"pair": {}})

        self.create_permit()

        self.assertIsNone(
            cache.get(
                detection_cache_key("detect-illegalBerthing")
            )
        )
        self.assertIsNone(
            cache.get(ILLEGAL_BERTHING_STATE_CACHE_KEY)
        )

    def test_home_page_exposes_berthing_permit_crud_panel(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'id="berthing-panel-container"',
        )
        self.assertContains(response, 'id="berthing-save"')
        self.assertContains(response, "function loadBerthingPermits()")
        self.assertContains(response, "function saveBerthingPermit()")
        self.assertContains(response, "function editBerthingPermit(")
        self.assertContains(response, "function toggleBerthingPermit(")
        self.assertContains(response, "function deleteBerthingPermit(")
