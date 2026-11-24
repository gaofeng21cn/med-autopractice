"""Cross-platform filesystem locking and private-storage primitives."""

from med_autopractice.platform.locking import exclusive_file_lock
from med_autopractice.platform.security import (
    ensure_private_file_mode,
    is_windows,
    open_private_rw,
    verify_private_directory,
    verify_private_file,
)

__all__ = [
    "ensure_private_file_mode",
    "exclusive_file_lock",
    "is_windows",
    "open_private_rw",
    "verify_private_directory",
    "verify_private_file",
]
