#!/usr/bin/env python3
"""Compare a source directory and its backup snapshot by inventory and SHA-256."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import stat


PROTECTED_PROFILE_NAME = re.compile(
    r"^(?:chrome|chromium|edge|msedge|browser|playwright).*test[-_ ]?profile(?:[-_ ].*)?$",
    re.IGNORECASE,
)


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


def normalize_exclusions(values: list[str]) -> tuple[pathlib.Path, ...]:
    normalized = []
    seen = set()
    for raw in values:
        relative = pathlib.PurePath(raw)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError(f"invalid exclusion outside source: {raw}")
        value = pathlib.Path(*relative.parts)
        key = value.as_posix().casefold()
        if key not in seen:
            normalized.append(value)
            seen.add(key)
    return tuple(normalized)


def is_excluded(relative: pathlib.Path, exclusions: tuple[pathlib.Path, ...]) -> bool:
    return any(relative == excluded or excluded in relative.parents for excluded in exclusions)


def file_hash_stable(path: pathlib.Path) -> tuple[int, int, str]:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise OSError(f"file changed while hashing: {path}")
    return after.st_size, after.st_mtime_ns, digest.hexdigest()


def inventory(
    root: pathlib.Path, exclusions: tuple[pathlib.Path, ...] = ()
) -> dict[str, tuple[str, int, int, str]]:
    result = {}
    stack = [root]
    while stack:
        current = stack.pop()
        relative = current.relative_to(root)
        if relative.parts and is_excluded(relative, exclusions):
            continue
        if is_link_like(current):
            raise OSError(f"reparse point is not supported: {current}")
        if relative.parts and current.is_dir() and PROTECTED_PROFILE_NAME.fullmatch(current.name):
            raise OSError(f"protected browser test profile must be excluded: {current}")
        key = relative.as_posix() if relative.parts else "."
        if current.is_dir():
            stat = current.stat()
            result[key] = ("dir", 0, stat.st_mtime_ns, "")
            stack.extend(sorted(current.iterdir(), key=lambda item: item.name.casefold(), reverse=True))
        elif current.is_file():
            size, mtime_ns, digest = file_hash_stable(current)
            result[key] = ("file", size, mtime_ns, digest)
        else:
            raise OSError(f"unsupported filesystem entry: {current}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=pathlib.Path)
    parser.add_argument("snapshot", type=pathlib.Path)
    parser.add_argument("--exclude-relative", action="append", default=[])
    args = parser.parse_args()

    source = checked_root(args.source)
    snapshot = checked_root(args.snapshot)
    if not source.is_dir() or not snapshot.is_dir():
        raise SystemExit("Source and snapshot must both be directories")
    try:
        exclusions = normalize_exclusions(args.exclude_relative)
        source_inventory_before = inventory(source, exclusions)
        snapshot_inventory = inventory(snapshot)
        source_inventory_after = inventory(source, exclusions)
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from error

    changed_source_names = sorted(
        name
        for name in set(source_inventory_before) | set(source_inventory_after)
        if source_inventory_before.get(name) != source_inventory_after.get(name)
    )
    if changed_source_names:
        raise SystemExit(
            f"Source changed while mirror verification was running at {len(changed_source_names)} path(s): "
            + json.dumps(changed_source_names[:20], ensure_ascii=False)
        )

    names = sorted(set(source_inventory_after) | set(snapshot_inventory))
    mismatches = [name for name in names if source_inventory_after.get(name) != snapshot_inventory.get(name)]
    if mismatches:
        preview = mismatches[:20]
        raise SystemExit(
            f"Source/snapshot mismatch at {len(mismatches)} path(s): "
            + json.dumps(preview, ensure_ascii=False)
        )
    print(json.dumps({"mirror_verified": True, "entries": len(source_inventory_after)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
