import time
import csv
import os
from pathlib import Path
from django.core.management.base import BaseCommand
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from AISData.consumers import AisConsumer
from AISData.detection_queue import enqueue_all_detections
from AISData.ais_state import (
    file_signature,
    load_file_checkpoints,
    merge_ais_state,
    save_file_checkpoint,
)
from AISData.normalization import (
    ais_record_kind,
    normalise_dynamic_ais_record,
    normalise_static_ais_record,
)
from AISData.trajectory_history import append_ais_history
from WanAna02702.settings import BASE_DIR

AIS_FOLDER = os.path.join(BASE_DIR, 'Data/AIS/timeDivision_v2_30s')

class Command(BaseCommand):
    help = 'Starts the AIS data worker to monitor files and push updates via WebSocket.'

    # 【新增方法】用于将 CSV 行数据转换为前端所需的字典格式
    def convert_row(self, row):
        """Converts a CSV row (dict) into the format expected by the frontend."""
        return normalise_dynamic_ais_record(row)

    def convert_rows(self, rows):
        """Return the latest snapshot plus every valid observation for history."""
        latest, history_points, _static_updates = self.convert_batch(rows)
        return latest, history_points

    def convert_batch(self, rows):
        """Separate dynamic positions from type 5/24 static updates."""
        latest_by_mmsi = {}
        history_points = []
        static_updates = []
        for row in rows:
            if ais_record_kind(row) == "static":
                static_update = normalise_static_ais_record(row)
                if static_update is not None:
                    static_updates.append(static_update)
                continue
            converted_ship = self.convert_row(row)
            if not converted_ship or not converted_ship["mmsi"]:
                continue
            history_points.append(converted_ship)
            current = latest_by_mmsi.get(converted_ship["mmsi"])
            if (
                current is None
                or converted_ship["timestamp"] >= current["timestamp"]
            ):
                latest_by_mmsi[converted_ship["mmsi"]] = converted_ship
        return list(latest_by_mmsi.values()), history_points, static_updates


    def handle(self, *args, **options):
        channel_layer = get_channel_layer()
        self.stdout.write("Starting AIS data worker in continuous monitoring mode...")
        
        # 扫描间隔（秒）
        SCAN_INTERVAL = 60 
        
        # 强制等待 5 秒，确保 Channels Layer 订阅完成 (保留此修复)
        # self.stdout.write(self.style.WARNING("Waiting 5 seconds for Channels Layer setup..."))
        # time.sleep(5) 
        
        while True:
            try:
                # 1. 查找所有 CSV 文件并按文件名（时间）排序。消费进度
                # 存在共享缓存中，worker 重启后不会重新投递未修改文件。
                all_files = sorted([f for f in os.listdir(AIS_FOLDER) if f.endswith('.csv')])
                new_files_to_process = []
                checkpoints = load_file_checkpoints()
                
                # 2. 识别新增文件
                for f in all_files:
                    path = Path(AIS_FOLDER) / f
                    signature = file_signature(path)
                    if checkpoints.get(f) != signature:
                        new_files_to_process.append((f, signature))
                
                if not new_files_to_process:
                    self.stdout.write(f"No new files found. Checking again in {SCAN_INTERVAL} seconds...")
                    time.sleep(SCAN_INTERVAL)
                    continue
                
                self.stdout.write(self.style.NOTICE(f"Found {len(new_files_to_process)} new file(s) to process."))
                
                # 3. 遍历并处理新增文件
                for latest_file, signature in new_files_to_process:
                    file_path = os.path.join(AIS_FOLDER, latest_file)
                    self.stdout.write(f"Processing file: {latest_file}...")
                    
                    # 4. 读取 CSV 文件内容并解析
                    with open(file_path, 'r', encoding='utf-8') as f:
                        reader = csv.DictReader(f)
                        ais_data, ais_history_points, static_updates = (
                            self.convert_batch(reader)
                        )
                                
                    
                    if not ais_data and not static_updates:
                        self.stdout.write(self.style.WARNING(f"File {latest_file} is empty or invalid after parsing. Skipping."))
                        save_file_checkpoint(latest_file, signature)
                        continue

                    # 前端和检测器仍接收每艘船的最新状态；文件内全部有效
                    # 观测点另存入滚动历史，供违法事件补齐识别前航迹。
                    append_ais_history(ais_history_points)

                    state_update = merge_ais_state(
                        dynamic_updates=ais_data,
                        static_updates=static_updates,
                    )
                    ais_snapshot = state_update.snapshot
                    self.stdout.write(
                        self.style.SUCCESS(
                            "AIS state merged: "
                            f"accepted={state_update.accepted}, "
                            f"duplicate={state_update.duplicate}, "
                            f"out_of_order={state_update.out_of_order}, "
                            f"targets={len(ais_snapshot)}"
                        )
                    )
                        
                    # 5. 构造并发送消息到 Channels 群组
                    delta = state_update.delta()
                    if delta["upserts"] or delta["removes"]:
                        async_to_sync(channel_layer.group_send)(
                            AisConsumer.AIS_GROUP_NAME,
                            {
                                "type": "send_ais_delta",
                                "data": delta,
                            },
                        )

                    self.stdout.write(self.style.SUCCESS(f"Pushed update from: {latest_file} ({len(delta['upserts'])} upserts, {len(delta['removes'])} removes)"))

                    # 只投递轻量触发信号。独立 detection_worker 负责执行
                    # 算法，避免重型检测阻塞下一批 AIS 数据推送。
                    queue_status = enqueue_all_detections(
                        ship_list=ais_snapshot
                    )
                    queued_count = sum(queue_status.values())
                    self.stdout.write(
                        self.style.NOTICE(
                            "Backend detection triggers: "
                            f"{queued_count} queued, "
                            f"{len(queue_status) - queued_count} coalesced"
                        )
                    )
                    
                    # 6. 【关键操作】标记文件为已处理
                    save_file_checkpoint(latest_file, signature)
                    
                    # 【重要】每次推送后短暂等待，模拟数据间隔和避免 Redis 拥堵
                    time.sleep(SCAN_INTERVAL) 

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"AIS worker encountered a major error: {e}"))
                
            # 【重要】处理完一轮文件后，等待下一次扫描
            time.sleep(SCAN_INTERVAL)
