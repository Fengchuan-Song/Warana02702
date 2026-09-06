# 模型性能测试

统一入口支持 `AISData.detection.DETECTORS` 中的 14 个 AIS 模型，以及
`detect-ais-off`、`detect-spoofing` 两个融合模型：

```powershell
python manage.py evaluate_detector `
    --feature-id detect-highSpeedBoat `
    --dataset E:\evaluation\high_speed `
    --output E:\evaluation_results
```

评测按 AIS/雷达源时间排序后无等待回放。AIS 模型直接调用
`run_detector_core`；融合模型直接调用正式 JPDA 和融合检测核心。每个样本使用
独立进程内缓存，AIS 模型产生的 ORM 滚动状态位于强制回滚的事务中。评测不会
切换 active detection source，不写正式违法事件，也不发送 WebSocket。

## 数据集格式

`--dataset` 可以指向清单文件，也可以指向包含以下任一清单的目录：

- `samples.json`
- `samples.jsonl`
- `samples.csv`

推荐的 `samples.json`：

```json
{
  "dataset_version": "2026-09-v1",
  "metadata": {"description": "高速模型平衡测试集"},
  "samples": [
    {
      "sample_id": "S001",
      "track_id": "T001",
      "feature_id": "detect-highSpeedBoat",
      "ground_truth": 1,
      "ais_file": "ais/S001.csv"
    }
  ]
}
```

`feature_id` 可省略，此时使用命令行值。`track_id` 可省略，此时等于
`sample_id`。`ground_truth` 必须为 `0/1`。AIS 和 Radar 记录既可通过
`ais_file` / `radar_file` 引用 CSV、JSON 或 JSONL，也可直接以内嵌数组写入
`ais` / `radar`。

AIS 字段接受系统现有格式（`timestamp,mmsi,lon,lat`）或融合数据格式
（`DateTime,ID,X,Y`）。融合样本必须同时提供 AIS 和 Radar，其中 `X` 为纬度、
`Y` 为经度。多船样本把相关船舶记录放在同一 AIS 文件中即可，runner 会统一按
源时间合并回放。

## 输出

每次运行写入：

```text
<output>/<feature_id>/<test_id>/
├── predictions.csv
├── metrics.json
├── config.json
└── errors.log
```

错误样本的 `prediction` 留空且不参与 TP/FP/TN/FN，避免把执行失败误计为漏报或
真阴性。`config.json` 记录 Git commit、数据集 SHA-256、参数、区域版本和融合
权重 hash。未指定 `--test-id` 时会自动生成；指定的目录已存在时命令拒绝覆盖。

