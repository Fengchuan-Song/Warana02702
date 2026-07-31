import shutil
import subprocess
import time
import uuid
from pathlib import Path

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError

from home.consumers import camera_stream_group_name


VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".mkv",
    ".avi",
    ".webm",
    ".m4v",
}
JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"
PIPE_READ_SIZE = 64 * 1024
MAX_FRAME_BUFFER_SIZE = 8 * 1024 * 1024
WORKER_LOCK_TIMEOUT = 30
WORKER_LOCK_REFRESH_INTERVAL = 5


def iter_mjpeg_frames(stream):
    """Yield complete JPEG images from an FFmpeg image2pipe stream."""
    buffer = bytearray()
    while True:
        chunk = stream.read(PIPE_READ_SIZE)
        if not chunk:
            break
        buffer.extend(chunk)

        while True:
            start = buffer.find(JPEG_START)
            if start < 0:
                if len(buffer) > MAX_FRAME_BUFFER_SIZE:
                    buffer.clear()
                break
            end = buffer.find(JPEG_END, start + len(JPEG_START))
            if end < 0:
                if start:
                    del buffer[:start]
                if len(buffer) > MAX_FRAME_BUFFER_SIZE:
                    buffer.clear()
                break

            end += len(JPEG_END)
            yield bytes(buffer[start:end])
            del buffer[:end]


