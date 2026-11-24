"""Authenticated encryption envelope for local evidence screenshots and clinical logs."""

from __future__ import annotations

import base64
from hashlib import pbkdf2_hmac, sha256
import hmac
import json
import os
from pathlib import Path
import secrets
import struct
from typing import Protocol

from med_autopractice.platform.security import is_windows

_MAGIC = b"MAPENC1\n"
_REPO_ROOT = Path(__file__).resolve().parents[3]


class KeyProvider(Protocol):
    key_id: str

    def get_key(self) -> bytes: ...


def _decode_key_material(raw: bytes | str) -> bytes:
    if isinstance(raw, str):
        stripped = raw.strip()
        try:
            decoded = base64.b64decode(stripped.encode("ascii"), validate=True)
            if len(decoded) >= 32:
                return decoded[:32]
        except Exception:
            pass
        raw_bytes = stripped.encode("utf-8")
    else:
        raw_bytes = raw.strip()
    if len(raw_bytes) < 32:
        raise ValueError("encryption key material must be at least 32 bytes")
    return raw_bytes[:32]


class EnvKeyProvider:
    """Read encryption key material from a configured environment variable."""

    def __init__(self, env_var: str = "MAP_EVIDENCE_KEY", *, key_id: str | None = None):
        self.env_var = env_var
        self.key_id = key_id or f"env:{env_var}"

    def get_key(self) -> bytes:
        val = os.environ.get(self.env_var)
        if not val:
            raise ValueError(f"missing encryption key environment variable: {self.env_var}")
        return _decode_key_material(val)


class FileKeyProvider:
    """Read encryption key material from a private 0600 file strictly outside the repository."""

    def __init__(self, path: Path, *, repo_root: Path = _REPO_ROOT, key_id: str | None = None):
        resolved = path.expanduser().resolve()
        if resolved.is_relative_to(repo_root.resolve()):
            raise ValueError("key file must live outside the source repository")
        if path.is_symlink() or not resolved.is_file():
            raise ValueError(f"key file must be a regular non-symlink file: {path}")
        if not is_windows():
            mode = resolved.stat().st_mode & 0o777
            if mode & 0o077:
                raise ValueError(f"key file permissions too open ({oct(mode)}); require 0600")
        self.path = resolved
        self.key_id = key_id or f"file:{resolved.name}"

    def get_key(self) -> bytes:
        return _decode_key_material(self.path.read_bytes())


def _ctr_xor(enc_key: bytes, nonce: bytes, data: bytes) -> bytes:
    out = bytearray(len(data))
    counter = 0
    offset = 0
    while offset < len(data):
        block_key = hmac.new(enc_key, nonce + struct.pack(">Q", counter), sha256).digest()
        chunk = data[offset : offset + 32]
        for idx, b in enumerate(chunk):
            out[offset + idx] = b ^ block_key[idx]
        offset += len(chunk)
        counter += 1
    return bytes(out)


