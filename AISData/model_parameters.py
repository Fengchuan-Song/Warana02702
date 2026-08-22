"""Runtime-editable parameter schemas for illegal-event detectors."""

import math
from contextlib import contextmanager

from django.conf import settings
from django.db import OperationalError, ProgrammingError


def _number(key, label, unit="", minimum=0, maximum=None, step="any", help_text=""):
    return {
        "key": key,
        "label": label,
        "type": "number",
        "unit": unit,
        "min": minimum,
        "max": maximum,
        "step": step,
        "help": help_text,
    }


def _integer(key, label, unit="", minimum=0, maximum=None, help_text=""):
    field = _number(
        key,
        label,
        unit=unit,
        minimum=minimum,
        maximum=maximum,
        step=1,
        help_text=help_text,
    )
    field["type"] = "integer"
    return field


def _without_configurable_parameters(label, description):
    return {
        "label": label,
        "description": description,
        "setting": None,
        "fields": [],
    }


MODEL_PARAMETER_SCHEMAS = {
    "detect-abnormalStaying": {
        "label": "异常停泊预警",
        "description": "低速目标在监控区域内持续停留的判定阈值。",
        "setting": "ABNORMAL_PARKING",
        "fields": [
            _number("max_speed_knots", "最大停泊航速", "节", 0, 20, 0.1),
            _number("distance_threshold_metres", "位置聚集半径", "米", 1, 5000, 1),
            _number("min_duration_minutes", "最短持续时间", "分钟", 0, 1440, 1),
            _integer("min_points", "最少轨迹点", "点", 2, 10000),
            _integer("analysis_window_minutes", "分析时间窗口", "分钟", 1, 10080),
        ],
    },
    "detect-abnormalWandering": {
        "label": "异常徘徊预警",
        "description": "依据轨迹长度、转向次数与位移比识别徘徊行为。",
        "setting": "ABNORMAL_WANDERING",
        "fields": [
            _integer("analysis_window_minutes", "分析时间窗口", "分钟", 1, 10080),
            _integer("min_points", "最少轨迹点", "点", 4, 10000),
            _number("min_duration_minutes", "最短持续时间", "分钟", 0, 1440, 1),
            _number("min_path_distance_metres", "最短轨迹长度", "米", 1, 100000, 10),
            _number("min_turn_angle_degrees", "最小转向角", "度", 0, 180, 1),
            _integer("min_turn_count", "最少转向次数", "次", 1, 1000),
            _number("max_displacement_ratio", "最大位移比", "", 0, 1, 0.01),
        ],
    },
    "detect-abnormalTransfer": {
        "label": "异常接驳预警",
        "description": "两船近距离、低相对速度持续接触的识别参数。",
        "setting": "ABNORMAL_TRANSFER_DETECTION",
        "fields": [
            _number("base_contact_distance_metres", "基础接触距离", "米", 1, 10000, 1),
            _number("maximum_contact_distance_metres", "最大接触距离", "米", 1, 20000, 1),
            _number("maximum_candidate_speed_knots", "候选船最大航速", "节", 0, 102.2, 0.1),
            _number("maximum_relative_speed_knots", "最大相对航速", "节", 0, 50, 0.1),
            _number("maximum_course_difference_degrees", "最大航向差", "度", 0, 180, 1),
            _integer("minimum_duration_seconds", "最短持续时间", "秒", 0, 86400),
            _integer("minimum_observations", "最少观测次数", "次", 2, 10000),
        ],
    },
    "detect-collision": {
        "label": "船舶碰撞预警",
        "description": "通过 TCPA、DCPA 和当前距离划分碰撞风险等级。",
        "setting": "COLLISION_DETECTION",
        "fields": [
            _number("warning_tcpa_minutes", "预警 TCPA", "分钟", 0, 1440, 0.1),
            _number("critical_tcpa_minutes", "严重 TCPA", "分钟", 0, 1440, 0.1),
            _number("warning_dcpa_metres", "预警 DCPA", "米", 0, 100000, 1),
            _number("critical_dcpa_metres", "严重 DCPA", "米", 0, 100000, 1),
            _number("immediate_distance_metres", "紧急距离", "米", 0, 100000, 1),
            _number("minimum_relative_speed_mps", "最小相对速度", "米/秒", 0, 100, 0.1),
        ],
    },
    "detect-deviation": {
        "label": "航道偏离预警",
        "description": "判断航行目标是否持续偏离已知航道。",
        "setting": "DEVIATION_DETECTION",
        "fields": [
            _number("minimum_speed_knots", "最低分析航速", "节", 0, 102.2, 0.1),
            _integer("minimum_duration_seconds", "最短持续时间", "秒", 0, 86400),
            _integer("minimum_observations", "最少观测次数", "次", 2, 10000),
            _number("route_entry_distance_metres", "航道进入距离", "米", 0, 100000, 10),
            _number("deviation_distance_metres", "偏航距离阈值", "米", 0, 100000, 10),
            _integer("confirmation_observations", "确认观测次数", "次", 1, 10000),
            _number("direction_tolerance_degrees", "航向容差", "度", 0, 180, 1),
        ],
    },
    "detect-doubleDragging": {
        "label": "双拖预警",
        "description": "识别两船以相近速度和航向保持间距航行的行为。",
        "setting": "DOUBLE_DRAGGING_DETECTION",
        "fields": [
            _number("min_duration_minutes", "最短持续时间", "分钟", 0, 1440, 1),
            _integer("min_aligned_points", "最少对齐轨迹点", "点", 2, 10000),
            _number("min_pair_distance_metres", "最小船间距", "米", 0, 100000, 10),
            _number("max_pair_distance_metres", "最大船间距", "米", 0, 100000, 10),
            _number("min_operating_speed_knots", "最低作业航速", "节", 0, 102.2, 0.1),
            _number("max_operating_speed_knots", "最高作业航速", "节", 0, 102.2, 0.1),
            _number("max_course_difference_degrees", "最大航向差", "度", 0, 180, 1),
        ],
    },
    "detect-CrossingBoundary": {
        "label": "海上围栏越界预警",
        "description": "轨迹穿越电子围栏边界时的容差与状态保留参数。",
        "setting": "CROSSING_BOUNDARY_DETECTION",
        "fields": [
            _number("boundary_tolerance_metres", "边界容差", "米", 0, 10000, 1),
            _integer("max_position_age_seconds", "最大位置时延", "秒", 1, 86400),
            _number("state_retention_hours", "状态保留时间", "小时", 0, 8760, 1),
            _number("event_retention_minutes", "事件展示时间", "分钟", 0, 10080, 1),
            _integer("max_vertices", "围栏最大顶点数", "点", 3, 100000),
        ],
    },
    "detect-smuggling": {
        "label": "海上走私风险预警",
        "description": "结合夜航、异常靠岸、吃水变化和高速小艇特征计算风险。",
        "setting": "SMUGGLING_DETECTION",
        "fields": [
            _integer("night_start_hour", "夜间开始小时", "时", 0, 23),
            _integer("night_end_hour", "夜间结束小时", "时", 0, 23),
            _number("maximum_voyage_hours", "最大航次时长", "小时", 0, 720, 0.5),
            _number("landing_speed_knots", "靠岸航速上限", "节", 0, 102.2, 0.1),
            _integer("landing_minimum_duration_seconds", "靠岸最短时间", "秒", 0, 86400),
            _number("guangdong_port_buffer_metres", "广东港口外围预警距离", "米", 0, 100000, 100),
            _number("draught_change_metres", "吃水变化阈值", "米", 0, 100, 0.1),
            _integer("minimum_risk_score", "最低预警分数", "分", 0, 100),
        ],
    },
    "detect-highSpeedBoat": {
        "label": "高速快艇预警",
        "description": "目标持续超过航速上限时触发预警。",
        "setting": "HIGH_SPEED_DETECTION",
        "fields": [
            _number("default_speed_limit_knots", "航速上限", "节", 0, 102.2, 0.1),
            _integer("minimum_duration_seconds", "最短持续时间", "秒", 0, 86400),
            _integer("maximum_gap_seconds", "轨迹最大间隔", "秒", 1, 86400),
            _integer("analysis_window_minutes", "分析时间窗口", "分钟", 1, 10080),
            _number("high_risk_excess_knots", "高风险超速量", "节", 0, 102.2, 0.1),
        ],
    },
    "detect-lowSpeedBoat": {
        "label": "低速船舶预警",
        "description": "航行目标持续低于最低航速时触发预警。",
        "setting": "LOW_SPEED_DETECTION",
        "fields": [
            _number("default_minimum_speed_knots", "最低航速", "节", 0, 102.2, 0.1),
            _integer("minimum_duration_seconds", "最短持续时间", "秒", 0, 86400),
            _integer("minimum_observations", "最少观测次数", "次", 2, 10000),
            _integer("maximum_gap_seconds", "轨迹最大间隔", "秒", 1, 86400),
            _integer("analysis_window_minutes", "分析时间窗口", "分钟", 1, 10080),
        ],
    },
    "detect-illegalAnchored": {
        "label": "非法抛锚预警",
        "description": "目标在非合法锚地持续低速锚泊的判定参数。",
        "setting": "ILLEGAL_ANCHORED_DETECTION",
        "fields": [
            _number("max_speed_knots", "最大锚泊航速", "节", 0, 20, 0.1),
            _integer("min_duration_seconds", "最短持续时间", "秒", 0, 86400),
            _integer("min_observations", "最少观测次数", "次", 2, 10000),
            _number("max_drift_metres", "最大漂移距离", "米", 1, 100000, 1),
            _integer("history_window_seconds", "历史时间窗口", "秒", 1, 604800),
        ],
    },
    "detect-illegalStaying": {
        "label": "非法驻留预警",
        "description": "目标在禁停区域内持续低速驻留的判定参数。",
        "setting": "ILLEGAL_STAYING",
        "fields": [
            _number("max_speed_knots", "最大驻留航速", "节", 0, 20, 0.1),
            _number("distance_threshold_metres", "位置聚集半径", "米", 1, 5000, 1),
            _number("min_duration_minutes", "最短持续时间", "分钟", 0, 1440, 1),
            _integer("min_points", "最少轨迹点", "点", 2, 10000),
            _integer("maximum_gap_seconds", "轨迹最大间隔", "秒", 1, 86400),
        ],
    },
    "detect-illegalBerthing": {
        "label": "非法搭靠预警",
        "description": "中国籍船舶与外籍船舶持续低速、近距离搭靠的判定参数。",
        "setting": "ILLEGAL_BERTHING_DETECTION",
        "fields": [
            _number("base_contact_distance_metres", "基础接触距离", "米", 1, 10000, 1),
            _number("maximum_contact_distance_metres", "最大接触距离", "米", 1, 20000, 1),
            _number("maximum_speed_knots", "最大搭靠航速", "节", 0, 102.2, 0.1),
            _number("maximum_relative_speed_knots", "最大相对航速", "节", 0, 50, 0.1),
            _integer("minimum_duration_seconds", "最短持续时间", "秒", 0, 86400),
            _integer("minimum_observations", "最少观测次数", "次", 2, 10000),
        ],
    },
    "detect-ais-off": _without_configurable_parameters(
        "关闭 AIS 船舶检测",
        "根据 AIS/雷达融合后的未匹配雷达轨迹进行识别，当前没有独立的可配置参数。",
    ),
    "detect-blackList": _without_configurable_parameters(
        "黑名单船舶检测",
        "根据已启用的黑名单 MMSI 进行精确匹配，名单内容请在黑名单管理中维护。",
    ),
    "detect-illegalFarming": _without_configurable_parameters(
        "非法养殖检测",
        "该检测模型当前尚未提供可配置参数。",
    ),
    "detect-illegalFishing": _without_configurable_parameters(
        "非法捕捞船舶检测",
        "该检测模型当前尚未提供可配置参数。",
    ),
    "detect-illegalPollutionDis": _without_configurable_parameters(
        "非法排污船舶检测",
        "该检测模型当前尚未提供可配置参数。",
    ),
    "detect-illegalSandMining": _without_configurable_parameters(
        "盗采海砂船舶检测",
        "该检测模型当前尚未提供可配置参数。",
    ),
    "detect-overload": _without_configurable_parameters(
        "超载船舶检测",
        "该模型由独立视频识别进程运行，当前没有可在线修改的参数。",
    ),
    "detect-spoofing": _without_configurable_parameters(
        "身份伪造船舶检测",
        "根据 AIS/雷达融合后的未匹配 AIS 轨迹进行识别，当前没有独立的可配置参数。",
    ),
}


