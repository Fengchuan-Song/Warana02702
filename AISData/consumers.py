import json
from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.core.cache import cache

from AISData.detection import get_cached_detection_results
from AISData.normalization import normalise_ais_snapshot


def load_initial_realtime_state():
    """Load the state a client may have missed before it connected."""
    return (
        normalise_ais_snapshot(cache.get('latest_ais_data_raw', [])),
        get_cached_detection_results(),
    )

class AisConsumer(AsyncWebsocketConsumer):
    # 实时 AIS 数据群组名
    AIS_GROUP_NAME = 'ais_updates'

    # 1. 建立连接时
    async def connect(self):
        # 接受连接
        await self.accept()
        
        # 将新连接加入到 AIS_GROUP_NAME 群组
        await self.channel_layer.group_add(
            self.AIS_GROUP_NAME,
            self.channel_name
        )

        # Redis/Channels groups only deliver future messages. Replay the
        # latest cached state so a refreshed or late client does not remain
        # stuck at "等待后端检测".
        ais_data, detection_results = await sync_to_async(
            load_initial_realtime_state,
            thread_sensitive=True,
        )()
        if ais_data:
            await self.send(text_data=json.dumps({
                'type': 'ais_update',
                'data': ais_data,
            }))
        if detection_results:
            await self.send(text_data=json.dumps({
                'type': 'detection_update',
                'data': detection_results,
            }))

        print(f"WebSocket connected and joined group: {self.channel_name}")

    # 2. 断开连接时
    async def disconnect(self, close_code):
        # 将连接从群组移除
        await self.channel_layer.group_discard(
            self.AIS_GROUP_NAME,
            self.channel_name
        )
        print(f"WebSocket disconnected and left group: {self.channel_name}")

    # 3. 接收群组消息时（由后台数据生产者调用）
    async def send_ais_update(self, event):
        # 从事件中提取 AIS 数据
        ais_data = normalise_ais_snapshot(event['text'])

        # 通过 WebSocket 发送数据到客户端
        await self.send(text_data=json.dumps({
            'type': 'ais_update',
            'data': ais_data
        }))

    async def send_detection_update(self, event):
        """Push results produced by the backend detection pipeline."""
        await self.send(text_data=json.dumps({
            'type': 'detection_update',
            'data': event['results']
        }))

    # 4. (可选) 接收客户端消息时 (此场景用不到)
    # async def receive(self, text_data):
    #     pass
