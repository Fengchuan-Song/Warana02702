"""Side-effect-free entry point for one causal AIS/Radar fusion frame."""

from __future__ import annotations

import pandas as pd


def run_fusion_frame(ais_rows, radar_rows, *, matcher=None):
    """Run the production matcher for one Radar-anchored sensor frame.

    ``ais_rows`` must contain only observations at or before the Radar frame.
    The production JPDA matcher independently enforces that causal constraint.
    No cache, database, or channel state is mutated by this function.
    """
    if matcher is None:
        from AISRadar.inference.predictor import get_matcher
        from AISRadar.views import _config

        matcher = get_matcher(_config())
    return matcher.predict_tables(
        pd.DataFrame(ais_rows or []),
        pd.DataFrame(radar_rows or []),
    )
