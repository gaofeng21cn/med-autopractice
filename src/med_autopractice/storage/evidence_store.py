"""Runtime integration helpers for encrypted evidence screenshots and site retention."""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
from typing import Any

from med_autopractice.storage.crypto import EnvKeyProvider, EvidenceCipher, FileKeyProvider
from med_autopractice.storage.retention import RetentionPolicy

_REPO_ROOT = Path(__file__).resolve().parents[3]


def build_evidence_cipher(
    security_cfg: Mapping[str, Any] | None,
    site_dir: Path,
    *,
    repo_root: Path = _REPO_ROOT,
) -> EvidenceCipher | None:
    """Build an EvidenceCipher from deployment.json evidence_security settings."""
    if not isinstance(security_cfg, Mapping):
        return None
    enc_cfg = security_cfg.get("encryption")
    if not isinstance(enc_cfg, Mapping):
        enc_cfg = security_cfg

    has_key_cfg = bool(enc_cfg.get("key_file") or enc_cfg.get("key_env"))
    mode = str(enc_cfg.get("mode") or ("required" if has_key_cfg else "off"))
    if mode == "off":
        return None
    if mode not in {"off", "optional", "required"}:
        raise ValueError(f"unsupported evidence encryption mode: {mode!r}")

    prefer_aesgcm = bool(enc_cfg.get("prefer_aesgcm", True))
    key_file = enc_cfg.get("key_file")
    if key_file:
        key_path = Path(str(key_file)).expanduser()
        if not key_path.is_absolute():
            key_path = (site_dir / key_path).resolve()
        return EvidenceCipher(
            FileKeyProvider(key_path, repo_root=repo_root),
            prefer_aesgcm=prefer_aesgcm,
        )

    key_env = enc_cfg.get("key_env")
    if key_env:
        env_name = str(key_env).strip()
        if mode == "optional" and not os.environ.get(env_name):
            return None
        provider = EnvKeyProvider(env_name)
        provider.get_key()
        return EvidenceCipher(provider, prefer_aesgcm=prefer_aesgcm)

    if mode == "required":
        raise ValueError("required evidence encryption needs key_file or key_env")
    return None


def retention_policy_from_config(
    security_cfg: Mapping[str, Any] | None,
    *,
    dry_run: bool = False,
) -> RetentionPolicy:
    """Construct RetentionPolicy from deployment.json evidence_security settings."""
    ret = security_cfg.get("retention") if isinstance(security_cfg, Mapping) else None
    if not isinstance(ret, Mapping):
        ret = {}
    return RetentionPolicy(
        screenshot_max_age_days=int(ret.get("screenshot_max_age_days", 30)),
        task_log_max_age_days=int(ret.get("task_log_max_age_days", 90)),
        dry_run=dry_run,
    )


class ManagedEvidenceDriver:
    """UIDriver proxy that encrypts screenshots at rest and removes plaintext copies."""

    def __init__(
        self,
        driver: Any,
        evidence_dir: Path,
        *,
        cipher: EvidenceCipher | None = None,
        site_id: str = "",
    ):
        self._driver = driver
        self.evidence_dir = evidence_dir
        self.cipher = cipher
        self.site_id = site_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self._driver, name)

    def observe(self, region: tuple[int, int, int, int] | None = None):
        return self._driver.observe(region)

    def locate(self, target: str | None, *, selector: str | None = None, region_hint=None):
        return self._driver.locate(target, selector=selector, region_hint=region_hint)

    def act(self, kind: str, *, target: str | None = None, selector: str | None = None, region_hint=None, value: Any = None):
        return self._driver.act(kind, target=target, selector=selector, region_hint=region_hint, value=value)

    def input_text(self, text: str, *, target: str | None = None, selector: str | None = None, region_hint=None, subject=None):
        return self._driver.input_text(text, target=target, selector=selector, region_hint=region_hint, subject=subject)

    def extract_table(self, *, target: str | None = None, selector: str | None = None, region_hint=None):
        return self._driver.extract_table(target=target, selector=selector, region_hint=region_hint)

    def check_human_interrupt(self) -> None:
        self._driver.check_human_interrupt()

    def screenshot(self, *, region: tuple[int, int, int, int] | None = None, label: str | None = None) -> str:
        raw_ref = self._driver.screenshot(region=region, label=label)
        if self.cipher is None:
            return raw_ref
        raw_path = Path(raw_ref)
        if not raw_path.is_file():
            raise ValueError(f"driver screenshot did not produce a local file to encrypt: {raw_ref}")
        enc_path = raw_path.with_name(raw_path.name + ".enc")
        try:
            self.cipher.encrypt_file(raw_path, enc_path, aad=self.site_id.encode("utf-8"))
        finally:
            raw_path.unlink(missing_ok=True)
        return str(enc_path)

    def decrypt_evidence_bytes(self, path: str | Path) -> bytes:
        target = Path(path)
        if self.cipher is None:
            return target.read_bytes()
        return self.cipher.decrypt_bytes(target.read_bytes(), aad=self.site_id.encode("utf-8"))
