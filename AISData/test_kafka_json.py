import json
from unittest import TestCase

from AISData.kafka_json import parse_ais_json


class KafkaAisJsonTests(TestCase):
    def test_parses_dynamic_ais_target(self):
        payload = {
            "mmsi": 413123456,
            "heading": 91,
            "sclass": "AIS_A",
            "lastTm": 1_725_158_400_000,
            "shipType": 70,
            "course": 90,
            "position": {"latitude": 22.168, "longitude": 113.667},
            "speed": 8.5,
            "status": 0,
            "vesselName": "测试船",
        }

        result = parse_ais_json(json.dumps(payload).encode("utf-8"))

        self.assertEqual(result.invalid_targets, 0)
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0]["MMSI"], "413123456")
        self.assertEqual(result.rows[0]["longitude"], 113.667)
        self.assertEqual(result.rows[0]["latitude"], 22.168)
        self.assertEqual(result.rows[0]["ship_type"], 70)
        self.assertEqual(result.rows[0]["collection_type"], "AIS_A")

    def test_ignores_radar_target(self):
        result = parse_ais_json(
            {
                "mmsi": 0,
                "sclass": "RADAR",
                "lastTm": 1_725_158_400_000,
                "position": {"latitude": 22.168, "longitude": 113.667},
            }
        )

        self.assertEqual(result.rows, [])
        self.assertEqual(result.ignored_counts, {"RADAR": 1})

    def test_accepts_numeric_class_and_list_envelope(self):
        payload = {
            "list": [
                {
                    "mmsi": 413123456,
                    "sclass": 0,
                    "lastTm": "2024-09-01T00:00:00Z",
                    "position": {"latitude": 22.168, "longitude": 113.667},
                }
            ]
        }

        result = parse_ais_json(payload)

        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0]["collection_type"], "AIS_A")
        self.assertEqual(result.rows[0]["timestamp"], "2024-09-01T00:00:00+00:00")