def get_schema(feature_id):
    try:
        return MODEL_PARAMETER_SCHEMAS[feature_id]
    except KeyError as exc:
        raise ValueError("该模型没有可配置的数值参数") from exc


def _default_parameters(schema):
    if not schema["fields"]:
        return {}
    configured = getattr(settings, schema["setting"], {})
    return {
        field["key"]: configured.get(field["key"])
        for field in schema["fields"]
    }


def validate_parameters(feature_id, raw_parameters):
    schema = get_schema(feature_id)
    if not schema["fields"]:
        raise ValueError("该模型暂无可配置参数")
    if not isinstance(raw_parameters, dict):
        raise ValueError("parameters 必须是 JSON 对象")

    fields = {field["key"]: field for field in schema["fields"]}
    unknown = sorted(set(raw_parameters).difference(fields))
    if unknown:
        raise ValueError("包含未知参数：" + "、".join(unknown))

    cleaned = {}
    for key, raw_value in raw_parameters.items():
        field = fields[key]
        if isinstance(raw_value, bool):
            raise ValueError(f"{field['label']}必须是数字")
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            raise ValueError(f"{field['label']}必须是数字")
        if not math.isfinite(value):
            raise ValueError(f"{field['label']}必须是有限数字")
        if field["type"] == "integer":
            if not value.is_integer():
                raise ValueError(f"{field['label']}必须是整数")
            value = int(value)
        if field["min"] is not None and value < field["min"]:
            raise ValueError(f"{field['label']}不能小于 {field['min']}")
        if field["max"] is not None and value > field["max"]:
            raise ValueError(f"{field['label']}不能大于 {field['max']}")
        cleaned[key] = value

    effective = {**_default_parameters(schema), **cleaned}
    comparison_rules = {
        "detect-abnormalTransfer": (
            "base_contact_distance_metres",
            "maximum_contact_distance_metres",
            "基础接触距离不能大于最大接触距离",
        ),
        "detect-illegalBerthing": (
            "base_contact_distance_metres",
            "maximum_contact_distance_metres",
            "基础接触距离不能大于最大接触距离",
        ),
        "detect-doubleDragging": (
            "min_pair_distance_metres",
            "max_pair_distance_metres",
            "最小船间距不能大于最大船间距",
        ),
    }
    rule = comparison_rules.get(feature_id)
    if rule and effective[rule[0]] > effective[rule[1]]:
        raise ValueError(rule[2])
    if feature_id == "detect-doubleDragging" and (
        effective["min_operating_speed_knots"]
        > effective["max_operating_speed_knots"]
    ):
        raise ValueError("最低作业航速不能大于最高作业航速")
    if feature_id == "detect-collision" and (
        effective["critical_tcpa_minutes"] > effective["warning_tcpa_minutes"]
        or effective["critical_dcpa_metres"] > effective["warning_dcpa_metres"]
    ):
        raise ValueError("严重碰撞阈值不能大于预警阈值")
    if feature_id == "detect-illegalAnchored" and (
        effective["history_window_seconds"]
        < effective["min_duration_seconds"]
    ):
        raise ValueError("历史时间窗口不能短于最短持续时间")
    return cleaned


