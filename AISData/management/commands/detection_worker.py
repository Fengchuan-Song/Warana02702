import time

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from AISData.consumers import ViolationConsumer
from AISData.detection import DETECTORS, run_detector
from AISData.detection_queue import wait_for_detection_trigger
from AISData.detection_source import (
    detection_snapshot_cache_key,
    detection_source_id,
    is_detection_source_active,
)


class Command(BaseCommand):
    help = (
        "Runs one detector in an independent process and pushes its results "
        "through WebSocket."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--detector",
            required=True,
            choices=sorted(DETECTORS),
            help="Feature id of the detector owned by this worker.",
        )
        parser.add_argument(
            "--namespace",
            choices=("operational", "predict"),
            default="operational",
        )
        parser.add_argument("--simulation-id", default="")

    def handle(self, *args, **options):
        feature_id = options["detector"]
        namespace = options["namespace"]
        simulation_id = options["simulation_id"].strip() or None
        if namespace == "predict" and simulation_id is None:
            raise CommandError(
                "--simulation-id is required for namespace=predict"
            )
        source_id = detection_source_id(namespace, simulation_id)
        channel_layer = get_channel_layer()
        self.stdout.write(
            self.style.SUCCESS(
                "Independent detection worker started: "
                f"{feature_id}, source={source_id}"
            )
        )

        try:
            while True:
                try:
                    trigger = wait_for_detection_trigger(
                        feature_id,
                        timeout=5,
                        namespace=namespace,
                        simulation_id=simulation_id,
                    )
                    if trigger is None:
                        continue
                    if not is_detection_source_active(
                        namespace,
                        simulation_id,
                    ):
                        continue

                    close_old_connections()
                    ship_list = (
                        trigger
                        if isinstance(trigger, list)
                        else cache.get(
                            detection_snapshot_cache_key(namespace),
                            [],
                        )
                    )
                    if not ship_list:
                        self.stdout.write(
                            self.style.WARNING(
                                f"{feature_id}: skipped empty AIS snapshot"
                            )
                        )
                        continue

                    started_at = time.monotonic()

                    def push_detection_result(_, payload):
                        async_to_sync(channel_layer.group_send)(
                            ViolationConsumer.GROUP_NAME,
                            {
                                "type": "send_detection_update",
                                "results": {feature_id: payload},
                            },
                        )

                    payload = run_detector(
                        feature_id,
                        ship_list=ship_list,
                        on_result=push_detection_result,
                        detection_context={
                            "namespace": namespace,
                            "simulation_id": simulation_id,
                            "source_id": source_id,
                        },
                    )
                    elapsed = time.monotonic() - started_at
                    status = "succeeded" if payload.get("success") else "failed"
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"{feature_id}: {status} in {elapsed:.2f}s"
                        )
                    )
                except Exception as exc:
                    close_old_connections()
                    self.stderr.write(
                        self.style.ERROR(
                            f"{feature_id} worker error: {exc}; "
                            "retrying in 5s"
                        )
                    )
                    time.sleep(5)
        except KeyboardInterrupt:
            self.stdout.write(
                self.style.WARNING(
                    f"Independent detection worker stopped: {feature_id}"
                )
            )
