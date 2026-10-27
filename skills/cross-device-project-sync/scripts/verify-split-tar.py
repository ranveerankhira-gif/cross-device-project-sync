#!/usr/bin/env python3
"""Verify split parts and every regular-file payload against the backup manifest."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import math
import pathlib
import re
import tarfile


HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
MTIME_TOLERANCE_SECONDS = 0.01
WINDOWS_RESERVED_STEMS = {
    "con", "prn", "aux", "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
WINDOWS_FORBIDDEN_CHARACTERS = set('<>:"\\|?*')


class SplitReader(io.RawIOBase):
    def __init__(self, parts: list[pathlib.Path]):
        self.parts = iter(parts)
        self.current = None

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        view = memoryview(buffer)
        total = 0
        while total < len(view):
            if self.current is None:
                try:
                    self.current = next(self.parts).open("rb")
                except StopIteration:
                    break
            count = self.current.readinto(view[total:])
            if count:
                total += count
            else:
                self.current.close()
                self.current = None
        return total

    def close(self) -> None:
        if not self.closed and self.current is not None:
            self.current.close()
            self.current = None
        super().close()


def file_hash(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_member_name(raw: str) -> tuple[str, ...]:
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("backup", type=pathlib.Path)
    args = parser.parse_args()

    backup = args.backup.resolve(strict=True)
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
    archive_stem = str(manifest.get("archive_stem", "sample-project-full-backup"))
    if not archive_stem or len(archive_stem) > 80 or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in archive_stem):
        raise SystemExit("Unsafe archive stem")
    if manifest.get("source_state_changed_during_backup"):
        raise SystemExit("Backup records source changes and is not a stable snapshot")
    source_name = str(manifest.get("source_name", ""))
    try:
        source_parts = safe_member_name(source_name)
        source_key = windows_path_key(source_parts)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    if len(source_parts) != 1:
        raise SystemExit("Source name must be one safe path segment")

    expected = manifest.get("entries")
    if not isinstance(expected, list) or not expected:
        raise SystemExit("Manifest entries are missing")
    if manifest.get("entry_count") != len(expected):
        raise SystemExit("Manifest entry count does not match its entry inventory")
    seen_names = set()
    windows_entries: dict[tuple[str, ...], tuple[str, str]] = {}
    for index, record in enumerate(expected, start=1):
        raw = str(record.get("path", ""))
        try:
            parts = safe_member_name(raw)
            windows_key = windows_path_key(parts)
        except ValueError as error:
            raise SystemExit(f"Entry {index}: {error}") from error
        if parts[0] != source_name:
            raise SystemExit(f"Entry {index} is outside the source root")
        if raw in seen_names:
            raise SystemExit(f"Duplicate manifest entry: {raw}")
        seen_names.add(raw)
        entry_type = str(record.get("type"))
        if entry_type not in {"0", "5", "\x00"}:
            raise SystemExit(f"Unsupported entry type {entry_type!r} at position {index}")
        entry_size = record.get("size")
        if not isinstance(entry_size, int) or isinstance(entry_size, bool) or entry_size < 0:
            raise SystemExit(f"Invalid entry size at position {index}")
        entry_mtime = record.get("mtime")
        if (
            not isinstance(entry_mtime, (int, float))
            or isinstance(entry_mtime, bool)
            or not math.isfinite(entry_mtime)
        ):
            raise SystemExit(f"Invalid entry timestamp at position {index}")
        if entry_type in {"0", "\x00"} and not HEX_SHA256.fullmatch(str(record.get("sha256", ""))):
            raise SystemExit(f"Invalid file hash at position {index}")
        previous = windows_entries.get(windows_key)
        if previous is not None:
            raise SystemExit(
                f"Windows path collision between {previous[0]!r} and {raw!r}"
            )
        for parent_length in range(1, len(windows_key)):
            parent = windows_entries.get(windows_key[:parent_length])
            if parent is None or parent[1] != "5":
                raise SystemExit(f"Entry {index} has a missing or non-directory parent")
        windows_entries[windows_key] = (raw, entry_type)

    if windows_entries.get(source_key) != (source_name, "5"):
        raise SystemExit("The first source-root entry must be one directory")

    parts_manifest = manifest.get("parts")
    if not isinstance(parts_manifest, list) or not parts_manifest:
        raise SystemExit("Manifest parts are missing")
    part_paths = []
    total_part_bytes = 0
    configured_part_size = manifest.get("part_size_bytes")
    if not isinstance(configured_part_size, int) or configured_part_size <= 0:
        raise SystemExit("Invalid configured part size")
    for index, part in enumerate(parts_manifest, start=1):
        expected_name = f"{archive_stem}.tar.part{index:03d}"
        if part.get("name") != expected_name:
            raise SystemExit(f"Unexpected part name at position {index}")
        if not HEX_SHA256.fullmatch(str(part.get("sha256", ""))):
            raise SystemExit(f"Invalid part hash at position {index}")
        if not isinstance(part.get("size"), int) or part["size"] <= 0:
            raise SystemExit(f"Invalid part size at position {index}")
        if index < len(parts_manifest) and part["size"] != configured_part_size:
            raise SystemExit(f"Non-final part {index} has an unexpected size")
        if index == len(parts_manifest) and part["size"] > configured_part_size:
            raise SystemExit("Final part exceeds the configured part size")
        path = backup / part["name"]
        if path.parent != backup:
            raise SystemExit(f"Part {index} resolves outside the backup directory")
        if not path.is_file() or path.stat().st_size != part["size"]:
            raise SystemExit(f"Part {index} is missing or has the wrong size")
        if file_hash(path) != part["sha256"]:
            raise SystemExit(f"Part {index} hash mismatch")
        part_paths.append(path)
        total_part_bytes += part["size"]
        print(json.dumps({"verified_parts": index}), flush=True)
    if total_part_bytes != manifest.get("archive_total_bytes"):
        raise SystemExit("Archive byte total does not match the part inventory")
    if total_part_bytes % tarfile.RECORDSIZE:
        raise SystemExit("Archive byte total is not aligned to a canonical TAR record")

    stream = io.BufferedReader(SplitReader(part_paths), buffer_size=8 * 1024 * 1024)
    seen = 0
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for seen, member in enumerate(archive, start=1):
            if seen > len(expected):
                raise SystemExit("Archive contains more entries than the manifest")
            record = expected[seen - 1]
            member_type = member.type.decode("ascii", errors="replace") if isinstance(member.type, bytes) else str(member.type)
            if (
                member.name != record["path"]
                or member.size != record["size"]
                or member_type != str(record["type"])
                or abs(float(member.mtime) - float(record["mtime"])) > MTIME_TOLERANCE_SECONDS
            ):
                raise SystemExit(f"Entry metadata mismatch at position {seen}")
            if member.isreg():
                source = archive.extractfile(member)
                if source is None:
                    raise SystemExit(f"Cannot read regular-file payload at position {seen}")
                digest = hashlib.sha256()
                for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
                    digest.update(chunk)
                if digest.hexdigest() != record.get("sha256"):
                    raise SystemExit(f"Entry hash mismatch at position {seen}")
            if seen % 1000 == 0:
                print(json.dumps({"verified_entries": seen}), flush=True)
    for trailing in iter(lambda: stream.read(8 * 1024 * 1024), b""):
        if any(trailing):
            raise SystemExit("Archive contains non-zero data after the TAR end marker")
    if seen != len(expected):
        raise SystemExit(f"Entry count mismatch: {seen} != {len(expected)}")
    print(json.dumps({
        "verified": True,
        "parts": len(part_paths),
        "entries": seen,
        "archive_bytes": manifest["archive_total_bytes"],
        "source_state_changed_during_backup": manifest["source_state_changed_during_backup"],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
