"""KVM-only UI primitives and clinical-system boundary.

Hospital-specific navigation is deliberately absent until its actual screens
and permissions have been qualified.
"""

from __future__ import annotations

from dataclasses import dataclass
import asyncio
import json
import os
from typing import Any, Protocol

from med_autopractice.domain.task import (
    ExecutionReceipt, Proposal, Snapshot, SubjectRef, SystemReadback, Task,
)


class UIStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class Screen:
    frame_id: str
    size: tuple[int, int]
    captured_at: str
    words: tuple[dict[str, Any], ...]
    image: bytes | None
    shot_path: str | None = None


class KVMClient(Protocol):
    async def observe(self, region=None, max_width=1280, ocr=True, include_image=False): ...

    async def wait(self, region, condition="changed", text=None, **kwargs): ...

    async def action(self, frame_id: str, kind: str, **kwargs): ...

    async def shot(self, region=None, **kwargs): ...


class KVMNavigator:
    def __init__(self, client: KVMClient, expected_size: tuple[int, int] | None = None):
        self.client = client
        self.expected_size = expected_size

    async def observe(self, region: list[int] | None = None) -> Screen:
        # Text-only by default: the picture stays on the KVM host unless the
        # caller explicitly asks for one through `screenshot`.
        meta, image = await self.client.observe(region, ocr=True, include_image=False)
        return self._screen(meta, image)

    async def screenshot(self, region: list[int] | None = None, label: str | None = None) -> Screen:
        """Keep one deliberate picture as a private file; return its path, not raw bytes."""
        meta = await self.client.shot(region=region, label=label)
        return self._screen(meta, None)

    def _screen(self, meta: dict, image: bytes | None) -> Screen:
        # Frame identity and OCR provenance are required; a picture is optional
        # evidence now that routine steps keep their bytes out of the model
        # context. `screenshot()` is the deliberate path that adds `shot_path`.
        if (not meta.get("frame_id") or not meta.get("captured_at")
                or not meta.get("original_size")
                or "words" not in meta.get("ocr", {})):
            raise UIStateError("KVM observation lacks live frame or OCR provenance")
        screen = Screen(
            meta["frame_id"], tuple(meta["original_size"]), meta["captured_at"],
            tuple(meta["ocr"]["words"]), image, meta.get("shot_path"),
        )
        if self.expected_size is not None and screen.size != self.expected_size:
            raise UIStateError("remote resolution differs from the qualified UI profile")
        return screen

    @staticmethod
    def require_visible_identity(screen: Screen, subject: SubjectRef) -> None:
        texts = {word.get("text", "").strip() for word in screen.words}
        if subject.patient_id not in texts or subject.encounter_id not in texts:
            raise UIStateError("unique patient and encounter identifiers are not both visible")

    async def wait_for_text(self, text: str, region: list[int], timeout: float = 30) -> Screen:
        meta, image = await self.client.wait(
            region, condition="text", text=text, timeout=timeout, result_mode="ocr",
        )
        if meta.get("wait", {}).get("status") != "matched":
            raise UIStateError("expected text was not observed before KVM wait ended")
        return self._screen(meta, image)

    async def type_ascii(self, text: str, subject: SubjectRef) -> Screen:
        if not text or not text.isascii():
            raise ValueError("KVM text entry only accepts nonempty ASCII")
        screen = await self.observe()
        self.require_visible_identity(screen, subject)
        meta, image = await self.client.action(
            screen.frame_id, "text", text=text, guard_region=None,
            result_region=None, result_mode="ocr",
        )
        return self._screen(meta, image)

    async def click_unique_text(
        self, label: str, region: list[int], *, subject: SubjectRef | None = None
    ) -> Screen:
        if not label or len(region) != 4:
            raise ValueError("a label and bounded screen region are required")
        screen = await self.observe(None if subject else region)
        if subject is not None:
            self.require_visible_identity(screen, subject)
        matches = [word for word in screen.words
                   if word.get("text", "").strip() == label
                   and word.get("confidence", 0) >= 80
                   and len(word.get("box", [])) == 4
                   and region[0] <= word["box"][0] < region[2]
                   and region[1] <= word["box"][1] < region[3]]
        if len(matches) != 1:
            raise UIStateError("screen label is absent, ambiguous, or low confidence")
        left, top, width, height = matches[0]["box"]
        if width < 1 or height < 1:
            raise UIStateError("OCR anchor has no usable bounds")
        x, y = left + width // 2, top + height // 2
        if not (region[0] <= x < region[2] and region[1] <= y < region[3]):
            raise UIStateError("OCR anchor lies outside the observed region")
        meta, image = await self.client.action(
            screen.frame_id, "click", x=x, y=y,
            guard_region=None if subject else region,
            result_region=None if subject else region, result_mode="ocr",
        )
        return self._screen(meta, image)


