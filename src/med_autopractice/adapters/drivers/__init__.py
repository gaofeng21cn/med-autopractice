"""Built-in Computer Use and IP KVM channel drivers."""

from med_autopractice.adapters.drivers.browser_cdp import BrowserCDPDriver
from med_autopractice.adapters.drivers.desktop_win import WindowsUIADriver
from med_autopractice.adapters.drivers.kvm_driver import (
    GLKVMNativeTransport,
    KVMTransport,
    KVMUIDriver,
    PiKVMHttpTransport,
    VNCHidTransport,
)
from med_autopractice.adapters.drivers.ocr import CallableOCREngine, OCREngine, OCRWord, RapidOCRBackend
from med_autopractice.adapters.drivers.text_entry import (
    ChineseEntryUnqualifiedError,
    ClipboardRelayStrategy,
    DraftOnlyFallbackStrategy,
    PureHIDSegmentedStrategy,
    TextEntryResult,
    TextEntryStrategy,
)

__all__ = [
    "BrowserCDPDriver",
    "CallableOCREngine",
    "ChineseEntryUnqualifiedError",
    "ClipboardRelayStrategy",
    "DraftOnlyFallbackStrategy",
    "GLKVMNativeTransport",
    "KVMTransport",
    "KVMUIDriver",
    "OCREngine",
    "OCRWord",
    "PiKVMHttpTransport",
    "PureHIDSegmentedStrategy",
    "RapidOCRBackend",
    "TextEntryResult",
    "TextEntryStrategy",
    "VNCHidTransport",
    "WindowsUIADriver",
]
