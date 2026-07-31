# 船舶超载视频识别

本模块迁移自：

`E:\Illegal_Prediction\CollisionGuard.overload\Overload`

迁移内容包括定制 YOLO 推理逻辑、Ultralytics 8.2.90 运行代码、
`weights/best.pt` 载重线模型和 `weights/yolov8n.pt` 船舶存在性模型。
训练集、训练输出、文档、IDE 文件和缓存未迁入。

## 数据流

1. `camera_stream_worker` 按源视频 FPS 循环读取 `Data/Video`。
2. 视频帧通过 Channels/Redis 广播到 `camera_stream_<camera-key>`。
3. `overload_stream_worker` 订阅同一频道，并按配置的推理 FPS 抽帧。
4. 连续达到确认帧数后，结果写入 `detect-overload` 缓存。
5. 结果通过现有 AIS WebSocket 推送到“超载船舶检测”界面。

视频推流和模型识别是两个独立进程；前端是否打开摄像头不会影响任何进程。

## 启动

先启动视频流：

```powershell
python manage.py camera_stream_worker --camera-key harbor-01
```

再启动超载识别：

```powershell
python manage.py overload_stream_worker --camera-key harbor-01
```

两个命令的 `camera-key` 必须相同。模型启动时会先进行一次 GPU 预热。

## 识别规则

- 未检测到船舶：正常，不触发超载预警。
- 检测到船舶但未检测到载重线：疑似超载。
- 检测到类别 `light load`（类别 0）：未超载。
- 检测到其他类别（当前权重为 `full load`）：疑似超载。
- 默认连续 3 个抽样帧判断为超载后才正式预警。

运行参数位于 `WanAna02702/settings.py` 的
`OVERLOAD_VIDEO_DETECTION`。