def serialize_configuration(feature_id):
    from .models import DetectionModelConfiguration

    schema = get_schema(feature_id)
    defaults = _default_parameters(schema)
    try:
        stored = DetectionModelConfiguration.objects.filter(
            feature_id=feature_id
        ).first()
    except (OperationalError, ProgrammingError):
        stored = None
    overrides = stored.parameters if stored else {}
    return {
        "feature_id": feature_id,
        "label": schema["label"],
        "description": schema["description"],
        "fields": schema["fields"],
        "is_configurable": bool(schema["fields"]),
        "defaults": defaults,
        "parameters": {**defaults, **overrides},
        "is_customized": bool(stored),
        "updated_at": stored.updated_at.isoformat() if stored else None,
    }


@contextmanager
def apply_runtime_parameter_override(feature_id):
    """Temporarily expose stored values through the detector's setting dict."""
    schema = MODEL_PARAMETER_SCHEMAS.get(feature_id)
    if schema is None or not schema["fields"]:
        yield
        return

    from .models import DetectionModelConfiguration

    try:
        stored = DetectionModelConfiguration.objects.filter(
            feature_id=feature_id
        ).first()
    except (OperationalError, ProgrammingError):
        stored = None
    if stored is None:
        yield
        return

    setting_name = schema["setting"]
    original = getattr(settings, setting_name, {})
    effective = {**original, **stored.parameters}
    setattr(settings, setting_name, effective)
    try:
        yield
    finally:
        setattr(settings, setting_name, original)
