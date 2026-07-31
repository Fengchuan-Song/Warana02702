import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from django.conf import settings

from .runtime import import_opencv


cv2 = import_opencv()

VENDOR_DIRECTORY = Path(__file__).resolve().parent / "vendor"
if str(VENDOR_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIRECTORY))

from ultralytics import YOLO  # noqa: E402


@dataclass(frozen=True)
class OverloadInference:
    overloaded: bool
    reason: str
    confidence: float
    detections: tuple
    vessel_present: bool = True
    vessel_confidence: float = 0.0
    vessel_detections: tuple = ()

    def as_dict(self):
        return {
            "overloaded": self.overloaded,
            "reason": self.reason,
            "confidence": self.confidence,
            "detections": list(self.detections),
            "vessel_present": self.vessel_present,
            "vessel_confidence": self.vessel_confidence,
            "vessel_detections": list(self.vessel_detections),
        }


class OverloadDetector:
    """
    Video-frame overload detector migrated from the standalone project.

    The load-line model treats class 0 (light load) as normal and class 1
    (full load) as overload. When it finds no load line, a separate vessel
    model distinguishes "vessel with no load line" from "no vessel".
    """

    VESSEL_CLASS_NAMES = frozenset({"boat", "ship", "vessel"})

    def __init__(
        self,
        weights=None,
        confidence=0.5,
        device=None,
        vessel_weights=None,
        vessel_confidence=None,
    ):
        config = getattr(settings, "OVERLOAD_VIDEO_DETECTION", {})
        self.weights = Path(
            weights
            or config.get(
                "weights",
                Path(__file__).resolve().parent / "weights" / "best.pt",
            )
        ).resolve()
        if not self.weights.is_file():
            raise FileNotFoundError(f"超载模型权重不存在：{self.weights}")

        self.confidence = float(confidence)
        if not 0 < self.confidence <= 1:
            raise ValueError("置信度阈值必须在 0 到 1 之间")
        self.device = device
        self.model = YOLO(str(self.weights))

        self.vessel_weights = Path(
            vessel_weights
            or config.get(
                "vessel_weights",
                Path(__file__).resolve().parent
                / "weights"
                / "yolov8n.pt",
            )
        ).resolve()
        if not self.vessel_weights.is_file():
            raise FileNotFoundError(
                f"船舶检测模型权重不存在：{self.vessel_weights}"
            )
        self.vessel_confidence = float(
            vessel_confidence
            if vessel_confidence is not None
            else config.get("vessel_confidence", 0.25)
        )
        if not 0 < self.vessel_confidence <= 1:
            raise ValueError("船舶置信度阈值必须在 0 到 1 之间")

        self.vessel_model = YOLO(str(self.vessel_weights))
        vessel_names = self.vessel_model.names
        name_items = (
            vessel_names.items()
            if isinstance(vessel_names, dict)
            else enumerate(vessel_names)
        )
        self.vessel_class_ids = [
            int(class_id)
            for class_id, class_name in name_items
            if str(class_name).strip().lower() in self.VESSEL_CLASS_NAMES
        ]
        if not self.vessel_class_ids:
            raise ValueError(
                "船舶检测模型中没有 boat、ship 或 vessel 类别"
            )

    def warmup(self, image_size=640):
        """Warm both models before subscribing to live frames."""
        frame = np.zeros((image_size, image_size, 3), dtype=np.uint8)
        self.model.predict(
            source=frame,
            conf=self.confidence,
            device=self.device,
            verbose=False,
        )
        self.vessel_model.predict(
            source=frame,
            conf=self.vessel_confidence,
            classes=self.vessel_class_ids,
            device=self.device,
            verbose=False,
        )

    def decode_jpeg(self, jpeg_bytes):
        encoded = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        frame = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError("无法解码摄像头 JPEG 视频帧")
        return frame

    def predict_jpeg(self, jpeg_bytes):
        return self.predict_frame(self.decode_jpeg(jpeg_bytes))

    def predict_frame(self, frame):
        results = self.model.predict(
            source=frame,
            conf=self.confidence,
            device=self.device,
            verbose=False,
        )
        detections = self._collect_detections(results[0], self.model)

        if not detections:
            vessel_results = self.vessel_model.predict(
                source=frame,
                conf=self.vessel_confidence,
                classes=self.vessel_class_ids,
                device=self.device,
                verbose=False,
            )
            vessel_detections = self._collect_detections(
                vessel_results[0],
                self.vessel_model,
            )
            return self._classify_detections(
                detections,
                vessel_detections,
            )

        return self._classify_detections(detections)

    @staticmethod
    def _collect_detections(result, model):
        detections = []
        boxes = result.boxes
        for box in boxes:
            class_id = int(box.cls.item())
            confidence = float(box.conf.item())
            detections.append(
                {
                    "class_id": class_id,
                    "class_name": str(
                        model.names.get(class_id, class_id)
                    ),
                    "confidence": round(confidence, 6),
                    "xyxy": [
                        round(float(value), 2)
                        for value in box.xyxy[0].tolist()
                    ],
                }
            )
        return detections

    @staticmethod
    def _classify_detections(detections, vessel_detections=()):
        if not detections:
            if not vessel_detections:
                return OverloadInference(
                    overloaded=False,
                    reason="画面中未检测到船舶，不触发超载预警",
                    confidence=0.0,
                    detections=(),
                    vessel_present=False,
                    vessel_confidence=0.0,
                    vessel_detections=(),
                )

            best_vessel = max(
                vessel_detections,
                key=lambda item: item["confidence"],
            )
            return OverloadInference(
                overloaded=True,
                reason="检测到船舶但未检测到载重线，船舶疑似超载",
                confidence=best_vessel["confidence"],
                detections=(),
                vessel_present=True,
                vessel_confidence=best_vessel["confidence"],
                vessel_detections=tuple(vessel_detections),
            )

        overload_detections = [
            item for item in detections if item["class_id"] != 0
        ]
        if overload_detections:
            best = max(
                overload_detections,
                key=lambda item: item["confidence"],
            )
            return OverloadInference(
                overloaded=True,
                reason="载重线不完整，船舶疑似超载",
                confidence=best["confidence"],
                detections=tuple(detections),
                vessel_present=True,
            )

        best = max(detections, key=lambda item: item["confidence"])
        return OverloadInference(
            overloaded=False,
            reason="载重线完整，船舶未超载",
            confidence=best["confidence"],
            detections=tuple(detections),
            vessel_present=True,
        )
