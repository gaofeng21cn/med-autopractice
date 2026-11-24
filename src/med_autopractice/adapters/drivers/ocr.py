"""Local offline OCR engine abstraction for IP KVM and fallback inspection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class OCRWord:
    text: str
    confidence: float
    box: tuple[int, int, int, int]

    def to_dict(self) -> dict:
        return {"text": self.text, "confidence": self.confidence, "box": list(self.box)}


class OCREngine(Protocol):
    def recognize(
        self, image_bytes: bytes, *, region: tuple[int, int, int, int] | None = None
    ) -> tuple[OCRWord, ...]: ...


class CallableOCREngine:
    """Wrap an injected OCR callable (for tests or site-configured engines)."""

    def __init__(self, fn: Callable[[bytes, tuple[int, int, int, int] | None], tuple[OCRWord, ...]]):
        self._fn = fn

    def recognize(
        self, image_bytes: bytes, *, region: tuple[int, int, int, int] | None = None
    ) -> tuple[OCRWord, ...]:
        return tuple(self._fn(image_bytes, region))


class RapidOCRBackend:
    """Offline RapidOCR backend loaded lazily only when `[ocr]` extra is used."""

    def __init__(self):
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-not-found]
        except ImportError as error:
            raise RuntimeError(
                "install optional dependency 'ocr' (pip install med-autopractice[ocr])"
            ) from error
        self._ocr = RapidOCR()

    def recognize(
        self, image_bytes: bytes, *, region: tuple[int, int, int, int] | None = None
    ) -> tuple[OCRWord, ...]:
        result, _ = self._ocr(image_bytes)
        if not result:
            return ()
        words: list[OCRWord] = []
        for poly, text, score in result:
            xs = [int(p[0]) for p in poly]
            ys = [int(p[1]) for p in poly]
            left, top = min(xs), min(ys)
            width, height = max(xs) - left, max(ys) - top
            if region is not None:
                if not (region[0] <= left < region[2] and region[1] <= top < region[3]):
                    continue
            words.append(OCRWord(str(text), float(score) * 100.0, (left, top, width, height)))
        return tuple(words)
