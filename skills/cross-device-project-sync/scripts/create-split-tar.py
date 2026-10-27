#!/usr/bin/env python3
"""Create a complete, split TAR backup without consulting ignore rules."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import pathlib
import re
import stat
import tarfile
import time
from dataclasses import dataclass


@dataclass
class Part:
    path: pathlib.Path
    size: int
    sha256: str


PROTECTED_PROFILE_NAME = re.compile(
    r"^(?:chrome|chromium|edge|msedge|browser|playwright).*test[-_ ]?profile(?:[-_ ].*)?$",
    re.IGNORECASE,
)
WINDOWS_RESERVED_STEMS = {
    "con", "prn", "aux", "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
WINDOWS_FORBIDDEN_CHARACTERS = set('<>:"\\|?*')


class SplitWriter(io.RawIOBase):
    def __init__(self, output_dir: pathlib.Path, stem: str, part_size: int):
        self.output_dir = output_dir
        self.stem = stem
        self.part_size = part_size
        self.index = 0
        self.current = None
        self.current_path = None
        self.current_size = 0
        self.current_hash = None
        self.parts: list[Part] = []
        self.total = 0

    def writable(self) -> bool:
        return True

    def _open_part(self) -> None:
        self.index += 1
        self.current_path = self.output_dir / f"{self.stem}.tar.part{self.index:03d}"
        self.current = self.current_path.open("xb")
        self.current_size = 0
        self.current_hash = hashlib.sha256()

    def _close_part(self) -> None:
        if self.current is None:
            return
        self.current.flush()
        os.fsync(self.current.fileno())
        self.current.close()
        self.parts.append(
            Part(self.current_path, self.current_size, self.current_hash.hexdigest())
        )
        self.current = None

    def write(self, data) -> int:
        view = memoryview(data)
        written = 0
        while written < len(view):
            if self.current is None:
                self._open_part()
            room = self.part_size - self.current_size
            chunk = view[written : written + room]
            count = self.current.write(chunk)
            if not count:
                raise OSError("short write while creating a backup part")
            self.current_hash.update(chunk[:count])
            self.current_size += count
            self.total += count
            written += count
            if self.current_size == self.part_size:
                self._close_part()
        return written

    def close(self) -> None:
        if not self.closed:
            self._close_part()
        super().close()


class HashingReader:
    def __init__(self, raw):
        self.raw = raw
        self.digest = hashlib.sha256()
        self.bytes_read = 0

    def read(self, size=-1):
        chunk = self.raw.read(size)
        if chunk:
            self.digest.update(chunk)
            self.bytes_read += len(chunk)
        return chunk


def release_part_size(value: str) -> int:
    size = int(value)
    if not 1 <= size <= 1024:
        raise argparse.ArgumentTypeError("part size must be between 1 and 1024 MiB")
    return size


def archive_stem(value: str) -> str:
    if not value or len(value) > 80 or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in value):
        raise argparse.ArgumentTypeError("archive stem must use 1-80 ASCII letters, digits, dots, underscores, or hyphens")
    return value


def device_label(value: str) -> str:
    if not value or len(value) > 80 or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise argparse.ArgumentTypeError("device label must contain 1-80 printable characters")
    return value


def windows_path_key(parts: tuple[str, ...]) -> tuple[str, ...]:
    key = []
    for component in parts:
        if component.endswith((" ", ".")):
            raise OSError(f"Windows-unsafe trailing space or dot: {component!r}")
        if any(ord(character) < 32 or character in WINDOWS_FORBIDDEN_CHARACTERS for character in component):
            raise OSError(f"Windows-unsafe filename component: {component!r}")
        stem = component.split(".", 1)[0].casefold()
        if stem in WINDOWS_RESERVED_STEMS:
            raise OSError(f"Windows reserved filename component: {component!r}")
        key.append(component.casefold())
    return tuple(key)


def is_link_like(path: pathlib.Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def checked_root(path: pathlib.Path) -> pathlib.Path:
    # resolve() follows links, so inspect the supplied path before resolving it.
    absolute = path.absolute()
    for component in (absolute, *absolute.parents):
        if is_link_like(component):
            raise SystemExit(f"Root path may not pass through a reparse point: {component}")
        if PROTECTED_PROFILE_NAME.fullmatch(component.name):
            raise SystemExit(f"Root path is inside a protected browser test profile: {component}")
    return absolute.resolve(strict=True)


def is_excluded(path: pathlib.Path, root: pathlib.Path, exclusions: tuple[pathlib.Path, ...]) -> bool:
    relative = path.relative_to(root)
    return any(relative == excluded or excluded in relative.parents for excluded in exclusions)


def iter_entries(root: pathlib.Path, exclusions: tuple[pathlib.Path, ...] = ()):
    stack = [root]
    while stack:
        current = stack.pop()
        if current != root and is_excluded(current, root, exclusions):
            continue
        if current != root and is_link_like(current):
            raise OSError(f"reparse point is not supported: {current}")
        if current != root and current.is_dir() and PROTECTED_PROFILE_NAME.fullmatch(current.name):
            raise OSError(f"protected browser test profile must be excluded: {current}")
        yield current
        if current.is_dir() and not current.is_symlink():
            children = sorted(current.iterdir(), key=lambda p: p.name.casefold(), reverse=True)
            stack.extend(children)


def tree_state(
    root: pathlib.Path, exclusions: tuple[pathlib.Path, ...] = ()
) -> dict[str, tuple[str, int, int]]:
    state = {}
    for path in iter_entries(root, exclusions):
        rel = path.relative_to(root.parent).as_posix()
        stat = path.lstat()
        if path.is_symlink():
            kind = "link"
        elif path.is_dir():
            kind = "dir"
        elif path.is_file():
            kind = "file"
        else:
            kind = "other"
        # Directory st_size is not a stable change signal on Windows. It can
        # change after enumeration even when the directory entries and mtime
        # are unchanged. File and link sizes remain meaningful.
        stable_size = stat.st_size if kind in {"file", "link"} else 0
        state[rel] = (kind, stable_size, stat.st_mtime_ns)
    return state


def file_hash(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=pathlib.Path)
    parser.add_argument("output", type=pathlib.Path)
    parser.add_argument("--part-size-mib", type=release_part_size, default=512)
    parser.add_argument("--archive-stem", type=archive_stem, default="project-full-backup")
    parser.add_argument("--source-device", type=device_label, required=True)
    parser.add_argument("--source-computer", type=device_label, required=True)
    parser.add_argument("--exclude-relative", action="append", default=[])
    args = parser.parse_args()

    source = checked_root(args.source)
    if not source.is_dir() or not source.name:
        raise SystemExit("Source must be a named directory, not a file or filesystem root")
    if is_link_like(source):
        raise SystemExit("Source root may not be a reparse point")
    if PROTECTED_PROFILE_NAME.fullmatch(source.name):
        raise SystemExit("A protected browser test profile may not be used as SourceRoot")
    try:
        source_key = windows_path_key((source.name,))
    except OSError as error:
        raise SystemExit(str(error)) from error
    output = args.output.resolve()
    if output == source or source in output.parents:
        raise SystemExit("Output must be outside the source tree")

    exclusions = []
    seen_exclusions = set()
    for raw in args.exclude_relative:
        relative = pathlib.PurePath(raw)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise SystemExit(f"Invalid exclusion outside source: {raw}")
        normalized = pathlib.Path(*relative.parts)
        key = normalized.as_posix().casefold()
        if key not in seen_exclusions:
            exclusions.append(normalized)
            seen_exclusions.add(key)
    exclusion_tuple = tuple(exclusions)

    before = tree_state(source, exclusion_tuple)
    output.mkdir(parents=True, exist_ok=False)
    writer = SplitWriter(output, args.archive_stem, args.part_size_mib * 1024 * 1024)
    records = []
    windows_entries: dict[tuple[str, ...], tuple[str, str]] = {}
    started = time.time()

    try:
        with tarfile.open(fileobj=writer, mode="w|", format=tarfile.PAX_FORMAT, dereference=False) as archive:
            for index, path in enumerate(iter_entries(source, exclusion_tuple), start=1):
                rel = path.relative_to(source.parent).as_posix()
                # Preserve bytes at every path without hard-link deduplication.
                # Keep dereference=False so symbolic links are never followed.
                archive.inodes.clear()
                info = archive.gettarinfo(str(path), arcname=rel)
                if info is None or not (info.isreg() or info.isdir()):
                    raise OSError(f"unsupported filesystem entry while archiving: {rel}")
                record = {
                    "path": rel,
                    "type": info.type.decode("ascii", errors="replace") if isinstance(info.type, bytes) else str(info.type),
                    "size": info.size,
                    "mtime": info.mtime,
                }
                windows_key = windows_path_key(tuple(pathlib.PurePosixPath(rel).parts))
                previous = windows_entries.get(windows_key)
                if previous is not None:
                    raise OSError(f"Windows path collision between {previous[0]!r} and {rel!r}")
                for parent_length in range(1, len(windows_key)):
                    parent = windows_entries.get(windows_key[:parent_length])
                    if parent is None or parent[1] != "5":
                        raise OSError(f"Missing or non-directory archive parent for {rel!r}")
                windows_entries[windows_key] = (rel, record["type"])
                if info.isreg():
                    with path.open("rb") as raw:
                        reader = HashingReader(raw)
                        archive.addfile(info, reader)
                        if reader.bytes_read != info.size:
                            raise OSError(f"short read while archiving {rel}")
                        record["sha256"] = reader.digest.hexdigest()
                else:
                    archive.addfile(info)
                records.append(record)
                if index % 500 == 0:
                    print(json.dumps({"entries": index, "archive_bytes": writer.total}), flush=True)
            if windows_entries.get(source_key) != (source.name, "5"):
                raise OSError("The source-root entry must be one directory")
    finally:
        writer.close()

    content_changes = []
    for record in records:
        if str(record.get("type")) not in {"0", "\x00"}:
            continue
        rel = record["path"]
        source_path = source.parent.joinpath(*pathlib.PurePosixPath(rel).parts)
        stat_before_hash = source_path.stat()
        current_hash = file_hash(source_path)
        stat_after_hash = source_path.stat()
        if (
            stat_before_hash.st_size != stat_after_hash.st_size
            or stat_before_hash.st_mtime_ns != stat_after_hash.st_mtime_ns
            or stat_after_hash.st_size != record["size"]
            or current_hash != record["sha256"]
        ):
            content_changes.append({
                "path": rel,
                "reason": "file content or metadata changed during archive verification",
            })

    after = tree_state(source, exclusion_tuple)
    changed_by_path = {item["path"]: item for item in content_changes}
    all_paths = sorted(set(before) | set(after))
    for rel in all_paths:
        if before.get(rel) != after.get(rel):
            changed_by_path[rel] = {"path": rel, "before": before.get(rel), "after": after.get(rel)}
    changed = [changed_by_path[path] for path in sorted(changed_by_path)]

    manifest = {
        "format": "split-tar-pax-v2",
        "archive_stem": args.archive_stem,
        "source_name": source.name,
        "source_device": args.source_device,
        "source_computer": args.source_computer,
        "excluded_source_paths": [path.as_posix() for path in exclusion_tuple],
        "created_local": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "part_size_bytes": writer.part_size,
        "archive_total_bytes": writer.total,
        "entry_count": len(records),
        "source_state_changed_during_backup": bool(changed),
        "changed_entries": changed,
        "parts": [
            {"name": part.path.name, "size": part.size, "sha256": part.sha256}
            for part in writer.parts
        ],
        "entries": records,
    }
    manifest_path = output / "BACKUP_MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "RESTORE.txt").write_text(
        "Keep every file in this directory together. On Windows, use the locally installed "
        "cross-device-project-sync scripts/restore-full-backup.ps1 with this directory as "
        "-BackupPath, a new empty -Destination, the known mandatory -ReleaseTag, and independently "
        "confirmed -ExpectedRepository and -ExpectedManifestSha256 values. The helper "
        "verifies all split parts, rejects unsafe archive paths, restores, and checks every "
        "restored file against BACKUP_MANIFEST.json. Do not execute downloaded helper scripts or "
        "extract unverified parts manually. The trusted upload helper replaces this preliminary "
        "file with instructions bound to the actual repository, Release tag, and manifest hash.\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "entries": len(records),
        "parts": len(writer.parts),
        "archive_bytes": writer.total,
        "changed_entries": len(changed),
        "elapsed_seconds": round(time.time() - started, 2),
        "output": str(output),
    }, ensure_ascii=False))
    return 2 if changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
