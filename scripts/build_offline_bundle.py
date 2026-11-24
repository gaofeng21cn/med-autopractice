"""Build and verify self-contained offline deployment bundles from opl-package.json."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
from uuid import uuid4
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def _sha256_file(path: Path) -> str:
    h = sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _ensure_outside_repo(path: Path, repo_root: Path = ROOT) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.is_relative_to(repo_root.resolve()):
        raise ValueError(f"offline bundle destination must live outside the repository: {resolved}")
    return resolved


def build_offline_bundle(
    output_dir: Path,
    *,
    repo_root: Path = ROOT,
    wheelhouse_dir: Path | None = None,
    archive: str | None = None,
    overwrite: bool = False,
) -> dict:
    """Copy content-locked files to an isolated offline bundle directory with SHA256 manifest."""
    dest = _ensure_outside_repo(output_dir, repo_root=repo_root)
    if dest.exists() or dest.is_symlink():
        if not overwrite:
            raise ValueError(
                f"offline bundle destination already exists: {dest}; pass --force to replace an existing MAP bundle"
            )
        if dest.is_symlink() or not dest.is_dir() or not (dest / "bundle-manifest.json").is_file():
            raise ValueError(
                f"refusing --force overwrite: destination is not an existing MAP bundle directory ({dest})"
            )

    package = json.loads((repo_root / "opl-package.json").read_text(encoding="utf-8"))
    lock_paths = list(package["content_lock"]["paths"])
    all_paths = ["opl-package.json", *lock_paths]

    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    stage_dir = Path(tempfile.mkdtemp(prefix=f".{dest.name}.stage-", dir=str(dest.parent)))
    os.chmod(stage_dir, 0o755)
    backup_dir: Path | None = None
    archive_path: Path | None = None

    try:
        file_entries: list[dict] = []
        for rel in all_paths:
            src = (repo_root / rel).resolve()
            if not src.is_relative_to(repo_root.resolve()) or not src.is_file():
                raise ValueError(f"missing content-locked file: {rel}")
            target = stage_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            file_entries.append({
                "path": rel,
                "size": target.stat().st_size,
                "sha256": _sha256_file(target),
            })

        wheel_entries: list[dict] = []
        if wheelhouse_dir is not None:
            wh_src = wheelhouse_dir.expanduser().resolve()
            if not wh_src.is_dir():
                raise ValueError(f"wheelhouse directory does not exist: {wh_src}")
            wh_dest = stage_dir / "wheelhouse"
            wh_dest.mkdir(parents=True, exist_ok=True)
            for item in sorted(wh_src.iterdir()):
                if item.is_file() and item.suffix in {".whl", ".gz", ".zip"}:
                    copied = wh_dest / item.name
                    shutil.copy2(item, copied)
                    wheel_entries.append({
                        "path": f"wheelhouse/{item.name}",
                        "size": copied.stat().st_size,
                        "sha256": _sha256_file(copied),
                    })

        manifest = {
            "schema_version": "map-offline-bundle.v1",
            "package_id": package["package_id"],
            "version": package["version"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "min_python": "3.12",
            "content_lock_digest": package["content_lock"]["digest"],
            "files_count": len(file_entries),
            "files": file_entries,
            "wheelhouse": wheel_entries,
            "offline_verify_command": "python3 scripts/project_status.py --json",
        }
        stage_manifest = stage_dir / "bundle-manifest.json"
        stage_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        check = verify_offline_bundle(stage_dir)
        if not check["valid"]:
            raise ValueError(f"staged offline bundle failed self-verification: {check['mismatches']}")

        if dest.exists():
            backup_dir = dest.parent / f".{dest.name}.backup-{uuid4().hex[:8]}"
            os.replace(dest, backup_dir)
        try:
            os.replace(stage_dir, dest)
        except Exception:
            if backup_dir is not None and backup_dir.exists() and not dest.exists():
                os.replace(backup_dir, dest)
            raise
        if backup_dir is not None and backup_dir.exists():
            shutil.rmtree(backup_dir)

        manifest_path = dest / "bundle-manifest.json"
        if archive == "tar.gz":
            archive_path = Path(f"{dest}.tar.gz")
            tmp_archive = dest.parent / f".{archive_path.name}.{uuid4().hex[:8]}.tmp"
            try:
                with tarfile.open(tmp_archive, "w:gz") as tf:
                    tf.add(dest, arcname=dest.name)
                os.replace(tmp_archive, archive_path)
            except Exception:
                tmp_archive.unlink(missing_ok=True)
                raise
        elif archive == "zip":
            archive_path = Path(f"{dest}.zip")
            tmp_archive = dest.parent / f".{archive_path.name}.{uuid4().hex[:8]}.tmp"
            try:
                with zipfile.ZipFile(tmp_archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    for item in sorted(dest.rglob("*")):
                        if item.is_file():
                            zf.write(item, arcname=f"{dest.name}/{item.relative_to(dest)}")
                os.replace(tmp_archive, archive_path)
            except Exception:
                tmp_archive.unlink(missing_ok=True)
                raise
        elif archive is not None:
            raise ValueError(f"unsupported archive format: {archive!r}")

        return {
            "bundle_dir": str(dest),
            "manifest": str(manifest_path),
            "files_count": len(file_entries),
            "wheelhouse_count": len(wheel_entries),
            "archive": str(archive_path) if archive_path else None,
        }
    finally:
        if stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)


def verify_offline_bundle(bundle_dir: Path) -> dict:
    """Verify every file listed in bundle-manifest.json against its recorded SHA256."""
    root = bundle_dir.expanduser().resolve()
    manifest_path = root / "bundle-manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"bundle-manifest.json missing in {root}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "map-offline-bundle.v1":
        raise ValueError("unsupported offline bundle manifest schema")

    mismatches: list[str] = []
    entries = [*manifest.get("files", []), *manifest.get("wheelhouse", [])]
    for entry in entries:
        rel = entry["path"]
        target = (root / rel).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            mismatches.append(f"missing:{rel}")
            continue
        actual = _sha256_file(target)
        if actual != entry["sha256"]:
            mismatches.append(f"sha256_mismatch:{rel}")

    return {
        "valid": not mismatches,
        "checked_files": len(entries),
        "mismatches": mismatches,
        "package_id": manifest.get("package_id"),
        "version": manifest.get("version"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--output", type=Path, help="Destination directory outside repo for offline bundle")
    group.add_argument("--verify", type=Path, help="Verify integrity of an existing offline bundle directory")
    parser.add_argument("--wheelhouse", type=Path, default=None, help="Optional local wheelhouse directory")
    parser.add_argument("--archive", choices=("tar.gz", "zip"), default=None)
    parser.add_argument("--force", action="store_true", help="Replace an existing MAP offline bundle directory")
    args = parser.parse_args(argv)

    try:
        if args.verify is not None:
            result = verify_offline_bundle(args.verify)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["valid"] else 1
        result = build_offline_bundle(
            args.output, wheelhouse_dir=args.wheelhouse, archive=args.archive, overwrite=args.force,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ValueError as error:
        print(json.dumps({"valid": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
