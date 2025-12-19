# -*- coding: utf-8 -*-

import os
import django
from pathlib import Path

# 设置Dango运行时需要的环境变量DJANGO_SETTINGS_MODULE
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'WanAna02702.settings')

# 加载Django的设置
django.setup()

BASE_DIR = Path(__file__).resolve().parent.parent

print('BASE_DIR:', BASE_DIR)