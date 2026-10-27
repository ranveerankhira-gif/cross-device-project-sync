#!/usr/bin/env python3
"""Verify a restored split-TAR backup without following links."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import pathlib
import re


HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
MTIME_TOLERANCE_SECONDS = 0.01
WINDOWS_RESERVED_STEMS = {
    "con", "prn", "aux", "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
WINDOWS_FORBIDDEN_CHARACTERS = set('<>:"\\|?*')


def safe_parts(raw: str) -> tuple[str, ...]:
    if not raw or "\\" in raw or "\x00" in raw or ":" in raw:
        raise ValueError(f"unsafe archive path: {raw!r}")
    path = pathlib.PurePosixPath(raw)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe archive path: {raw!r}")
    if pathlib.PureWindowsPath(raw).drive:
        raise ValueError(f"unsafe archive path: {raw!r}")
    if path.as_posix() != raw:
        raise ValueError(f"non-canonical archive path: {raw!r}")
    return path.parts


def windows_path_key(parts: tuple[str, ...]) -> tuple[str, ...]:
    key = []
    for component in parts:
        if component.endswith((" ", ".")):
            raise ValueError(f"Windows-unsafe trailing space or dot: {component!r}")
        if any(ord(character) < 32 or character in WINDOWS_FORBIDDEN_CHARACTERS for character in component):
            raise ValueError(f"Windows-unsafe filename component: {component!r}")
        stem = component.split(".", 1)[0].casefold()
        if stem in WINDOWS_RESERVED_STEMS:
            raise ValueError(f"Windows reserved filename component: {component!r}")
        key.append(component.casefold())
    return tuple(key)


def file_hash(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("backup", type=pathlib.Path)
    parser.add_argument("restored_root", type=pathlib.Path)
    args = parser.parse_args()

    backup = args.backup.resolve(strict=True)
    restored = args.restored_root.resolve(strict=True)
    manifest = json.loads((backup / "BACKUP_MANIFEST.json").read_text(encoding="utf-8"))
    manifest_format = manifest.get("format")
    if manifest_format not in {"split-tar-pax-v1", "split-tar-pax-v2"}:
        raise SystemExit("Unsupported or missing backup format")
    if manifest_format == "split-tar-pax-v2":
        for field in ("source_device", "source_computer"):
            value = manifest.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > 80 or any(ord(character) < 32 or ord(character) == 127 for character in value):
                raise SystemExit(f"Backup manifest has an invalid {field}")
        created_utc = manifest.get("created_utc")
        if not isinstance(created_utc, str) or not created_utc.endswith("Z"):
            raise SystemExit("Backup manifest has an invalid created_utc")
        try:
            parsed_created_utc = dt.datetime.fromisoformat(created_utc[:-1] + "+00:00")
        except ValueError as error:
            raise SystemExit("Backup manifest has an invalid created_utc") from error
        if parsed_created_utc.utcoffset() != dt.timedelta(0):
            raise SystemExit("Backup manifest created_utc must use UTC")
    if manifest.get("entry_count") != len(manifest.get("entries", [])):
        raise SystemExit("Manifest entry count does not match its entry inventory")
    source_name = manifest.get("source_name")
    try:
        source_parts = safe_parts(source_name)
        source_key = windows_path_key(source_parts)
    except (TypeError, ValueError) as error:
        raise SystemExit(f"Unsafe source name in manifest: {error}") from error
    if len(source_parts) != 1:
        raise SystemExit("Manifest source name must be one path segment")
    expected_paths: set[str] = set()
    windows_entries: dict[tuple[str, ...], tuple[str, str]] = {}

    for index, record in enumerate(manifest.get("entries", []), start=1):
        raw = record.get("path")
        try:
            parts = safe_parts(raw)
            windows_key = windows_path_key(parts)
        except (TypeError, ValueError) as error:
            raise SystemExit(f"Unsafe path in manifest entry {index}: {error}") from error
        if parts[0] != source_name:
            raise SystemExit(f"Entry {index} is outside the source root")
        if raw in expected_paths:
            raise SystemExit(f"Duplicate manifest entry: {raw}")
        expected_paths.add(raw)

        entry_type = str(record.get("type"))
        previous = windows_entries.get(windows_key)
        if previous is not None:
            raise SystemExit(f"Windows path collision between {previous[0]!r} and {raw!r}")
        for parent_length in range(1, len(windows_key)):
            parent = windows_entries.get(windows_key[:parent_length])
            if parent is None or parent[1] != "5":
                raise SystemExit(f"Manifest entry {index} has a missing or non-directory parent")
        windows_entries[windows_key] = (raw, entry_type)

        target = restored.joinpath(*parts)
        if target.is_symlink() or bool(getattr(target, "is_junction", lambda: False)()):
            raise SystemExit(f"Restored link is not allowed: {raw}")
        expected_mtime = record.get("mtime")
        if (
            not isinstance(expected_mtime, (int, float))
            or isinstance(expected_mtime, bool)
            or not math.isfinite(expected_mtime)
        ):
            raise SystemExit(f"Invalid restored timestamp in manifest: {raw}")
        if entry_type == "5":
            if not target.is_dir():
                raise SystemExit(f"Restored directory is missing: {raw}")
        elif entry_type in {"0", "\x00"}:
            if not target.is_file():
                raise SystemExit(f"Restored file is missing: {raw}")
            if target.stat().st_size != int(record.get("size", -1)):
                raise SystemExit(f"Restored file size mismatch: {raw}")
            expected_hash = str(record.get("sha256", ""))
            if not HEX_SHA256.fullmatch(expected_hash) or file_hash(target) != expected_hash:
                raise SystemExit(f"Restored file hash mismatch: {raw}")
        else:
            raise SystemExit(f"Unsupported restored entry type {entry_type!r}: {raw}")
        if abs(target.stat().st_mtime - float(expected_mtime)) > MTIME_TOLERANCE_SECONDS:
            raise SystemExit(f"Restored timestamp mismatch: {raw}")

    if windows_entries.get(source_key) != (source_name, "5"):
        raise SystemExit("The source-root entry must be one directory")

    actual_paths: set[str] = set()
    for path in restored.rglob("*"):
        if path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)()):
            raise SystemExit(f"Unexpected restored link: {path}")
        actual_paths.add(path.relative_to(restored).as_posix())

    missing = expected_paths - actual_paths
    extra = actual_paths - expected_paths
    if missing or extra:
        raise SystemExit(f"Restored inventory mismatch: missing={len(missing)} extra={len(extra)}")

    print(json.dumps({"restored_verified": True, "entries": len(expected_paths)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
