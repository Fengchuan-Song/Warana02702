"""Detection-input selection while preserving the public result contract."""

from django.core.cache import cache


ACTIVE_DETECTION_SOURCE_CACHE_KEY = "ais:detection:active-source:v1"
OPERATIONAL_SOURCE_ID = "operational"

MODEL_RUNTIME_CACHE_KEYS = (
    "abnormal_transfer:tracking_state:v1",
    "collision:risk_state:v1",
    "deviation:tracking_state:v2",
    "double_dragging:event_state:v1",
    "crossing_boundary:state:v1",
    "crossing_boundary:events:v1",
    "high_speed:event_state:v1",
    "illegal_staying:event_state:v1",
    "illegal_anchored:history:v1",
    "illegal_berthing:tracking_state:v1",
    "low_speed:event_state:v1",
    "smuggling:voyage_state:v2",
    "smuggling:recent_events:v2",
)


def detection_source_id(namespace=None, simulation_id=None):
    namespace_value = str(namespace or "operational").strip().lower()
    if namespace_value == "operational":
        return OPERATIONAL_SOURCE_ID
    simulation_value = str(simulation_id or "").strip()
    if not simulation_value:
        raise ValueError("simulation_id is required for a non-operational source")
    if ":" in namespace_value or ":" in simulation_value:
        raise ValueError("detection source values cannot contain ':'")
    return f"{namespace_value}:{simulation_value}"


def get_active_detection_source():
    value = cache.get(ACTIVE_DETECTION_SOURCE_CACHE_KEY)
    return str(value or OPERATIONAL_SOURCE_ID)


def is_detection_source_active(namespace=None, simulation_id=None):
    return get_active_detection_source() == detection_source_id(
        namespace,
        simulation_id,
    )


def internal_detection_cache_key(feature_id, namespace=None, simulation_id=None):
    source_id = detection_source_id(namespace, simulation_id)
    return f"ais:detection:result:{source_id}:{feature_id}"


def detection_snapshot_cache_key(namespace=None):
    namespace_value = str(namespace or "operational").strip().lower()
    if namespace_value == "operational":
        return "latest_ais_data_raw"
    return f"{namespace_value}:latest_ais_data_raw"


def reset_model_runtime_state():
    """Clear transient detector state when the one public source changes."""
    from AISData.detection import DETECTORS, detection_cache_key

    cache.delete_many(
        list(MODEL_RUNTIME_CACHE_KEYS)
        + [detection_cache_key(feature_id) for feature_id in DETECTORS]
    )

    # These tables are rolling algorithm buffers, not warning history or
    # configuration. A source switch must not connect two unrelated tracks.
    from AbnormalParking.models import ParkingBuffer
    from AbnormalWandering.models import TrajectoryBuffer
    from Deviation.models import TrajectoryPoint
    from DoubleDragging.models import DoubleDraggingPoint
    from HighSpeedBoat.models import HighSpeedPoint
    from IllegalStaying.models import StayingBuffer
    from LowSpeed.models import LowSpeedPoint

    deleted = 0
    for model in (
        ParkingBuffer,
        TrajectoryBuffer,
        TrajectoryPoint,
        DoubleDraggingPoint,
        HighSpeedPoint,
        StayingBuffer,
        LowSpeedPoint,
    ):
        count, _details = model.objects.all().delete()
        deleted += count
    return deleted


def activate_detection_source(namespace=None, simulation_id=None, force=False):
    """Select the only source allowed to mutate models and publish results."""
    source_id = detection_source_id(namespace, simulation_id)
    changed = get_active_detection_source() != source_id
    cache.set(ACTIVE_DETECTION_SOURCE_CACHE_KEY, source_id, timeout=None)
    deleted = reset_model_runtime_state() if changed or force else 0
    return {
        "source_id": source_id,
        "changed": changed,
        "deleted_buffer_rows": deleted,
    }
