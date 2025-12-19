# Deviation/apps.py

from django.apps import AppConfig
import pandas as pd
import numpy as np
import joblib
from fastdtw import fastdtw
from scipy.spatial.distance import euclidean

# --- 1. 全局配置与知识库变量 ---
PREPARED_DATA_FILE = 'Deviation/knowledge/QZHX_trajectories.parquet'
INDEX_FILE = 'Deviation/knowledge/QZHX_start_point_index.joblib'
START_POINTS_FILE = 'Deviation/knowledge/QZHX_start_points.csv'
K_NEIGHBORS = 10
DEVIATION_THRESHOLD_DTW = 0.1

KNOWLEDGE_BASE = None

def load_knowledge_base(data_path, index_path, start_points_path):
    """加载离线准备阶段生成的所有知识库文件。"""
    print("--- Deviation App: 正在加载知识库... ---")
    try:
        df_history = pd.read_parquet(data_path)
        tree = joblib.load(index_path)
        df_start_points = pd.read_csv(start_points_path)
        print("Deviation App: 知识库加载成功！")
        return (df_history, tree, df_start_points)
    except FileNotFoundError as e:
        print(f"Deviation App 错误：未能找到必要的知识库文件: {e}")
        return None
    except Exception as e:
        print(f"Deviation App 错误：加载知识库时发生意外错误: {e}")
        return None


class DeviationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "Deviation"

    def ready(self):
        """应用启动时（或配置加载时）加载知识库"""
        global KNOWLEDGE_BASE
        # 确保只在主进程中加载一次 (防止重复加载)
        if not KNOWLEDGE_BASE:
            KNOWLEDGE_BASE = load_knowledge_base(PREPARED_DATA_FILE, INDEX_FILE, START_POINTS_FILE)
            if KNOWLEDGE_BASE is None:
                print("Deviation App 严重警告：航道偏离检测知识库加载失败。检测功能将无法使用。")