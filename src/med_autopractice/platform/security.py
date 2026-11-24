"""Cross-platform owner and boundary verification for private site storage."""

from __future__ import annotations

import os
from pathlib import Path
import stat


def is_windows() -> bool:
    return os.name == "nt"


def ensure_private_file_mode(path: Path, *, mode: int = 0o600) -> None:
    if is_windows():
        return
    os.chmod(path, mode)


def open_private_rw(path: Path) -> None:
    """Create or open a private regular file and verify owner permissions."""

    if path.is_symlink():
        raise PermissionError("task database must not be a symlink")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        identity = os.fstat(fd)
        if not stat.S_ISREG(identity.st_mode):
            raise PermissionError("task database must be a regular file")
        if not is_windows():
            if identity.st_uid != os.getuid() or stat.S_IMODE(identity.st_mode) & 0o077:
                raise PermissionError("task database must be private to its owner")
    finally:
        os.close(fd)


def verify_private_directory(path: Path, repo_root: Path | None = None) -> Path:
    """Verify a site directory exists outside Git and belongs to the local owner."""

    resolved = path.expanduser().resolve(strict=True)
    if resolved.is_symlink() or not resolved.is_dir():
        raise PermissionError("deployment path must be a real directory")
    if repo_root is not None and resolved.is_relative_to(repo_root):
        raise PermissionError("use a private deployment directory outside Git")
    if any((parent / ".git").exists() for parent in (resolved, *resolved.parents)):
        raise PermissionError("use a private deployment directory outside Git")
    if not is_windows():
        identity = resolved.stat()
        if identity.st_uid != os.getuid() or stat.S_IMODE(identity.st_mode) & 0o077:
            raise PermissionError("deployment directory must be private to its owner")
    return resolved


def verify_private_file(path: Path) -> bytes:
    """Read a deployment file after verifying it is a private non-symlink file."""

    if path.is_symlink() or not path.is_file():
        raise PermissionError("deployment input must be a regular file")
    if not is_windows():
        identity = path.stat()
        if identity.st_uid != os.getuid() or stat.S_IMODE(identity.st_mode) & 0o022:
            raise PermissionError("deployment input must be owned and writable only by its owner")
    return path.read_bytes()
