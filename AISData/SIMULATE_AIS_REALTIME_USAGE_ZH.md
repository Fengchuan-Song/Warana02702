# simulate_ais_realtime 命令参数与多 CSV 循环播放

适用文件：`AISData/management/commands/simulate_ais_realtime.py`。
本文依据当前源码、`AISData/ais_simulation.py` 和实际运行的 `--help` 整理。

## 1. 你的两个需求需要怎么设置

| 需求 | 当前支持情况 | 使用方式 |
| --- | --- | --- |
| 播放多个 CSV | 支持读取一个目录中的所有 CSV，包括子目录 | `--file "目录路径"` |
| 只选择指定的几个 CSV | 不支持在命令中直接传入多个文件路径 | 将需要的 CSV 复制到一个专用目录，然后把该目录传给 `--file` |
| 无限循环播放 | 没有内置循环参数，单次命令播放结束后会退出 | 用 PowerShell 的 `while ($true)` 重复执行命令，见第 3 节 |
| 播放完整时间范围 | 默认只取 60 分钟，需取消该限制 | `--duration-minutes 0`，且不设置 `--end` |
| 不限制每轮生成/投递的记录数量 | 默认已不限制 | `--max-events 0` |
| 按原时间间隔播放 | 默认是 10 倍速，需主动修改 | `--speed-factor 1` |

**关键区别：`--duration-minutes 0` 和 `--max-events 0` 都不表示循环。当前命令没有 `--loop`、`--repeat` 或 `--files` 参数。**

默认最多选择 200 个 MMSI。需要更多船舶时，设置足够大的正整数，例如 `--max-ships 10000`；`--max-ships 0` 无效。此参数没有“0 表示全部”的写法。

## 2. 多个 CSV 的读取方式

例如，准备以下目录，里面只放本次需要播放的 CSV：

```text
Data/AIS/replay_selected/
  first.csv
  second.csv
  extra/
    third.csv
```

然后指定目录：

```powershell
python manage.py simulate_ais_realtime --file ".\Data\AIS\replay_selected" --mode observed --source-timezone Asia/Shanghai --speed-factor 1 --duration-minutes 0 --max-events 0 --max-ships 10000
```

以上目录和文件名是示例，需要自行准备或替换成实际路径。命令应在含有 `manage.py` 的项目根目录运行，并使用安装了项目依赖的 Python 环境。

读取规则：

- 自动递归读取目录中的 `.csv` 文件，扩展名不区分大小写。
- 多个文件合并成同一批船舶轨迹，再按事件时间组织回放；并非先完整播放第一个文件，再播放第二个文件。
- 同一 MMSI、相同规范化时间戳的动态记录会去重，后读取的记录覆盖先读取的记录。
- 时间范围和船舶数量限制作用于合并后的数据，不是给每个 CSV 单独分配 60 分钟或 200 艘船。
- 不能写成 `--file a.csv b.csv`，也不能写成 `--file "a.csv,b.csv"`。重复写 `--file a.csv --file b.csv` 只会保留最后一个值。

如果多个文件的时间跨度很大，按 1 倍速播放时，中间的时间空档也可能造成长时间等待。可以提高 `--speed-factor`，或通过 `--start`、`--end` 选择时间范围。

## 3. 可直接修改使用：多个 CSV + 无限循环

在 PowerShell 中进入项目目录，激活项目环境：

```powershell
Set-Location -LiteralPath "E:\Illegal_Prediction\WanAna02702"
conda activate Predict
```

若使用其他环境，替换上面的环境激活命令。随后执行：

```powershell
# 替换为实际目录；该目录及其子目录中应只放本次要播放的 CSV。
$aisReplayDirectory = "E:\Illegal_Prediction\WanAna02702\Data\AIS\replay_selected"

$aisReplayArguments = @(
    "manage.py"
    "simulate_ais_realtime"
    "--file", $aisReplayDirectory
    "--mode", "observed"
    "--source-timezone", "Asia/Shanghai"
    "--speed-factor", "1"
    "--duration-minutes", "0"
    "--max-events", "0"
    "--max-ships", "10000"
    "--namespace", "predict"
    "--simulation-id", "csv-loop-demo"
)

while ($true) {
    python @aisReplayArguments
    if ($LASTEXITCODE -ne 0) {
        throw "AIS 播放退出，退出码：$LASTEXITCODE。请检查上方错误信息。"
    }
    # 一轮正常结束后等待 1 秒，再从头播放。
    Start-Sleep -Seconds 1
}
```

按 **Ctrl+C** 停止。正常播放完一轮会自动开始下一轮；命令报错则停止，便于处理路径或服务问题。

此示例的设置含义：

