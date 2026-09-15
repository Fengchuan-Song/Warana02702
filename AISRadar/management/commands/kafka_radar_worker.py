"""Consume JSON Radar targets from Kafka and publish live state."""

import time

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from AISData.consumers import AisConsumer
from AISData.kafka_json import TIMESTAMP_DIVISORS
from AISRadar.fusion_coordination import enqueue_radar_fusion_event
from AISRadar.kafka_json import parse_radar_json, radar_rows_for_fusion
from AISRadar.radar_state import merge_radar_state


class Command(BaseCommand):
    help = (
        "Consumes JSON target messages from Kafka, retains Radar "
        "targets, and publishes Redis/WebSocket/JPDA input state."
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
            help="Also accept targets whose ShipClass is SIM_RADAR.",
        )

    @staticmethod
    def _consumer_class():
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
        configured = dict(getattr(settings, "KAFKA_RADAR", {}))
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
                raise CommandError(f"KAFKA_RADAR.{required} cannot be empty")
        if configured.get("auto_offset_reset") not in {"earliest", "latest", "error"}:
            raise CommandError(
                "KAFKA_RADAR.auto_offset_reset must be earliest, latest or error"
            )
        if configured.get("timestamp_unit") not in TIMESTAMP_DIVISORS:
            raise CommandError(
                "KAFKA_RADAR.timestamp_unit must be seconds, milliseconds, "
                "microseconds or nanoseconds"
            )
        return configured

    def process_rows(
        self,
        rows,
        channel_layer,
        *,
        topic=None,
        partition=None,
        offset=None,
    ):
        """Merge and publish one successfully decoded Kafka envelope."""
        state_update = merge_radar_state(rows)
        if state_update.changed:
            async_to_sync(channel_layer.group_send)(
                AisConsumer.AIS_RADAR_GROUP_NAME,
                {
                    "type": "send_radar_update",
                    "data": state_update.snapshot,
                },
            )

        # Out-of-order observations are excluded. Kafka redeliveries remain
        # eligible so a retry can finish fusion publication after a partial
        # failure that happened after the Radar state was already stored.
        fusion_rows = radar_rows_for_fusion(state_update.current_rows)
        fusion_queued = False
        if (
            fusion_rows
            and topic is not None
            and partition is not None
            and offset is not None
        ):
            event_time = max(row["DateTime"] for row in fusion_rows)
            enqueue_radar_fusion_event(
                topic=topic,
                partition=partition,
                offset=offset,
                sensor_timestamp=event_time,
                radar_rows=fusion_rows,
            )
            fusion_queued = True

        return {
            "accepted": state_update.accepted,
            "targets": len(state_update.snapshot),
            "removes": len(state_update.removes),
            "fusion_queued": fusion_queued,
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
                "Kafka Radar worker started: "
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
                    from confluent_kafka import KafkaError

                    if error.code() == KafkaError._PARTITION_EOF:
                        continue
                    self.stderr.write(
                        self.style.ERROR(f"Kafka consumer error: {error}")
                    )
                    if error.fatal():
                        raise CommandError(str(error))
                    time.sleep(retry_seconds)
                    continue

                try:
                    parsed = parse_radar_json(
                        message.value(),
                        timestamp_unit=config.get("timestamp_unit", "milliseconds"),
                        accept_sim=bool(config.get("accept_sim", False)),
                        fallback_target_id=message.key(),
                    )
                    result = self.process_rows(
                        parsed.rows,
                        channel_layer,
                        topic=message.topic(),
                        partition=message.partition(),
                        offset=message.offset(),
                    )
                except (TypeError, ValueError) as exc:
                    self.stderr.write(
                        self.style.ERROR(
                            "Invalid Kafka Radar payload: "
                            f"topic={message.topic()} "
                            f"partition={message.partition()} "
                            f"offset={message.offset()} error={exc}"
                        )
                    )
                    consumer.commit(message=message, asynchronous=False)
                    continue
                except Exception as exc:
                    self.stderr.write(
                        self.style.ERROR(
                            "Kafka Radar processing failed; offset not committed: "
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
                    "Kafka Radar batch processed: "
                    f"message={processed_messages}, "
                    f"decoded={len(parsed.rows)}, "
                    f"invalid={parsed.invalid_targets}, "
                    f"ignored={sum(parsed.ignored_counts.values())}, "
                    f"accepted={result['accepted']}, "
                    f"targets={result['targets']}, "
                    f"fusion_queued={'yes' if result['fusion_queued'] else 'no'}"
                )
        except KeyboardInterrupt:
            self.stdout.write(
                self.style.WARNING("Kafka Radar worker stopped by user")
            )
        finally:
            consumer.close()
