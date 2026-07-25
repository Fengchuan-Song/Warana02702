import json
from datetime import datetime, timedelta, timezone

from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import resolve, reverse

from AISData.views import cached_detection_result

from .models import SmugglingVoyagePermit, SmugglingZone
from .views import (
    SMUGGLING_EVENT_CACHE_KEY,
    SMUGGLING_STATE_CACHE_KEY,
    detect_smuggling,
)


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "smuggling-tests",
    }
}

TEST_CONFIG = {
    "timezone": "Asia/Shanghai",
    "night_start_hour": 20,
    "night_end_hour": 6,
    "max_position_age_seconds": 120,
    "maximum_gap_seconds": 45,
    "origin_minimum_duration_seconds": 30,
    "origin_minimum_observations": 2,
    "maximum_voyage_hours": 6,
    "state_retention_hours": 12,
    "landing_speed_knots": 1.5,
    "landing_minimum_duration_seconds": 60,
    "landing_minimum_observations": 2,
    "draught_change_metres": 0.5,
    "small_craft_length_metres": 50,
    "fast_craft_speed_knots": 15,
    "event_retention_minutes": 60,
    "minimum_risk_score": 40,
}


@override_settings(CACHES=TEST_CACHES, SMUGGLING_DETECTION=TEST_CONFIG)
class SmugglingDetectionTests(TestCase):
    base_time = datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc)

    def setUp(self):
        cache.delete(SMUGGLING_STATE_CACHE_KEY)
        cache.delete(SMUGGLING_EVENT_CACHE_KEY)
        SmugglingVoyagePermit.objects.all().delete()
        SmugglingZone.objects.all().delete()
        self.factory = RequestFactory()
        self.origin = SmugglingZone.objects.create(
            name="香港测试起航区",
            zone_type=SmugglingZone.HONG_KONG_ORIGIN,
            vertices=[
                [114.00, 22.20],
                [114.10, 22.20],
                [114.10, 22.30],
                [114.00, 22.30],
            ],
        )
        self.destination = SmugglingZone.objects.create(
            name="广东非设关测试点",
            zone_type=SmugglingZone.NON_CUSTOMS_LANDING,
            vertices=[
                [114.20, 22.20],
                [114.21, 22.20],
                [114.21, 22.21],
                [114.20, 22.21],
            ],
            buffer_metres=3000,
        )

    def ship(
        self,
        longitude,
        latitude,
        seconds=0,
        speed=8,
        draught=2,
        mmsi="477123456",
        **extra,
    ):
        result = {
            "timestamp": (
                self.base_time + timedelta(seconds=seconds)
            ).isoformat(),
            "mmsi": mmsi,
            "name": "测试目标",
            "lon": longitude,
            "lat": latitude,
            "speed": speed,
            "course": 90,
            "heading": 90,
            "flag": "Hong Kong",
            "iso3": "HKG",
            "ship_type": "cargo",
            "draught": draught,
            "length": 30,
            "width": 8,
            "imo": "9876543",
            "nav_status": 0,
            "at_dock": False,
        }
        result.update(extra)
        return result

    def call(self, ships):
        request = self.factory.get("/internal/detection/detect-smuggling/")
        request.ais_ship_list = ships
        response = detect_smuggling(request)
        return json.loads(response.content)

    def establish_departure(self):
        self.call([self.ship(114.05, 22.25)])
        self.call([self.ship(114.06, 22.25, seconds=30)])
        return self.call([self.ship(114.12, 22.25, seconds=60)])

    def test_night_hong_kong_departure_and_approach_is_risk(self):
        self.establish_departure()
        payload = self.call(
            [self.ship(114.19, 22.205, seconds=90)]
        )

        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["new_count"], 1)
        event = payload["results"][0]
        self.assertEqual(event["event"], "Smuggling")
        self.assertEqual(event["risk"], "中风险")
        self.assertTrue(event["night_departure"])
        self.assertFalse(event["inside_destination_zone"])
        self.assertEqual(
            event["destination_zone_id"], self.destination.id
        )
        self.assertEqual(event["ship_type"], "cargo")
        self.assertEqual(event["draught"], 2.0)
        self.assertEqual(event["length"], 30.0)
        self.assertIn("风险研判", event["details"])

    def test_confirmed_landing_escalates_risk(self):
        self.establish_departure()
        self.call([self.ship(114.19, 22.205, seconds=90)])
        first_inside = self.call(
            [
                self.ship(
                    114.205,
                    22.205,
                    seconds=120,
                    speed=1,
                    nav_status=5,
                )
            ]
        )
        confirmed = self.call(
            [
                self.ship(
                    114.205,
                    22.205,
                    seconds=180,
                    speed=0.5,
                    draught=2.7,
                    nav_status=5,
                    at_dock=True,
                )
            ]
        )

        self.assertEqual(first_inside["results"][0]["risk"], "高风险")
        self.assertEqual(confirmed["count"], 1)
        event = confirmed["results"][0]
        self.assertEqual(event["risk"], "极高风险")
        self.assertTrue(event["is_new"])
        self.assertGreaterEqual(event["draught_change_metres"], 0.5)
        self.assertIn("航次吃水变化明显", event["risk_reasons"])
        self.assertIn("AIS停靠标志", event["risk_reasons"])

    def test_valid_permit_suppresses_destination_warning(self):
        SmugglingVoyagePermit.objects.create(
            mmsi="477123456",
            permit_number="TEST-001",
            origin_zone=self.origin,
            destination_zone=self.destination,
            valid_from=self.base_time - timedelta(hours=1),
            valid_until=self.base_time + timedelta(hours=2),
        )
        self.establish_departure()
        payload = self.call(
            [self.ship(114.205, 22.205, seconds=90, speed=1)]
        )

        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["authorized_count"], 1)

    def test_legal_customs_port_takes_priority(self):
        SmugglingZone.objects.create(
            name="合法口岸",
            zone_type=SmugglingZone.CUSTOMS_PORT,
            vertices=self.destination.vertices,
            buffer_metres=3000,
        )
        self.establish_departure()
        payload = self.call(
            [self.ship(114.205, 22.205, seconds=90, speed=1)]
        )

        self.assertEqual(payload["count"], 0)

    def test_single_origin_fix_does_not_establish_departure(self):
        self.call([self.ship(114.05, 22.25)])
        self.call([self.ship(114.12, 22.25, seconds=30)])
        payload = self.call(
            [self.ship(114.205, 22.205, seconds=60, speed=1)]
        )

        self.assertEqual(payload["count"], 0)

    def test_identity_mismatch_and_small_fast_craft_raise_score(self):
        self.call(
            [
                self.ship(
                    114.05,
                    22.25,
                    speed=18,
                    flag="China",
                    iso3="CHN",
                )
            ]
        )
        self.call(
            [
                self.ship(
                    114.06,
                    22.25,
                    seconds=30,
                    speed=18,
                    flag="China",
                    iso3="CHN",
                )
            ]
        )
        self.call(
            [
                self.ship(
                    114.12,
                    22.25,
                    seconds=60,
                    speed=18,
                    flag="China",
                    iso3="CHN",
                )
            ]
        )
        payload = self.call(
            [
                self.ship(
                    114.19,
                    22.205,
                    seconds=90,
                    speed=18,
                    flag="China",
                    iso3="CHN",
                )
            ]
        )

        event = payload["results"][0]
        self.assertIn(
            "MMSI与船籍字段不一致", event["risk_reasons"]
        )
        self.assertIn("小型高速船特征", event["risk_reasons"])
        self.assertEqual(event["risk"], "高风险")

    def test_missing_required_zone_configuration_is_reported(self):
        self.destination.delete()
        payload = self.call([self.ship(114.05, 22.25)])

        self.assertEqual(payload["count"], 0)
        self.assertTrue(payload["success"])
        self.assertFalse(payload["configured"])
        self.assertIn("请先配置", payload["message"])


