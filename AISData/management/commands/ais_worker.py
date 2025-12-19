import time
import csv
import os
from django.core.management.base import BaseCommand
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from AISData.consumers import AisConsumer
from WanAna02702.settings import BASE_DIR
from django.core.cache import cache

AIS_FOLDER = os.path.join(BASE_DIR, 'Data/AIS/timeDivision_v2_30s')

class Command(BaseCommand):
    help = 'Starts the AIS data worker to monitor files and push updates via WebSocket.'

    # 【新增方法】用于将 CSV 行数据转换为前端所需的字典格式
    def convert_row(self, row):
        """Converts a CSV row (dict) into the format expected by the frontend."""
        # 关键假设：CSV 文件中包含以下字段（或类似字段），请根据您的实际 CSV 头部调整键名。
        # 前端期望的键名: mmsi, name, lon, lat, course, speed
        try:
            # 这里使用了 .get() 和 or 来尝试兼容常见的 CSV 头部字段名，并确保数值类型正确
            return {
                'timestamp': row.get('timestamp'),
                'mmsi': row.get('MMSI'),
                'name': row.get('Name'),
                'lon': float(row.get('longitude')),
                'lat': float(row.get('latitude')),
                # COG/course (航向), SOG/speed (航速)
                'course': float(row.get('course')), 
                'speed': float(row.get('speed')),  
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
        
        
        # 【修改】外层无限循环
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
                        
                    # 5. 构造并发送消息到 Channels 群组
                    async_to_sync(channel_layer.group_send)(
                        AisConsumer.AIS_GROUP_NAME,
                        {
                            'type': 'send_ais_update',
                            'text': ais_data 
                        }
                    )

                    self.stdout.write(self.style.SUCCESS(f"Pushed update from: {latest_file} ({len(ais_data)} ships)"))
                    
                    # 6. 【关键操作】标记文件为已处理
                    processed_files.add(latest_file)
                    
                    # 【重要】每次推送后短暂等待，模拟数据间隔和避免 Redis 拥堵
                    time.sleep(1) 

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"AIS worker encountered a major error: {e}"))
                
            # 【重要】处理完一轮文件后，等待下一次扫描
            time.sleep(SCAN_INTERVAL)