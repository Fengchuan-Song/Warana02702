"""Run one AIS/Radar-fusion detector as an independent worker."""

import time

from channels.layers import get_channel_layer
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from AISRadar.anomaly_detection import (
    FUSION_DETECTORS,
    publish_fusion_detection_result,
)
from AISRadar.fusion_state import get_fusion_state


class Command(BaseCommand):
    help = "Watches JPDA fusion state and runs one CloseAIS/Forgery detector."

    def add_arguments(self, parser):
        parser.add_argument(
            "--detector",
            required=True,
            choices=sorted(FUSION_DETECTORS),
        )
        parser.add_argument(
            "--poll-interval",
            type=float,
            default=0.5,
            help="Seconds between fusion-state checks (default: 0.5).",
        )

    @staticmethod
    def _state_marker(state):
        return state.get("updated_at") or (
            state.get("source_end_time"),
            state.get("count"),
        )

    def handle(self, *args, **options):
        feature_id = options["detector"]
        poll_interval = float(options["poll_interval"])
        if poll_interval <= 0:
            raise CommandError("--poll-interval must be positive")

        channel_layer = get_channel_layer()
        # Treat the state present at startup as history.  Only snapshots
        # published after this worker starts should create fresh alerts.
        last_marker = self._state_marker(get_fusion_state())
        self.stdout.write(
            self.style.SUCCESS(
                f"Fusion detection worker started: {feature_id}"
            )
        )

        try:
            while True:
                try:
                    state = get_fusion_state()
                    marker = self._state_marker(state)
                    if state.get("available") and marker != last_marker:
                        close_old_connections()
                        payload = publish_fusion_detection_result(
                            feature_id,
                            state,
                            channel_layer=channel_layer,
                        )
                        self.stdout.write(
                            f"{feature_id}: time={state.get('source_end_time')} "
                            f"alerts={payload.get('count', 0)}"
                        )
                    last_marker = marker
                except Exception as exc:
                    close_old_connections()
                    self.stderr.write(
                        self.style.ERROR(
                            f"{feature_id} fusion worker error: {exc}"
                        )
                    )
                time.sleep(poll_interval)
        except KeyboardInterrupt:
            self.stdout.write(
                self.style.WARNING(
                    f"Fusion detection worker stopped: {feature_id}"
                )
            )
