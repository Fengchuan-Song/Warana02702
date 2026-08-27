import asyncio
import time
import uuid

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django.utils import timezone

from AISData.consumers import ViolationConsumer
from AISData.detection import annotate_new_results, detection_cache_key
from AISData.violation_records import persist_detection_payload
from home.consumers import camera_stream_group_name
from home.models import CameraConfiguration


FEATURE_ID = "detect-overload"
LOCK_TIMEOUT = 30
LOCK_REFRESH_INTERVAL = 5


async def receive_event_with_timeout(channel_layer, channel_name, timeout=5):
    try:
        return await asyncio.wait_for(
            channel_layer.receive(channel_name),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return None


def publish_overload_detection(payload, channel_layer):
    """Annotate, cache, persist, and broadcast one overload result."""
    cache_key = detection_cache_key(FEATURE_ID)
    previous_payload = cache.get(cache_key)
    annotate_new_results(FEATURE_ID, payload, previous_payload)
    cache.set(
        cache_key,
        payload,
        timeout=getattr(settings, "CACHE_TTL", 300),
    )
    persist_detection_payload(FEATURE_ID, payload)
    async_to_sync(channel_layer.group_send)(
        ViolationConsumer.GROUP_NAME,
        {
            "type": "send_detection_update",
            "results": {FEATURE_ID: payload},
        },
    )
    return payload


class Command(BaseCommand):
    help = (
        "Consumes the simulated camera stream, runs the migrated overload "
        "model, and publishes detection results to the existing warning UI."
    )

    def add_arguments(self, parser):
        config = getattr(settings, "OVERLOAD_VIDEO_DETECTION", {})
        parser.add_argument(
            "--camera-key",
            default=config.get("camera_key", "harbor-01"),
        )
        parser.add_argument(
            "--weights",
            default=str(config.get("weights", "")),
        )
        parser.add_argument(
            "--confidence",
            type=float,
            default=float(config.get("confidence", 0.5)),
        )
        parser.add_argument(
            "--vessel-weights",
            default=str(config.get("vessel_weights", "")),
        )
        parser.add_argument(
            "--vessel-confidence",
            type=float,
            default=float(config.get("vessel_confidence", 0.25)),
        )
        parser.add_argument(
            "--inference-fps",
            type=float,
            default=float(config.get("inference_fps", 2)),
            help="Maximum model inference rate; stream input remains native FPS.",
        )
        parser.add_argument(
            "--confirmation-frames",
            type=int,
            default=int(config.get("confirmation_frames", 3)),
        )
        parser.add_argument(
            "--device",
            default=str(config.get("device", "0")),
            help="Ultralytics device, for example 0 or cpu.",
        )
        parser.add_argument(
            "--max-inferences",
            type=int,
            default=0,
            help="Stop after N inferences; useful for smoke tests.",
        )

    def handle(self, *args, **options):
        if options["inference_fps"] <= 0:
            raise CommandError("--inference-fps 必须大于 0")
        if options["confirmation_frames"] <= 0:
            raise CommandError("--confirmation-frames 必须大于 0")

        from Overload.inference import OverloadDetector

        camera_key = options["camera_key"]
        camera_name = self._get_camera_name(camera_key)
        group_name = camera_stream_group_name(camera_key)
        channel_layer = get_channel_layer()
        channel_name = async_to_sync(channel_layer.new_channel)(
            "overload.video."
        )
        lock_key = f"overload:stream_worker_lock:{camera_key}"
        lock_token = uuid.uuid4().hex
        if not cache.add(lock_key, lock_token, timeout=LOCK_TIMEOUT):
            raise CommandError(
                f"摄像头 {camera_key} 已有超载识别进程运行。"
            )

        try:
            detector = OverloadDetector(
                weights=options["weights"] or None,
                confidence=options["confidence"],
                device=options["device"],
                vessel_weights=options["vessel_weights"] or None,
                vessel_confidence=options["vessel_confidence"],
            )
            self.stdout.write("正在预热船舶及超载识别模型……")
            detector.warmup()
        except Exception:
            if cache.get(lock_key) == lock_token:
                cache.delete(lock_key)
            raise
        async_to_sync(channel_layer.group_add)(group_name, channel_name)

        minimum_interval = 1 / options["inference_fps"]
        last_inference_at = 0.0
        last_lock_refresh = time.monotonic()
        consecutive_overload_frames = 0
        inference_count = 0

        self.stdout.write(
            self.style.SUCCESS(
                f"超载视频识别进程已启动：camera={camera_key}, "
                f"inference_fps={options['inference_fps']:g}, "
                f"device={options['device']}"
            )
        )

        try:
            while True:
                event = async_to_sync(receive_event_with_timeout)(
                    channel_layer,
                    channel_name,
                    timeout=LOCK_REFRESH_INTERVAL,
                )
                now = time.monotonic()
                if now - last_lock_refresh >= LOCK_REFRESH_INTERVAL:
                    if cache.get(lock_key) != lock_token:
                        raise CommandError("超载识别进程锁已失效。")
                    cache.touch(lock_key, timeout=LOCK_TIMEOUT)
                    last_lock_refresh = now

                if event is None:
                    continue
                if event.get("type") != "camera.frame":
                    continue

                if now - last_inference_at < minimum_interval:
                    continue
                last_inference_at = now

                inference = detector.predict_jpeg(event["frame"])
                if inference.overloaded:
                    consecutive_overload_frames += 1
                else:
                    consecutive_overload_frames = 0
                confirmed = (
                    inference.overloaded
                    and consecutive_overload_frames
                    >= options["confirmation_frames"]
                )

                payload = self._build_payload(
                    camera_key=camera_key,
                    camera_name=camera_name,
                    inference=inference,
                    confirmed=confirmed,
                    consecutive_frames=consecutive_overload_frames,
                )
                publish_overload_detection(payload, channel_layer)
                inference_count += 1
                self.stdout.write(
                    f"{camera_key}: {inference.reason}; "
                    f"confirmed={confirmed}; "
                    f"confidence={inference.confidence:.3f}"
                )

                if (
                    options["max_inferences"] > 0
                    and inference_count >= options["max_inferences"]
                ):
                    return
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("超载视频识别进程已停止。"))
        finally:
            async_to_sync(channel_layer.group_discard)(
                group_name,
                channel_name,
            )
            if cache.get(lock_key) == lock_token:
                cache.delete(lock_key)

    @staticmethod
    def _get_camera_name(camera_key):
        close_old_connections()
        try:
            return (
                CameraConfiguration.objects.filter(
                    camera_key=camera_key
                ).values_list("name", flat=True).first()
                or camera_key
            )
        finally:
            close_old_connections()

    @staticmethod
    def _build_payload(
        camera_key,
        camera_name,
        inference,
        confirmed,
        consecutive_frames,
    ):
        now = timezone.now().isoformat()
        result = {
            "name": camera_name,
            "mmsi": "",
            "status": "超载预警" if confirmed else "正常",
            "risk": "高风险" if confirmed else "正常",
            "details": inference.reason,
            "camera_key": camera_key,
            "confidence": round(inference.confidence, 6),
            "consecutive_frames": consecutive_frames,
            **inference.as_dict(),
        }
        return {
            "success": True,
            "feature_id": FEATURE_ID,
            "timestamp": now,
            "computed_at": now,
            "count": 1 if confirmed else 0,
            "results": [result] if confirmed else [],
            "latest": result,
            "message": inference.reason,
        }
