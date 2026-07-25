import time
import csv
import math
import os
from django.core.management.base import BaseCommand
from django.conf import settings
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from AISData.consumers import AisConsumer
from AISData.detection_queue import enqueue_all_detections
from AISData.normalization import normalise_ais_name
from WanAna02702.settings import BASE_DIR
from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime

AIS_FOLDER = os.path.join(BASE_DIR, 'Data/AIS/timeDivision_v2_30s')

class Command(BaseCommand):
    help = 'Starts the AIS data worker to monitor files and push updates via WebSocket.'

    # 【新增方法】用于将 CSV 行数据转换为前端所需的字典格式
    def convert_row(self, row):
        """Converts a CSV row (dict) into the format expected by the frontend."""
        # 关键假设：CSV 文件中包含以下字段（或类似字段），请根据您的实际 CSV 头部调整键名。
        # 前端期望的键名: mmsi, name, lon, lat, course, speed
        try:
            def optional_float(*field_names):
                for field_name in field_names:
                    raw_value = row.get(field_name)
                    if raw_value in (None, ""):
                        continue
                    try:
                        value = float(raw_value)
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(value):
                        return value
                return None

            raw_timestamp = row.get('timestamp')
            timestamp = (
                parse_datetime(raw_timestamp)
                if isinstance(raw_timestamp, str)
                else raw_timestamp
            )
            if timestamp is None:
                raise ValueError(
                    f"Invalid AIS timestamp: {raw_timestamp!r}"
                )
            if timezone.is_naive(timestamp):
                timestamp = timezone.make_aware(
                    timestamp,
                    timezone.get_current_timezone(),
                )

            raw_status = row.get('status')
            try:
                nav_status = (
                    int(float(raw_status))
                    if raw_status not in (None, '')
                    else None
                )
            except (ValueError, TypeError):
                nav_status = None

            raw_at_dock = row.get('at_dock', False)
            if isinstance(raw_at_dock, bool):
                at_dock = raw_at_dock
            else:
                at_dock = str(raw_at_dock).strip().lower() in {
                    '1', 'true', 'yes', 'y', '是'
                }

            raw_port_name = (
                row.get('matchedPortName')
                or row.get('matched_port_name')
                or ''
            )
            matched_port_name = str(raw_port_name).strip()
            if matched_port_name.lower() in {'nan', 'none', 'null'}:
                matched_port_name = ''

            # 这里使用了 .get() 和 or 来尝试兼容常见的 CSV 头部字段名，并确保数值类型正确
            return {
                # 缓存和 WebSocket 中统一使用包含时区偏移的 ISO 8601 字符串，
                # 避免启用 USE_TZ 时各检测模型写入 naive datetime。
                'timestamp': timestamp.isoformat(),
                'mmsi': str(row.get('MMSI') or '').strip(),
                'name': normalise_ais_name(row.get('Name')),
                'lon': float(row.get('longitude')),
                'lat': float(row.get('latitude')),
                # COG/course (航向), SOG/speed (航速)
                'course': float(row.get('course')), 
                'speed': float(row.get('speed')),
                'heading': optional_float('heading'),
                'imo': str(row.get('IMO') or '').strip(),
                'flag': str(row.get('flag') or '').strip(),
                'iso3': str(row.get('iso3') or '').strip().upper(),
                'draught': optional_float('draught'),
                'ship_type': str(
                    row.get('ship_type')
                    or row.get('ship_and_cargo_type')
                    or ''
                ).strip(),
                'length': optional_float('length'),
                'width': optional_float('width'),
                'accuracy': optional_float('accuracy'),
                # 抛锚检测需要区分锚泊/靠泊。缺失状态时仍可使用低航速规则。
                'nav_status': nav_status,
                'at_dock': at_dock,
                # 港区匹配信息用于排除港内正常停泊/作业船舶。
                'matched_port_name': matched_port_name,
            }
        except (ValueError, TypeError) as e:
            # 如果经纬度或航速无法转换为浮点数，则跳过该行
            self.stdout.write(self.style.NOTICE(f"Skipping row due to data conversion error: {e}. Row: {row}"))
            return None


    def handle(self, *args, **options):
        channel_layer = get_channel_layer()
        self.stdout.write("Starting AIS data worker in continuous monitoring mode...")
        
        # 【新增】存储已处理文件名的集合
        processed_files = set()
        # 扫描间隔（秒）
        SCAN_INTERVAL = 10 
        
        # 强制等待 5 秒，确保 Channels Layer 订阅完成 (保留此修复)
        # self.stdout.write(self.style.WARNING("Waiting 5 seconds for Channels Layer setup..."))
        # time.sleep(5) 
        
        while True:
            try:
                # 1. 查找所有 CSV 文件并按文件名（时间）排序
                all_files = sorted([f for f in os.listdir(AIS_FOLDER) if f.endswith('.csv')])
                new_files_to_process = []
                
                # 2. 识别新增文件
                for f in all_files:
                    if f not in processed_files:
                        new_files_to_process.append(f)
                
                if not new_files_to_process:
                    self.stdout.write(f"No new files found. Checking again in {SCAN_INTERVAL} seconds...")
                    time.sleep(SCAN_INTERVAL)
                    continue
                
                self.stdout.write(self.style.NOTICE(f"Found {len(new_files_to_process)} new file(s) to process."))
                
                # 3. 遍历并处理新增文件
                for latest_file in new_files_to_process:
                    file_path = os.path.join(AIS_FOLDER, latest_file)
                    ais_data = []

                    # 支持数据清洗
                    ais_data_clean = {}
                    
                    self.stdout.write(f"Processing file: {latest_file}...")
                    
                    # 4. 读取 CSV 文件内容并解析
                    with open(file_path, 'r', encoding='utf-8') as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            converted_ship = self.convert_row(row)
                            if converted_ship and converted_ship['mmsi']:
                                # 只保留一条数据
                                ais_data_clean[converted_ship['mmsi']] = converted_ship
                    
                    for key in ais_data_clean.keys():
                        ais_data.append(ais_data_clean[key])
                                
                    
                    if not ais_data:
                        self.stdout.write(self.style.WARNING(f"File {latest_file} is empty or invalid after parsing. Skipping."))
                        processed_files.add(latest_file) # 即使跳过，也要标记为已处理
                        continue

                    # 将数据存储到Redis中
                    cache.set(
                        'latest_ais_data_raw',
                        ais_data,
                        timeout=getattr(settings, 'CACHE_TTL', 300),
                    )
                    self.stdout.write(self.style.SUCCESS("已更新系统公共变量: latest_ais_data_raw"))
                        
                    # 5. 构造并发送消息到 Channels 群组
                    async_to_sync(channel_layer.group_send)(
                        AisConsumer.AIS_GROUP_NAME,
                        {
                            'type': 'send_ais_update',
                            'text': ais_data 
                        }
                    )

                    self.stdout.write(self.style.SUCCESS(f"Pushed update from: {latest_file} ({len(ais_data)} ships)"))

                    # 只投递轻量触发信号。独立 detection_worker 负责执行
                    # 算法，避免重型检测阻塞下一批 AIS 数据推送。
                    queue_status = enqueue_all_detections(
                        ship_list=ais_data
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
                    processed_files.add(latest_file)
                    
                    # 【重要】每次推送后短暂等待，模拟数据间隔和避免 Redis 拥堵
                    time.sleep(1) 

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"AIS worker encountered a major error: {e}"))
                
            # 【重要】处理完一轮文件后，等待下一次扫描
            time.sleep(SCAN_INTERVAL)
