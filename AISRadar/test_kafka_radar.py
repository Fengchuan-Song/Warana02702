from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from google.protobuf.message import DecodeError

from AISData.consumers import AisConsumer
from AISData.protos import UnionTargsZV1_pb2 as union_targets
from AISData.trajectory_history import append_ais_history
from AISRadar.fusion_coordination import (
    ais_is_ready,
    build_causal_ais_rows,
    publish_ais_ingest_progress,
)
from AISRadar.kafka_protobuf import (
    parse_radar_protobuf,
    radar_rows_for_fusion,
)
from AISRadar.management.commands.fusion_coordinator_worker import (
    Command as CoordinatorCommand,
)
from AISRadar.management.commands.kafka_radar_worker import Command
from AISRadar.radar_state import merge_radar_state


LOC_MEMORY_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "kafka-radar-tests",
    }
}


class KafkaRadarProtobufTests(SimpleTestCase):
    def _target(self, envelope, target_class, target_id="1-1", with_position=True):
        target = envelope.list.add()
        target.lastTm = 1_725_158_400_000
        target.pos.sclass = target_class
        target.pos.srcTargetKey = target_id
        target.pos.speed = 12.5
        target.pos.course = 75.0
        target.pos.heading = 74.0
        if with_position:
            target.pos.geoPtn.longitude = 113.667
            target.pos.geoPtn.latitude = 22.168
        return target

    def test_parses_radar_and_ignores_ais_and_fused_targets(self):
        envelope = union_targets.TargetProtoListZ()
        self._target(envelope, union_targets.RADAR)
        self._target(envelope, union_targets.AIS_A, "413123456")
        self._target(envelope, union_targets.NORMAL_AIS_A_RADAR, "fused-1")

        result = parse_radar_protobuf(envelope.SerializeToString())

        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0]["id"], "1-1")
        self.assertEqual(result.rows[0]["lon"], 113.667)
        self.assertEqual(result.rows[0]["lat"], 22.168)
        self.assertEqual(result.rows[0]["collection_type"], "RADAR")
        self.assertEqual(
            result.ignored_counts,
            {"AIS_A": 1, "NORMAL_AIS_A_RADAR": 1},
        )

    def test_sim_radar_requires_opt_in(self):
        envelope = union_targets.TargetProtoListZ()
        self._target(envelope, union_targets.SIM_RADAR)
        payload = envelope.SerializeToString()

        self.assertEqual(parse_radar_protobuf(payload).rows, [])
        self.assertEqual(
            len(parse_radar_protobuf(payload, accept_sim=True).rows),
            1,
        )

    def test_missing_position_or_track_id_is_invalid(self):
        envelope = union_targets.TargetProtoListZ()
        self._target(envelope, union_targets.RADAR, target_id="", with_position=False)

        result = parse_radar_protobuf(envelope.SerializeToString())

        self.assertEqual(result.rows, [])
        self.assertEqual(result.invalid_targets, 1)

    def test_rejects_invalid_protobuf(self):
        with self.assertRaises(DecodeError):
            parse_radar_protobuf(b"not-protobuf")

    def test_builds_matcher_rows(self):
        result = radar_rows_for_fusion(
            [
                {
                    "timestamp": "2024-09-01T00:00:00+00:00",
                    "id": "1-1",
                    "lat": 22.168,
                    "lon": 113.667,
                    "speed": 12.5,
                    "course": 75.0,
                    "gtid": "413123456",
                }
            ]
        )

        self.assertEqual(
            result,
            [
                {
                    "DateTime": "2024-09-01T00:00:00+00:00",
                    "ID": "1-1",
                    "X": 22.168,
                    "Y": 113.667,
                    "speed": 12.5,
                    "course": 75.0,
                    "GTID": "413123456",
                }
            ],
        )


@override_settings(
    CACHES=LOC_MEMORY_CACHE,
    RADAR_LATEST_STATE_RETENTION_SECONDS=60,
)
class KafkaRadarStateTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    @staticmethod
    def _row(timestamp, target_id="1-1", received_at="first"):
        return {
            "timestamp": timestamp,
            "received_at": received_at,
            "id": target_id,
            "lon": 113.667,
            "lat": 22.168,
            "speed": 12.5,
            "course": 75.0,
        }

    def test_merge_rejects_old_rows_and_deduplicates_redelivery(self):
        first = merge_radar_state(
            [self._row("2024-09-01T00:00:30+00:00")]
        )
        old = merge_radar_state(
            [self._row("2024-09-01T00:00:20+00:00")]
        )
        duplicate = merge_radar_state(
            [
                self._row(
                    "2024-09-01T00:00:30+00:00",
                    received_at="redelivery",
                )
            ]
        )

        self.assertEqual(first.accepted, 1)
        self.assertEqual(old.out_of_order, 1)
        self.assertEqual(duplicate.duplicate, 1)
        self.assertFalse(duplicate.changed)

    def test_new_event_time_removes_stale_tracks(self):
        merge_radar_state(
            [self._row("2024-09-01T00:00:00+00:00", target_id="old")]
        )
        update = merge_radar_state(
            [self._row("2024-09-01T00:02:00+00:00", target_id="new")]
        )

        self.assertEqual(update.removes, ["old"])
        self.assertEqual([row["id"] for row in update.snapshot], ["new"])

    def test_worker_publishes_websocket_snapshot_and_queues_fusion_event(self):
        class RecordingChannelLayer:
            def __init__(self):
                self.events = []

            async def group_send(self, group, event):
                self.events.append((group, event))

        layer = RecordingChannelLayer()
        row = self._row("2024-09-01T00:00:30+00:00")
        with patch(
            "AISRadar.management.commands.kafka_radar_worker."
            "enqueue_radar_fusion_event"
        ) as enqueue:
            result = Command().process_rows(
                [row],
                layer,
                topic="union-targets",
                partition=2,
                offset=17,
            )

        self.assertTrue(result["fusion_queued"])
        self.assertEqual(layer.events[0][0], AisConsumer.AIS_RADAR_GROUP_NAME)
        self.assertEqual(layer.events[0][1]["type"], "send_radar_update")
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.kwargs["partition"], 2)
        self.assertEqual(enqueue.call_args.kwargs["offset"], 17)
        self.assertEqual(enqueue.call_args.kwargs["radar_rows"][0]["ID"], "1-1")


