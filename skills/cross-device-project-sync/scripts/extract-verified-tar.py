#!/usr/bin/env python3
"""Extract an already verified TAR into a pre-created, replacement-locked tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import tarfile


WINDOWS_RESERVED_STEMS = {
    "con", "prn", "aux", "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
WINDOWS_FORBIDDEN_CHARACTERS = set('<>:"\\|?*')


def safe_parts(raw: str) -> tuple[str, ...]:
    if not raw or "\\" in raw or "\x00" in raw or ":" in raw:
        raise ValueError(f"unsafe archive path: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe archive path: {raw!r}")
    if PureWindowsPath(raw).drive or path.as_posix() != raw:
        raise ValueError(f"non-canonical archive path: {raw!r}")
    return path.parts


def windows_path_key(parts: tuple[str, ...]) -> tuple[str, ...]:
    key = []
    for component in parts:
        if component.endswith((" ", ".")):
            raise ValueError(f"Windows-unsafe trailing space or dot: {component!r}")
        if any(ord(character) < 32 or character in WINDOWS_FORBIDDEN_CHARACTERS for character in component):
            raise ValueError(f"Windows-unsafe filename component: {component!r}")
        if component.split(".", 1)[0].casefold() in WINDOWS_RESERVED_STEMS:
            raise ValueError(f"Windows reserved filename component: {component!r}")
        key.append(component.casefold())
    return tuple(key)


def is_link_like(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def require_precreated_target(root: Path, parts: tuple[str, ...], entry_type: str) -> Path:
    target = root.joinpath(*parts)
    cursor = target
    while True:
        if is_link_like(cursor):
            raise ValueError(f"restore path is a link or reparse point: {target}")
        if cursor == root:
            break
        cursor = cursor.parent
    resolved = target.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError(f"restore path resolved outside destination: {target}") from error
    if entry_type == "5" and not target.is_dir():
        raise ValueError(f"pre-created restore directory is missing: {target}")
    if entry_type in {"0", "\x00"} and not target.is_file():
        raise ValueError(f"pre-created restore file is missing: {target}")
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()

    archive_path = args.archive.resolve(strict=True)
    destination = args.destination.resolve(strict=True)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest.get("format") not in {"split-tar-pax-v1", "split-tar-pax-v2"}:
        raise SystemExit("Unsupported or missing backup format")
    records = manifest.get("entries")
    if not isinstance(records, list) or manifest.get("entry_count") != len(records):
        raise SystemExit("Manifest entry inventory is incomplete")

    expected: list[tuple[str, tuple[str, ...], str, dict]] = []
    seen_names: set[str] = set()
    windows_entries: dict[tuple[str, ...], tuple[str, str]] = {}
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise SystemExit(f"Manifest entry {index} is invalid")
        raw = str(record.get("path") or "")
        entry_type = str(record.get("type"))
        try:
            parts = safe_parts(raw)
            key = windows_path_key(parts)
        except ValueError as error:
            raise SystemExit(f"Manifest entry {index}: {error}") from error
        if raw in seen_names or key in windows_entries:
            raise SystemExit(f"Duplicate or Windows-colliding manifest path: {raw}")
        for parent_length in range(1, len(key)):
            parent = windows_entries.get(key[:parent_length])
            if parent is None or parent[1] != "5":
                raise SystemExit(f"Manifest entry {index} has a missing or non-directory parent")
        if entry_type not in {"0", "5", "\x00"}:
            raise SystemExit(f"Unsupported manifest entry type: {entry_type!r}")
        seen_names.add(raw)
        windows_entries[key] = (raw, entry_type)
        expected.append((raw, parts, entry_type, record))

    extracted = 0
    with tarfile.open(archive_path, mode="r:") as archive:
        members = archive.getmembers()
        if len(members) != len(expected):
            raise SystemExit("Archive and manifest entry counts differ")
        for index, (member, item) in enumerate(zip(members, expected), start=1):
            raw, parts, entry_type, record = item
            if member.name != raw:
                raise SystemExit(f"Archive order or path differs from manifest at entry {index}")
            if (entry_type == "5") != member.isdir() or (entry_type in {"0", "\x00"}) != member.isreg():
                raise SystemExit(f"Archive type differs from manifest: {raw}")
            target = require_precreated_target(destination, parts, entry_type)
            expected_mtime = record.get("mtime")
            if (
                not isinstance(expected_mtime, (int, float))
                or isinstance(expected_mtime, bool)
                or not math.isfinite(expected_mtime)
                or float(member.mtime) != float(expected_mtime)
            ):
                raise SystemExit(f"Archive timestamp differs from manifest: {raw}")
            if entry_type in {"0", "\x00"}:
                expected_size = record.get("size")
                expected_hash = str(record.get("sha256") or "")
                if (
                    member.size != expected_size
                    or len(expected_hash) != 64
                    or any(character not in "0123456789abcdef" for character in expected_hash)
                ):
                    raise SystemExit(f"Archive size or hash metadata differs from manifest: {raw}")
                source = archive.extractfile(member)
                if source is None:
                    raise SystemExit(f"Archive file payload is missing: {raw}")
                digest = hashlib.sha256()
                written = 0
                with source, target.open("r+b") as output:
                    output.seek(0)
                    output.truncate(0)
                    for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
                        output.write(chunk)
                        digest.update(chunk)
                        written += len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if written != expected_size or digest.hexdigest() != expected_hash:
                    raise SystemExit(f"Extracted bytes differ from manifest: {raw}")
                # Windows does not implement follow_symlinks=False for utime.
                # The target and every ancestor were checked above and remain
                # replacement-locked by the calling PowerShell process.
                os.utime(target, (float(expected_mtime), float(expected_mtime)))
            extracted += 1

    print(json.dumps({"extracted": True, "entries": extracted}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