| 设置 | 效果 |
| --- | --- |
| `--file` 指向目录 | 每一轮读取目录内的全部 CSV |
| `--mode observed` | 回放清洗、筛选和去重后的原始观测点，不插值、不模拟丢包 |
| `--source-timezone Asia/Shanghai` | 将没有时区信息的时间按北京时间解释；若源数据实际是 UTC，应改成 `UTC` |
| `--speed-factor 1` | 按源数据时间间隔，以约 1 倍速批量推送；改为 `10` 即约 10 倍速 |
| `--duration-minutes 0` | 取消默认的 60 分钟截取 |
| `--max-events 0` | 不设置每轮记录数上限 |
| `--max-ships 10000` | 允许选择最多 10000 个 MMSI；根据实际船舶数量调整 |
| `--namespace predict` | 写入 Predict AIS 状态并向对应 WebSocket 分组发布 |
| `--simulation-id csv-loop-demo` | 每轮使用同一个模拟标识，方便追踪；该参数本身不负责循环 |
| `while ($true)` | 每轮结束后重新启动一次播放 |

循环示例不加 `--keep-state`：每次启动按默认逻辑重置所选命名空间的 AIS 状态，并清理选中船舶对应的轨迹与行为结果。历史数据从头播放时，时间戳会回到起点；保留上一轮状态可能使新一轮记录被当作旧数据或重复数据处理。每轮起点会有状态重置和进程重启间隔，这不是时间戳连续向前的无缝循环。

如果需要模拟接收机的丢包、延迟和乱序，把示例中的 `"observed"` 改为 `"received"`。如果要尽快推送数据，可添加 `"--no-wait"`，此时不再按倍速等待。

正式推送依赖项目配置中的 Redis/Channels；需要在页面看到数据时，还需运行连接对应 Predict 数据源的 ASGI/WebSocket 服务。这个管理命令本身不启动这些服务。

## 4. 完整业务参数表

以下是脚本定义的全部参数。开关类参数仅写参数名即可启用，不要在后面加 `true` 或 `false`。

### 4.1 数据源、船舶和时间范围

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--file PATH` | 必填 | 单个 CSV 文件路径或目录路径；目录会递归搜索 CSV |
| `--max-ships N` | `200` | 未指定 MMSI 时，按加载顺序选择最多 N 个 MMSI；必须大于 0，实际可用动态轨迹数可能更少 |
| `--mmsi MMSI` | 不筛选 | 可重复指定，例如 `--mmsi 123456789 --mmsi 987654321`；显式指定后按列表筛选，不再受船舶数量上限截断，但 `--max-ships` 仍须为正数 |
| `--start TIME` | 已加载动态轨迹的最早时间 | 回放开始时间，含边界；建议使用 ISO 8601，例如 `2026-09-01T00:00:00+08:00` |
| `--end TIME` | 按时长推算 | 回放结束时间，含边界；显式设置后优先于 `--duration-minutes` |
| `--duration-minutes N` | `60.0` | 未设置 `--end` 时，从开始时间截取 N 分钟；`0` 不自动设置结束时间；不能为负数 |
| `--source-timezone ZONE` | `UTC` | CSV 时间及 `--start`、`--end` 缺少时区时使用的时区；已有时区的时间保留自身时区 |

### 4.2 播放模式、速度和批次

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--mode MODE` | `received` | `observed`：原始观测回放；`broadcast`：在有效轨迹段中按模拟广播周期插值；`received`：在广播模拟上增加接收异常 |
| `--speed-factor N` | `10.0` | 回放倍速，必须大于 0；`1` 为约实时，`10` 为约 10 倍速 |
| `--no-wait` | 关闭 | 不按时间间隔休眠，尽快生成并推送；不负责循环 |
| `--batch-ms N` | `1000.0` | 按目标墙钟毫秒窗口组织批次；对应源时间窗口为该值乘倍速；必须大于 0，并非每次固定休眠该时长 |
| `--batch-size N` | `1000` | 每批最多记录数，必须大于 0；记录数或时间窗口满足条件时刷新批次 |
| `--max-events N` | `0` | 单次运行最多生成/投递 N 条记录，包括静态记录和模拟重复记录；`0` 不限制；不能为负数 |
| `--seed N` | `42` | 随机种子；相同输入与配置可复现模拟随机结果 |

### 4.3 接收异常模拟

