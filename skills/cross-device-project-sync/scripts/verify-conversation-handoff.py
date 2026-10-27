#!/usr/bin/env python3
"""Verify and optionally extract a cross-device conversation handoff bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
import zipfile
import owned_temp

from conversation_history import tree_statistics, validate_tree


FORMAT_V1 = "cross-device-conversation-handoff-v1"
FORMAT_V2 = "cross-device-conversation-handoff-v2"
FORMAT_V3 = "cross-device-conversation-handoff-v3"
SUPPORTED_FORMATS = {FORMAT_V1, FORMAT_V2, FORMAT_V3}
MANIFEST_NAME = "CONVERSATION_HANDOFF_MANIFEST.json"
ZIP_NAME = "CONVERSATION_HANDOFF.zip"
REQUIRED_NAMES = {
    "BUNDLE_METADATA.json",
    "CONVERSATION_INDEX.json",
    "DETAILED_HANDOFF.md",
    "FULL_TRANSCRIPT.jsonl",
}
V2_REQUIRED_NAMES = REQUIRED_NAMES | {"RECEIVER_START.md"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_small_json(archive: zipfile.ZipFile, name: str, limit: int = 256 * 1024 * 1024):
    info = archive.getinfo(name)
    if info.file_size > limit:
        raise ValueError(f"Conversation metadata file is unexpectedly large: {name}")
    with archive.open(info, "r") as handle:
        return json.load(handle)


def zip_member_size_and_sha256(archive: zipfile.ZipFile, name: str) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with archive.open(name, "r") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def safe_name(name: str) -> PurePosixPath:
    if "\\" in name or "\x00" in name:
        raise ValueError(f"Unsafe ZIP member name: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Unsafe ZIP member name: {name!r}")
    return path


def verify_bundle(root: Path, expected_project_manifest: str | None, max_uncompressed_bytes: int) -> tuple[dict, zipfile.ZipFile]:
    manifest_path = root / MANIFEST_NAME
    zip_path = root / ZIP_NAME
    if not manifest_path.is_file() or not zip_path.is_file():
        raise ValueError("Both conversation handoff assets are required.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bundle_format = manifest.get("format")
    if bundle_format not in SUPPORTED_FORMATS or manifest.get("zip_name") != ZIP_NAME:
        raise ValueError("Conversation handoff manifest format is invalid.")
    project_hash = str(manifest.get("project_manifest_sha256") or "").lower()
    if len(project_hash) != 64 or any(char not in "0123456789abcdef" for char in project_hash):
        raise ValueError("Conversation handoff has an invalid project manifest binding.")
    if expected_project_manifest and project_hash != expected_project_manifest.lower():
        raise ValueError("Conversation handoff is bound to a different project manifest.")
    if zip_path.stat().st_size != int(manifest.get("zip_size") or -1) or sha256_file(zip_path) != manifest.get("zip_sha256"):
        raise ValueError("Conversation handoff ZIP size or SHA-256 does not match its manifest.")

    archive = zipfile.ZipFile(zip_path, "r")
    infos = archive.infolist()
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        archive.close()
        raise ValueError("Conversation handoff ZIP contains duplicate member names.")
    total = 0
    for info in infos:
        safe_name(info.filename)
        if info.is_dir():
            continue
        mode = (info.external_attr >> 16) & 0xF000
        if mode == 0xA000:
            archive.close()
            raise ValueError("Conversation handoff ZIP may not contain symbolic links.")
        total += info.file_size
        if total > max_uncompressed_bytes:
            archive.close()
            raise ValueError("Conversation handoff exceeds the configured extraction size limit.")
    if total != int(manifest.get("uncompressed_total_bytes") or -1):
        archive.close()
        raise ValueError("Conversation handoff uncompressed size does not match its manifest.")
    required_names = V2_REQUIRED_NAMES if bundle_format in {FORMAT_V2, FORMAT_V3} else REQUIRED_NAMES
    if not required_names.issubset(set(names)):
        archive.close()
        raise ValueError("Conversation handoff ZIP is missing required files.")

    metadata = read_small_json(archive, "BUNDLE_METADATA.json")
    index = read_small_json(archive, "CONVERSATION_INDEX.json")
    if metadata.get("format") != bundle_format or index.get("format") != bundle_format or metadata.get("status") != "complete":
        archive.close()
        raise ValueError("Conversation bundle metadata is incomplete or invalid.")
    if metadata.get("project_manifest_sha256") != project_hash or index.get("thread_id") != metadata.get("thread_id"):
        archive.close()
        raise ValueError("Conversation bundle internal identity is inconsistent.")
    for field in ("thread_id", "thread_title", "project_source_device", "conversation_source_device", "message_count", "attachment_count", "cutoff_timestamp"):
        if manifest.get(field) != metadata.get(field):
            archive.close()
            raise ValueError(f"Conversation handoff manifest does not match bundle metadata: {field}")
    transcript_count = 0
    with archive.open("FULL_TRANSCRIPT.jsonl", "r") as transcript:
        for line_number, line in enumerate(transcript, 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                archive.close()
                raise ValueError(f"Conversation transcript is invalid at line {line_number}.") from exc
            if record.get("sequence") != line_number or record.get("role") not in {"user", "assistant"}:
                archive.close()
                raise ValueError("Conversation transcript sequence or role is invalid.")
            transcript_count += 1
    if transcript_count != int(metadata.get("message_count") or -1) or transcript_count != int(index.get("message_count") or -1):
        archive.close()
        raise ValueError("Conversation transcript count does not match metadata.")

    expected_files = {item["path"]: item for item in metadata.get("files") or []}
    actual_files = {name for name in names if name != "BUNDLE_METADATA.json" and not name.endswith("/")}
    if set(expected_files) != actual_files:
        archive.close()
        raise ValueError("Conversation bundle file inventory does not match metadata.")
    for name, expected in expected_files.items():
        size, digest = zip_member_size_and_sha256(archive, name)
        if size != int(expected.get("size") or -1) or digest != expected.get("sha256"):
            archive.close()
            raise ValueError(f"Conversation bundle member failed verification: {name}")
    try:
        validate_tree(actual_files | {"BUNDLE_METADATA.json"}, archive.read,
                      lambda name: zip_member_size_and_sha256(archive, name))
        statistics = tree_statistics(actual_files | {"BUNDLE_METADATA.json"}, archive.read)
    except Exception:
        archive.close()
        raise
    if bundle_format == FORMAT_V3:
        expected_statistics = {
            "task_count": metadata.get("lineage_task_count"),
            "message_count": metadata.get("lineage_message_count"),
            "attachment_count": metadata.get("lineage_attachment_count"),
        }
        if statistics != expected_statistics:
            archive.close()
            raise ValueError("Conversation lineage counts do not match the verified history tree.")
        for field in ("lineage_task_count", "lineage_message_count", "lineage_attachment_count"):
            if manifest.get(field) != metadata.get(field):
                archive.close()
                raise ValueError("Conversation manifest lineage counts are inconsistent.")
    return manifest, archive


def extract_verified(archive: zipfile.ZipFile, destination: Path) -> None:
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise ValueError("Conversation handoff destination must be new or empty.")
    else:
        destination.mkdir(parents=True)
    temp = owned_temp.make_temp(destination.parent, "handoff-extract-")
    try:
        for info in archive.infolist():
            relative = safe_name(info.filename)
            target = temp.joinpath(*relative.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
        for child in temp.iterdir():
            child.replace(destination / child.name)
        owned_temp.release(temp)
        temp.rmdir()
    except Exception:
        if temp.exists():
            owned_temp.remove(temp, destination.parent)
        else:
            owned_temp.release(temp)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup_path")
    parser.add_argument("--expected-project-manifest-sha256")
    parser.add_argument("--extract-to")
    parser.add_argument("--max-uncompressed-bytes", type=int, default=8 * 1024 * 1024 * 1024)
    args = parser.parse_args()
    try:
        if args.max_uncompressed_bytes < 1:
            raise ValueError("Extraction size limit must be positive.")
        root = Path(args.backup_path).resolve()
        manifest, archive = verify_bundle(root, args.expected_project_manifest_sha256, args.max_uncompressed_bytes)
        try:
            if args.extract_to:
                extract_verified(archive, Path(args.extract_to).resolve())
        finally:
            archive.close()
        print(json.dumps({
            "verified": True,
            "thread_id": manifest.get("thread_id"),
            "thread_title": manifest.get("thread_title"),
            "message_count": manifest.get("message_count"),
            "attachment_count": manifest.get("attachment_count"),
            "lineage_task_count": manifest.get("lineage_task_count", 1),
            "lineage_message_count": manifest.get("lineage_message_count", manifest.get("message_count")),
            "lineage_attachment_count": manifest.get("lineage_attachment_count", manifest.get("attachment_count")),
            "cutoff_timestamp": manifest.get("cutoff_timestamp"),
            "extracted_to": str(Path(args.extract_to).resolve()) if args.extract_to else None,
        }, ensure_ascii=False))
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
