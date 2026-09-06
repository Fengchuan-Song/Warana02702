"""Per-sample isolation without touching operational Redis or committed rows."""

from __future__ import annotations

from contextlib import contextmanager
import uuid

from django.core.cache import cache, caches
from django.db import transaction
from django.test.utils import override_settings


def _clear_rolling_tables():
    # These are algorithm working buffers, not configuration or event history.
    from AbnormalParking.models import ParkingBuffer
    from AbnormalWandering.models import TrajectoryBuffer
    from Deviation.models import TrajectoryPoint
    from DoubleDragging.models import DoubleDraggingPoint
    from HighSpeedBoat.models import HighSpeedPoint
    from IllegalStaying.models import StayingBuffer
    from LowSpeed.models import LowSpeedPoint

    for model in (
        ParkingBuffer,
        TrajectoryBuffer,
        TrajectoryPoint,
        DoubleDraggingPoint,
        HighSpeedPoint,
        StayingBuffer,
        LowSpeedPoint,
    ):
        model.objects.all().delete()


@contextmanager
def isolated_sample_state(*, include_database=True):
    """Use local cache state and roll back every database mutation.

    Existing operational buffer rows are hidden from the detector by deleting
    them inside the sample transaction.  The forced rollback restores those
    rows and discards all writes made by the evaluated detector.
    """
    cache_settings = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": f"detector-evaluation-{uuid.uuid4().hex}",
        }
    }
    with override_settings(CACHES=cache_settings):
        caches.close_all()
        try:
            cache.clear()
            from AISRadar.fusion_state import clear_fusion_state

            clear_fusion_state()
            if not include_database:
                yield
                return
            with transaction.atomic():
                try:
                    _clear_rolling_tables()
                    yield
                finally:
                    transaction.set_rollback(True)
        finally:
            cache.clear()
            caches.close_all()