以下参数只在 `--mode received` 下影响接收过程。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--loss-rate P` | `0.05` | 单条消息丢包概率，即默认 5%；范围 `[0, 1]` |
| `--duplicate-rate P` | `0.005` | 对未丢失消息额外生成重复消息的概率，即默认 0.5%；范围 `[0, 1]` |
| `--out-of-order-rate P` | `0.01` | 添加额外延迟的概率，即默认 1%，可能造成乱序；范围 `[0, 1]` |
| `--minimum-latency-ms N` | `50.0` | 基础接收延迟下限，毫秒 |
| `--maximum-latency-ms N` | `500.0` | 基础接收延迟上限，毫秒；不得小于下限 |
| `--out-of-order-extra-ms N` | `3000.0` | 触发乱序模拟时，额外增加 0 到 N 毫秒的随机延迟 |
| `--outage-rate-per-hour N` | `0.02` | 每艘船的接收中断发生率；按消息之间经过的源时间换算触发概率；`0` 关闭 |
| `--outage-minimum-seconds N` | `30.0` | 接收中断持续时长下限，秒 |
| `--outage-maximum-seconds N` | `300.0` | 接收中断持续时长上限，秒；不得小于下限 |

以上延迟、额外延迟、中断发生率和中断时长均不能为负数。

### 4.4 插值轨迹参数

以下参数影响 `broadcast` 和 `received` 模式的轨迹插值，`observed` 不执行插值。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--gps-noise-metres N` | `5.0` | 插值点东西、南北方向独立高斯位置噪声的标准差，米；`0` 关闭；不能为负数；原始锚点不添加此噪声 |
| `--max-segment-gap-seconds N` | `1800.0` | 相邻锚点间允许插值的最大时间间隔，秒；超过后保留锚点但不补点；必须大于 0 |
| `--max-segment-speed-knots N` | `80.0` | 相邻锚点距离与时间推算的最大允许航速，节；超过后不补点；必须大于 0 |

### 4.5 发布位置、状态和检测

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--namespace NAME` | `predict` | 可选 `predict` 或 `operational`，控制 AIS 状态和推送使用的命名空间 |
| `--allow-operational-write` | 关闭 | 使用 `--namespace operational` 时必须同时指定，否则命令报错 |
| `--keep-state` | 关闭 | 启动前不清理已有 AIS 状态、选中船舶历史和对应行为结果；从头循环历史数据时通常不应启用 |
| `--enqueue-detections` | 关闭 | 将检测快照加入队列；当前实现支持 Predict 和 operational，Predict 还需要匹配的检测 worker 和已激活的检测源 |
| `--dry-run` | 关闭 | 只生成记录及统计，不清理或写入缓存，不发送 WebSocket 数据、不入检测队列，也不按回放时间休眠 |
| `--simulation-id ID` | 自动生成 UUID | 指定模拟标识；每次单独启动且未指定时，会生成新标识 |

仅用于播放时不需要 `--enqueue-detections`。如需 Predict 检测，`--simulation-id` 应与已激活检测源和检测 worker 使用的标识一致。

## 5. Django 自动提供的通用参数

这些参数也出现在实际 `--help` 中，但不改变多文件和循环支持情况。

| 参数 | 说明 |
| --- | --- |
| `-h` / `--help` | 显示帮助后退出 |
| `--version` | 显示 Django 版本后退出 |
| `-v N` / `--verbosity N` | 通用详细程度，取值 `0`、`1`、`2`、`3`，默认 `1`；本命令自行打印的加载和完成统计未按此值做条件过滤 |
| `--settings MODULE` | 指定 Django 设置模块；项目默认通过 `manage.py` 使用 `WanAna02702.settings`，环境变量可覆盖 |
| `--pythonpath PATH` | 将目录加入 Python 模块搜索路径 |
| `--traceback` | 对 `CommandError` 输出异常堆栈 |
| `--no-color` | 禁用彩色输出 |
| `--force-color` | 强制彩色输出，不能与 `--no-color` 同时使用 |
| `--skip-checks` | 跳过 Django 系统检查 |

查看本机帮助：

```powershell
python manage.py simulate_ais_realtime --help
```

## 6. 播放前验证输入

可以先试生成最多 100 条记录，核对 CSV 路径和可用数据：

```powershell
python manage.py simulate_ais_realtime --file ".\Data\AIS\replay_selected" --mode observed --source-timezone Asia/Shanghai --duration-minutes 0 --max-ships 10000 --max-events 100 --dry-run
```

`--dry-run` 不会真的播放到页面，也不会限制 CSV 加载阶段只读 100 行；它限制的是生成的记录数量。观察输出中的 `files=`、`ships=`、`source_rows=`、`invalid_rows=` 和 `delivered=`。

**不要把此验证命令中的 `--dry-run` 或 `--max-events 100` 留在正式完整播放命令中。**

补充：项目启动脚本 `scripts/ais_predict_demo.ps1` 中的 `-AISRadarLoop` 作用于另一个 AIS/Radar 回放命令，不会让 `simulate_ais_realtime` 循环。
