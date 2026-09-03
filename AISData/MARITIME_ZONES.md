# 港口、锚地和泊位管理

在主页面左侧点击“海事区域”。支持新增、编辑、启用/停用和地图定位。

## 手动添加或地图选点

1. 点击“新建 / 清空”，填写名称，选择港口、锚地、泊位或具体码头类型。
2. 输入 WGS84 坐标，每行“经度, 纬度”；也可点击“地图选点”，依次选择至少 3 个边界顶点。
3. 可撤销最后一个点、取消选点或按 Esc 退出。选完点击“完成选点”，浏览器本地将高德 GCJ-02 坐标转换为 WGS84；坐标无效时保留选点供修正。
4. 点击“预览边界”检查位置，再点击“保存区域”。单区最多 500 个顶点，不允许自相交或零面积多边形。

地图图层中“港口/码头”“锚地”“泊位”可分别开关。泊位区域同时作为靠泊设施参与异常停泊判断。

选点、预览及海事区域图层使用同一套本地数值转换，不调用高德在线坐标转换接口，因此不会受到该接口的 `INVALID_USER_SCODE` 或调用配额影响。底图本身仍使用高德 SDK。转换算法基于 BSD 许可的 [eviltransform](https://github.com/googollee/eviltransform)，用于地图显示与 AIS 坐标衔接，数值往返精度不等于测绘精度。

## CSV / Excel 导入

展开“批量导入 / 导出”，可下载 CSV 或 Excel 模板，也可导出现有全部区域后修改并重新上传。

| 字段 | 含义 |
| --- | --- |
| `id` / 编号 | 留空新增；保留编号更新。不存在或重复的编号会报错。 |
| `AOI_Name` / 名称 | 必填，最多 255 个字符。 |
| `type` / 类型 | 必填：PRT 港口、ANC 锚地、BTH 泊位；也可使用中文名称或具体码头代码。 |
| `locode` / 港口代码 | 选填，默认 CN；示例 CNSZX、HKHKG。 |
| `AOI_Description` / 说明 | 选填，最多 2000 个字符。 |
| `geometry` / 坐标 | 必填，WGS84 `[经度,纬度]` 数组；兼容单环二维 WKT、GeoJSON Polygon、每行一对坐标。 |
| `is_active` / 启用 | true/false、1/0、是/否；空值默认启用。 |

具体码头代码：LIQ 液体散货、GCO 杂货、DRY 干散货、CTR 集装箱、GAS 气体、ROR 滚装、PAX 客运。

文件支持 `.csv`、`.xlsx`、`.xls`，最大 5 MB、2000 行。CSV 支持 UTF-8（含 BOM）与 GB18030；含逗号的坐标字段须使用 CSV 双引号转义，建议从模板开始。Excel 只读取第一个工作表；请使用实际值，XLSX 中的公式和错误单元格会被拒绝。XLS 使用表格中保存的值。

上传后点击“校验并预览”，查看新增、更新数量和各行信息，再点击“确认导入”。任何一行无效时整批不保存；上传文件未包含的区域不会被删除。停用区域可通过列表重新启用。已有区域的编号会保持稳定，走私航次等引用无需更换编号。

若提示数据已被更新，文件需要重新预览；手动编辑需要刷新列表后重新点击该区域的“编辑”，避免覆盖其他用户的修改。

## 存储和检测

- 原始加密文件 `AISData/resources/maritime_zones_gd_hk_mo.csv.aes` 保留作为初始数据。
- `IllegalAnchored/zones.py` 中的 17 个已公布锚地也纳入同一管理列表，使用固定负数编号；原列表顺序作为编号映射须保持稳定。
- 用户新增、修改和启停状态保存在 `AISData.MaritimeZoneOverride` 数据表。原文件和代码中的坐标不被直接改写。
- `get_maritime_zones()` 合并初始数据和数据库修改。地图与港口/锚地检测均使用该查询；每次请求或检测批次使用一致的区域快照，下一批次读取已提交的新版本。
- 固定锚地移动或停用后，非法抛锚不会退回使用它的旧坐标。明确禁锚区规则仍优先。同一位置若还被其他已启用区域覆盖，该区域仍独立生效。
- 共享低速行为缓存记录区域版本，区域更新后同一 AIS 观测也会重新计算空间背景。历史识别记录不会被自动重写。

## 部署与接口

安装项目依赖（新增 `xlrd==2.0.1`，XLSX 使用既有 `openpyxl`），执行 `python manage.py migrate`，然后重启 Web 服务、检测进程和 AIS 接入进程以加载新代码。正常区域修改不需要重启服务。

- `GET /AISData/maritime-zones/`：已启用区域 GeoJSON，支持 `type`、`locode` 筛选。
- `GET /AISData/maritime-zones/manage/`：所有区域及当前 `revision`。
- `POST /AISData/maritime-zones/manage/`：JSON `{rows: [...], revision: n}`，原子保存，版本冲突返回 409。
- `POST /AISData/maritime-zones/import-preview/`：multipart `file`，只校验不保存，返回规范化 `rows` 和版本。
- `GET /AISData/maritime-zones/export/?format=csv|xlsx`：导出；添加 `template=1` 下载模板。

写入和上传接口沿用项目 CSRF 保护。新增表结构的迁移为 `AISData.0006_maritime_zone_management`。
