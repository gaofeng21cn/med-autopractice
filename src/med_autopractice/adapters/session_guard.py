"""Hospital session lock / timeout guard: halts automation and hands off re-login to clinician."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from med_autopractice.adapters.playbook_runner import ScreenObservation
from med_autopractice.adapters.ui import UIStateError
from med_autopractice.domain.task import SubjectRef

SessionState = Literal["active", "paused_reauth_required"]

DEFAULT_LOCK_KEYWORDS: tuple[str, ...] = (
    "重新登录",
    "会话已过期",
    "登录超时",
    "屏幕已锁定",
    "账号已下线",
    "请刷卡",
    "CA证书",
    "Session Expired",
    "Session Timeout",
    "Sign In",
    "Please Login",
)


class SessionLockedError(UIStateError):
    """Raised when hospital session requires manual clinician re-authentication."""


@dataclass(frozen=True)
class ReauthEvent:
    detected_at: str
    reason: str
    matched_keyword: str
    frame_id: str
    action_required: str = "clinician_manual_relogin"


class ClinicalSessionGuard:
    """Monitors active clinical terminal observations and blocks writes upon lock/timeout."""

    def __init__(self, *, lock_keywords: Sequence[str] = DEFAULT_LOCK_KEYWORDS):
        self.lock_keywords = tuple(lock_keywords)
        self.state: SessionState = "active"
        self.events: list[ReauthEvent] = []

    def _find_lock_keyword(self, text: str) -> str | None:
        lower = text.lower()
        for kw in self.lock_keywords:
            if kw.lower() in lower:
                return kw
        return None

    def inspect_observation(self, obs: ScreenObservation) -> ReauthEvent | None:
        """Inspect a screen observation; transition to paused_reauth_required on lock/timeout."""
        keyword = self._find_lock_keyword(obs.full_text())
        if keyword is None:
            return None
        self.state = "paused_reauth_required"
        event = ReauthEvent(
            detected_at=datetime.now(timezone.utc).isoformat(),
            reason="hospital session expired or screen locked; clinician manual login required",
            matched_keyword=keyword,
            frame_id=obs.frame_id,
        )
        self.events.append(event)
        return event

    def assert_can_write(self) -> None:
        """Refuse any write or submission while session requires clinician re-authentication."""
        if self.state != "active":
            raise SessionLockedError(
                "clinical session is paused_reauth_required; clinician must re-authenticate manually"
            )

    def confirm_reauthenticated(self, obs: ScreenObservation, *, subject: SubjectRef) -> None:
        """Resume active state only after lock prompt is gone and subject identity is verified."""
        keyword = self._find_lock_keyword(obs.full_text())
        if keyword is not None:
            raise SessionLockedError(f"lock or login screen still visible ({keyword!r})")
        full = obs.full_text()
        if subject.patient_id not in full or subject.encounter_id not in full:
            raise SessionLockedError(
                "cannot resume session until unique patient and encounter identifiers are verified"
            )
        self.state = "active"
