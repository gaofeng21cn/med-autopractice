"""Track B (Fallback Form): Hardware-agnostic IP KVM transport plugins and UIDriver."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from med_autopractice.adapters.drivers.ocr import OCREngine, OCRWord
from med_autopractice.adapters.drivers.text_entry import (
    ChineseEntryUnqualifiedError,
    ClipboardRelayStrategy,
    DraftOnlyFallbackStrategy,
    PureHIDSegmentedStrategy,
    TextEntryStrategy,
)
from med_autopractice.platform.security import is_windows
from med_autopractice.adapters.playbook_runner import (
    HumanInterruptError, ScreenObservation, TableData,
)
from med_autopractice.adapters.ui import UIStateError
from med_autopractice.domain.task import SubjectRef


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class KVMTransport(Protocol):
    """Hardware-agnostic IP KVM device transport."""

    def status(self) -> dict[str, Any]: ...

    def grab_frame(
        self,
        *,
        region: tuple[int, int, int, int] | None = None,
        ocr: bool = True,
        include_image: bool = False,
    ) -> dict[str, Any]: ...

    def click(self, x: int, y: int, *, frame_id: str | None = None) -> dict[str, Any]: ...

    def double_click(self, x: int, y: int, *, frame_id: str | None = None) -> dict[str, Any]: ...

    def send_keys(self, keys: str, *, frame_id: str | None = None) -> dict[str, Any]: ...

    def send_text_hid(self, text: str, *, frame_id: str | None = None) -> dict[str, Any]: ...

    def backspace(self, count: int, *, frame_id: str | None = None) -> dict[str, Any]: ...

    def scroll(
        self,
        delta: int,
        *,
        x: int | None = None,
        y: int | None = None,
        frame_id: str | None = None,
    ) -> dict[str, Any]: ...

    def set_clipboard(self, text: str) -> bool: ...

    def save_shot(
        self,
        destination: Path,
        *,
        region: tuple[int, int, int, int] | None = None,
        label: str | None = None,
    ) -> str: ...


class GLKVMNativeTransport:
    """Configurable adapter for glkvm-native matching gateway.RemoteClient contract."""

    supports_unicode_hid = False

    def __init__(self, *, receipt_path: Path | None = None, profile: str | None = None):
        self.receipt_path = receipt_path
        self.profile = profile or os.environ.get("GLKVM_PROFILE")
        self._client = None
        self._last_frame_id: str | None = None
        self._last_size: tuple[int, int] = (1280, 720)

    @classmethod
    def discover_receipt(cls) -> Path | None:
        env_path = os.environ.get("GLKVM_INSTALLATION", "").strip()
        if env_path and Path(env_path).is_file():
            return Path(env_path)
        opl_bin = os.environ.get("OPL_BIN") or shutil.which("opl")
        if opl_bin:
            try:
                proc = subprocess.run(
                    [opl_bin, "packages", "locate", "glkvm-native", "--json"],
                    capture_output=True, text=True, timeout=5, check=False,
                )
                if proc.returncode == 0:
                    loc = json.loads(proc.stdout).get("opl_package_location", {})
                    root = loc.get("package_root") if loc.get("found") else None
                    if isinstance(root, str) and root:
                        candidate = Path(root) / "installation.json"
                        if candidate.is_file():
                            return candidate
            except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, TypeError):
                pass
        codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        fallback = codex_home / "glkvm-native" / "installation.json"
        return fallback if fallback.is_file() else None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        receipt_file = self.receipt_path or self.discover_receipt()
        if receipt_file is None or not receipt_file.is_file():
            raise UIStateError("glkvm-native receipt not found; configure receipt_path or GLKVM_INSTALLATION")
        receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
        import sys

        runtime = str(receipt["runtime"])
        if runtime not in sys.path:
            sys.path.insert(0, runtime)
        from gateway import RemoteClient  # type: ignore[import-not-found]

        self._client = RemoteClient(self.profile or receipt["profile"])
        return self._client

    def _run(self, coro):
        return asyncio.run(coro)

    def _track_meta(self, meta: dict[str, Any]) -> dict[str, Any]:
        if meta.get("frame_id"):
            self._last_frame_id = str(meta["frame_id"])
        orig = meta.get("original_size")
        if isinstance(orig, (list, tuple)) and len(orig) == 2:
            self._last_size = (int(orig[0]), int(orig[1]))
        return meta

    def status(self) -> dict[str, Any]:
        return dict(self._run(self._ensure_client().status()))

    def grab_frame(
        self, *, region=None, ocr: bool = True, include_image: bool = False
    ) -> dict[str, Any]:
        meta, image = self._run(
            self._ensure_client().observe(list(region) if region else None, ocr=ocr, include_image=include_image)
        )
        self._track_meta(meta)
        return {**meta, "image": image}

    def click(self, x: int, y: int, *, frame_id: str | None = None) -> dict[str, Any]:
        fid = frame_id or self._last_frame_id or ""
        meta, _ = self._run(
            self._ensure_client().action(fid, "click", x=int(x), y=int(y), count=1, result_mode="ocr")
        )
        return self._track_meta(meta)

    def double_click(self, x: int, y: int, *, frame_id: str | None = None) -> dict[str, Any]:
        fid = frame_id or self._last_frame_id or ""
        meta, _ = self._run(
            self._ensure_client().action(fid, "click", x=int(x), y=int(y), count=2, result_mode="ocr")
        )
        return self._track_meta(meta)

    def send_keys(self, keys: str, *, frame_id: str | None = None) -> dict[str, Any]:
        fid = frame_id or self._last_frame_id or ""
        meta, _ = self._run(self._ensure_client().action(fid, "keys", keys=keys, result_mode="ocr"))
        return self._track_meta(meta)

    def send_text_hid(self, text: str, *, frame_id: str | None = None) -> dict[str, Any]:
        if not text or not text.isascii():
            raise ChineseEntryUnqualifiedError("glkvm-native HID text entry accepts non-empty ASCII only")
        fid = frame_id or self._last_frame_id or ""
        meta, _ = self._run(self._ensure_client().action(fid, "text", text=text, result_mode="ocr"))
        return self._track_meta(meta)

    def backspace(self, count: int, *, frame_id: str | None = None) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        next_fid = frame_id or self._last_frame_id
        for _ in range(count):
            meta = self.send_keys("Backspace", frame_id=next_fid)
            next_fid = str(meta.get("frame_id") or self._last_frame_id or "")
        return meta

    def scroll(self, delta: int, *, x=None, y=None, frame_id: str | None = None) -> dict[str, Any]:
        fid = frame_id or self._last_frame_id or ""
        raw_dy = int(delta)
        dy = -1 if raw_dy == 0 else max(-20, min(20, raw_dy))
        tx = self._last_size[0] // 2 if x is None else int(x)
        ty = self._last_size[1] // 2 if y is None else int(y)
        meta, _ = self._run(
            self._ensure_client().action(fid, "scroll", dy=dy, x=tx, y=ty, result_mode="ocr")
        )
        return self._track_meta(meta)

    def set_clipboard(self, text: str) -> bool:
        return False

    def save_shot(self, destination: Path, *, region=None, label: str | None = None) -> str:
        meta = self._run(self._ensure_client().shot(region=list(region) if region else None, label=label))
        shot_path = meta.get("shot_path")
        if shot_path:
            src = Path(str(shot_path))
            if src.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, destination)
                if not is_windows():
                    destination.chmod(0o600)
                return str(destination)
            return str(shot_path)
        return str(destination)

    def release_lease(self) -> dict[str, Any]:
        if self._client is None:
            return {"released": False}
        return dict(self._run(self._client.release()))


class PiKVMHttpTransport:
    """Standard PiKVM / JetKVM / NanoKVM HTTP API transport (HTTPS or loopback HTTP)."""

    def __init__(
        self,
        base_url: str,
        *,
        auth_header: Callable[[], str] | None = None,
        timeout: float = 10.0,
    ):
        parsed = urlsplit(base_url)
        loopback = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if not parsed.hostname or not (parsed.scheme == "https" or loopback):
            raise ValueError("PiKVMHttpTransport requires HTTPS or loopback HTTP URL")
        self.base_url = base_url.rstrip("/")
        self.auth_header = auth_header
        self.timeout = timeout
        self._opener = build_opener(_NoRedirect())

    def _request(self, method: str, path: str, *, body: bytes | None = None, query: dict | None = None) -> bytes:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "text/plain; charset=utf-8"
        if self.auth_header:
            headers["Authorization"] = self.auth_header()
        req = Request(url, data=body, headers=headers, method=method)
        with self._opener.open(req, timeout=self.timeout) as resp:
            return resp.read()

    def status(self) -> dict[str, Any]:
        data = json.loads(self._request("GET", "/api/info").decode("utf-8"))
        return {"connected": bool(data.get("ok", True)), "backend": "pikvm", "info": data.get("result", data)}

    def grab_frame(
        self, *, region=None, ocr: bool = True, include_image: bool = True
    ) -> dict[str, Any]:
        image = self._request("GET", "/api/streamer/snapshot")
        return {
            "frame_id": "pikvm-" + uuid4().hex[:8],
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "original_size": [1280, 720],
            "image": image,
            "ocr": {"words": []},
        }

    def click(self, x: int, y: int, *, frame_id: str | None = None) -> dict[str, Any]:
        self._request("POST", "/api/hid/events/send_mouse_move", query={"to_x": x, "to_y": y})
        self._request("POST", "/api/hid/events/send_mouse_button", query={"button": "left", "state": 1})
        self._request("POST", "/api/hid/events/send_mouse_button", query={"button": "left", "state": 0})
        return self.grab_frame(include_image=True)

    def double_click(self, x: int, y: int, *, frame_id: str | None = None) -> dict[str, Any]:
        self.click(x, y, frame_id=frame_id)
        return self.click(x, y, frame_id=frame_id)

    def send_keys(self, keys: str, *, frame_id: str | None = None) -> dict[str, Any]:
        self._request("POST", "/api/hid/events/send_key", query={"key": keys})
        return self.grab_frame(include_image=True)

    def send_text_hid(self, text: str, *, frame_id: str | None = None) -> dict[str, Any]:
        self._request("POST", "/api/hid/print", body=text.encode("utf-8"))
        return self.grab_frame(include_image=True)

    def backspace(self, count: int, *, frame_id: str | None = None) -> dict[str, Any]:
        for _ in range(count):
            self._request("POST", "/api/hid/events/send_key", query={"key": "Backspace"})
        return self.grab_frame(include_image=True)

    def scroll(self, delta: int, *, x=None, y=None, frame_id: str | None = None) -> dict[str, Any]:
        self._request("POST", "/api/hid/events/send_mouse_wheel", query={"delta_y": delta})
        return self.grab_frame(include_image=True)

    def set_clipboard(self, text: str) -> bool:
        return False

    def save_shot(self, destination: Path, *, region=None, label: str | None = None) -> str:
        destination.parent.mkdir(parents=True, exist_ok=True)
        image = self._request("GET", "/api/streamer/snapshot")
        destination.write_bytes(image)
        return str(destination)


class VNCHidTransport:
    """Generic VNC/RFB HID transport wrapper around an injected RFB client."""

    def __init__(self, rfb_client: Any):
        self.rfb = rfb_client

    def status(self) -> dict[str, Any]:
        return {"connected": bool(self.rfb.is_connected()), "backend": "vnc_hid"}

    def grab_frame(self, *, region=None, ocr: bool = True, include_image: bool = True) -> dict[str, Any]:
        frame = self.rfb.capture_screen(region=region)
        return {
            "frame_id": frame.get("frame_id") or ("vnc-" + uuid4().hex[:8]),
            "captured_at": frame.get("captured_at") or datetime.now(timezone.utc).isoformat(),
            "original_size": frame.get("size") or [1280, 720],
            "image": frame.get("image"),
            "ocr": {"words": frame.get("words", [])},
        }

    def click(self, x: int, y: int, *, frame_id: str | None = None) -> dict[str, Any]:
        self.rfb.mouse_click(x, y, button=1)
        return self.grab_frame()

    def double_click(self, x: int, y: int, *, frame_id: str | None = None) -> dict[str, Any]:
        self.rfb.mouse_click(x, y, button=1)
        self.rfb.mouse_click(x, y, button=1)
        return self.grab_frame()

    def send_keys(self, keys: str, *, frame_id: str | None = None) -> dict[str, Any]:
        self.rfb.key_press(keys)
        return self.grab_frame()

    def send_text_hid(self, text: str, *, frame_id: str | None = None) -> dict[str, Any]:
        self.rfb.type_text(text)
        return self.grab_frame()

    def backspace(self, count: int, *, frame_id: str | None = None) -> dict[str, Any]:
        for _ in range(count):
            self.rfb.key_press("BackSpace")
        return self.grab_frame()

    def scroll(self, delta: int, *, x=None, y=None, frame_id: str | None = None) -> dict[str, Any]:
        self.rfb.mouse_wheel(delta, x=x, y=y)
        return self.grab_frame()

    def set_clipboard(self, text: str) -> bool:
        setter = getattr(self.rfb, "set_clipboard", None)
        if callable(setter):
            return bool(setter(text))
        return False

    def save_shot(self, destination: Path, *, region=None, label: str | None = None) -> str:
        destination.parent.mkdir(parents=True, exist_ok=True)
        image = self.grab_frame(region=region, include_image=True).get("image") or b""
        destination.write_bytes(image)
        return str(destination)


class KVMUIDriver:
    """UIDriver bridging hardware-agnostic KVMTransport + OCREngine + Chinese TextEntryStrategy."""

    def __init__(
        self,
        transport: KVMTransport,
        *,
        ocr_engine: OCREngine | None = None,
        text_strategy: TextEntryStrategy | None = None,
        expected_size: tuple[int, int] | None = None,
        screenshot_dir: Path | None = None,
        long_text_qualified: bool = False,
        allow_clipboard_relay: bool = False,
        ime_composer: Callable[[str], str] | None = None,
    ):
        self.transport = transport
        self.ocr_engine = ocr_engine
        self.expected_size = expected_size
        self.screenshot_dir = screenshot_dir
        self._last_frame_id: str | None = None
        self._interrupted = False
        if text_strategy is not None:
            self.text_strategy = text_strategy
        elif not long_text_qualified:
            self.text_strategy = DraftOnlyFallbackStrategy()
        elif allow_clipboard_relay:
            self.text_strategy = ClipboardRelayStrategy(allow_clipboard_relay=True)
        elif ime_composer is not None:
            self.text_strategy = PureHIDSegmentedStrategy(ime_composer=ime_composer)
        elif getattr(transport, "supports_unicode_hid", False):
            self.text_strategy = PureHIDSegmentedStrategy(allow_unicode_hid=True)
        else:
            # Pure USB HID cannot transmit non-ASCII directly without IME composer or clipboard relay.
            self.text_strategy = DraftOnlyFallbackStrategy()

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        *,
        screenshot_dir: Path | None = None,
        ocr_engine: OCREngine | None = None,
    ) -> "KVMUIDriver":
        backend = config.get("kvm_backend", "glkvm")
        if backend == "pikvm":
            transport: KVMTransport = PiKVMHttpTransport(config["base_url"])
        elif backend == "glkvm":
            receipt = Path(config["receipt_path"]) if config.get("receipt_path") else None
            transport = GLKVMNativeTransport(receipt_path=receipt, profile=config.get("glkvm_profile"))
        else:
            raise ValueError(f"unsupported kvm_backend in config: {backend!r}")

        resolved_ocr = ocr_engine
        ocr_backend = config.get("ocr_backend")
        if resolved_ocr is None:
            if ocr_backend == "rapidocr" or (ocr_backend is None and backend == "pikvm"):
                from med_autopractice.adapters.drivers.ocr import RapidOCRBackend

                resolved_ocr = RapidOCRBackend()
            elif ocr_backend not in {None, "none"}:
                raise ValueError(f"unsupported ocr_backend in config: {ocr_backend!r}")

        size = tuple(config["expected_size"]) if config.get("expected_size") else None
        return cls(
            transport,
            ocr_engine=resolved_ocr,
            expected_size=size,
            screenshot_dir=screenshot_dir,
            long_text_qualified=bool(config.get("long_text_qualified", False)),
            allow_clipboard_relay=bool(config.get("allow_clipboard_relay", False)),
        )

    def _to_observation(
        self, meta: dict[str, Any], region: tuple[int, int, int, int] | None = None
    ) -> ScreenObservation:
        frame_id = str(meta.get("frame_id") or "")
        captured_at = str(meta.get("captured_at") or datetime.now(timezone.utc).isoformat())
        size = tuple(meta.get("original_size") or (1280, 720))
        if self.expected_size is not None and size != self.expected_size:
            raise UIStateError("remote resolution differs from qualified UI profile")
        words_raw = (meta.get("ocr") or {}).get("words")
        words: list[dict[str, Any]] = list(words_raw) if isinstance(words_raw, list) else []
        if not words and self.ocr_engine is not None and meta.get("image"):
            recognized: tuple[OCRWord, ...] = self.ocr_engine.recognize(meta["image"], region=region)
            words = [w.to_dict() for w in recognized]
        self._last_frame_id = frame_id or self._last_frame_id
        text = str(meta.get("text") or " ".join(str(w.get("text", "")).strip() for w in words if w.get("text")))
        return ScreenObservation(
            frame_id=frame_id or ("kvm-" + uuid4().hex[:8]),
            text=text,
            captured_at=captured_at,
            words=tuple(words),
            size=size,
        )

    def check_human_interrupt(self) -> None:
        if self._interrupted:
            raise HumanInterruptError("KVM control lease interrupted by clinician takeover")
        st = self.transport.status()
        if (
            st.get("control_mode") in {"human_takeover", "manual", "paused", "handoff"}
            or st.get("control_owner") in {"manual", "paused", "external_handoff"}
            or bool(st.get("human_active"))
        ):
            raise HumanInterruptError(
                f"KVM device reports active human control ({st.get('control_mode') or st.get('control_owner')})"
            )

    def observe(self, region: tuple[int, int, int, int] | None = None) -> ScreenObservation:
        self.check_human_interrupt()
        meta = self.transport.grab_frame(
            region=region, ocr=True, include_image=self.ocr_engine is not None,
        )
        return self._to_observation(meta, region=region)

    def locate(
        self,
        target: str | None,
        *,
        selector: str | None = None,
        region_hint: tuple[int, int, int, int] | None = None,
    ) -> tuple[int, int, int, int]:
        label = (target or selector or "").strip()
        if not label:
            raise UIStateError("KVM locate requires target text")
        obs = self.observe(region_hint)
        normalized_label = "".join(label.split())
        matches = []
        for word in obs.words:
            text = str(word.get("text", "")).strip()
            box = word.get("box")
            conf = float(word.get("confidence", 0))
            if not box or len(box) != 4 or conf < 75:
                continue
            if region_hint is not None:
                if not (region_hint[0] <= box[0] < region_hint[2] and region_hint[1] <= box[1] < region_hint[3]):
                    continue
            if text == label or (normalized_label and normalized_label in "".join(text.split())):
                matches.append(tuple(int(v) for v in box))
        if len(matches) != 1:
            raise UIStateError(f"KVM anchor {label!r} is absent or ambiguous ({len(matches)} matches)")
        return matches[0]

    def act(
        self,
        kind: str,
        *,
        target: str | None = None,
        selector: str | None = None,
        region_hint: tuple[int, int, int, int] | None = None,
        value: Any = None,
    ) -> ScreenObservation:
        self.check_human_interrupt()
        if kind in {"click", "double_click", "select_option"}:
            box = self.locate(target, selector=selector, region_hint=region_hint)
            x = box[0] + max(1, box[2]) // 2
            y = box[1] + max(1, box[3]) // 2
            meta = (
                self.transport.double_click(x, y, frame_id=self._last_frame_id)
                if kind == "double_click"
                else self.transport.click(x, y, frame_id=self._last_frame_id)
            )
            return self._to_observation(meta, region=region_hint)
        if kind == "keys":
            meta = self.transport.send_keys(str(value or target or ""), frame_id=self._last_frame_id)
            return self._to_observation(meta, region=region_hint)
        if kind == "scroll":
            delta = -3 if value in (None, "down") else int(value) if str(value).lstrip("-").isdigit() else 3
            meta = self.transport.scroll(delta, frame_id=self._last_frame_id)
            return self._to_observation(meta, region=region_hint)
        raise UIStateError(f"unsupported KVM action: {kind!r}")

    def input_text(
        self,
        text: str,
        *,
        target: str | None = None,
        selector: str | None = None,
        region_hint: tuple[int, int, int, int] | None = None,
        subject: SubjectRef | None = None,
    ) -> ScreenObservation:
        self.check_human_interrupt()
        if subject is not None:
            full = self.observe(None).full_text()
            if subject.patient_id not in full or subject.encounter_id not in full:
                raise UIStateError("unique patient and encounter identifiers are not both visible")
        if target or selector:
            try:
                self.act("click", target=target, selector=selector, region_hint=region_hint)
            except UIStateError:
                if region_hint is None:
                    raise
                self.act("click", target=target, selector=selector, region_hint=None)
        if text.isascii() and len(text) <= 64 and not isinstance(self.text_strategy, DraftOnlyFallbackStrategy):
            meta = self.transport.send_text_hid(text, frame_id=self._last_frame_id)
            return self._to_observation(meta, region=region_hint)

        def _clip_paste(payload: str) -> bool:
            if not self.transport.set_clipboard(payload):
                return False
            meta = self.transport.send_keys("Ctrl+v", frame_id=self._last_frame_id)
            if isinstance(meta, dict) and meta.get("frame_id"):
                self._last_frame_id = str(meta["frame_id"])
            return True

        def _hid_send(chunk: str) -> None:
            meta = self.transport.send_text_hid(chunk, frame_id=self._last_frame_id)
            if isinstance(meta, dict) and meta.get("frame_id"):
                self._last_frame_id = str(meta["frame_id"])

        def _bs_send(cnt: int) -> None:
            meta = self.transport.backspace(cnt, frame_id=self._last_frame_id)
            if isinstance(meta, dict) and meta.get("frame_id"):
                self._last_frame_id = str(meta["frame_id"])

        self.text_strategy.enter_text(
            text,
            hid_sender=_hid_send,
            backspace_sender=_bs_send,
            ocr_reader=lambda reg: self.observe(reg).full_text(),
            clipboard_paster=_clip_paste,
            verify_region=region_hint,
        )
        return self.observe(region_hint)

    def extract_table(
        self,
        *,
        target: str | None = None,
        selector: str | None = None,
        region_hint: tuple[int, int, int, int] | None = None,
    ) -> TableData:
        obs = self.observe(region_hint)
        meta_rows = self.transport.grab_frame(region=region_hint, ocr=True, include_image=False).get("table")
        if isinstance(meta_rows, dict) and isinstance(meta_rows.get("rows"), list):
            return TableData(
                headers=tuple(meta_rows.get("headers", ())),
                rows=tuple(meta_rows["rows"]),
                has_next=bool(meta_rows.get("has_next", False)),
                next_target=meta_rows.get("next_target"),
            )
        rows = tuple(
            {"value": str(w.get("text", "")), "summary": str(w.get("text", ""))}
            for w in obs.words if w.get("text")
        )
        has_next = any("下一页" in str(w.get("text", "")) for w in obs.words)
        return TableData(headers=("text",), rows=rows, has_next=has_next, next_target="下一页" if has_next else None)

    def screenshot(
        self, *, region: tuple[int, int, int, int] | None = None, label: str | None = None
    ) -> str:
        self.check_human_interrupt()
        directory = self.screenshot_dir or Path.cwd()
        directory.mkdir(parents=True, exist_ok=True)
        safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (label or "shot"))
        dest = directory / f"kvm-{safe_label}-{uuid4().hex[:8]}.png"
        return self.transport.save_shot(dest, region=region, label=label)
