import importlib
import os
import sys
from pathlib import Path


def import_opencv():
    """Import cv2, falling back to another local Conda environment."""
    original_error = None
    try:
        return importlib.import_module("cv2")
    except ModuleNotFoundError as exc:
        original_error = exc

    configured_path = os.environ.get("OVERLOAD_OPENCV_SITE_PACKAGES", "").strip()
    candidates = [Path(configured_path)] if configured_path else []

    conda_prefix = Path(sys.prefix)
    envs_directory = conda_prefix.parent
    if envs_directory.name.lower() == "envs" and envs_directory.is_dir():
        candidates.extend(
            environment / "Lib" / "site-packages"
            for environment in envs_directory.iterdir()
            if environment.is_dir() and environment != conda_prefix
        )

    for site_packages in candidates:
        if not (site_packages / "cv2").is_dir():
            continue
        site_packages_text = str(site_packages)
        if site_packages_text not in sys.path:
            sys.path.append(site_packages_text)
        try:
            return importlib.import_module("cv2")
        except (ImportError, ModuleNotFoundError):
            continue

    raise RuntimeError(
        "超载模型需要 OpenCV。请在 Predict 环境安装 opencv-python，"
        "或通过 OVERLOAD_OPENCV_SITE_PACKAGES 指定包含 cv2 的 site-packages。"
    ) from original_error
