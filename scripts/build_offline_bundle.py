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

from package_metadata import verify_package

ROOT = Path(__file__).resolve().parents[1]
_BUNDLE_SCHEMA = "map-offline-bundle.v1"


def _sha256_file(path: Path) -> str:
    h = sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _ensure_outside_repo(path: Path, repo_root: Path = ROOT) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ValueError("offline bundle destination must be a real directory")
    resolved = expanded.resolve()
    if resolved.is_relative_to(repo_root.resolve()):
        raise ValueError(f"offline bundle destination must live outside the repository: {resolved}")
    return resolved


def _check_replacement(dest: Path, overwrite: bool) -> None:
    if dest.is_symlink():
        raise ValueError("offline bundle destination must be a real directory")
    if not dest.exists():
        return
    if not overwrite:
        raise ValueError(f"offline bundle destination already exists: {dest}; use --force to replace a MAP bundle")
    try:
        manifest = json.loads((dest / "bundle-manifest.json").read_text(encoding="utf-8"))
        package = json.loads((dest / "opl-package.json").read_text(encoding="utf-8"))
        identity = package["identity"]
        lock = json.loads((dest / "contracts/package_content_lock.json").read_text(encoding="utf-8"))
        if (manifest["schema_version"] != _BUNDLE_SCHEMA or manifest["package_id"] != "med-autopractice"
                or identity["id"] != manifest["package_id"] or manifest["version"] != identity["version"]
                or manifest["content_lock_digest"] != lock["digest"]):
            raise ValueError("bundle and package identity differ")
        managed = {"bundle-manifest.json", "opl-package.json", "contracts/package_content_lock.json", *lock["paths"]}
        managed.update(entry["path"] for entry in manifest["wheelhouse"])
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"refusing --force overwrite of an unrecognized bundle: {dest}") from error
    for path in dest.rglob("*"):
        python_cache = path.parent.name == "__pycache__" and path.suffix == ".pyc"
        if path.is_symlink() or (path.is_file() and not python_cache and path.relative_to(dest).as_posix() not in managed):
            raise ValueError(f"refusing --force overwrite of a bundle with unmanaged content: {path}")


def _check_archive_replacement(archive_path: Path, dest: Path, overwrite: bool) -> None:
    if not archive_path.exists() and not archive_path.is_symlink():
        return
    if archive_path.is_symlink() or not overwrite or not (dest / "bundle-manifest.json").is_file():
        raise ValueError(f"offline bundle archive destination already exists: {archive_path}")
    entry = f"{dest.name}/bundle-manifest.json"
    try:
        if archive_path.suffix == ".zip":
            with zipfile.ZipFile(archive_path) as archive:
                manifest = archive.read(entry)
        else:
            with tarfile.open(archive_path, "r:gz") as archive:
                with archive.extractfile(entry) as member:
                    manifest = member.read()
        if manifest == (dest / "bundle-manifest.json").read_bytes():
            return
    except (OSError, KeyError, TypeError, ValueError, AttributeError, tarfile.TarError, zipfile.BadZipFile):
        pass
    raise ValueError(f"refusing to overwrite an unrelated archive: {archive_path}")


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
    if archive not in {None, "zip", "tar.gz"}:
        raise ValueError(f"unsupported archive format: {archive!r}")
    archive_path = dest.with_name(f"{dest.name}.{archive}") if archive is not None else None
    _check_replacement(dest, overwrite)
    if archive_path is not None:
        _check_archive_replacement(archive_path, dest, overwrite)

    package = json.loads((repo_root / "opl-package.json").read_text(encoding="utf-8"))
    identity = package["identity"]
    lock = json.loads((repo_root / "contracts/package_content_lock.json").read_text(encoding="utf-8"))
    lock_paths = list(lock["paths"])
    all_paths = ["opl-package.json", "contracts/package_content_lock.json", *lock_paths]

    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    stage_dir = Path(tempfile.mkdtemp(prefix=f".{dest.name}.stage-", dir=str(dest.parent)))
    os.chmod(stage_dir, 0o755)
    backup_dir: Path | None = None
    tmp_archive: Path | None = None

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
            "schema_version": _BUNDLE_SCHEMA,
            "package_id": identity["id"],
            "version": identity["version"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "min_python": "3.10",
            "content_lock_digest": lock["digest"],
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

        if archive_path is not None:
            tmp_archive = stage_dir.with_name(stage_dir.name + ".archive")
            if archive == "tar.gz":
                with tarfile.open(tmp_archive, "w:gz") as tf:
                    tf.add(stage_dir, arcname=dest.name)
            else:
                with zipfile.ZipFile(tmp_archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    for item in sorted(stage_dir.rglob("*")):
                        if item.is_file():
                            zf.write(item, arcname=f"{dest.name}/{item.relative_to(stage_dir).as_posix()}")

        _check_replacement(dest, overwrite)
        if archive_path is not None:
            _check_archive_replacement(archive_path, dest, overwrite)
        if dest.exists():
            backup_dir = dest.parent / f".{dest.name}.backup-{uuid4().hex[:8]}"
            os.replace(dest, backup_dir)
        installed = False
        try:
            os.replace(stage_dir, dest)
            installed = True
            if tmp_archive is not None:
                os.replace(tmp_archive, archive_path)
        except Exception:
            if installed:
                os.replace(dest, stage_dir)
            if backup_dir is not None and backup_dir.exists() and not dest.exists():
                os.replace(backup_dir, dest)
            raise
        if backup_dir is not None and backup_dir.exists():
            shutil.rmtree(backup_dir)

        return {
            "bundle_dir": str(dest),
            "manifest": str(dest / "bundle-manifest.json"),
            "files_count": len(file_entries),
            "wheelhouse_count": len(wheel_entries),
            "archive": str(archive_path) if archive_path else None,
        }
    finally:
        if tmp_archive is not None:
            tmp_archive.unlink(missing_ok=True)
        if stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)


def verify_offline_bundle(bundle_dir: Path) -> dict:
    """Verify every file listed in bundle-manifest.json against its recorded SHA256."""
    root = bundle_dir.expanduser().resolve()
    manifest_path = root / "bundle-manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"bundle-manifest.json missing in {root}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != _BUNDLE_SCHEMA:
        raise ValueError("unsupported offline bundle manifest schema")

    mismatches: list[str] = []
    files = manifest["files"]
    entries = [*files, *manifest["wheelhouse"]]
    try:
        verify_package(root)
        package = json.loads((root / "opl-package.json").read_text(encoding="utf-8"))
        identity = package["identity"]
        lock = json.loads((root / "contracts/package_content_lock.json").read_text(encoding="utf-8"))
        expected_paths = ["opl-package.json", "contracts/package_content_lock.json", *lock["paths"]]
        if (len(files) != manifest["files_count"] or [item["path"] for item in files] != expected_paths
                or manifest["content_lock_digest"] != lock["digest"]
                or manifest["package_id"] != identity["id"] or manifest["version"] != identity["version"]):
            mismatches.append("package_manifest_mismatch")
    except (OSError, KeyError, TypeError, ValueError):
        mismatches.append("package_content_lock_mismatch")
    for entry in entries:
        rel = entry["path"]
        target = root / rel
        if target.is_symlink() or not target.resolve().is_relative_to(root) or not target.is_file():
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
    except (KeyError, OSError, TypeError, ValueError) as error:
        print(json.dumps({"valid": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
