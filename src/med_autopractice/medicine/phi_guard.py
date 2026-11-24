"""PHI boundary guard supporting local_trusted and reversible pseudonymize_outbound modes."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import re
from typing import Any, Literal
from urllib.parse import urlsplit

from med_autopractice.domain.task import SubjectRef

PrivacyMode = Literal["local_trusted", "pseudonymize_outbound"]

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_RE_ID_CARD = re.compile(r"(?<![0-9A-Za-z])[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![0-9A-Za-z])")
_RE_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_RE_LABELED_NAME = re.compile(r"(?:患者|姓名|病人)\s*[:：]\s*([^\s,，。；;()\n]{2,8})")
_RE_LABELED_ADDR = re.compile(r"(?:住址|地址|家庭住址)\s*[:：]\s*([^\n,，。；;]{3,60})")
_RE_LABELED_BIRTH = re.compile(r"(?:出生日期|生日)\s*[:：]\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)")
_RE_ENCOUNTER_LABEL = re.compile(r"(?:住院号|门诊号|就诊号|病案号)\s*[:：]\s*([A-Za-z0-9_-]{3,40})")


def validate_model_endpoint(
    endpoint: str,
    privacy_mode: PrivacyMode,
    *,
    trusted_hosts: Sequence[str] = (),
) -> str:
    """Validate that the LLM endpoint complies with the configured privacy policy."""
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ValueError("model endpoint must be a non-empty URL")
    parsed = urlsplit(endpoint.strip())
    host = (parsed.hostname or "").lower()
    if not host or parsed.username or parsed.password:
        raise ValueError("model endpoint must be a clean URL without embedded credentials")

    is_loopback = host in _LOOPBACK_HOSTS
    trusted_set = {h.strip().lower() for h in trusted_hosts if isinstance(h, str) and h.strip()}

    if privacy_mode == "local_trusted":
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("local_trusted endpoint must use HTTP or HTTPS")
        if not is_loopback and host not in trusted_set:
            raise ValueError(
                f"local_trusted mode forbids unapproved remote host {host!r}; "
                "use loopback/trusted hospital LAN host or switch to pseudonymize_outbound"
            )
        return endpoint.strip().rstrip("/")

    if privacy_mode == "pseudonymize_outbound":
        if parsed.scheme != "https" and not is_loopback:
            raise ValueError("pseudonymize_outbound non-loopback endpoint must use HTTPS")
        return endpoint.strip().rstrip("/")

    raise ValueError(f"unsupported privacy_mode: {privacy_mode!r}")


@dataclass
class PseudonymizationSession:
    """Ephemeral in-memory reversible PHI pseudonymizer cleared on exit."""

    privacy_mode: PrivacyMode
    subject: SubjectRef
    known_identifiers: tuple[str, ...] = ()
    _forward: dict[str, str] = field(default_factory=dict)
    _reverse: dict[str, str] = field(default_factory=dict)
    _counters: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.privacy_mode == "pseudonymize_outbound":
            self._register("PATIENT", self.subject.patient_id)
            self._register("ENCOUNTER", self.subject.encounter_id)
            for item in self.known_identifiers:
                if isinstance(item, str) and item.strip():
                    self._register("IDENT", item.strip())

    def __enter__(self) -> "PseudonymizationSession":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.clear()

    def clear(self) -> None:
        """Wipe all reversible mapping tables from memory."""
        self._forward.clear()
        self._reverse.clear()
        self._counters.clear()

    def _register(self, category: str, raw_value: str) -> str:
        clean = raw_value.strip()
        if not clean:
            return raw_value
        if clean in self._forward:
            return self._forward[clean]
        seq = self._counters.get(category, 0) + 1
        self._counters[category] = seq
        token = f"<{category}_{seq:02d}>"
        self._forward[clean] = token
        self._reverse[token] = clean
        return token

    def _collect_patterns(self, text: str) -> None:
        for match in _RE_ID_CARD.finditer(text):
            self._register("IDCARD", match.group(0))
        for match in _RE_PHONE.finditer(text):
            self._register("PHONE", match.group(0))
        for match in _RE_LABELED_NAME.finditer(text):
            candidate = match.group(1)
            if not candidate.startswith("<") and not candidate.endswith(">"):
                self._register("NAME", candidate)
        for match in _RE_LABELED_ADDR.finditer(text):
            candidate = match.group(1).strip()
            if not candidate.startswith("<"):
                self._register("ADDR", candidate)
        for match in _RE_LABELED_BIRTH.finditer(text):
            candidate = match.group(1).strip()
            if not candidate.startswith("<"):
                self._register("BIRTHDATE", candidate)
        for match in _RE_ENCOUNTER_LABEL.finditer(text):
            candidate = match.group(1).strip()
            if not candidate.startswith("<"):
                self._register("ENCOUNTER", candidate)

    def scrub(self, text: str) -> str:
        """Scrub direct PHI identifiers in outbound text when pseudonymization is enabled."""
        if self.privacy_mode == "local_trusted" or not text:
            return text
        self._collect_patterns(text)
        result = text
        # Replace longest identifiers first to prevent partial-prefix corruption.
        for raw in sorted(self._forward, key=len, reverse=True):
            result = result.replace(raw, self._forward[raw])
        return result

    def scrub_payload(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.scrub(value)
        if isinstance(value, list):
            return [self.scrub_payload(v) for v in value]
        if isinstance(value, tuple):
            return [self.scrub_payload(v) for v in value]
        if isinstance(value, dict):
            return {k: self.scrub_payload(v) for k, v in value.items()}
        return value

    def restore(self, text: str) -> str:
        """Restore placeholders back to original identifiers in memory."""
        if self.privacy_mode == "local_trusted" or not text:
            return text
        result = text
        for token in sorted(self._reverse, key=len, reverse=True):
            result = result.replace(token, self._reverse[token])
        return result

    def restore_payload(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.restore(value)
        if isinstance(value, list):
            return [self.restore_payload(v) for v in value]
        if isinstance(value, dict):
            return {k: self.restore_payload(v) for k, v in value.items()}
        return value

    def assert_no_phi_leaked(self, scrubbed_text: str) -> None:
        """Secondary safety check before outbound cloud transmission."""
        if self.privacy_mode != "pseudonymize_outbound":
            return
        if _RE_ID_CARD.search(scrubbed_text) or _RE_PHONE.search(scrubbed_text):
            raise ValueError("PHI leakage detected: unmasked national ID or phone pattern remains")
        for raw in self._forward:
            if raw and raw in scrubbed_text:
                raise ValueError("PHI leakage detected: registered identifier remains in outbound prompt")


class PHIGuard:
    """Coordinates endpoint policy validation and per-task pseudonymization sessions."""

    def __init__(
        self,
        privacy_mode: PrivacyMode = "local_trusted",
        *,
        trusted_hosts: Sequence[str] = (),
    ):
        if privacy_mode not in {"local_trusted", "pseudonymize_outbound"}:
            raise ValueError(f"unsupported privacy_mode: {privacy_mode!r}")
        self.privacy_mode: PrivacyMode = privacy_mode
        self.trusted_hosts = tuple(trusted_hosts)

    def validate_endpoint(self, endpoint: str) -> str:
        return validate_model_endpoint(endpoint, self.privacy_mode, trusted_hosts=self.trusted_hosts)

    def new_session(
        self,
        subject: SubjectRef,
        *,
        known_identifiers: Sequence[str] = (),
    ) -> PseudonymizationSession:
        return PseudonymizationSession(
            privacy_mode=self.privacy_mode,
            subject=subject,
            known_identifiers=tuple(known_identifiers),
        )
