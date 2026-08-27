import json

from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

from AISData.detection import DETECTORS, detection_cache_key

from .models import BlackList
from .views import detect_black_list


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "blacklist-tests",
    }
}


@override_settings(CACHES=TEST_CACHES)
class BlackListApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()

    def tearDown(self):
        cache.clear()

    def create_entry(self, mmsi="123456789"):
        response = self.client.post(
            reverse("blacklist_collection"),
            data=json.dumps(
                {
                    "mmsi": mmsi,
                    "ship_name": "测试船",
                    "ship_type": "货船",
                    "length": 120.5,
                    "width": 20.2,
                    "reason": "多次违规",
                    "is_active": True,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        return response.json()["result"]

    def test_create_list_retrieve_update_patch_and_delete(self):
        created = self.create_entry()

        list_response = self.client.get(
            reverse("blacklist_collection"),
            {"q": "测试船"},
        )
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.json()["count"], 1)

        detail_url = reverse("blacklist_detail", args=[created["id"]])
        detail_response = self.client.get(detail_url)
        self.assertEqual(
            detail_response.json()["result"]["mmsi"],
            "123456789",
        )

        update_response = self.client.put(
            detail_url,
            data=json.dumps(
                {
                    "mmsi": "123456789",
                    "ship_name": "修改后船名",
                    "ship_type": "油轮",
                    "length": None,
                    "width": 22,
                    "reason": "更新原因",
                    "is_active": True,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(
            update_response.json()["result"]["ship_name"],
            "修改后船名",
        )
        self.assertIsNone(update_response.json()["result"]["length"])

        patch_response = self.client.patch(
            detail_url,
            data=json.dumps({"is_active": False}),
            content_type="application/json",
        )
        self.assertEqual(patch_response.status_code, 200)
        self.assertFalse(patch_response.json()["result"]["is_active"])

        inactive_response = self.client.get(
            reverse("blacklist_collection"),
            {"active": "false"},
        )
        self.assertEqual(inactive_response.json()["count"], 1)

        delete_response = self.client.delete(detail_url)
        self.assertEqual(delete_response.status_code, 200)
        self.assertFalse(BlackList.objects.exists())

    def test_only_mmsi_is_required(self):
        response = self.client.post(
            reverse("blacklist_collection"),
            data=json.dumps({"mmsi": "987654321"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        result = response.json()["result"]
        self.assertEqual(result["ship_name"], "")
        self.assertIsNone(result["length"])

    def test_invalid_mmsi_and_dimensions_are_rejected(self):
        response = self.client.post(
            reverse("blacklist_collection"),
            data=json.dumps({"mmsi": "123", "length": -1}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("9 位数字", response.json()["message"])

        response = self.client.post(
            reverse("blacklist_collection"),
            data=json.dumps({"mmsi": "123456789", "length": -1}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("大于 0", response.json()["message"])

    def test_duplicate_mmsi_is_rejected(self):
        self.create_entry()

        response = self.client.post(
            reverse("blacklist_collection"),
            data=json.dumps({"mmsi": "123456789"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409)

    def test_mutation_requires_csrf_token(self):
        csrf_client = Client(enforce_csrf_checks=True)
        response = csrf_client.post(
            reverse("blacklist_collection"),
            data=json.dumps({"mmsi": "123456789"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)

    def test_mutation_invalidates_cached_detection_result(self):
        key = detection_cache_key("detect-blackList")
        cache.set(key, {"count": 99})

        self.create_entry()

        self.assertIsNone(cache.get(key))

    def test_home_page_exposes_blacklist_management_panel(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="blacklist-panel-container"')
        self.assertContains(response, 'id="blacklist-save"')
        self.assertContains(response, "function loadBlacklist()")
        self.assertContains(response, "function saveBlacklistEntry()")
        self.assertContains(response, "function deleteBlacklistEntry(entry)")


@override_settings(CACHES=TEST_CACHES)
class BlackListDetectionTests(TestCase):
    def setUp(self):
        self.request = RequestFactory().get("/internal/black-list/")
        self.entry = BlackList.objects.create(
            mmsi="123456789",
            shipName="名单船",
            shipType="货船",
            reason="重点关注",
            is_active=True,
        )

    def call_detector(self, ships):
        self.request.ais_ship_list = ships
        response = detect_black_list(self.request)
        return json.loads(response.content)

    def test_detector_is_registered(self):
        self.assertEqual(
            DETECTORS["detect-blackList"],
            "BlackList.views.detect_black_list",
        )

    def test_active_entry_generates_detailed_alert(self):
        payload = self.call_detector(
            [
                {
                    "timestamp": "2020-12-27T08:00:00+00:00",
                    "mmsi": 123456789,
                    "name": "",
                    "lon": 114.1,
                    "lat": 22.5,
                }
            ]
        )

        self.assertEqual(payload["count"], 1)
        result = payload["results"][0]
        self.assertEqual(result["mmsi"], "123456789")
        self.assertEqual(result["name"], "名单船")
        self.assertEqual(result["blacklist_id"], self.entry.id)
        self.assertEqual(
            result["timestamp"],
            "2020-12-27T08:00:00+00:00",
        )
        self.assertEqual(
            payload["timestamp"],
            "2020-12-27T08:00:00+00:00",
        )
        self.assertIn("重点关注", result["details"])

    def test_payload_uses_latest_snapshot_timestamp_not_first_ship(self):
        payload = self.call_detector(
            [
                {
                    "timestamp": "2020-12-27T08:00:00+00:00",
                    "mmsi": "987654321",
                    "lon": 114.0,
                    "lat": 22.4,
                },
                {
                    "timestamp": "2020-12-27T08:05:00+00:00",
                    "mmsi": "123456789",
                    "lon": 114.1,
                    "lat": 22.5,
                },
            ]
        )

        self.assertEqual(
            payload["timestamp"],
            "2020-12-27T08:05:00+00:00",
        )
        self.assertEqual(
            payload["results"][0]["timestamp"],
            "2020-12-27T08:05:00+00:00",
        )

    def test_inactive_entry_does_not_alert(self):
        self.entry.is_active = False
        self.entry.save()

        payload = self.call_detector(
            [
                {
                    "timestamp": "2020-12-27T08:00:00+00:00",
                    "mmsi": "123456789",
                    "lon": 114.1,
                    "lat": 22.5,
                }
            ]
        )

        self.assertEqual(payload["count"], 0)

    def test_empty_snapshot_is_safe(self):
        payload = self.call_detector([])

        self.assertEqual(payload["count"], 0)
        self.assertIsNone(payload["timestamp"])