class Command(BaseCommand):
    help = (
        "Continuously reads every video in Data/Video and broadcasts live "
        "JPEG frames through the camera WebSocket group."
    )

    def add_arguments(self, parser):
        config = getattr(settings, "CAMERA_STREAM", {})
        parser.add_argument(
            "--camera-key",
            default=config.get("camera_key", "harbor-01"),
        )
        parser.add_argument(
            "--video-dir",
            default=str(
                config.get(
                    "video_directory",
                    Path(settings.BASE_DIR) / "Data" / "Video",
                )
            ),
        )
        parser.add_argument(
            "--fps",
            type=float,
            default=float(config.get("fps", 0)),
            help=(
                "输出帧率。设为 0 时保持每个源视频的原始帧率，"
                "大于 0 时统一转换为指定帧率。"
            ),
        )
        parser.add_argument(
            "--max-width",
            type=int,
            default=int(config.get("max_width", 1280)),
        )
        parser.add_argument(
            "--jpeg-quality",
            type=int,
            default=int(config.get("jpeg_quality", 5)),
        )
        parser.add_argument(
            "--ffmpeg",
            default=config.get("ffmpeg_executable", ""),
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="Process the current video list once, then exit.",
        )
        parser.add_argument(
            "--max-frames",
            type=int,
            default=0,
            help="Stop after this many frames; useful for a smoke test.",
        )

    def handle(self, *args, **options):
        video_directory = Path(options["video_dir"]).expanduser().resolve()
        if not video_directory.is_dir():
            raise CommandError(f"视频目录不存在：{video_directory}")
        if options["fps"] < 0 or options["fps"] > 60:
            raise CommandError("--fps 必须为 0 到 60；0 表示保持源视频帧率")
        if options["max_width"] < 160:
            raise CommandError("--max-width 不能小于 160")
        if not 2 <= options["jpeg_quality"] <= 31:
            raise CommandError("--jpeg-quality 必须在 2 到 31 之间")

        ffmpeg = options["ffmpeg"] or shutil.which("ffmpeg")
        if not ffmpeg:
            raise CommandError(
                "未找到 FFmpeg。请安装 FFmpeg，或通过 --ffmpeg 指定路径。"
            )

        self.camera_key = options["camera_key"]
        self.group_name = camera_stream_group_name(self.camera_key)
        self.channel_layer = get_channel_layer()
        self.fps = options["fps"]
        self.max_width = options["max_width"]
        self.jpeg_quality = options["jpeg_quality"]
        self.ffmpeg = ffmpeg
        self.max_frames = max(0, options["max_frames"])
        self.frames_sent = 0
        self.lock_key = f"camera_stream:worker_lock:{self.camera_key}"
        self.lock_token = uuid.uuid4().hex
        self.last_lock_refresh = time.monotonic()

        if not cache.add(
            self.lock_key,
            self.lock_token,
            timeout=WORKER_LOCK_TIMEOUT,
        ):
            raise CommandError(
                f"摄像头 {self.camera_key} 已有推流进程运行，不能重复启动。"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"摄像头推流进程已启动：camera={self.camera_key}, "
                f"directory={video_directory}, "
                f"fps={'source' if self.fps == 0 else f'{self.fps:g}'}"
            )
        )

        try:
            while True:
                self._refresh_worker_lock()
                videos = self._find_videos(video_directory)
                if not videos:
                    self.stdout.write(
                        self.style.WARNING(
                            f"目录中没有支持的视频，5 秒后重新扫描：{video_directory}"
                        )
                    )
                    time.sleep(5)
                    if options["once"]:
                        return
                    continue

                for video_path in videos:
                    self._broadcast_status("playing", video_path.name)
                    self._stream_video(video_path)
                    if self.max_frames and self.frames_sent >= self.max_frames:
                        return

                if options["once"]:
                    return
                self.stdout.write("全部视频播放完毕，从第一个视频重新循环。")
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("摄像头推流进程已停止。"))
        finally:
            self._broadcast_status("stopped")
            self._release_worker_lock()

    @staticmethod
    def _find_videos(video_directory):
        return sorted(
            (
                path
                for path in video_directory.iterdir()
                if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
            ),
            key=lambda path: path.name.lower(),
        )

    def _stream_video(self, video_path):
        self.stdout.write(f"开始推送：{video_path.name}")
        video_filters = [
            (
                f"scale={self.max_width}:-2:"
                "force_original_aspect_ratio=decrease"
            )
        ]
        if self.fps > 0:
            video_filters.insert(0, f"fps={self.fps:g}")

        command = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-re",
            "-i",
            str(video_path),
            "-an",
            "-vf",
            ",".join(video_filters),
            "-q:v",
            str(self.jpeg_quality),
            "-c:v",
            "mjpeg",
            "-f",
            "image2pipe",
            "pipe:1",
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        try:
            for frame in iter_mjpeg_frames(process.stdout):
                self._refresh_worker_lock()
                async_to_sync(self.channel_layer.group_send)(
                    self.group_name,
                    {
                        "type": "camera.frame",
                        "frame": frame,
                    },
                )
                self.frames_sent += 1
                if self.max_frames and self.frames_sent >= self.max_frames:
                    process.terminate()
                    break
        finally:
            if process.stdout:
                process.stdout.close()
            try:
                return_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                return_code = process.wait(timeout=5)

            error_output = (
                process.stderr.read().decode("utf-8", errors="replace").strip()
                if process.stderr
                else ""
            )
            if process.stderr:
                process.stderr.close()

        if return_code not in (0, -15) and not (
            self.max_frames and self.frames_sent >= self.max_frames
        ):
            raise CommandError(
                f"FFmpeg 处理 {video_path.name} 失败"
                + (f"：{error_output}" if error_output else "")
            )
        self.stdout.write(f"完成推送：{video_path.name}")

    def _refresh_worker_lock(self):
        now = time.monotonic()
        if now - self.last_lock_refresh < WORKER_LOCK_REFRESH_INTERVAL:
            return
        if cache.get(self.lock_key) != self.lock_token:
            raise CommandError("摄像头推流进程锁已失效，进程将停止。")
        cache.touch(self.lock_key, timeout=WORKER_LOCK_TIMEOUT)
        self.last_lock_refresh = now

    def _release_worker_lock(self):
        if cache.get(self.lock_key) == self.lock_token:
            cache.delete(self.lock_key)

    def _broadcast_status(self, status, video_name=""):
        try:
            async_to_sync(self.channel_layer.group_send)(
                self.group_name,
                {
                    "type": "camera.status",
                    "status": status,
                    "video_name": video_name,
                },
            )
        except Exception as exc:
            self.stderr.write(f"推送状态失败：{exc}")
