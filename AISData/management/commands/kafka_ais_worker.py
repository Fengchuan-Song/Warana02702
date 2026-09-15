"""Consume JSON AIS targets from Kafka and publish operational state."""

import time

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from AISData.ais_state import merge_ais_state
from AISData.behavior_recognition import update_behavior_results
from AISData.consumers import AisConsumer
from AISData.detection_queue import enqueue_all_detections
from AISData.kafka_json import TIMESTAMP_DIVISORS, parse_ais_json
from AISData.normalization import (
    ais_record_kind,
    normalise_dynamic_ais_record,
    normalise_static_ais_record,
)
from AISData.trajectory_history import append_ais_history
from AISRadar.fusion_coordination import publish_ais_ingest_progress


class Command(BaseCommand):
    help = (
        "Consumes JSON target messages from Kafka, retains AIS data, "
        "and publishes the operational Redis/WebSocket/model state."
    )

    def add_arguments(self, parser):
        parser.add_argument("--bootstrap-servers")
        parser.add_argument("--topic")
        parser.add_argument("--group-id")
        parser.add_argument(
            "--auto-offset-reset",
            choices=("earliest", "latest", "error"),
        )
        parser.add_argument(
            "--timestamp-unit",
            choices=("seconds", "milliseconds", "microseconds", "nanoseconds"),
        )
        parser.add_argument(
            "--accept-sim",
            action="store_true",
            default=None,
            help="Also accept targets whose ShipClass is SIM.",
        )

    @staticmethod
    def convert_batch(rows):
        """Reuse the operational CSV worker's canonical AIS split contract."""
        latest_by_mmsi = {}
        history_points = []
        static_updates = []
        for row in rows:
            if ais_record_kind(row) == "static":
                static_update = normalise_static_ais_record(row)
                if static_update is not None:
                    static_updates.append(static_update)
                continue

            converted_ship = normalise_dynamic_ais_record(row)
            if not converted_ship or not converted_ship["mmsi"]:
                continue
            history_points.append(converted_ship)
            current = latest_by_mmsi.get(converted_ship["mmsi"])
            if (
                current is None
                or converted_ship["timestamp"] >= current["timestamp"]
            ):
                latest_by_mmsi[converted_ship["mmsi"]] = converted_ship
        return list(latest_by_mmsi.values()), history_points, static_updates

    @staticmethod
    def _consumer_class():
        # Keep Django startup/check commands usable before the optional Kafka
        # wheel has been installed in an existing deployment environment.
        try:
            from confluent_kafka import Consumer
        except ImportError as exc:
            raise CommandError(
                "confluent-kafka is required; install project requirements"
            ) from exc
        return Consumer

    @staticmethod
    def _consumer_config(config):
        result = {
            "bootstrap.servers": config["bootstrap_servers"],
            "group.id": config["group_id"],
            "auto.offset.reset": config["auto_offset_reset"],
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
        }
        optional_keys = {
            "security_protocol": "security.protocol",
            "sasl_mechanism": "sasl.mechanism",
            "sasl_username": "sasl.username",
            "sasl_password": "sasl.password",
        }
        for setting_name, kafka_name in optional_keys.items():
            value = str(config.get(setting_name) or "").strip()
            if value:
                result[kafka_name] = value
        return result

    def _options(self, options):
        configured = dict(getattr(settings, "KAFKA_AIS", {}))
        for option_name in (
            "bootstrap_servers",
            "topic",
            "group_id",
            "auto_offset_reset",
            "timestamp_unit",
        ):
            if options.get(option_name) is not None:
                configured[option_name] = options[option_name]
        if options.get("accept_sim") is not None:
            configured["accept_sim"] = options["accept_sim"]

        for required in ("bootstrap_servers", "topic", "group_id"):
            if not str(configured.get(required) or "").strip():
                raise CommandError(f"KAFKA_AIS.{required} cannot be empty")
        if configured.get("auto_offset_reset") not in {"earliest", "latest", "error"}:
            raise CommandError(
                "KAFKA_AIS.auto_offset_reset must be earliest, latest or error"
            )
        if configured.get("timestamp_unit") not in TIMESTAMP_DIVISORS:
            raise CommandError(
                "KAFKA_AIS.timestamp_unit must be seconds, milliseconds, "
                "microseconds or nanoseconds"
            )
        return configured

    def process_rows(self, rows, channel_layer):
        """Normalise and publish one successfully decoded Kafka envelope."""
        ais_data, history_points, static_updates = self.convert_batch(rows)
        if not ais_data and not static_updates:
            return {
                "accepted": 0,
                "targets": 0,
                "upserts": 0,
                "removes": 0,
                "queued": 0,
            }

        append_ais_history(history_points)
        update_behavior_results(history_points)
        state_update = merge_ais_state(
            dynamic_updates=history_points,
            static_updates=static_updates,
        )
        delta = state_update.delta()
        if delta["upserts"] or delta["removes"]:
            async_to_sync(channel_layer.group_send)(
                AisConsumer.AIS_GROUP_NAME,
                {"type": "send_ais_delta", "data": delta},
            )

        queue_status = enqueue_all_detections(
            ship_list=state_update.snapshot,
            incremental_ship_list=state_update.accepted_dynamic,
        )
        return {
            "accepted": state_update.accepted,
            "targets": len(state_update.snapshot),
            "upserts": len(delta["upserts"]),
            "removes": len(delta["removes"]),
            "queued": sum(queue_status.values()),
        }

    def handle(self, *args, **options):
        config = self._options(options)
        channel_layer = get_channel_layer()
        if channel_layer is None:
            raise CommandError("Django Channels layer is not configured")

        Consumer = self._consumer_class()
        consumer = Consumer(self._consumer_config(config))
        consumer.subscribe([config["topic"]])
        poll_timeout = max(0.1, float(config.get("poll_timeout_seconds", 1.0)))
        retry_seconds = max(0.1, float(config.get("error_retry_seconds", 5.0)))
        processed_messages = 0

        self.stdout.write(
            self.style.SUCCESS(
                "Kafka AIS worker started: "
                f"brokers={config['bootstrap_servers']}, "
                f"topic={config['topic']}, group={config['group_id']}"
            )
        )
        try:
            while True:
                message = consumer.poll(timeout=poll_timeout)
                if message is None:
                    continue
                if message.error():
                    error = message.error()
                    # Import lazily with the client so ordinary Django commands
                    # do not depend on librdkafka being loaded.
                    from confluent_kafka import KafkaError

                    if error.code() == KafkaError._PARTITION_EOF:
                        continue
                    self.stderr.write(self.style.ERROR(f"Kafka consumer error: {error}"))
                    if error.fatal():
                        raise CommandError(str(error))
                    time.sleep(retry_seconds)
                    continue

                try:
                    parsed = parse_ais_json(
                        message.value(),
                        timestamp_unit=config.get(
                            "timestamp_unit",
                            "milliseconds",
                        ),
                        accept_sim=bool(config.get("accept_sim", False)),
                    )
                    result = self.process_rows(parsed.rows, channel_layer)
                    event_time = max(
                        (
                            row.get("timestamp")
                            for row in parsed.rows
                            if isinstance(row, dict) and row.get("timestamp")
                        ),
                        default=None,
                    )
                    publish_ais_ingest_progress(
                        message.topic(),
                        message.partition(),
                        message.offset(),
                        event_time,
                    )
                except (TypeError, ValueError) as exc:
                    # A malformed payload cannot recover by blocking its Kafka
                    # partition forever. Log metadata, skip it and commit.
                    self.stderr.write(
                        self.style.ERROR(
                            "Invalid Kafka AIS payload: "
                            f"topic={message.topic()} "
                            f"partition={message.partition()} "
                            f"offset={message.offset()} error={exc}"
                        )
                    )
                    consumer.commit(message=message, asynchronous=False)
                    continue
                except Exception as exc:
                    # Do not commit a message whose Redis/model publication
                    # failed. Seek back immediately; merely omitting commit is
                    # insufficient because this live consumer could otherwise
                    # poll and later commit a newer offset in the partition.
                    self.stderr.write(
                        self.style.ERROR(
                            "Kafka AIS processing failed; offset not committed: "
                            f"topic={message.topic()} "
                            f"partition={message.partition()} "
                            f"offset={message.offset()} error={exc}"
                        )
                    )
                    from confluent_kafka import TopicPartition

                    consumer.seek(
                        TopicPartition(
                            message.topic(),
                            message.partition(),
                            message.offset(),
                        )
                    )
                    time.sleep(retry_seconds)
                    continue

                consumer.commit(message=message, asynchronous=False)
                processed_messages += 1
                self.stdout.write(
                    "Kafka AIS batch processed: "
                    f"message={processed_messages}, "
                    f"decoded={len(parsed.rows)}, "
                    f"invalid={parsed.invalid_targets}, "
                    f"ignored={sum(parsed.ignored_counts.values())}, "
                    f"accepted={result['accepted']}, "
                    f"targets={result['targets']}, "
                    f"queued={result['queued']}"
                )
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("Kafka AIS worker stopped by user"))
        finally:
            consumer.close()
