"""Binary warning metrics with explicit undefined-rate handling."""

from __future__ import annotations


def _safe_ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def calculate_metrics(records, *, feature_id=None):
    valid = [record for record in records if record.prediction in (0, 1)]
    tp = sum(
        record.ground_truth == 1 and record.prediction == 1
        for record in valid
    )
    fp = sum(
        record.ground_truth == 0 and record.prediction == 1
        for record in valid
    )
    tn = sum(
        record.ground_truth == 0 and record.prediction == 0
        for record in valid
    )
    fn = sum(
        record.ground_truth == 1 and record.prediction == 0
        for record in valid
    )
    warnings = []
    if fp + tn == 0:
        warnings.append("false_alarm_rate is undefined: no negative samples")
    if fn + tp == 0:
        warnings.append("miss_rate is undefined: no positive samples")
    if not valid:
        warnings.append("accuracy is undefined: no successfully evaluated samples")
    return {
        "feature_id": feature_id,
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "accuracy": _safe_ratio(tp + tn, len(valid)),
        "false_alarm_rate": _safe_ratio(fp, fp + tn),
        "miss_rate": _safe_ratio(fn, fn + tp),
        "sample_count": len(records),
        "evaluated_count": len(valid),
        "error_count": len(records) - len(valid),
        "warnings": warnings,
    }

