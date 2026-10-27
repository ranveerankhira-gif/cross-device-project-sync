#!/usr/bin/env python3
"""Create or verify a deterministic, hash-bound installer ZIP for this skill."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import zipfile


FORMAT = "cross-device-project-sync-skill-package-v1"
MANIFEST_NAME = "SKILL_PACKAGE_MANIFEST.json"


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def source_files(root: Path):
    result = []
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix().casefold()):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix.casefold() in {".pyc", ".pyo"}:
            continue
        if is_link(path):
            raise ValueError(f"skill package may not contain links: {path}")
        if path.is_file():
            result.append(path)
    return result


def validate_version(value: str) -> str:
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]{0,79}", value):
        raise ValueError("invalid skill package version")
    return value


def package_names(version: str) -> tuple[str, str]:
    zip_name = f"cross-device-project-sync-{validate_version(version)}.zip"
    return zip_name, zip_name + ".sha256"


def build(args):
    root = Path(args.skill_root).absolute()
    output = Path(args.output).absolute()
    skill_text = (root / "SKILL.md").read_text(encoding="utf-8")
    match = re.search(r"(?m)^\s*version:\s*['\"]?([^'\"\s]+)", skill_text)
    if not match:
        raise ValueError("SKILL.md metadata.version is required")
    version = validate_version(match.group(1))
    zip_name, sha256_name = package_names(version)
    output.mkdir(parents=True, exist_ok=False)
    files = source_files(root)
    records = []
    zip_path = output / zip_name
    with zipfile.ZipFile(zip_path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9, allowZip64=True) as archive:
        for path in files:
            relative = (PurePosixPath(root.name) / path.relative_to(root).as_posix()).as_posix()
            data = path.read_bytes()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
            records.append({"path": relative, "size": len(data), "sha256": digest(data)})
    manifest = {
        "format": FORMAT,
        "skill_name": root.name,
        "version": version,
        "payload_name": zip_name,
        "payload_size": zip_path.stat().st_size,
        "payload_sha256": digest(zip_path.read_bytes()),
        "files": records,
    }
    (output / MANIFEST_NAME).write_bytes(canonical(manifest))
    (output / sha256_name).write_text(
        f"{manifest['payload_sha256']} *{zip_name}\n", encoding="ascii", newline="\n"
    )
    return manifest


def verify(args):
    root = Path(args.package).absolute()
    manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    version = validate_version(str(manifest.get("version") or ""))
    zip_name, sha256_name = package_names(version)
    if manifest.get("format") != FORMAT or manifest.get("payload_name") != zip_name:
        raise ValueError("unsupported skill package manifest")
    zip_path = root / zip_name
    if zip_path.stat().st_size != manifest["payload_size"] or digest(zip_path.read_bytes()) != manifest["payload_sha256"]:
        raise ValueError("skill package ZIP identity mismatch")
    expected_checksum = f"{manifest['payload_sha256']} *{zip_name}\n"
    if (root / sha256_name).read_text(encoding="ascii") != expected_checksum:
        raise ValueError("skill package SHA-256 sidecar mismatch")
    expected = {item["path"]: item for item in manifest["files"]}
    with zipfile.ZipFile(zip_path) as archive:
        if len(archive.namelist()) != len(expected) or set(archive.namelist()) != set(expected):
            raise ValueError("skill package inventory mismatch")
        for name in archive.namelist():
            pure = PurePosixPath(name)
            if pure.is_absolute() or ".." in pure.parts or "\\" in name:
                raise ValueError(f"unsafe skill package path: {name}")
            if "__pycache__" in pure.parts or pure.suffix.casefold() in {".pyc", ".pyo"}:
                raise ValueError(f"skill package contains compiled Python cache: {name}")
            data = archive.read(name)
            if len(data) != expected[name]["size"] or digest(data) != expected[name]["sha256"]:
                raise ValueError(f"skill package member mismatch: {name}")
    return manifest


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("build")
    make.add_argument("--skill-root", required=True)
    make.add_argument("--output", required=True)
    make.set_defaults(handler=build)
    check = sub.add_parser("verify")
    check.add_argument("--package", required=True)
    check.set_defaults(handler=verify)
    args = parser.parse_args()
    try:
        result = args.handler(args)
        print(json.dumps({"version": result["version"], "payload_sha256": result["payload_sha256"], "file_count": len(result["files"]), "verified": True}, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
