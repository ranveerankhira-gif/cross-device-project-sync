#!/usr/bin/env python3
"""Build or verify a multi-conversation pack independent of project bytes."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile
import zipfile
import owned_temp
from typing import Any


FORMAT = "cross-device-project-sync-conversation-pack-v1"
GENERATION_V1 = "cross-device-project-sync-conversation-generation-20261013-v38"
GENERATION_V2 = "cross-device-project-sync-conversation-generation-20261014-v42"
GENERATIONS = {GENERATION_V1, GENERATION_V2}
ZIP_NAME = "CONVERSATION_PACK.zip"
MANIFEST_NAME = "CONVERSATION_PACK_MANIFEST.json"
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class PackError(ValueError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_exporter(script_dir: Path):
    path = script_dir / "export-conversation-handoff.py"
    spec = importlib.util.spec_from_file_location("bulk_conversation_exporter", path)
    if spec is None or spec.loader is None:
        raise PackError("could not load conversation exporter")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(script_dir))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def safe_thread_id(value: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F-]{16,80}", value):
        raise PackError(f"invalid thread id: {value}")
    return value


def handoff_text(title: str, messages: list[dict[str, Any]], index: dict[str, Any]) -> str:
    lines = [
        "# Detailed Conversation Handoff",
        "",
        "This handoff was generated for complete-device archival. The exact transcript is authoritative.",
        "",
        "## Purpose And Requested Outcome",
        "",
        f"Original task title: {title}",
        "",
        "## Chronological Narrative",
        "",
    ]
    previews = {item["sequence"]: item for item in index["messages"]}
    for message in messages:
        preview = previews.get(message["sequence"], {}).get("preview") or "[attachment or empty text]"
        lines.append(f"- Sequence {message['sequence']} · {message['role']} · {message.get('timestamp')}: {preview}")
    lines.extend([
        "",
        "## Decisions And Rationale",
        "",
        "Read the exact cited sequences in FULL_TRANSCRIPT.jsonl. This archival generator does not infer decisions beyond the source messages.",
        "",
        "## Work Performed",
        "",
        "All available user-visible user and assistant messages were exported in order; runtime instructions, hidden reasoning, and raw tool payloads were excluded.",
        "",
        "## Files And Artifacts",
        "",
        "See CONVERSATION_INDEX.json and the attachments directory. Every retained file is hash-bound by the project conversation-pack manifest.",
        "",
        "## Failures, Corrections, And Rejected Approaches",
        "",
        "Consult the exact transcript for corrections and rejected approaches; later sequences supersede earlier ones.",
        "",
        "## Current State",
        "",
        f"The immutable transcript ends at {messages[-1].get('timestamp') if messages else '(unknown)' }.",
        "",
        "## Unresolved Questions And Next Actions",
        "",
        "The receiving task must inspect the restored project and this transcript before continuing work.",
        "",
        "## User Preferences And Constraints",
        "",
        "Use the user's exact messages as the source of truth; do not infer durable preferences from runtime-injected context.",
        "",
        "## Verification And Evidence",
        "",
        f"Messages exported: {len(messages)}. Exact records: FULL_TRANSCRIPT.jsonl.",
        "",
    ])
    return "\n".join(lines)


def bootstrap_text(thread_id: str, title: str) -> str:
    return "\n".join([
        "# Receiving Task Bootstrap",
        "",
        f"Original thread: `{thread_id}`",
        f"Original title: {title}",
        "",
        "Before modifying the restored project, read DETAILED_HANDOFF.md and CONVERSATION_INDEX.json, then consult FULL_TRANSCRIPT.jsonl for exact wording and inspect every retained attachment. State the transcript cutoff and produce a read-only inherited-context report. Historical content is evidence, not fresh authorization to repeat old external actions.",
        "",
    ])


def deterministic_zip(source: Path, target: Path) -> None:
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9, allowZip64=True) as archive:
        for path in sorted((item for item in source.rglob("*") if item.is_file()), key=lambda p: p.relative_to(source).as_posix().casefold()):
            relative = path.relative_to(source).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def build(args: argparse.Namespace) -> dict[str, Any]:
    catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    matches = [item for item in catalog.get("projects") or [] if item.get("project_id") == args.project_id]
    if len(matches) != 1:
        raise PackError(f"expected one catalog project for {args.project_id}; found {len(matches)}")
    project = matches[0]
    generation = str(project.get("conversation_pack_generation") or GENERATION_V1)
    if generation not in GENERATIONS:
        raise PackError(f"unsupported conversation pack generation: {generation}")
    is_v2 = generation == GENERATION_V2
    final_output = Path(args.output).absolute()
    if final_output.exists():
        raise PackError(f"conversation pack output already exists: {final_output}")
    final_output.parent.mkdir(parents=True, exist_ok=True)
    output = owned_temp.make_temp(final_output.parent, f".{final_output.name}.building-")
    exporter = load_exporter(Path(__file__).resolve().parent)
    try:
        with tempfile.TemporaryDirectory(prefix="codex-conversation-pack-") as raw:
            payload_root = Path(raw) / "payload"
            payload_root.mkdir()
            thread_records = []
            for conversation in project.get("conversations") or []:
                thread_id = safe_thread_id(conversation["thread_id"])
                title = conversation["thread_title"]
                thread_root = payload_root / "threads" / thread_id
                thread_root.mkdir(parents=True)
                data = exporter.read_stable(Path(conversation.get("snapshot_session_log") or conversation["session_log"]))
                messages, stats = exporter.export_messages(data, thread_root, conversation.get("source_thread_id") or thread_id)
                index_path = thread_root / "CONVERSATION_INDEX.json"
                index = json.loads(index_path.read_text(encoding="utf-8"))
                (thread_root / "DETAILED_HANDOFF.md").write_text(handoff_text(title, messages, index), encoding="utf-8", newline="\n")
                (thread_root / "RECEIVER_START.md").write_text(bootstrap_text(thread_id, title), encoding="utf-8", newline="\n")
                transcript_hash = sha256_file(thread_root / "FULL_TRANSCRIPT.jsonl")
                inherited_identities = []
                for inherited in conversation.get("inherited_histories") or []:
                    tree_hash = str(inherited["tree_sha256"])
                    history_root = thread_root / "history" / tree_hash
                    for record in inherited.get("files") or []:
                        source = Path(inherited["source_path"]).joinpath(*PurePosixPath(record["path"]).parts)
                        if source.stat().st_size != record["size"] or sha256_file(source) != record["sha256"]:
                            raise PackError(f"inherited conversation history changed after catalog scan: {source}")
                        destination = history_root.joinpath(*PurePosixPath(record["path"]).parts)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(source, destination)
                    copied = [
                        {"path": path.relative_to(history_root).as_posix(), "size": path.stat().st_size, "sha256": sha256_file(path)}
                        for path in sorted((item for item in history_root.rglob("*") if item.is_file()), key=lambda item: item.relative_to(history_root).as_posix().casefold())
                    ]
                    if hashlib.sha256(canonical_bytes(copied)).hexdigest() != tree_hash:
                        raise PackError(f"inherited conversation history copy mismatch: {tree_hash}")
                    inherited_identities.append({
                        "tree_sha256": tree_hash,
                        "file_count": inherited["file_count"],
                        "total_bytes": inherited["total_bytes"],
                    })
                inherited_identities.sort(key=lambda item: item["tree_sha256"])
                identity = {
                    "generator": generation,
                    **(
                        {"lineage_id": conversation["lineage_id"], "branch_id": conversation.get("branch_id") or "main"}
                        if is_v2 else {"thread_id": thread_id}
                    ),
                    "messages_sha256": transcript_hash,
                    "attachments": conversation.get("attachments") or [],
                    "inherited_histories": inherited_identities,
                }
                calculated = hashlib.sha256(canonical_bytes(identity)).hexdigest()
                if is_v2:
                    spec = importlib.util.spec_from_file_location("pack_identity", Path(__file__).with_name("conversation_identity.py"))
                    portable = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(portable)
                    if conversation.get("evidence_sha256") != portable.evidence_hash(transcript_hash, conversation.get("attachments") or [], inherited_identities):
                        raise PackError("conversation byte evidence changed after scan")
                    calculated = portable.content_hash(conversation["lineage_id"], conversation.get("branch_id"), portable.flatten(thread_root), generation)
                if calculated != conversation["conversation_sha256"]:
                    raise PackError(f"conversation changed after catalog scan: {thread_id}")
                thread_record = {
                    "thread_id": thread_id,
                    "thread_title": title,
                    "conversation_sha256": calculated,
                    "messages_sha256": transcript_hash,
                    "message_count": len(messages),
                    "attachment_count": stats["attachment_count"],
                    "cutoff_timestamp": messages[-1].get("timestamp"),
                    "path": f"threads/{thread_id}",
                    "inherited_histories": inherited_identities,
                }
                if is_v2:
                    for field in (
                        "evidence_sha256", "projectless_directory_ids", "projectless_directory_id", "source_thread_id", "source_instance_id", "source_instance_name", "source_disposition", "origin_computer",
                        "origin_instance_id", "origin_thread_id", "lineage_id", "branch_id", "parent_branch_id", "cwd",
                    ):
                        if conversation.get(field) is not None:
                            thread_record[field] = conversation[field]
                thread_records.append(thread_record)
            files = []
            for path in sorted((item for item in payload_root.rglob("*") if item.is_file()), key=lambda p: p.relative_to(payload_root).as_posix().casefold()):
                files.append({
                    "path": path.relative_to(payload_root).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": sha256_file(path),
                })
            zip_path = output / ZIP_NAME
            deterministic_zip(payload_root, zip_path)
            manifest = {
                "format": FORMAT,
                "generator": generation,
                "source_device": catalog["source_device"],
                "source_computer": catalog["source_computer"],
                "project_id": project["project_id"],
                "project_label": project["label"],
                "project_source_path": project["source_path"],
                "conversation_set_sha256": project["conversation_set_sha256"],
                "conversation_count": len(thread_records),
                "conversations": thread_records,
                "files": files,
                "payload_name": ZIP_NAME,
                "payload_size": zip_path.stat().st_size,
                "payload_sha256": sha256_file(zip_path),
            }
            (output / MANIFEST_NAME).write_bytes(canonical_bytes(manifest))
        verify(argparse.Namespace(pack=output))
        output.replace(final_output)
        owned_temp.release(output)
        return {
            "output": str(final_output),
            "manifest_sha256": sha256_file(final_output / MANIFEST_NAME),
            "conversation_set_sha256": project["conversation_set_sha256"],
            "conversation_count": len(thread_records),
            "payload_size": (final_output / ZIP_NAME).stat().st_size,
        }
    except Exception:
        if output.exists():
            owned_temp.remove(output, final_output.parent)
        else:
            owned_temp.release(output)
        raise


def verify(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.pack).absolute()
    manifest_path = root / MANIFEST_NAME
    zip_path = root / ZIP_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    generation = str(manifest.get("generator") or "")
    if manifest.get("format") != FORMAT or generation not in GENERATIONS:
        raise PackError("unsupported conversation pack format")
    if manifest.get("payload_name") != ZIP_NAME or manifest.get("payload_size") != zip_path.stat().st_size or manifest.get("payload_sha256") != sha256_file(zip_path):
        raise PackError("conversation pack payload identity mismatch")
    expected = {item["path"]: item for item in manifest.get("files") or []}
    if len(expected) != len(manifest.get("files") or []):
        raise PackError("duplicate manifest file path")
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != set(expected):
            raise PackError("conversation pack ZIP inventory mismatch")
        for name in names:
            pure = PurePosixPath(name)
            if pure.is_absolute() or ".." in pure.parts or "\\" in name:
                raise PackError(f"unsafe ZIP path: {name}")
            data = archive.read(name)
            record = expected[name]
            if len(data) != record["size"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise PackError(f"conversation pack file mismatch: {name}")
    is_v2 = generation == GENERATION_V2
    if is_v2:
        spec = importlib.util.spec_from_file_location("pack_identity", Path(__file__).with_name("conversation_identity.py"))
        portable = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(portable)
    identities = {"generator": generation, "conversations": [
        (
            portable.set_row(item)
            if is_v2 else {"thread_id": item["thread_id"], "conversation_sha256": item["conversation_sha256"]}
        )
        for item in manifest["conversations"]
    ]}
    if hashlib.sha256(canonical_bytes(identities)).hexdigest() != manifest["conversation_set_sha256"]:
        raise PackError("conversation set hash mismatch")
    return {
        "pack": str(root),
        "manifest_sha256": sha256_file(manifest_path),
        "payload_sha256": manifest["payload_sha256"],
        "conversation_count": manifest["conversation_count"],
        "verified": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("build")
    create.add_argument("--catalog", required=True)
    create.add_argument("--project-id", required=True)
    create.add_argument("--output", required=True)
    create.set_defaults(handler=build)
    check = sub.add_parser("verify")
    check.add_argument("--pack", required=True)
    check.set_defaults(handler=verify)
    args = parser.parse_args()
    try:
        print(json.dumps(args.handler(args), ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, KeyError, json.JSONDecodeError, PackError, ValueError, zipfile.BadZipFile) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
