import json
from collections import defaultdict
from pathlib import Path

import numpy as np
import pyarrow.parquet as parquet
from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Build the compact deviation route-corridor index."

    def add_arguments(self, parser):
        parser.add_argument("--grid-degrees", type=float, default=0.002)
        parser.add_argument("--min-sog", type=float, default=2.0)
        parser.add_argument("--min-cell-points", type=int, default=10)

    def handle(self, *args, **options):
        source = (
            Path(settings.BASE_DIR)
            / "Deviation"
            / "knowledge"
            / "QZHX_trajectories.parquet"
        )
        destination = (
            Path(settings.BASE_DIR)
            / "Deviation"
            / "knowledge"
            / "QZHX_route_samples.npz"
        )
        grid = max(0.0001, options["grid_degrees"])
        min_sog = max(0.0, options["min_sog"])
        min_cell_points = max(1, options["min_cell_points"])

        counts = defaultdict(int)
        lat_sums = defaultdict(float)
        lon_sums = defaultdict(float)
        cos_sums = defaultdict(float)
        sin_sums = defaultdict(float)

        source_file = parquet.ParquetFile(source)
        total_valid = 0
        for batch in source_file.iter_batches(
            batch_size=500_000,
            columns=["Longitude", "Latitude", "COG", "SOG"],
        ):
            frame = batch.to_pandas()
            lon = frame["Longitude"].to_numpy(dtype=float)
            lat = frame["Latitude"].to_numpy(dtype=float)
            course = frame["COG"].to_numpy(dtype=float)
            speed = frame["SOG"].to_numpy(dtype=float)
            valid = (
                np.isfinite(lon)
                & np.isfinite(lat)
                & np.isfinite(course)
                & np.isfinite(speed)
                & (lon >= -180)
                & (lon <= 180)
                & (lat >= -90)
                & (lat <= 90)
                & (course >= 0)
                & (course < 360)
                & (speed >= min_sog)
            )
            lon = lon[valid]
            lat = lat[valid]
            course = course[valid]
            total_valid += len(lon)
            if len(lon) == 0:
                continue

            lat_bin = np.floor(lat / grid).astype(np.int64)
            lon_bin = np.floor(lon / grid).astype(np.int64)
            keys = lat_bin * 1_000_000 + lon_bin
            unique, inverse = np.unique(keys, return_inverse=True)
            radians_double = np.deg2rad(course * 2)
            batch_count = np.bincount(inverse)
            batch_lat = np.bincount(inverse, weights=lat)
            batch_lon = np.bincount(inverse, weights=lon)
            batch_cos = np.bincount(
                inverse,
                weights=np.cos(radians_double),
            )
            batch_sin = np.bincount(
                inverse,
                weights=np.sin(radians_double),
            )
            for index, raw_key in enumerate(unique):
                key = int(raw_key)
                counts[key] += int(batch_count[index])
                lat_sums[key] += float(batch_lat[index])
                lon_sums[key] += float(batch_lon[index])
                cos_sums[key] += float(batch_cos[index])
                sin_sums[key] += float(batch_sin[index])

        retained = [
            key for key, count in counts.items() if count >= min_cell_points
        ]
        retained.sort()
        latitudes = []
        longitudes = []
        axes = []
        coherence = []
        cell_counts = []
        for key in retained:
            count = counts[key]
            latitudes.append(lat_sums[key] / count)
            longitudes.append(lon_sums[key] / count)
            angle = np.degrees(
                0.5 * np.arctan2(sin_sums[key], cos_sums[key])
            ) % 180
            axes.append(angle)
            coherence.append(
                min(
                    1.0,
                    np.hypot(cos_sums[key], sin_sums[key]) / count,
                )
            )
            cell_counts.append(count)

        metadata = {
            "source": source.name,
            "source_rows": source_file.metadata.num_rows,
            "source_time_range": [
                "2018-10-01T00:00:00",
                "2018-10-31T23:59:59",
            ],
            "grid_degrees": grid,
            "min_sog_knots": min_sog,
            "min_cell_points": min_cell_points,
            "valid_moving_points": total_valid,
            "sample_count": len(retained),
        }
        np.savez_compressed(
            destination,
            latitudes=np.asarray(latitudes, dtype=np.float32),
            longitudes=np.asarray(longitudes, dtype=np.float32),
            axes=np.asarray(axes, dtype=np.float32),
            coherence=np.asarray(coherence, dtype=np.float32),
            counts=np.asarray(cell_counts, dtype=np.int32),
            metadata_json=np.asarray(
                json.dumps(metadata, ensure_ascii=False)
            ),
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Built {destination} with {len(retained)} route cells "
                f"from {total_valid} moving points."
            )
        )