@override_settings(
    CACHES=LOC_MEMORY_CACHE,
    KAFKA_AIS={"topic": "union-targets"},
    KAFKA_RADAR_AIS_HISTORY_SECONDS=1800,
    AIS_RADAR_FUSION_COORDINATOR={
        "wait_ms": 0,
        "completed_ttl_seconds": 60,
    },
)
class FusionCoordinationTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_same_topic_waits_for_matching_partition_offset(self):
        publish_ais_ingest_progress(
            "union-targets",
            2,
            16,
            "2024-09-01T00:00:20+00:00",
        )

        self.assertFalse(
            ais_is_ready(
                "union-targets",
                2,
                17,
                "2024-09-01T00:00:20+00:00",
            )
        )
        publish_ais_ingest_progress(
            "union-targets",
            2,
            17,
            "2024-09-01T00:00:21+00:00",
        )
        self.assertTrue(
            ais_is_ready(
                "union-targets",
                2,
                17,
                "2024-09-01T00:00:20+00:00",
            )
        )

    def test_different_topics_use_event_time_watermark(self):
        publish_ais_ingest_progress(
            "ais-topic",
            0,
            9,
            "2024-09-01T00:00:20+00:00",
        )

        self.assertTrue(
            ais_is_ready(
                "radar-topic",
                0,
                100,
                "2024-09-01T00:00:19+00:00",
            )
        )
        self.assertFalse(
            ais_is_ready(
                "radar-topic",
                0,
                100,
                "2024-09-01T00:00:21+00:00",
            )
        )

    def test_cross_topic_watermark_uses_slowest_known_ais_partition(self):
        publish_ais_ingest_progress(
            "ais-topic",
            0,
            9,
            "2024-09-01T00:00:20+00:00",
        )
        publish_ais_ingest_progress(
            "ais-topic",
            1,
            4,
            "2024-09-01T00:00:10+00:00",
        )

        self.assertFalse(
            ais_is_ready(
                "radar-topic",
                0,
                100,
                "2024-09-01T00:00:15+00:00",
            )
        )
        publish_ais_ingest_progress(
            "ais-topic",
            1,
            5,
            "2024-09-01T00:00:21+00:00",
        )
        self.assertTrue(
            ais_is_ready(
                "radar-topic",
                0,
                100,
                "2024-09-01T00:00:15+00:00",
            )
        )

    def test_causal_ais_rows_exclude_future_observations(self):
        points = [
            {
                "mmsi": "413123456",
                "timestamp": "2024-09-01T00:00:10+00:00",
                "lon": 113.667,
                "lat": 22.168,
                "speed": 8.0,
                "course": 90.0,
            },
            {
                "mmsi": "413123456",
                "timestamp": "2024-09-01T00:00:30+00:00",
                "lon": 113.668,
                "lat": 22.169,
                "speed": 8.0,
                "course": 90.0,
            },
        ]
        append_ais_history(points)
        cache.set("latest_ais_data_raw", [points[-1]], timeout=300)

        rows = build_causal_ais_rows("2024-09-01T00:00:20+00:00")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["DateTime"], "2024-09-01T00:00:10+00:00")

    def test_no_ais_publishes_all_radar_as_unmatched(self):
        class RecordingChannelLayer:
            def __init__(self):
                self.events = []

            async def group_send(self, group, event):
                self.events.append((group, event))

        event = {
            "event_id": "union-targets:0:1",
            "topic": "union-targets",
            "partition": 0,
            "offset": 1,
            "sensor_timestamp": "2024-09-01T00:00:20+00:00",
            "radar_rows": [
                {
                    "DateTime": "2024-09-01T00:00:20+00:00",
                    "ID": "1-1",
                    "X": 22.168,
                    "Y": 113.667,
                }
            ],
        }
        layer = RecordingChannelLayer()
        matcher = Mock()

        with patch(
            "AISRadar.management.commands.fusion_coordinator_worker."
            "mark_fusion_event_completed"
        ) as completed:
            with patch(
                "AISRadar.management.commands.fusion_coordinator_worker."
                "publish_fusion_detection_results"
            ) as detect:
                outcome = CoordinatorCommand().process_event(
                    event,
                    matcher,
                    layer,
                    wait_ms=0,
                )

        matcher.predict_tables.assert_not_called()
        completed.assert_called_once_with(event["event_id"])
        detect.assert_called_once()
        self.assertFalse(outcome["ais_ready"])
        self.assertEqual(
            outcome["fusion_state"]["unmatched_radar_targets"],
            [{"id": "1-1", "x": 22.168, "y": 113.667}],
        )
        self.assertEqual(
            outcome["fusion_state"]["fusion_event_id"],
            event["event_id"],
        )
        self.assertEqual(
            layer.events[0][1]["type"],
            "send_ais_radar_fusion_update",
        )
