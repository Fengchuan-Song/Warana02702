import csv
import io
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings

from AISData.ais_simulation import (
    LoadedAISData,
    SimulationConfig,
    discover_ais_csv_paths,
    iter_simulated_records,
    load_ais_csv,
    nominal_reporting_interval,
)
from AISData.ais_state import (
    load_ais_snapshot,
    load_ais_state_version,
    merge_ais_state,
)


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "ais-simulation-tests",
    }
}
TEST_CHANNEL_LAYERS = {
    "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}
}


def _point(timestamp, mmsi="123456789", **overrides):
    point = {
        "timestamp": timestamp,
        "received_at": timestamp,
        "mmsi": mmsi,
        "msg_type": 1,
        "name": "测试船",
        "lon": 113.7,
        "lat": 22.4,
        "speed": 10.0,
        "course": 90.0,
        "heading": 90.0,
        "rot": 0.0,
        "nav_status": 0,
        "quality_flags": [],
    }
    point.update(overrides)
    return point


def _loaded(points):
    return LoadedAISData(
        tracks={"123456789": points},
        static_records=[],
        selected_mmsis=("123456789",),
        source_rows=len(points),
        invalid_rows=0,
    )


@override_settings(CACHES=TEST_CACHES, CHANNEL_LAYERS=TEST_CHANNEL_LAYERS)
class AISSimulationTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def _csv_path(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "ais.csv"
        with path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(
                output,
                fieldnames=(
                    "timestamp",
                    "MMSI",
                    "msg_type",
                    "latitude",
                    "longitude",
                    "speed",
                    "course",
                    "heading",
                    "rot",
                    "status",
                    "Name",
                ),
            )
            writer.writeheader()
            writer.writerows(
                [
                    {
                        "timestamp": "2026-08-23 08:00:00",
                        "MMSI": "123456789",
                        "msg_type": 1,
                        "latitude": 22.4,
                        "longitude": 113.7,
                        "speed": 10,
                        "course": 90,
                        "heading": 90,
                        "rot": 0,
                        "status": 0,
                        "Name": "测试船",
                    },
                    {
                        "timestamp": "2026-08-23 08:01:00",
                        "MMSI": "123456789",
                        "msg_type": 1,
                        "latitude": 22.4,
                        "longitude": 113.705,
                        "speed": 10,
                        "course": 90,
                        "heading": 90,
                        "rot": 0,
                        "status": 0,
                        "Name": "测试船",
                    },
                ]
            )
        return path

    def test_nominal_reporting_periods_follow_class_and_state(self):
        self.assertEqual(nominal_reporting_interval(_point("2026-08-23T00:00:00Z")), 10)
        self.assertEqual(
            nominal_reporting_interval(
                _point(
                    "2026-08-23T00:00:00Z",
                    speed=0.1,
                    nav_status=1,
                )
            ),
            180,
        )
        self.assertEqual(
            nominal_reporting_interval(
                _point("2026-08-23T00:00:00Z", speed=20, rot=1)
            ),
            2,
        )
        self.assertEqual(
            nominal_reporting_interval(
                _point("2026-08-23T00:00:00Z", msg_type=18, speed=10)
            ),
            30,
        )

    def test_broadcast_mode_interpolates_standard_ten_second_reports(self):
        loaded = _loaded(
            [
                _point("2026-08-23T00:00:00Z", lon=113.7000),
                _point("2026-08-23T00:01:00Z", lon=113.7050),
            ]
        )
        config = SimulationConfig(mode="broadcast", gps_noise_metres=0)
        records = list(iter_simulated_records(loaded, config, seed=7))

        self.assertEqual(len(records), 7)
        self.assertEqual(
            [item.record["timestamp"][14:19] for item in records],
            ["00:00", "00:10", "00:20", "00:30", "00:40", "00:50", "01:00"],
        )
        self.assertFalse(records[0].record["synthetic"])
        self.assertTrue(records[1].record["synthetic"])
        self.assertAlmostEqual(records[3].record["lon"], 113.7025, places=4)

    def test_received_mode_can_model_total_loss_and_duplicates(self):
        loaded = _loaded(
            [
                _point("2026-08-23T00:00:00Z"),
                _point("2026-08-23T00:00:20Z", lon=113.701),
            ]
        )
        lost = list(
            iter_simulated_records(
                loaded,
                SimulationConfig(
                    mode="received",
                    gps_noise_metres=0,
                    loss_rate=1,
                    outage_rate_per_hour=0,
                ),
                seed=2,
            )
        )
        duplicated = list(
            iter_simulated_records(
                loaded,
                SimulationConfig(
                    mode="received",
                    gps_noise_metres=0,
                    loss_rate=0,
                    duplicate_rate=1,
                    out_of_order_rate=0,
                    outage_rate_per_hour=0,
                ),
                seed=2,
            )
        )

        self.assertEqual(lost, [])
        self.assertEqual(len(duplicated), 6)
        self.assertEqual(
            sum(bool(item.record.get("simulation_duplicate")) for item in duplicated),
            3,
        )

    def test_loader_applies_source_timezone_and_duration(self):
        loaded = load_ais_csv(
            self._csv_path(),
            source_timezone="Asia/Shanghai",
            duration_minutes=1,
        )

        points = loaded.tracks["123456789"]
        self.assertEqual(len(points), 2)
        self.assertIn("+08:00", points[0]["timestamp"])

    def test_loader_recursively_combines_every_csv_in_a_directory(self):
        first = self._csv_path()
        nested = first.parent / "nested"
        nested.mkdir()
        second = nested / "second.CSV"
        second.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")
        (nested / "ignored.txt").write_text("not AIS", encoding="utf-8")

        loaded = load_ais_csv(
            first.parent,
            source_timezone="Asia/Shanghai",
            duration_minutes=1,
        )

        self.assertEqual(discover_ais_csv_paths(first.parent), (first, second))
        self.assertEqual(len(loaded.source_files), 2)
        self.assertEqual(loaded.source_rows, 4)
        self.assertEqual(len(loaded.tracks["123456789"]), 2)

    def test_command_publishes_only_to_predict_namespace(self):
        merge_ais_state(
            [_point("2026-08-23T00:00:00Z", mmsi="987654321")],
            namespace="predict",
        )
        previous_version = load_ais_state_version("predict")
        output = io.StringIO()
        call_command(
            "simulate_ais_realtime",
            file=str(self._csv_path()),
            mode="broadcast",
            source_timezone="Asia/Shanghai",
            no_wait=True,
            max_ships=1,
            duration_minutes=1,
            batch_ms=1000,
            batch_size=100,
            max_events=20,
            namespace="predict",
            stdout=output,
        )

        predict_snapshot = load_ais_snapshot("predict")
        self.assertEqual(
            [item["mmsi"] for item in predict_snapshot],
            ["123456789"],
        )
        self.assertGreater(load_ais_state_version("predict"), previous_version)
        self.assertEqual(load_ais_snapshot(), [])
        self.assertIn("AIS simulation complete", output.getvalue())

    def test_operational_namespace_requires_explicit_permission(self):
        with self.assertRaisesMessage(
            Exception,
            "requires --allow-operational-write",
        ):
            call_command(
                "simulate_ais_realtime",
                file=str(self._csv_path()),
                namespace="operational",
                no_wait=True,
            )

    @patch(
        "AISData.management.commands.simulate_ais_realtime."
        "enqueue_all_detections"
    )
    def test_predict_simulation_can_enqueue_isolated_detection_snapshots(
        self,
        enqueue_all,
    ):
        call_command(
            "simulate_ais_realtime",
            file=str(self._csv_path()),
            mode="broadcast",
            source_timezone="Asia/Shanghai",
            no_wait=True,
            max_ships=1,
            duration_minutes=1,
            batch_size=100,
            max_events=20,
            namespace="predict",
            simulation_id="simulation-test",
            enqueue_detections=True,
            verbosity=0,
        )

        self.assertTrue(enqueue_all.called)
        self.assertTrue(
            all(
                call.kwargs["namespace"] == "predict"
                and call.kwargs["simulation_id"] == "simulation-test"
                and isinstance(call.kwargs["ship_list"], list)
                and isinstance(
                    call.kwargs["incremental_ship_list"],
                    list,
                )
                for call in enqueue_all.call_args_list
            )
        )
