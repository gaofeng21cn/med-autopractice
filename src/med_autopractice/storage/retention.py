"""Time-bounded evidence screenshot & audit log retention cleaner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class RetentionPolicy:
    screenshot_max_age_days: int = 30
    task_log_max_age_days: int = 90
    dry_run: bool = False
    screenshot_extensions: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp")
    log_extensions: tuple[str, ...] = (".jsonl", ".log", ".json", ".enc")

    def __post_init__(self) -> None:
        if self.screenshot_max_age_days < 0 or self.task_log_max_age_days < 0:
            raise ValueError("retention days must be >= 0")


@dataclass(frozen=True)
class RetentionReport:
    scanned: int
    deleted: int
    kept: int
    freed_bytes: int
    dry_run: bool
    deleted_paths: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "deleted": self.deleted,
            "kept": self.kept,
            "freed_bytes": self.freed_bytes,
            "dry_run": self.dry_run,
            "deleted_paths": list(self.deleted_paths),
        }


def apply_retention(
    evidence_dir: Path,
    policy: RetentionPolicy,
    *,
    now: datetime | None = None,
) -> RetentionReport:
    """Safely purge expired evidence files strictly inside evidence_dir without following symlinks."""
    root = evidence_dir.expanduser().resolve()
    if evidence_dir.is_symlink() or not root.is_dir():
        raise ValueError(f"evidence_dir must be a regular directory: {evidence_dir}")

    ref_time = now or datetime.now(timezone.utc)
    shot_cutoff = ref_time - timedelta(days=policy.screenshot_max_age_days)
    log_cutoff = ref_time - timedelta(days=policy.task_log_max_age_days)

    scanned = 0
    deleted = 0
    kept = 0
    freed_bytes = 0
    deleted_paths: list[str] = []

    for candidate in sorted(root.rglob("*")):
        if candidate.is_symlink():
            raise ValueError(f"symlink rejected inside evidence directory: {candidate}")
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"path escapes evidence directory: {candidate}")

        suffixes = [s.lower() for s in resolved.suffixes]
        is_encrypted_shot = (
            len(suffixes) >= 2
            and suffixes[-1] == ".enc"
            and suffixes[-2] in policy.screenshot_extensions
        )
        if suffixes and (suffixes[-1] in policy.screenshot_extensions or is_encrypted_shot):
            cutoff = shot_cutoff
        elif suffixes and suffixes[-1] in policy.log_extensions:
            cutoff = log_cutoff
        else:
            continue

        scanned += 1
        st = resolved.stat()
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            if not policy.dry_run:
                resolved.unlink()
            deleted += 1
            freed_bytes += st.st_size
            deleted_paths.append(str(resolved.relative_to(root)))
        else:
            kept += 1

    return RetentionReport(
        scanned=scanned,
        deleted=deleted,
        kept=kept,
        freed_bytes=freed_bytes,
        dry_run=policy.dry_run,
        deleted_paths=tuple(deleted_paths),
    )