@dataclass(frozen=True)
class VisibleObject:
    subject: SubjectRef
    object_ref: str
    state: str
    payload: dict[str, Any]
    screenshot_ref: str
    trace_refs: tuple[str, ...]
    observed_at: str


@dataclass(frozen=True)
class UIProfile:
    site_id: str
    product: str
    version: str
    role: str
    display_size: tuple[int, int]
    qualification_ref: str
    write_capabilities: frozenset[str] = frozenset()
    long_text_qualified: bool = False


class HospitalUI(Protocol):
    """One tested hospital UI profile, implemented with KVMNavigator."""

    profile: UIProfile

    def collect(self, subject: SubjectRef, capability_id: str) -> Snapshot: ...

    def write(self, proposal: Proposal, existing_object_ref: str | None) -> ExecutionReceipt: ...

    def open_object(self, subject: SubjectRef, capability_id: str, object_ref: str) -> VisibleObject: ...

    def find_candidates(self, task: Task) -> tuple[VisibleObject, ...]: ...


class KVMClinicalSystem:
    """Validate site UI results; an internal key is never sent to the GUI."""

    def __init__(self, ui: HospitalUI):
        profile = ui.profile
        if (not all((profile.site_id, profile.product, profile.version, profile.role,
                     profile.qualification_ref)) or min(profile.display_size) < 1):
            raise ValueError("hospital UI profile is not identified and qualified")
        self.ui = ui

    def read_context(self, subject: SubjectRef, capability_id: str) -> Snapshot:
        snapshot = self.ui.collect(subject, capability_id)
        if (subject.site_id != self.ui.profile.site_id
                or snapshot.subject != subject or not snapshot.revision
                or not snapshot.trace_refs or not snapshot.coverage):
            raise UIStateError("UI collection lacks identity, revision, trace, or coverage")
        return snapshot

    def can_apply(self, proposal: Proposal) -> bool:
        profile = self.ui.profile
        if proposal.subject.site_id != profile.site_id or proposal.capability_id not in profile.write_capabilities:
            return False
        return (proposal.capability_id not in {"write_inpatient_notes", "write_outpatient_notes"}
                or profile.long_text_qualified)

    def apply(
        self, proposal: Proposal, idempotency_key: str, existing_object_ref: str | None
    ) -> ExecutionReceipt:
        if not self.can_apply(proposal):
            raise UIStateError("this KVM UI write or long-text entry has not been qualified")
        return self.ui.write(proposal, existing_object_ref)

    def read_back(self, receipt: ExecutionReceipt, task: Task) -> SystemReadback:
        visible = self.ui.open_object(
            task.request.subject, task.request.capability_id, receipt.object_ref,
        )
        digest = task.proposal.digest if task.proposal and visible.payload == task.proposal.payload else ""
        return SystemReadback(
            visible.subject, visible.object_ref, digest, visible.state,
            visible.screenshot_ref, visible.trace_refs, visible.payload,
            visible.observed_at,
        )

    def find_submission(self, task: Task) -> ExecutionReceipt | None:
        if task.proposal is None:
            return None
        exact = [item for item in self.ui.find_candidates(task)
                 if item.subject == task.request.subject
                 and item.payload == task.proposal.payload
                 and item.object_ref and item.trace_refs and item.screenshot_ref]
        if len(exact) != 1:
            return None
        item = exact[0]
        return ExecutionReceipt(item.object_ref, item.state, item.trace_refs)


def main() -> None:
    from gateway import RemoteClient

    profile = os.environ["GLKVM_PROFILE"]
    status = asyncio.run(RemoteClient(profile).status())
    expected = os.environ["MAP_EXPECTED_KVM_REVISION"]
    if status.get("runtime_revision") != expected:
        raise SystemExit("glkvm-native runtime revision does not match its installation")
    print(json.dumps({
        "backend": "ip_kvm",
        "connected": status["connected"],
        "control_mode": status["control_mode"],
        "runtime_revision": status["runtime_revision"],
        "clinical_ui_qualified": False,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
