import json

from channels.generic.websocket import AsyncWebsocketConsumer


def camera_stream_group_name(camera_key):
    return f"camera_stream_{camera_key}"


class CameraStreamConsumer(AsyncWebsocketConsumer):
    """Forward live JPEG frames from the camera worker to a browser."""

    async def connect(self):
        self.camera_key = self.scope["url_route"]["kwargs"]["camera_key"]
        self.group_name = camera_stream_group_name(self.camera_key)
        await self.channel_layer.group_add(
            self.group_name,
            self.channel_name,
        )
        await self.accept()
        await self.send(
            text_data=json.dumps(
                {
                    "type": "stream.connected",
                    "camera_key": self.camera_key,
                }
            )
        )

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(
            self.group_name,
            self.channel_name,
        )

    async def camera_frame(self, event):
        await self.send(bytes_data=event["frame"])

    async def camera_status(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "stream.status",
                    "camera_key": self.camera_key,
                    "status": event["status"],
                    "video_name": event.get("video_name", ""),
                }
            )
        )
