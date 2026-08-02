"""HTTP endpoints for AIS/Radar trajectory matching."""

import logging
from pathlib import Path

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST
import torch
import torch_geometric

from .inference.data import DataValidationError
from .inference.predictor import (
    DEFAULT_CHECKPOINT_SHA256,
    InferenceConfig,
    checkpoint_sha256,
    get_matcher,
)
from .fusion_state import get_fusion_state, publish_fusion_result


LOGGER = logging.getLogger(__name__)


def _options():
    defaults = {
        "weights": Path(settings.BASE_DIR) / "AISRadar" / "weights" / "mainline_seed42_epoch40.pth",
        "device": "auto",
        "window_size": 6,
        "geometry_weight": 0.01,
        "match_threshold": 0.0,
        "sinkhorn_iterations": 20,
        "expected_sha256": DEFAULT_CHECKPOINT_SHA256,
    }
    defaults.update(getattr(settings, "AIS_RADAR_INFERENCE", {}))
    return defaults


def _config():
    options = _options()
    return InferenceConfig(
        checkpoint_path=Path(options["weights"]),
        device=str(options["device"]),
        window_size=int(options["window_size"]),
        geometry_weight=float(options["geometry_weight"]),
        match_threshold=float(options["match_threshold"]),
        sinkhorn_iterations=int(options["sinkhorn_iterations"]),
        expected_sha256=str(options.get("expected_sha256") or ""),
    )


def _as_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _optional_positive_int(value, field_name):
    if value in (None, ""):
        return None
    number = int(value)
    if number < 1:
        raise ValueError(f"{field_name} must be at least 1")
    return number


@require_GET
def health(request):
    config = _config()
    checkpoint_exists = config.checkpoint_path.is_file()
    actual_hash = checkpoint_sha256(config.checkpoint_path) if checkpoint_exists else None
    return JsonResponse(
        {
            "service": "AIS/Radar trajectory matching",
            "ready": checkpoint_exists
            and (not config.expected_sha256 or actual_hash == config.expected_sha256.upper()),
            "checkpoint": {
                "path": str(config.checkpoint_path),
                "exists": checkpoint_exists,
                "sha256": actual_hash,
            },
            "runtime": {
                "configured_device": config.device,
                "cuda_available": torch.cuda.is_available(),
                "torch": torch.__version__,
                "torch_geometric": torch_geometric.__version__,
            },
        }
    )


@require_GET
def fused_targets(request):
    """Return the latest AIS IDs matched with Radar targets for map styling."""
    return JsonResponse(get_fusion_state(), json_dumps_params={"ensure_ascii": False})


@require_POST
def match(request):
    ais_file = request.FILES.get("ais_file")
    radar_file = request.FILES.get("radar_file")
    if ais_file is None or radar_file is None:
        return JsonResponse(
            {"error": "multipart fields 'ais_file' and 'radar_file' are required"},
            status=400,
        )

    try:
        all_windows = _as_bool(request.POST.get("all_windows"), default=False)
        stride = _optional_positive_int(request.POST.get("stride"), "stride") or 1
        max_windows = _optional_positive_int(request.POST.get("max_windows"), "max_windows")
        matcher = get_matcher(_config())
        result = matcher.predict_files(
            ais_file,
            radar_file,
            ais_filename=ais_file.name,
            radar_filename=radar_file.name,
            latest_only=not all_windows,
            stride=stride,
            max_windows=max_windows,
        )
        result["fusion_state"] = publish_fusion_result(result)
        return JsonResponse(result, json_dumps_params={"ensure_ascii": False})
    except (DataValidationError, ValueError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except (FileNotFoundError, RuntimeError) as exc:
        LOGGER.exception("AIS/Radar inference is unavailable")
        return JsonResponse({"error": str(exc)}, status=503)
    except Exception:
        LOGGER.exception("Unexpected AIS/Radar inference failure")
        return JsonResponse({"error": "AIS/Radar inference failed"}, status=500)