class EvidenceCipher:
    """Authenticated encryption supporting optional AES-256-GCM and zero-dependency stdlib fallback."""

    def __init__(self, provider: KeyProvider, *, prefer_aesgcm: bool = True):
        self.provider = provider
        self.prefer_aesgcm = prefer_aesgcm

    def _aesgcm_class(self):
        if not self.prefer_aesgcm:
            return None
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore[import-not-found]
            return AESGCM
        except ImportError:
            return None

    def encrypt_bytes(self, plaintext: bytes, *, aad: bytes = b"") -> bytes:
        master = self.provider.get_key()
        aesgcm_cls = self._aesgcm_class()
        if aesgcm_cls is not None:
            nonce = secrets.token_bytes(12)
            ct = aesgcm_cls(master[:32]).encrypt(nonce, plaintext, aad)
            header = {
                "alg": "aes-256-gcm",
                "key_id": self.provider.key_id,
                "nonce": base64.b64encode(nonce).decode("ascii"),
            }
            header_bytes = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
            return _MAGIC + struct.pack(">I", len(header_bytes)) + header_bytes + ct

        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(16)
        derived = pbkdf2_hmac("sha256", master, salt, 60000, dklen=64)
        enc_key, mac_key = derived[:32], derived[32:]
        ct = _ctr_xor(enc_key, nonce, plaintext)
        meta_prefix = json.dumps(
            {
                "alg": "stdlib-ctr-hmac-sha256",
                "key_id": self.provider.key_id,
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "salt": base64.b64encode(salt).decode("ascii"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        tag = hmac.new(mac_key, meta_prefix + aad + ct, sha256).digest()
        header = json.loads(meta_prefix.decode("utf-8"))
        header["tag"] = base64.b64encode(tag).decode("ascii")
        header_bytes = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return _MAGIC + struct.pack(">I", len(header_bytes)) + header_bytes + ct

    def decrypt_bytes(self, envelope: bytes, *, aad: bytes = b"") -> bytes:
        if not envelope.startswith(_MAGIC) or len(envelope) < len(_MAGIC) + 4:
            raise ValueError("invalid encrypted evidence envelope header")
        pos = len(_MAGIC)
        (hdr_len,) = struct.unpack(">I", envelope[pos : pos + 4])
        pos += 4
        if hdr_len <= 0 or pos + hdr_len > len(envelope):
            raise ValueError("corrupted encrypted evidence envelope length")
        try:
            header = json.loads(envelope[pos : pos + hdr_len].decode("utf-8"))
        except Exception as error:
            raise ValueError("corrupted encrypted evidence header JSON") from error
        ct = envelope[pos + hdr_len :]
        alg = header.get("alg")
        master = self.provider.get_key()

        if alg == "aes-256-gcm":
            aesgcm_cls = self._aesgcm_class()
            if aesgcm_cls is None:
                raise ValueError("cryptography package required to decrypt aes-256-gcm envelope")
            nonce = base64.b64decode(header["nonce"].encode("ascii"))
            try:
                return aesgcm_cls(master[:32]).decrypt(nonce, ct, aad)
            except Exception as error:
                raise ValueError("authenticated decryption failed (wrong key or tampered data)") from error

        if alg == "stdlib-ctr-hmac-sha256":
            try:
                nonce = base64.b64decode(header["nonce"].encode("ascii"))
                salt = base64.b64decode(header["salt"].encode("ascii"))
                tag = base64.b64decode(header["tag"].encode("ascii"))
            except Exception as error:
                raise ValueError("invalid envelope metadata encoding") from error
            derived = pbkdf2_hmac("sha256", master, salt, 60000, dklen=64)
            enc_key, mac_key = derived[:32], derived[32:]
            meta_prefix = json.dumps(
                {
                    "alg": alg,
                    "key_id": header.get("key_id"),
                    "nonce": header["nonce"],
                    "salt": header["salt"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            expected_tag = hmac.new(mac_key, meta_prefix + aad + ct, sha256).digest()
            if not hmac.compare_digest(tag, expected_tag):
                raise ValueError("authenticated decryption failed (wrong key or tampered data)")
            return _ctr_xor(enc_key, nonce, ct)

        raise ValueError(f"unsupported encryption algorithm: {alg!r}")

    def encrypt_file(self, src: Path, dst: Path, *, aad: bytes = b"") -> Path:
        data = src.read_bytes()
        encrypted = self.encrypt_bytes(data, aad=aad)
        dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        dst.write_bytes(encrypted)
        if not is_windows():
            dst.chmod(0o600)
        return dst

    def decrypt_file(self, src: Path, dst: Path, *, aad: bytes = b"") -> Path:
        plaintext = self.decrypt_bytes(src.read_bytes(), aad=aad)
        dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        dst.write_bytes(plaintext)
        if not is_windows():
            dst.chmod(0o600)
        return dst
