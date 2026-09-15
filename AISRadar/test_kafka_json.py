import json
from unittest import TestCase

from AISRadar.kafka_json import parse_radar_json, radar_rows_for_fusion


SAMPLE_RADAR_TARGET = {
    "wid": 0,
    "len": 0,
    "mmsi": 0,
    "heading": 0,
    "sclass": "RADAR",
    "lastTm": 1_788_865_671_734,
    "shipType": 0,
    "course": 26.56,
    "position": {
        "latitude": 22.197385787963867,
        "longitude": 113.63924407958984,
    },
    "speed": 4.17,
    "status": 1,
    "vesselName": "",
}


class KafkaRadarJsonTests(TestCase):
    def test_parses_supplied_json_contract_with_kafka_key(self):
        result = parse_radar_json(
            json.dumps(SAMPLE_RADAR_TARGET).encode(),
            fallback_target_id=b"radar-track-42",
            received_at="2026-09-15T00:00:00+00:00",
        )

        self.assertEqual(result.invalid_targets, 0)
        self.assertEqual(result.class_counts, {"RADAR": 1})
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(
            result.rows[0],
            {
                "timestamp": "2026-09-08T11:07:51.734000+00:00",
                "received_at": "2026-09-15T00:00:00+00:00",
                "id": "radar-track-42",
                "lon": 113.63924407958984,
                "lat": 22.197385787963867,
                "course": 26.56,
                "speed": 4.17,
                "heading": 0.0,
                "collection_type": "RADAR",
                "source": "",
                "device_owner_id": None,
                "display_id": None,
                "source_type": None,
                "source_index": None,
                "source_target_key": "",
                "mmsi": "0",
                "status": 1,
                "length": 0,
                "width": 0,
                "ship_type": 0,
                "vessel_name": "",
            },
        )

    def test_requires_stable_track_id(self):
        result = parse_radar_json(SAMPLE_RADAR_TARGET)

        self.assertEqual(result.rows, [])
        self.assertEqual(result.invalid_targets, 1)

    def test_accepts_array_and_envelope_and_ignores_non_radar(self):
        radar = dict(SAMPLE_RADAR_TARGET, trackId="track-1")
        ais = dict(SAMPLE_RADAR_TARGET, sclass="AIS_A", mmsi=413123456)

        array_result = parse_radar_json([radar, ais])
        envelope_result = parse_radar_json({"data": radar})

        self.assertEqual([row["id"] for row in array_result.rows], ["track-1"])
        self.assertEqual(array_result.ignored_counts, {"AIS_A": 1})
        self.assertEqual([row["id"] for row in envelope_result.rows], ["track-1"])

    def test_builds_matcher_rows(self):
        parsed = parse_radar_json(
            dict(SAMPLE_RADAR_TARGET, id="track-1")
        )

        self.assertEqual(
            radar_rows_for_fusion(parsed.rows),
            [
                {
                    "DateTime": "2026-09-08T11:07:51.734000+00:00",
                    "ID": "track-1",
                    "X": 22.197385787963867,
                    "Y": 113.63924407958984,
                    "speed": 4.17,
                    "course": 26.56,
                }
            ],
        )

    def test_rejects_invalid_json(self):
        with self.assertRaisesRegex(ValueError, "Invalid Kafka target JSON"):
            parse_radar_json(b"not-json")
