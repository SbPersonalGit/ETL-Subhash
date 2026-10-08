"""Shared helpers for ETL run archive, verification, and restore utilities."""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


ARCHIVE_SCHEMA_VERSION = 1
COMPONENTS = ("exports", "state", "logs", "verification", "config")
SENSITIVE_NAMES = {
    ".env",
    "source_jwt.key",
    "target_jwt.key",
    "test_source_jwt.key",
}
SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def is_sensitive(path: Path) -> bool:
    return path.name.lower() in SENSITIVE_NAMES or path.suffix.lower() in SENSITIVE_SUFFIXES


def ensure_within(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError(f"Path escapes configured root: {path}")
    return resolved


def copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def file_record(path: Path, relative_to: Path) -> dict:
    return {
        "path": path.relative_to(relative_to).as_posix(),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def create_zip(source_root: Path, destination: Path, files: Iterable[Path]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".uploading")
    if temporary.exists():
        temporary.unlink()
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for path in sorted(files):
            archive.write(path, path.relative_to(source_root).as_posix())
    temporary.replace(destination)


def safe_extract(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    destination_resolved = destination.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            target = (destination / member.filename).resolve()
            if target != destination_resolved and destination_resolved not in target.parents:
                raise ValueError(f"Unsafe ZIP member: {member.filename}")
        archive.extractall(destination)


def verify_package(package_path: Path, expected: dict) -> list[str]:
    errors: list[str] = []
    if not package_path.is_file():
        return [f"Missing package: {package_path}"]
    if package_path.stat().st_size != expected["size"]:
        errors.append(f"Size mismatch: {package_path}")
    actual_hash = sha256_file(package_path)
    if actual_hash != expected["sha256"]:
        errors.append(f"SHA-256 mismatch: {package_path}")
    try:
        with zipfile.ZipFile(package_path) as archive:
            bad_member = archive.testzip()
            if bad_member:
                errors.append(f"Corrupt ZIP member {bad_member}: {package_path}")
    except zipfile.BadZipFile:
        errors.append(f"Invalid ZIP package: {package_path}")
    return errors


def validate_duckdb(path: Path) -> dict:
    """Open a copied database read-only and capture table counts."""
    import duckdb

    connection = duckdb.connect(str(path), read_only=True)
    try:
        rows = connection.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' ORDER BY table_name"
        ).fetchall()
        counts = {}
        for (table_name,) in rows:
            escaped = table_name.replace('"', '""')
            counts[table_name] = connection.execute(
                f'SELECT COUNT(*) FROM "{escaped}"'
            ).fetchone()[0]
        return {"valid": True, "tables": counts}
    finally:
        connection.close()


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]

