"""Consume replayed sensor frames and run causal JPDA fusion independently."""

import time

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.management.base import BaseCommand, CommandError
import pandas as pd

from AISData.consumers import AisConsumer
from AISRadar.fusion_input import get_fusion_input
from AISRadar.fusion_state import (
    clear_fusion_state,
    get_fusion_state,
    publish_fusion_result,
)
from AISRadar.inference.data import DataValidationError
from AISRadar.inference.predictor import get_matcher
from AISRadar.views import _config


class Command(BaseCommand):
    help = "Consumes AIS/Radar replay frames and publishes method_JPDA results."

    def add_arguments(self, parser):
        parser.add_argument(
            "--poll-interval",
            type=float,
            default=0.1,
            help="Seconds between replay-input checks (default: 0.1).",
        )

    @staticmethod
    def _send_fusion_state(channel_layer, fusion_state):
        async_to_sync(channel_layer.group_send)(
            AisConsumer.AIS_RADAR_GROUP_NAME,
            {"type": "send_ais_radar_fusion_update", "data": fusion_state},
        )

    def handle(self, *args, **options):
        poll_interval = float(options["poll_interval"])
        if poll_interval <= 0:
            raise CommandError("--poll-interval must be positive")

        channel_layer = get_channel_layer()
        if channel_layer is None:
            raise CommandError("Django Channels layer is not configured")
        matcher = get_matcher(_config())
        self.stdout.write(
            self.style.SUCCESS("Independent JPDA fusion worker started")
        )

        last_marker = None
        last_run_id = None
        try:
            while True:
                state = get_fusion_input()
                marker = state.get("updated_at")
                if marker and marker != last_marker:
                    run_id = state.get("run_id")
                    if state.get("reset") or run_id != last_run_id:
                        clear_fusion_state()
                        self._send_fusion_state(
                            channel_layer,
                            get_fusion_state(),
                        )
                    last_run_id = run_id
                    last_marker = marker

                    if state.get("available") and state.get("radar_event"):
                        try:
                            result = matcher.predict_tables(
                                pd.DataFrame(state.get("ais_rows") or []),
                                pd.DataFrame(state.get("radar_rows") or []),
                            )
                            fusion_state = publish_fusion_result(result)
                            self._send_fusion_state(channel_layer, fusion_state)
                            self.stdout.write(
                                "JPDA: "
                                f"scene={state.get('scene_id')} "
                                f"time={state.get('sensor_timestamp')} "
                                f"matches={fusion_state.get('count', 0)}"
                            )
                        except DataValidationError as exc:
                            self.stderr.write(
                                self.style.WARNING(
                                    f"JPDA frame skipped: {exc}"
                                )
                            )
                        except Exception as exc:
                            self.stderr.write(
                                self.style.ERROR(
                                    f"JPDA fusion worker error: {exc}"
                                )
                            )
                time.sleep(poll_interval)
        except KeyboardInterrupt:
            self.stdout.write(
                self.style.WARNING("Independent JPDA fusion worker stopped")
            )
