#!/usr/bin/env python3
"""Discover protected browser test-profile roots without descending into them."""

from __future__ import annotations

import argparse
import json
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any


PROFILE_RE = re.compile(r"^(?:chrome|chromium|edge|msedge|browser|playwright).*test[-_ ]?profile(?:[-_ ].*)?$", re.IGNORECASE)


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def normalized(value: str) -> str:
    return PurePosixPath(value.replace("\\", "/")).as_posix()


def excluded(relative: str, values: set[str]) -> bool:
    folded = relative.casefold()
    return any(folded == item.casefold() or folded.startswith(item.casefold() + "/") for item in values)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--discovery", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    source = Path(args.discovery).resolve(strict=True)
    output = Path(args.output).absolute()
    if output.exists():
        raise ValueError("output discovery already exists")
    discovery = json.loads(source.read_text(encoding="utf-8"))
    found: list[dict[str, str]] = []
    for project in discovery.get("projects") or []:
        if project.get("source_path") is None:
            continue
        root = Path(str(project["source_path"])).absolute()
        exclusions = {normalized(str(item)) for item in project.get("exclude_relative_paths") or []}
        records = list(project.get("exclusion_records") or [])
        stack: list[tuple[Path, str]] = [(root, "")]
        while stack:
            current, relative = stack.pop()
            if relative and excluded(relative, exclusions):
                continue
            with os.scandir(current) as entries:
                for entry in entries:
                    child_relative = normalized(f"{relative}/{entry.name}" if relative else entry.name)
                    if excluded(child_relative, exclusions):
                        continue
                    info = entry.stat(follow_symlinks=False)
                    is_reparse = stat.S_ISLNK(info.st_mode) or bool(
                        getattr(info, "st_file_attributes", 0)
                        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                    )
                    if is_reparse:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if PROFILE_RE.fullmatch(entry.name):
                            exclusions.add(child_relative)
                            record = {
                                "path": child_relative,
                                "reason": "protected browser test profile; authentication state is not opened or uploaded",
                            }
                            records.append(record)
                            found.append({"project_id": str(project.get("project_id") or ""), **record})
                        else:
                            stack.append((Path(entry.path), child_relative))
        project["exclude_relative_paths"] = sorted(exclusions, key=str.casefold)
        unique: dict[str, dict[str, str]] = {}
        for record in records:
            unique[str(record.get("path") or "").casefold()] = record
        project["exclusion_records"] = [unique[key] for key in sorted(unique)]
    output.parent.mkdir(parents=True, exist_ok=True)
    if "discovery_sha256" in discovery:
        discovery.pop("discovery_sha256")
        discovery["discovery_sha256"] = hashlib.sha256(canonical_bytes(discovery)).hexdigest()
    output.write_bytes(canonical_bytes(discovery))
    print(json.dumps({"output": str(output), "protected_browser_profile_count": len(found), "profiles": found}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
