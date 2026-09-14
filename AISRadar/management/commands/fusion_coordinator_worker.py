"""Coordinate operational AIS/Radar events and run JPDA reliably."""

from dataclasses import replace
import time

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.management.base import BaseCommand, CommandError
from django_redis import get_redis_connection
import pandas as pd

from AISData.consumers import AisConsumer
from AISRadar.anomaly_detection import publish_fusion_detection_results
from AISRadar.fusion_coordination import (
    acknowledge_coordinator_event,
    build_causal_ais_rows,
    coordinator_config,
    ensure_coordinator_group,
    fusion_event_completed,
    mark_fusion_event_completed,
    read_coordinator_events,
    wait_for_ais,
)
from AISRadar.fusion_state import publish_fusion_result
from AISRadar.inference.predictor import get_matcher
from AISRadar.views import _config


class Command(BaseCommand):
    help = (
        "Consumes reliable Radar fusion events, waits for the AIS watermark, "
        "runs JPDA, and acknowledges each event after publication."
    )

    def add_arguments(self, parser):
        parser.add_argument("--consumer-name")
        parser.add_argument("--wait-ms", type=int)
        parser.add_argument("--poll-block-ms", type=int)

    @staticmethod
    def _options(options):
        config = coordinator_config()
        for name in ("consumer_name", "wait_ms", "poll_block_ms"):
            if options.get(name) is not None:
                config[name] = options[name]
        if not str(config.get("stream_key") or "").strip():
            raise CommandError("Fusion coordinator stream_key cannot be empty")
        if not str(config.get("group_name") or "").strip():
            raise CommandError("Fusion coordinator group_name cannot be empty")
        if not str(config.get("consumer_name") or "").strip():
            raise CommandError("Fusion coordinator consumer_name cannot be empty")
        if int(config.get("wait_ms", -1)) < 0:
            raise CommandError("Fusion coordinator wait_ms cannot be negative")
        if int(config.get("poll_block_ms", 0)) < 1:
            raise CommandError("Fusion coordinator poll_block_ms must be positive")
        if int(config.get("stream_max_length", 0)) < 0:
            raise CommandError(
                "Fusion coordinator stream_max_length cannot be negative"
            )
        if int(config.get("completed_ttl_seconds", 0)) < 1:
            raise CommandError(
                "Fusion coordinator completed_ttl_seconds must be positive"
            )
        return config

    @staticmethod
    def _matcher(config):
        inference_config = _config()
        max_gap = config.get("max_ais_time_gap_seconds")
        if max_gap not in (None, ""):
            inference_config = replace(
                inference_config,
                max_ais_time_gap_seconds=float(max_gap),
            )
        return get_matcher(inference_config)

    @staticmethod
    def _radar_only_result(event):
        """Make a valid fusion snapshot when no causal AIS is available."""
        rows = event.get("radar_rows") or []
        sensor_timestamp = str(event.get("sensor_timestamp") or "")
        targets = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            target_id = str(row.get("ID") or "").strip()
            if not target_id or target_id in seen:
                continue
            try:
                latitude = float(row.get("X"))
                longitude = float(row.get("Y"))
            except (TypeError, ValueError):
                continue
            seen.add(target_id)
            targets.append({"id": target_id, "x": latitude, "y": longitude})
        return {
            "model": {
                "algorithm": "JPDA AIS-Radar fusion",
                "max_ais_time_gap_seconds": None,
            },
            "input": {
                "ais_rows": 0,
                "radar_rows": len(rows),
                "radar_timestamps": 1 if rows else 0,
                "coordinate_system": "WGS84 latitude/longitude",
            },
            "summary": {"windows": 1, "matches": 0},
            "skipped_windows": 0,
            "windows": [
                {
                    "start_time": sensor_timestamp,
                    "end_time": sensor_timestamp,
                    "ais_trajectories": 0,
                    "radar_trajectories": len(targets),
                    "ais_targets": [],
                    "radar_targets": targets,
                    "matches": [],
                    "unmatched_ais_targets": [],
                    "unmatched_radar_targets": targets,
                }
            ],
        }

    @staticmethod
    def _send_fusion_state(channel_layer, fusion_state):
        async_to_sync(channel_layer.group_send)(
            AisConsumer.AIS_RADAR_GROUP_NAME,
            {"type": "send_ais_radar_fusion_update", "data": fusion_state},
        )

    def process_event(self, event, matcher, channel_layer, wait_ms=None):
        """Wait, fuse, publish, and mark one event idempotently."""
        event_id = event["event_id"]
        if fusion_event_completed(event_id):
            return {"duplicate": True, "ais_ready": True, "fusion_state": None}

        ais_ready = wait_for_ais(event, wait_ms=wait_ms)
        ais_rows = build_causal_ais_rows(event["sensor_timestamp"])
        if ais_rows:
            result = matcher.predict_tables(
                pd.DataFrame(ais_rows),
                pd.DataFrame(event.get("radar_rows") or []),
            )
        else:
            result = self._radar_only_result(event)
        # Carry the Kafka-derived event id into the detector state. If this
        # stream entry is retried before completion, confirmation stays on the
        # same frame instead of incorrectly incrementing its streak.
        result["fusion_event_id"] = event_id
        fusion_state = publish_fusion_result(result, raise_on_cache_error=True)
        self._send_fusion_state(channel_layer, fusion_state)
        # Run both association-based detectors before acknowledging the event;
        # a latest-value polling worker could otherwise miss fast Radar frames.
        publish_fusion_detection_results(
            fusion_state,
            channel_layer=channel_layer,
            strict=True,
        )
        # Mark completion before XACK. A crash between these operations causes
        # a cheap duplicate ACK after restart rather than duplicate alerts.
        mark_fusion_event_completed(event_id)
        return {
            "duplicate": False,
            "ais_ready": ais_ready,
            "fusion_state": fusion_state,
        }

    def handle(self, *args, **options):
        config = self._options(options)
        channel_layer = get_channel_layer()
        if channel_layer is None:
            raise CommandError("Django Channels layer is not configured")
        connection = get_redis_connection("default")
        ensure_coordinator_group(connection, config)
        matcher = self._matcher(config)

        self.stdout.write(
            self.style.SUCCESS(
                "AIS/Radar fusion coordinator started: "
                f"stream={config['stream_key']}, group={config['group_name']}, "
                f"consumer={config['consumer_name']}, wait={config['wait_ms']}ms"
            )
        )
        try:
            while True:
                try:
                    events = read_coordinator_events(connection, config)
                    for message_id, event in events:
                        if fusion_event_completed(event["event_id"]):
                            acknowledge_coordinator_event(
                                connection,
                                message_id,
                                config,
                            )
                            continue
                        outcome = self.process_event(
                            event,
                            matcher,
                            channel_layer,
                            wait_ms=int(config["wait_ms"]),
                        )
                        acknowledge_coordinator_event(
                            connection,
                            message_id,
                            config,
                        )
                        fusion_state = outcome.get("fusion_state") or {}
                        self.stdout.write(
                            "Fusion event completed: "
                            f"event={event['event_id']}, "
                            f"time={event['sensor_timestamp']}, "
                            f"ais_ready={outcome['ais_ready']}, "
                            f"matches={fusion_state.get('count', 0)}"
                        )
                except Exception as exc:
                    self.stderr.write(
                        self.style.ERROR(
                            "Fusion coordinator error; event left pending: "
                            f"{exc}"
                        )
                    )
                    time.sleep(
                        max(0.1, float(config.get("error_retry_seconds", 1.0)))
                    )
        except KeyboardInterrupt:
            self.stdout.write(
                self.style.WARNING("AIS/Radar fusion coordinator stopped")
            )