@override_settings(CACHES=TEST_CACHES)
class SmugglingManagementApiTests(TestCase):
    def setUp(self):
        SmugglingVoyagePermit.objects.all().delete()
        SmugglingZone.objects.all().delete()
        self.client = Client()
        self.vertices = [
            [114.00, 22.20],
            [114.10, 22.20],
            [114.10, 22.30],
            [114.00, 22.30],
        ]

    def create_zone(self, name, zone_type):
        response = self.client.post(
            reverse("smuggling_zone_collection"),
            data=json.dumps(
                {
                    "name": name,
                    "zone_type": zone_type,
                    "vertices": self.vertices,
                    "buffer_metres": 2000,
                    "legal_reference": "测试依据",
                    "is_active": True,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        return response.json()["result"]

    def test_zone_crud(self):
        created = self.create_zone(
            "香港起航区", SmugglingZone.HONG_KONG_ORIGIN
        )
        detail_url = reverse(
            "smuggling_zone_detail", args=[created["id"]]
        )
        patched = self.client.patch(
            detail_url,
            data=json.dumps({"buffer_metres": 3000}),
            content_type="application/json",
        )
        listed = self.client.get(reverse("smuggling_zone_collection"))
        deleted = self.client.delete(detail_url)

        self.assertEqual(patched.json()["result"]["buffer_metres"], 3000)
        self.assertEqual(listed.json()["count"], 1)
        self.assertEqual(deleted.status_code, 200)

    def test_permit_crud_and_validation(self):
        origin = self.create_zone(
            "香港起航区", SmugglingZone.HONG_KONG_ORIGIN
        )
        destination = self.create_zone(
            "非设关点", SmugglingZone.NON_CUSTOMS_LANDING
        )
        response = self.client.post(
            reverse("smuggling_permit_collection"),
            data=json.dumps(
                {
                    "mmsi": "477123456",
                    "permit_number": "PERMIT-001",
                    "origin_zone_id": origin["id"],
                    "destination_zone_id": destination["id"],
                    "valid_from": "2026-07-24T00:00:00+08:00",
                    "valid_until": "2026-07-25T00:00:00+08:00",
                    "is_active": True,
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        permit = response.json()["result"]
        detail_url = reverse(
            "smuggling_permit_detail", args=[permit["id"]]
        )
        patched = self.client.patch(
            detail_url,
            data=json.dumps({"notes": "应急报告"}),
            content_type="application/json",
        )
        listed = self.client.get(
            reverse("smuggling_permit_collection")
        )

        self.assertEqual(patched.status_code, 200)
        self.assertEqual(patched.json()["result"]["notes"], "应急报告")
        self.assertEqual(listed.json()["count"], 1)

    def test_legacy_smuggling_url_reads_cached_detector_result(self):
        match = resolve("/Smuggling/detectSmuggling/")

        self.assertIs(match.func, cached_detection_result)
        self.assertEqual(
            match.kwargs["feature_id"], "detect-smuggling"
        )

    def test_home_page_exposes_smuggling_configuration_crud_panel(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'id="smuggling-panel-container"',
        )
        self.assertContains(response, 'id="smuggling-zone-save"')
        self.assertContains(response, 'id="smuggling-permit-save"')
        self.assertContains(response, "function loadSmugglingZones()")
        self.assertContains(response, "function saveSmugglingZone()")
        self.assertContains(response, "function editSmugglingZone(")
        self.assertContains(response, "function deleteSmugglingZone(")
        self.assertContains(response, "function loadSmugglingPermits()")
        self.assertContains(response, "function saveSmugglingPermit()")
        self.assertContains(response, "function editSmugglingPermit(")
        self.assertContains(response, "function deleteSmugglingPermit(")
