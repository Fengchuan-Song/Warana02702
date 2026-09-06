import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase

from performance_tests.dataset import DatasetError, load_dataset


class DatasetTests(SimpleTestCase):
    def test_loads_relative_sensor_files_and_version(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ais.csv").write_text(
                "timestamp,mmsi,lon,lat,speed\n"
                "2026-01-01T00:00:00Z,413000001,120,30,31\n",
                encoding="utf-8",
            )
            (root / "samples.json").write_text(
                json.dumps(
                    {
                        "dataset_version": "v1",
                        "samples": [
                            {
                                "sample_id": "S001",
                                "track_id": "T001",
                                "ground_truth": 1,
                                "ais_file": "ais.csv",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            dataset = load_dataset(root, feature_id="detect-highSpeedBoat")
        self.assertEqual(dataset.version, "v1")
        self.assertEqual(dataset.samples[0].ground_truth, 1)
        self.assertEqual(dataset.samples[0].ais[0]["mmsi"], "413000001")
        self.assertEqual(len(dataset.fingerprint), 64)

    def test_rejects_a_mismatched_feature_id(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "samples.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "sample_id": "S001",
                            "feature_id": "detect-lowSpeedBoat",
                            "ground_truth": 0,
                            "ais": [
                                {
                                    "timestamp": "2026-01-01T00:00:00Z",
                                    "mmsi": "413000001",
                                    "lon": 120,
                                    "lat": 30,
                                }
                            ],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaises(DatasetError):
                load_dataset(path, feature_id="detect-highSpeedBoat")

