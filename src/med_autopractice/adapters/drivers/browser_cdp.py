"""Track A (Ideal Form): Browser CDP/Playwright attachment driver reusing clinician SSO."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from med_autopractice.adapters.playbook_runner import (
    HumanInterruptError, ScreenObservation, TableData,
)
from med_autopractice.adapters.ui import UIStateError
from med_autopractice.domain.task import SubjectRef


class BrowserCDPBackend(Protocol):
    """Abstracted CDP page interface (Playwright-backed or injected for tests)."""

    def read_dom_text(self, selector: str | None = None) -> ScreenObservation: ...

    def locate_element(self, selector: str | None = None, *, text: str | None = None) -> str: ...

    def perform(
        self, kind: str, *, selector: str | None = None, text: str | None = None, value: Any = None
    ) -> ScreenObservation: ...

    def fill_and_readback(self, selector: str, value: str) -> str: ...

    def read_dom_table(self, selector: str) -> TableData: ...

    def captured_xhr(self) -> tuple[dict[str, Any], ...]: ...

    def screenshot_page(self, destination: Path) -> None: ...

    def user_interrupted(self) -> bool: ...


class _LazyPlaywrightCDPBackend:
    """Attach over CDP to an already-authenticated hospital Chrome/Edge browser session."""

    def __init__(self, cdp_url: str, *, url_contains: str | None = None):
        try:
            from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
        except ImportError as error:
            raise RuntimeError(
                "install optional dependency 'browser' (pip install med-autopractice[browser])"
            ) from error
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(cdp_url)
        pages = [page for ctx in self._browser.contexts for page in ctx.pages]
        if url_contains:
            pages = [p for p in pages if url_contains in p.url] or pages
        if not pages:
            raise UIStateError("no open browser tab found on CDP endpoint")
        self.page = pages[0]
        self._xhr_buffer: list[dict[str, Any]] = []
        self.page.on("response", self._on_response)

    def _on_response(self, response) -> None:
        if response.request.resource_type in {"xhr", "fetch"} and response.status == 200:
            try:
                body = response.json()
            except Exception:
                return
            self._xhr_buffer.append({"url": response.url, "status": response.status, "json": body})

    def user_interrupted(self) -> bool:
        return False

    def _locator(self, selector: str | None, text: str | None):
        if selector and selector.startswith("iframe>>"):
            frame_sel, inner_sel = selector.split(">>", 1)
            return self.page.frame_locator(frame_sel).locator(inner_sel)
        if selector:
            return self.page.locator(selector)
        if text:
            return self.page.get_by_text(text, exact=False)
        raise UIStateError("browser locator requires selector or target text")

    def read_dom_text(self, selector: str | None = None) -> ScreenObservation:
        text = (
            self._locator(selector, None).inner_text()
            if selector
            else self.page.locator("body").inner_text()
        )
        return ScreenObservation(
            frame_id="cdp-" + uuid4().hex[:8],
            text=text,
            captured_at=datetime.now(timezone.utc).isoformat(),
            metadata={"url": self.page.url},
        )

    def locate_element(self, selector: str | None = None, *, text: str | None = None) -> str:
        loc = self._locator(selector, text)
        count = loc.count()
        if count < 1:
            raise UIStateError(f"browser element not found: {selector or text!r}")
        return selector or str(text)

    def perform(
        self, kind: str, *, selector: str | None = None, text: str | None = None, value: Any = None
    ) -> ScreenObservation:
        loc = self._locator(selector, text)
        if kind == "double_click":
            loc.first.dblclick()
        elif kind == "select_option":
            loc.first.select_option(str(value or ""))
        elif kind == "keys":
            self.page.keyboard.press(str(value or ""))
        else:
            loc.first.click()
        return self.read_dom_text()

    def fill_and_readback(self, selector: str, value: str) -> str:
        loc = self._locator(selector, None).first
        loc.fill(value)
        return str(loc.input_value())

    def read_dom_table(self, selector: str) -> TableData:
        loc = self._locator(selector, None).first
        headers = tuple(loc.locator("thead th").all_inner_texts())
        rows = []
        for tr in loc.locator("tbody tr").all():
            cells = tr.locator("td").all_inner_texts()
            if headers and len(headers) == len(cells):
                rows.append(dict(zip(headers, cells)))
            else:
                rows.append({"cells": cells, "summary": " | ".join(cells)})
        return TableData(headers=headers, rows=tuple(rows), has_next=False)

    def captured_xhr(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._xhr_buffer)

    def screenshot_page(self, destination: Path) -> None:
        self.page.screenshot(path=str(destination), full_page=True)


class BrowserCDPDriver:
    """UIDriver attaching to an authenticated hospital web session via CDP."""

    def __init__(
        self,
        *,
        cdp_url: str = "http://127.0.0.1:9222",
        url_contains: str | None = None,
        backend: BrowserCDPBackend | None = None,
        screenshot_dir: Path | None = None,
    ):
        self.cdp_url = cdp_url
        self.url_contains = url_contains
        self._backend = backend
        self.screenshot_dir = screenshot_dir

    def _get_backend(self) -> BrowserCDPBackend:
        if self._backend is None:
            self._backend = _LazyPlaywrightCDPBackend(self.cdp_url, url_contains=self.url_contains)
        return self._backend

    def check_human_interrupt(self) -> None:
        if self._get_backend().user_interrupted():
            raise HumanInterruptError("clinician input detected in browser session")

    def observe(self, region: tuple[int, int, int, int] | None = None) -> ScreenObservation:
        self.check_human_interrupt()
        return self._get_backend().read_dom_text()

    def read_field(
        self,
        *,
        target: str | None = None,
        selector: str | None = None,
        region_hint: tuple[int, int, int, int] | None = None,
    ) -> ScreenObservation:
        self.check_human_interrupt()
        return self._get_backend().read_dom_text(selector)

    def locate(
        self, target: str | None, *, selector: str | None = None,
        region_hint: tuple[int, int, int, int] | None = None,
    ) -> str:
        self.check_human_interrupt()
        return self._get_backend().locate_element(selector, text=target)

    def act(
        self, kind: str, *, target: str | None = None, selector: str | None = None,
        region_hint: tuple[int, int, int, int] | None = None, value: Any = None,
    ) -> ScreenObservation:
        self.check_human_interrupt()
        return self._get_backend().perform(kind, selector=selector, text=target, value=value)

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
        backend = self._get_backend()
        if subject is not None:
            obs = backend.read_dom_text()
            blob = obs.full_text()
            if subject.patient_id not in blob or subject.encounter_id not in blob:
                raise UIStateError("unique patient and encounter identifiers are not both visible")
        sel = selector or (target if target else "")
        if not sel:
            raise UIStateError("browser input_text requires a target selector")
        actual = backend.fill_and_readback(sel, text)
        if actual != text:
            raise UIStateError("browser input readback differs from entered clinical text")
        return backend.read_dom_text()

    def extract_table(
        self, *, target: str | None = None, selector: str | None = None,
        region_hint: tuple[int, int, int, int] | None = None,
    ) -> TableData:
        self.check_human_interrupt()
        sel = selector or target or "table"
        return self._get_backend().read_dom_table(sel)

    def captured_xhr(self) -> tuple[dict[str, Any], ...]:
        return self._get_backend().captured_xhr()

    def screenshot(
        self, *, region: tuple[int, int, int, int] | None = None, label: str | None = None
    ) -> str:
        self.check_human_interrupt()
        directory = self.screenshot_dir or Path.cwd()
        directory.mkdir(parents=True, exist_ok=True)
        safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (label or "shot"))
        dest = directory / f"cdp-{safe_label}-{uuid4().hex[:8]}.png"
        self._get_backend().screenshot_page(dest)
        return str(dest)
