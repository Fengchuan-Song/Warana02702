"""Replay historical AIS as an isolated, realistic local receiver stream."""

import time
import uuid
from pathlib import Path

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from AISData.ais_simulation import (
    SimulationConfig,
    iter_simulated_records,
    load_ais_csv,
)
from AISData.ais_state import clear_ais_state, merge_ais_state
from AISData.consumers import AisConsumer, PredictAisConsumer
from AISData.detection_queue import enqueue_all_detections
from AISData.trajectory_history import append_ais_history, clear_ais_history


class Command(BaseCommand):
    help = (
        "Simulate observed, nominal broadcast, or imperfectly received AIS "
        "from a historical CSV. The default predict namespace is isolated."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Source AIS CSV path.")
        parser.add_argument(
            "--mode",
            choices=("observed", "broadcast", "received"),
            default="received",
        )
        parser.add_argument("--speed-factor", type=float, default=10.0)
        parser.add_argument(
            "--no-wait",
            action="store_true",
            help="Generate and publish without wall-clock sleeps.",
        )
        parser.add_argument("--max-ships", type=int, default=200)
        parser.add_argument("--mmsi", action="append", dest="mmsis")
        parser.add_argument("--start", dest="start_at")
        parser.add_argument("--end", dest="end_at")
        parser.add_argument("--duration-minutes", type=float, default=60.0)
        parser.add_argument("--source-timezone", default="UTC")
        parser.add_argument("--batch-ms", type=float, default=1000.0)
        parser.add_argument("--batch-size", type=int, default=1000)
        parser.add_argument(
            "--max-events",
            type=int,
            default=0,
            help="Stop after this many delivered records; 0 means unlimited.",
        )
        parser.add_argument("--seed", type=int, default=42)
        parser.add_argument("--loss-rate", type=float, default=0.05)
        parser.add_argument("--duplicate-rate", type=float, default=0.005)
        parser.add_argument("--out-of-order-rate", type=float, default=0.01)
        parser.add_argument("--minimum-latency-ms", type=float, default=50.0)
        parser.add_argument("--maximum-latency-ms", type=float, default=500.0)
        parser.add_argument("--out-of-order-extra-ms", type=float, default=3000.0)
        parser.add_argument("--outage-rate-per-hour", type=float, default=0.02)
        parser.add_argument("--outage-minimum-seconds", type=float, default=30.0)
        parser.add_argument("--outage-maximum-seconds", type=float, default=300.0)
        parser.add_argument("--gps-noise-metres", type=float, default=5.0)
        parser.add_argument("--max-segment-gap-seconds", type=float, default=1800.0)
        parser.add_argument("--max-segment-speed-knots", type=float, default=80.0)
        parser.add_argument(
            "--namespace",
            choices=("predict", "operational"),
            default="predict",
        )
        parser.add_argument(
            "--allow-operational-write",
            action="store_true",
            help="Required when namespace=operational.",
        )
        parser.add_argument(
            "--keep-state",
            action="store_true",
            help="Do not clear the selected namespace before replay.",
        )
        parser.add_argument(
            "--enqueue-detections",
            action="store_true",
            help="Queue existing detectors; allowed only for operational namespace.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Generate records and statistics without cache or WebSocket writes.",
        )
        parser.add_argument("--simulation-id", default="")

    def handle(self, *args, **options):
        command_started = time.perf_counter()
        self._validate_options(options)
        source_path = Path(options["file"]).resolve()
        namespace = None if options["namespace"] == "operational" else "predict"
        simulation_id = options["simulation_id"].strip() or str(uuid.uuid4())

        try:
            config = SimulationConfig(
                mode=options["mode"],
                max_segment_gap_seconds=options["max_segment_gap_seconds"],
                max_segment_speed_knots=options["max_segment_speed_knots"],
                gps_noise_metres=options["gps_noise_metres"],
                loss_rate=options["loss_rate"],
                duplicate_rate=options["duplicate_rate"],
                out_of_order_rate=options["out_of_order_rate"],
                minimum_latency_ms=options["minimum_latency_ms"],
                maximum_latency_ms=options["maximum_latency_ms"],
                out_of_order_extra_ms=options["out_of_order_extra_ms"],
                outage_rate_per_hour=options["outage_rate_per_hour"],
                outage_minimum_seconds=options["outage_minimum_seconds"],
                outage_maximum_seconds=options["outage_maximum_seconds"],
            )
            loaded = load_ais_csv(
                source_path,
                max_ships=options["max_ships"],
                mmsis=options["mmsis"],
                start_at=options["start_at"],
                end_at=options["end_at"],
                duration_minutes=options["duration_minutes"],
                source_timezone=options["source_timezone"],
            )
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        if not loaded.tracks:
            raise CommandError("No usable dynamic AIS tracks matched the selection")

        self.stdout.write(
            self.style.SUCCESS(
                "AIS simulation loaded: "
                f"mode={config.mode}, ships={len(loaded.tracks)}, "
                f"source_rows={loaded.source_rows}, "
                f"invalid_rows={loaded.invalid_rows}, "
                f"namespace={options['namespace']}, id={simulation_id}"
            )
        )

        channel_layer = None if options["dry_run"] else get_channel_layer()
        if not options["dry_run"] and channel_layer is None:
            raise CommandError("Django Channels layer is not configured")
        group_name = (
            AisConsumer.AIS_GROUP_NAME
            if namespace is None
            else PredictAisConsumer.AIS_GROUP_NAME
        )
        if not options["dry_run"] and not options["keep_state"]:
            reset_version = clear_ais_state(
                namespace,
                preserve_version=True,
            )
            clear_ais_history(loaded.selected_mmsis, namespace)
            async_to_sync(channel_layer.group_send)(
                group_name,
                {
                    "type": "send_ais_snapshot",
                    "data": [],
                    "version": reset_version,
                },
            )

        delivered = batches = upserts = removes = 0
        simulated_duplicates = state_duplicates = out_of_order = 0
        iterator = iter_simulated_records(loaded, config, seed=options["seed"])
        batch = []
        origin_delivery = None
        batch_started = None
        wall_started = time.monotonic()
        source_window_seconds = (
            options["batch_ms"] / 1000.0 * options["speed_factor"]
        )

        for scheduled in iterator:
            if origin_delivery is None:
                origin_delivery = scheduled.delivery_time
            if batch_started is None:
                batch_started = scheduled.delivery_time
            should_flush = batch and (
                len(batch) >= options["batch_size"]
                or (
                    scheduled.delivery_time - batch_started
                ).total_seconds() > source_window_seconds
            )
            if should_flush:
                result = self._publish_batch(
                    batch,
                    namespace,
                    simulation_id,
                    channel_layer,
                    group_name,
                    origin_delivery,
                    wall_started,
                    options,
                )
                delivered += len(batch)
                batches += 1
                upserts += result["upserts"]
                removes += result["removes"]
                simulated_duplicates += result["simulated_duplicates"]
                state_duplicates += result["state_duplicates"]
                out_of_order += result["out_of_order"]
                batch = []
                batch_started = scheduled.delivery_time
                if options["max_events"] and delivered >= options["max_events"]:
                    break
            batch.append(scheduled)
            if options["max_events"] and delivered + len(batch) >= options["max_events"]:
                break

        if batch:
            if options["max_events"]:
                remaining = max(0, options["max_events"] - delivered)
                batch = batch[:remaining]
            if batch:
                result = self._publish_batch(
                    batch,
                    namespace,
                    simulation_id,
                    channel_layer,
                    group_name,
                    origin_delivery,
                    wall_started,
                    options,
                )
                delivered += len(batch)
                batches += 1
                upserts += result["upserts"]
                removes += result["removes"]
                simulated_duplicates += result["simulated_duplicates"]
                state_duplicates += result["state_duplicates"]
                out_of_order += result["out_of_order"]

        elapsed = max(0.001, time.perf_counter() - command_started)
        self.stdout.write(
            self.style.SUCCESS(
                "AIS simulation complete: "
                f"delivered={delivered}, batches={batches}, "
                f"upserts={upserts}, removes={removes}, "
                f"simulated_duplicates={simulated_duplicates}, "
                f"state_duplicates={state_duplicates}, "
                f"out_of_order={out_of_order}, "
                f"wall_seconds={elapsed:.3f}, rate={delivered / elapsed:.1f}/s, "
                f"id={simulation_id}"
            )
        )

    def _publish_batch(
        self,
        scheduled_records,
        namespace,
        simulation_id,
        channel_layer,
        group_name,
        origin_delivery,
        wall_started,
        options,
    ):
        if not options["no_wait"] and not options["dry_run"]:
            target_elapsed = (
                scheduled_records[0].delivery_time - origin_delivery
            ).total_seconds() / options["speed_factor"]
            wait_seconds = target_elapsed - (time.monotonic() - wall_started)
            if wait_seconds > 0:
                time.sleep(wait_seconds)

        dynamic = []
        static = []
        received_at = timezone.now().isoformat()
        duplicate_count = 0
        for scheduled in scheduled_records:
            item = scheduled.record.copy()
            item["received_at"] = received_at
            item["simulation_delivery_time"] = scheduled.delivery_time.isoformat()
            item["simulation_id"] = simulation_id
            if item.get("simulation_duplicate"):
                duplicate_count += 1
            if scheduled.kind == "static":
                static.append(item)
            else:
                dynamic.append(item)

        if options["dry_run"]:
            return {
                "upserts": 0,
                "removes": 0,
                "simulated_duplicates": duplicate_count,
                "state_duplicates": 0,
                "out_of_order": 0,
            }

        append_ais_history(dynamic, namespace=namespace)
        state_update = merge_ais_state(
            dynamic_updates=dynamic,
            static_updates=static,
            namespace=namespace,
        )
        delta = state_update.delta()
        if delta["upserts"] or delta["removes"]:
            async_to_sync(channel_layer.group_send)(
                group_name,
                {"type": "send_ais_delta", "data": delta},
            )
        if options["enqueue_detections"]:
            enqueue_all_detections(ship_list=state_update.snapshot)
        return {
            "upserts": len(delta["upserts"]),
            "removes": len(delta["removes"]),
            "simulated_duplicates": duplicate_count,
            "state_duplicates": state_update.duplicate,
            "out_of_order": state_update.out_of_order,
        }

    @staticmethod
    def _validate_options(options):
        if options["namespace"] == "operational" and not options[
            "allow_operational_write"
        ]:
            raise CommandError(
                "namespace=operational requires --allow-operational-write"
            )
        if options["enqueue_detections"] and options["namespace"] != "operational":
            raise CommandError(
                "--enqueue-detections requires namespace=operational"
            )
        positive_fields = (
            "speed_factor",
            "max_ships",
            "batch_ms",
            "batch_size",
            "max_segment_gap_seconds",
            "max_segment_speed_knots",
        )
        for field in positive_fields:
            if options[field] <= 0:
                raise CommandError(f"--{field.replace('_', '-')} must be positive")
        if options["duration_minutes"] < 0:
            raise CommandError("--duration-minutes cannot be negative")
        if options["max_events"] < 0:
            raise CommandError("--max-events cannot be negative")
