"""Load versioned AIS and AIS/Radar evaluation sample manifests."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from .schemas import EvaluationDataset, EvaluationSample


MANIFEST_NAMES = ("samples.json", "samples.jsonl", "samples.csv")


class DatasetError(ValueError):
    pass


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetError(f"Could not read JSON {path}: {exc}") from exc


def _read_jsonl(path):
    rows = []
    try:
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8-sig").splitlines(), 1
        ):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise DatasetError(
                    f"{path}:{line_number} must contain a JSON object"
                )
            rows.append(value)
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetError(f"Could not read JSONL {path}: {exc}") from exc
    return rows


def _read_csv(path):
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except (OSError, csv.Error) as exc:
        raise DatasetError(f"Could not read CSV {path}: {exc}") from exc


def _read_tabular(path):
    suffix = path.suffix.casefold()
    if suffix == ".csv":
        return _read_csv(path)
    if suffix in {".jsonl", ".ndjson"}:
        return _read_jsonl(path)
    if suffix == ".json":
        value = _read_json(path)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for key in ("records", "rows", "data", "ais", "radar"):
                if isinstance(value.get(key), list):
                    return value[key]
        raise DatasetError(f"Sensor file must contain a record list: {path}")
    raise DatasetError(f"Unsupported data file format: {path}")


def _manifest_path(source):
    source = Path(source).expanduser().resolve()
    if source.is_file():
        return source
    if not source.is_dir():
        raise DatasetError(f"Dataset does not exist: {source}")
    for name in MANIFEST_NAMES:
        candidate = source / name
        if candidate.is_file():
            return candidate
    raise DatasetError(
        f"Dataset directory must contain one of: {', '.join(MANIFEST_NAMES)}"
    )


def _manifest_document(path):
    if path.suffix.casefold() == ".json":
        value = _read_json(path)
    elif path.suffix.casefold() in {".jsonl", ".ndjson"}:
        value = _read_jsonl(path)
    elif path.suffix.casefold() == ".csv":
        value = _read_csv(path)
    else:
        raise DatasetError(f"Unsupported manifest format: {path}")

    if isinstance(value, list):
        return value, {}, ""
    if not isinstance(value, dict):
        raise DatasetError("Dataset manifest must be an object or a sample list")
    samples = value.get("samples")
    if not isinstance(samples, list):
        # A single-sample JSON manifest is useful for small hand-built cases.
        if value.get("sample_id"):
            return [value], {}, str(value.get("dataset_version") or "")
        raise DatasetError("Dataset manifest object must contain a samples list")
    metadata = value.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    version = str(value.get("dataset_version") or value.get("version") or "")
    return samples, metadata, version


def _binary_label(value, sample_id):
    if isinstance(value, bool):
        return int(value)
    text = str(value).strip().casefold()
    if text in {"1", "true", "positive", "yes"}:
        return 1
    if text in {"0", "false", "negative", "no"}:
        return 0
    raise DatasetError(f"Sample {sample_id}: ground_truth must be 0 or 1")


def _record_list(value, file_value, base_dir, field_name, used_files):
    if isinstance(value, str) and value.strip():
        stripped = value.strip()
        if stripped.startswith("["):
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise DatasetError(f"Invalid embedded {field_name} JSON: {exc}") from exc
        elif not file_value:
            file_value = stripped
            value = None
    if value in (None, ""):
        if not file_value:
            return []
        path = Path(str(file_value))
        if not path.is_absolute():
            path = base_dir / path
        path = path.resolve()
        if not path.is_file():
            raise DatasetError(f"Referenced {field_name} file does not exist: {path}")
        used_files.add(path)
        value = _read_tabular(path)
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise DatasetError(f"{field_name} must be a list of objects")
    return [dict(row) for row in value]


def _fingerprint(paths, base_dir):
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: str(item).casefold()):
        try:
            label = path.relative_to(base_dir).as_posix()
        except ValueError:
            label = path.name
        digest.update(label.encode("utf-8"))
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise DatasetError(f"Could not hash dataset file {path}: {exc}") from exc
    return digest.hexdigest()


def load_dataset(source, *, feature_id):
    manifest = _manifest_path(source)
    raw_samples, metadata, version = _manifest_document(manifest)
    used_files = {manifest}
    samples = []
    seen = set()
    for index, raw in enumerate(raw_samples, 1):
        if not isinstance(raw, dict):
            raise DatasetError(f"Sample #{index} must be an object")
        sample_id = str(raw.get("sample_id") or "").strip()
        if not sample_id:
            raise DatasetError(f"Sample #{index} is missing sample_id")
        if sample_id in seen:
            raise DatasetError(f"Duplicate sample_id: {sample_id}")
        seen.add(sample_id)
        sample_feature = str(raw.get("feature_id") or feature_id).strip()
        if sample_feature != feature_id:
            raise DatasetError(
                f"Sample {sample_id}: feature_id {sample_feature!r} does not "
                f"match requested {feature_id!r}"
            )
        ground_truth = _binary_label(
            raw.get("ground_truth", raw.get("label")), sample_id
        )
        ais = _record_list(
            raw.get("ais"),
            raw.get("ais_file", raw.get("ais_path")),
            manifest.parent,
            "AIS",
            used_files,
        )
        radar = _record_list(
            raw.get("radar"),
            raw.get("radar_file", raw.get("radar_path")),
            manifest.parent,
            "Radar",
            used_files,
        )
        if not ais:
            raise DatasetError(f"Sample {sample_id}: AIS data is empty")
        sample_metadata = raw.get("metadata")
        if not isinstance(sample_metadata, dict):
            sample_metadata = {}
        samples.append(
            EvaluationSample(
                sample_id=sample_id,
                track_id=str(raw.get("track_id") or sample_id).strip(),
                feature_id=sample_feature,
                ground_truth=ground_truth,
                ais=tuple(ais),
                radar=tuple(radar),
                metadata=sample_metadata,
            )
        )
    if not samples:
        raise DatasetError("Dataset contains no samples")
    return EvaluationDataset(
        source=manifest,
        samples=tuple(samples),
        version=version,
        metadata=metadata,
        fingerprint=_fingerprint(used_files, manifest.parent),
    )
