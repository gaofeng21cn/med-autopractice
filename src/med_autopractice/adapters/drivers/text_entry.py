"""Three-tier Chinese long-text entry strategies for IP KVM (B primary fallback / A optional / C draft_only)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from med_autopractice.adapters.ui import UIStateError


class ChineseEntryUnqualifiedError(UIStateError):
    """Strategy C safety floor: site is unqualified for KVM Chinese entry; keep draft_only."""


@dataclass(frozen=True)
class TextEntryResult:
    strategy: str
    total_chars: int
    segments_count: int
    retries: int
    verified_accuracy: float
    text_readback: str


class TextEntryStrategy(Protocol):
    name: str

    def enter_text(
        self,
        text: str,
        *,
        hid_sender: Callable[[str], None],
        backspace_sender: Callable[[int], None],
        ocr_reader: Callable[[tuple[int, int, int, int] | None], str],
        clipboard_paster: Callable[[str], bool] | None = None,
        verify_region: tuple[int, int, int, int] | None = None,
    ) -> TextEntryResult: ...


def segment_chinese_text(text: str, max_segment_chars: int = 30) -> list[str]:
    """Split clinical prose along punctuation boundaries without exceeding max_segment_chars."""

    if max_segment_chars < 1:
        raise ValueError("max_segment_chars must be >= 1")
    if not text:
        return []
    breaks = set("。；;，,\n！？!?")
    segments: list[str] = []
    buf: list[str] = []
    for ch in text:
        buf.append(ch)
        if len(buf) >= max_segment_chars or (ch in breaks and len(buf) >= max(8, max_segment_chars // 2)):
            segments.append("".join(buf))
            buf.clear()
    if buf:
        segments.append("".join(buf))
    return segments


class PureHIDSegmentedStrategy:
    """Strategy B (primary fallback): segmented ASCII/IME HID injection + local OCR verification."""

    name = "pure_hid_ocr"

    def __init__(
        self,
        *,
        max_segment_chars: int = 30,
        max_retries: int = 2,
        allow_unicode_hid: bool = False,
        ime_composer: Callable[[str], str] | None = None,
    ):
        self.max_segment_chars = max_segment_chars
        self.max_retries = max_retries
        self.allow_unicode_hid = allow_unicode_hid
        self.ime_composer = ime_composer

    def enter_text(
        self,
        text: str,
        *,
        hid_sender: Callable[[str], None],
        backspace_sender: Callable[[int], None],
        ocr_reader: Callable[[tuple[int, int, int, int] | None], str],
        clipboard_paster: Callable[[str], bool] | None = None,
        verify_region: tuple[int, int, int, int] | None = None,
    ) -> TextEntryResult:
        if not text.isascii() and not self.allow_unicode_hid and self.ime_composer is None:
            raise ChineseEntryUnqualifiedError(
                "HID text transport accepts ASCII only; non-ASCII Chinese requires qualified IME composer or clipboard relay"
            )
        segments = segment_chinese_text(text, self.max_segment_chars)
        accepted = ""
        retries = 0
        for index, seg in enumerate(segments):
            if not seg.isascii() and self.ime_composer is not None:
                payload = self.ime_composer(seg)
                if not payload or not payload.isascii():
                    raise ChineseEntryUnqualifiedError(
                        "IME composer must emit non-empty ASCII keystrokes for USB HID"
                    )
            else:
                payload = seg
            expected = accepted + seg
            verified = False
            for attempt in range(self.max_retries + 1):
                hid_sender(payload)
                observed = ocr_reader(verify_region)
                if observed.endswith(expected) or expected in observed:
                    accepted = expected
                    verified = True
                    break
                retries += 1
                # Determine mismatched tail length relative to accepted prefix and backspace.
                idx = observed.rfind(accepted) if accepted else 0
                if accepted and idx >= 0:
                    extra = len(observed) - (idx + len(accepted))
                else:
                    extra = len(seg)
                if extra > 0:
                    backspace_sender(extra)
                if attempt == self.max_retries:
                    raise UIStateError(
                        f"pure HID OCR verification failed at segment {index + 1}: "
                        f"expected {seg!r}, observed {observed[-len(expected):]!r}"
                    )
            if not verified:
                raise UIStateError(f"pure HID segment {index + 1} could not be verified")

        final_observed = ocr_reader(verify_region)
        if text not in final_observed:
            raise UIStateError("final full-text OCR readback mismatch after pure HID entry")
        return TextEntryResult(
            strategy=self.name,
            total_chars=len(text),
            segments_count=len(segments),
            retries=retries,
            verified_accuracy=1.0,
            text_readback=text,
        )


class ClipboardRelayStrategy:
    """Strategy A (optional accelerator when hospital policy permits KVM clipboard or relay)."""

    name = "clipboard_relay"

    def __init__(self, *, allow_clipboard_relay: bool = False):
        self.allow_clipboard_relay = allow_clipboard_relay

    def enter_text(
        self,
        text: str,
        *,
        hid_sender: Callable[[str], None],
        backspace_sender: Callable[[int], None],
        ocr_reader: Callable[[tuple[int, int, int, int] | None], str],
        clipboard_paster: Callable[[str], bool] | None = None,
        verify_region: tuple[int, int, int, int] | None = None,
    ) -> TextEntryResult:
        if not self.allow_clipboard_relay:
            raise UIStateError("clipboard relay strategy is disabled by site policy")
        if clipboard_paster is None or not clipboard_paster(text):
            raise UIStateError("KVM transport rejected clipboard relay paste")
        observed = ocr_reader(verify_region)
        if text not in observed:
            raise UIStateError("clipboard relay OCR readback mismatch")
        return TextEntryResult(
            strategy=self.name,
            total_chars=len(text),
            segments_count=1,
            retries=0,
            verified_accuracy=1.0,
            text_readback=text,
        )


class DraftOnlyFallbackStrategy:
    """Strategy C (safety floor): disallow unverified KVM Chinese input and keep draft_only."""

    name = "draft_only_fallback"

    def enter_text(
        self,
        text: str,
        *,
        hid_sender: Callable[[str], None],
        backspace_sender: Callable[[int], None],
        ocr_reader: Callable[[tuple[int, int, int, int] | None], str],
        clipboard_paster: Callable[[str], bool] | None = None,
        verify_region: tuple[int, int, int, int] | None = None,
    ) -> TextEntryResult:
        raise ChineseEntryUnqualifiedError(
            "IP KVM site has not passed Chinese long-text qualification; keep draft_only"
        )
