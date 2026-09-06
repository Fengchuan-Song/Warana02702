"""As-fast-as-possible AIS replay driven exclusively by source timestamps."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import groupby

from django.utils.dateparse import parse_datetime

from AISData.ais_state import merge_ais_state
from AISData.behavior_recognition import update_behavior_results
from AISData.normalization import (
    ais_record_kind,
    normalise_dynamic_ais_record,
    normalise_static_ais_record,
)
from AISData.trajectory_history import append_ais_history


@dataclass(frozen=True)
class AISReplayFrame:
    source_timestamp: str
    snapshot: list
    incremental: list


class AISReplayError(ValueError):
    pass


def _normalise(records):
    normalised = []
    invalid = 0
    for record in records:
        kind = ais_record_kind(record)
        if kind == "static":
            item = normalise_static_ais_record(record)
        else:
            kind = "dynamic"
            item = normalise_dynamic_ais_record(record)
        if item is None:
            invalid += 1
            continue
        source_time = parse_datetime(item["timestamp"])
        normalised.append((source_time, kind, item))
    normalised.sort(key=lambda value: (value[0], value[2].get("mmsi", "")))
    return normalised, invalid


def replay_ais(records, *, simulation_id):
    """Yield source-time frames without sleeping or using operational queues."""
    normalised, invalid = _normalise(records)
    if not normalised:
        raise AISReplayError(
            f"AIS sample contains no valid records ({invalid} invalid)"
        )

    dynamic_count = 0
    for timestamp, grouped in groupby(normalised, key=lambda value: value[0]):
        items = list(grouped)
        dynamic = [item for _, kind, item in items if kind == "dynamic"]
        static = [item for _, kind, item in items if kind == "static"]
        dynamic_count += len(dynamic)

        # Mirror the operational ingestion order, but keep all keys inside the
        # evaluation process's LocMem cache.
        if dynamic:
            append_ais_history(dynamic, namespace="evaluation")
            update_behavior_results(
                dynamic,
                namespace="evaluation",
                simulation_id=simulation_id,
                append_history=False,
            )
        state = merge_ais_state(
            dynamic_updates=dynamic,
            static_updates=static,
            namespace="evaluation",
        )
        yield AISReplayFrame(
            source_timestamp=timestamp.isoformat(),
            snapshot=state.snapshot,
            incremental=state.accepted_dynamic,
        )

    if not dynamic_count:
        raise AISReplayError("AIS sample contains no dynamic position records")
