import json
from channels.generic.websocket import AsyncWebsocketConsumer

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
        ais_data = event['text']

        # 通过 WebSocket 发送数据到客户端
        await self.send(text_data=json.dumps({
            'type': 'ais_update',
            'data': ais_data
        }))

    # 4. (可选) 接收客户端消息时 (此场景用不到)
    # async def receive(self, text_data):
    #     pass