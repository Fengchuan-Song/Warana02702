from datetime import datetime, timezone as dt_timezone

from django.test import SimpleTestCase
from google.protobuf.message import DecodeError

from AISData.kafka_protobuf import (
    parse_ais_protobuf,
    protobuf_timestamp_to_iso,
)
from AISData.management.commands.kafka_ais_worker import Command
from AISData.protos import UnionTargsZV1_pb2 as union_targets


class KafkaProtobufTests(SimpleTestCase):
    def _target(self, envelope, ship_class, *, with_position=True):
        target = envelope.list.add()
        target.lastTm = 1_725_158_400_000
        position = target.pos
        position.mmsi = 413123456
        position.sclass = ship_class
        position.vesselName = "测试船"
        position.speed = 8.5
        position.course = 120.0
        position.heading = 118.0
        position.status = 0
        position.len = 120
        position.wid = 20
        if with_position:
            position.geoPtn.longitude = 113.667
            position.geoPtn.latitude = 22.168
        return target

    def test_parses_ais_and_filters_radar_and_fusion_targets(self):
        envelope = union_targets.TargetProtoListZ()
        self._target(envelope, union_targets.AIS_A)
        self._target(envelope, union_targets.RADAR)
        self._target(envelope, union_targets.NORMAL_AIS_A_RADAR)

        result = parse_ais_protobuf(envelope.SerializeToString())

        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0]["MMSI"], "413123456")
        self.assertEqual(result.rows[0]["longitude"], 113.667)
        self.assertEqual(result.rows[0]["latitude"], 22.168)
        self.assertEqual(result.rows[0]["collection_type"], "AIS_A")
        self.assertEqual(
            result.ignored_counts,
            {"RADAR": 1, "NORMAL_AIS_A_RADAR": 1},
        )

        latest, history, static = Command.convert_batch(result.rows)
        self.assertEqual(len(latest), 1)
        self.assertEqual(len(history), 1)
        self.assertEqual(static, [])
        self.assertEqual(history[0]["mmsi"], "413123456")
        self.assertEqual(history[0]["lon"], 113.667)

    def test_static_target_becomes_type_five_without_position(self):
        envelope = union_targets.TargetProtoListZ()
        self._target(envelope, union_targets.STATIC, with_position=False)

        result = parse_ais_protobuf(envelope.SerializeToString())

        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0]["msg_type"], 5)
        self.assertNotIn("longitude", result.rows[0])

    def test_sim_target_requires_explicit_opt_in(self):
        envelope = union_targets.TargetProtoListZ()
        self._target(envelope, union_targets.SIM)
        payload = envelope.SerializeToString()

        rejected = parse_ais_protobuf(payload)
        accepted = parse_ais_protobuf(payload, accept_sim=True)

        self.assertEqual(rejected.rows, [])
        self.assertEqual(rejected.ignored_counts, {"SIM": 1})
        self.assertEqual(len(accepted.rows), 1)

    def test_dynamic_target_without_geo_point_is_invalid(self):
        envelope = union_targets.TargetProtoListZ()
        self._target(envelope, union_targets.AIS_B, with_position=False)

        result = parse_ais_protobuf(envelope.SerializeToString())

        self.assertEqual(result.rows, [])
        self.assertEqual(result.invalid_targets, 1)

    def test_invalid_target_does_not_discard_valid_sibling(self):
        envelope = union_targets.TargetProtoListZ()
        invalid = self._target(envelope, union_targets.AIS_A)
        invalid.lastTm = 0
        invalid.pos.lastTm = 0
        valid = self._target(envelope, union_targets.AIS_B)
        valid.pos.mmsi = 413654321

        result = parse_ais_protobuf(envelope.SerializeToString())

        self.assertEqual([row["MMSI"] for row in result.rows], ["413654321"])
        self.assertEqual(result.invalid_targets, 1)

    def test_timestamp_unit_is_explicit(self):
        value = protobuf_timestamp_to_iso(1_000, "milliseconds")

        self.assertEqual(
            datetime.fromisoformat(value),
            datetime(1970, 1, 1, 0, 0, 1, tzinfo=dt_timezone.utc),
        )
        with self.assertRaisesMessage(ValueError, "Unsupported Kafka timestamp unit"):
            protobuf_timestamp_to_iso(1, "auto")

    def test_rejects_invalid_protobuf(self):
        with self.assertRaises(DecodeError):
            parse_ais_protobuf(b"not-protobuf")
