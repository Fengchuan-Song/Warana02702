"""AIS/Radar inference package with lazy public model imports.

Importing a submodule such as :mod:`AISRadar.inference.data` must not load
PyTorch/CUDA in unrelated Django worker processes.
"""

__all__ = ["AISRadarMatcher", "InferenceConfig", "get_matcher"]


def __getattr__(name):
    if name in __all__:
        from .predictor import AISRadarMatcher, InferenceConfig, get_matcher

        exports = {
            "AISRadarMatcher": AISRadarMatcher,
            "InferenceConfig": InferenceConfig,
            "get_matcher": get_matcher,
        }
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
