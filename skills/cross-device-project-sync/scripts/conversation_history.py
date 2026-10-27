"""Verify portable conversation trees and their inherited history without executing them."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil


SUPPORTED_FORMATS = {
    "cross-device-conversation-handoff-v1",
    "cross-device-conversation-handoff-v2",
    "cross-device-conversation-handoff-v3",
}


def safe_relative(name):
    parts = name.split("/")
    if not name or "\\" in name or any(
        not part or part in {".", ".."} or part.endswith((".", " "))
        or re.search(r'[<>:"|?*\x00-\x1f]', part)
        for part in parts
    ):
        raise ValueError("Unsafe conversation history path: " + repr(name))
    return PurePosixPath(name)


def sha_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_tree(names, read, digest, prefix="", depth=0):
    """The callbacks support both restored directories and unopened ZIP members."""
    if depth > 64:
        raise ValueError("Conversation history exceeds 64 generations.")
    metadata_bytes = read(prefix + "BUNDLE_METADATA.json")
    metadata = json.loads(metadata_bytes)
    if metadata.get("format") not in SUPPORTED_FORMATS:
        raise ValueError("Conversation history format is unsupported.")
    if metadata.get("status") != "complete":
        raise ValueError("Inherited conversation is not finalized.")
    files = metadata.get("files")
    if not isinstance(files, list):
        raise ValueError("Conversation history file inventory is missing.")
    expected = {}
    folded_names = set()
    for entry in files:
        relative = entry["path"]
        safe_relative(relative)
        key = prefix + relative
        if key.casefold() in folded_names:
            raise ValueError("Duplicate conversation history member.")
        expected[key] = entry
        folded_names.add(key.casefold())
    actual = {n for n in names if n.startswith(prefix) and n != prefix + "BUNDLE_METADATA.json"}
    if actual != set(expected):
        raise ValueError("Conversation history inventory mismatch.")
    for name, entry in expected.items():
        if digest(name) != (entry["size"], entry["sha256"]):
            raise ValueError("Conversation history hash mismatch: " + name)
    required = {"DETAILED_HANDOFF.md", "FULL_TRANSCRIPT.jsonl", "CONVERSATION_INDEX.json"}
    if not {prefix + n for n in required}.issubset(actual):
        raise ValueError("Conversation history is missing required files.")
    parents = metadata.get("inherited_handoffs", [])
    seen = set()
    for parent in parents:
        identity = parent["metadata_sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", identity) or identity in seen:
            raise ValueError("Invalid or duplicate inherited history identity.")
        seen.add(identity)
        relative = "history/" + identity + "/"
        if parent["path"] != relative[:-1]:
            raise ValueError("Inherited history path does not match its identity.")
        child_prefix = prefix + relative
        child_bytes = read(child_prefix + "BUNDLE_METADATA.json")
        if hashlib.sha256(child_bytes).hexdigest() != identity:
            raise ValueError("Inherited metadata hash mismatch.")
        child = validate_tree(names, read, digest, child_prefix, depth + 1)
        if child.get("thread_id") != parent["thread_id"]:
            raise ValueError("Inherited task identity mismatch.")
    stored = {n[len(prefix):].split("/")[1] for n in actual if n.startswith(prefix + "history/")}
    if stored != seen:
        raise ValueError("Unindexed inherited conversation history.")
    return metadata


def tree_statistics(names, read, prefix="", depth=0):
    """Return aggregate counts for the current bundle and every stored ancestor."""
    if depth > 64:
        raise ValueError("Conversation history exceeds 64 generations.")
    metadata = json.loads(read(prefix + "BUNDLE_METADATA.json"))

    def count(field):
        value = metadata.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("Conversation history has an invalid " + field + ".")
        return value

    current_attachments = _current_attachment_records(names, read, prefix)
    if len(current_attachments) != count("attachment_count"):
        raise ValueError("Conversation attachment count does not match the transcript.")
    result = {
        "task_count": 1,
        "message_count": count("message_count"),
        "attachment_count": len(current_attachments),
    }
    for parent in metadata.get("inherited_handoffs", []):
        child = tree_statistics(
            names,
            read,
            prefix + "history/" + parent["metadata_sha256"] + "/",
            depth + 1,
        )
        for field in result:
            result[field] += child[field]
    return result


def _current_attachment_records(names, read, prefix=""):
    """Return and validate attachments referenced by one task transcript."""
    transcript_name = prefix + "FULL_TRANSCRIPT.jsonl"
    records = []
    seen = set()
    for raw_line in read(transcript_name).split(b"\n"):
        if not raw_line.strip():
            continue
        message = json.loads(raw_line)
        sequence = message.get("sequence")
        for part in message.get("content") or []:
            if part.get("type") != "image":
                continue
            relative = str(part.get("path") or "")
            safe_relative(relative)
            if not relative.startswith("attachments/"):
                raise ValueError("Conversation attachment is outside its attachment directory.")
            full_path = prefix + relative
            if full_path in seen or full_path not in names:
                raise ValueError("Conversation attachment is missing or referenced more than once: " + full_path)
            payload = read(full_path)
            if part.get("size") != len(payload) or part.get("sha256") != hashlib.sha256(payload).hexdigest():
                raise ValueError("Conversation attachment reference does not match its file: " + full_path)
            seen.add(full_path)
            records.append({
                "path": full_path,
                "transcript": transcript_name,
                "sequence": sequence,
                "mime_type": part.get("mime_type"),
            })
    stored = {name for name in names if name.startswith(prefix + "attachments/")}
    if seen != stored:
        raise ValueError("Conversation attachment inventory contains unreferenced or missing files.")
    return records


def tree_attachment_records(names, read, prefix="", depth=0):
    """Return validated attachment records for the current and inherited tasks."""
    if depth > 64:
        raise ValueError("Conversation history exceeds 64 generations.")
    metadata = json.loads(read(prefix + "BUNDLE_METADATA.json"))
    records = _current_attachment_records(names, read, prefix)
    for parent in metadata.get("inherited_handoffs", []):
        records.extend(tree_attachment_records(
            names,
            read,
            prefix + "history/" + parent["metadata_sha256"] + "/",
            depth + 1,
        ))
    return records


def directory_access(root):
    root = Path(root).absolute()
    for path in (root, *root.parents):
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("Conversation history may not use filesystem links.")
    names = set()
    for path in root.rglob("*"):
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("Conversation history may not contain links.")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            safe_relative(relative)
            names.add(relative)
    def read(name):
        return root.joinpath(*safe_relative(name).parts).read_bytes()
    def digest(name):
        path = root.joinpath(*safe_relative(name).parts)
        return path.stat().st_size, sha_file(path)
    return names, read, digest


def inherit_histories(destination, sources, scan_secrets):
    inherited = []
    for source in sources:
        source = Path(source).absolute()
        names, read, digest = directory_access(source)
        metadata = validate_tree(names, read, digest)
        statistics = tree_statistics(names, read)
        identity = hashlib.sha256(read("BUNDLE_METADATA.json")).hexdigest()
        if any(item["metadata_sha256"] == identity for item in inherited):
            continue
        relative = "history/" + identity
        target = destination / relative
        target.mkdir(parents=True)
        for name in sorted(names):
            output = target.joinpath(*safe_relative(name).parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source.joinpath(*safe_relative(name).parts), output)
        copied_names, copied_read, copied_digest = directory_access(target)
        validate_tree(copied_names, copied_read, copied_digest)
        if hashlib.sha256(copied_read("BUNDLE_METADATA.json")).hexdigest() != identity:
            raise ValueError("Inherited history changed during capture.")
        for name in copied_names:
            if name.endswith(("FULL_TRANSCRIPT.jsonl", "DETAILED_HANDOFF.md")):
                with (target / name).open(encoding="utf-8") as stream:
                    for line in stream:
                        # Decode JSON escapes before scanning dialogue credentials.
                        value = json.loads(line) if name.endswith(".jsonl") else line
                        if scan_secrets([json.dumps(value, ensure_ascii=False)]):
                            raise ValueError("Potential credentials in inherited conversation.")
        inherited.append({"path": relative, "metadata_sha256": identity,
                          "thread_id": metadata["thread_id"],
                          "cutoff_timestamp": metadata.get("cutoff_timestamp"),
                          "lineage_task_count": statistics["task_count"],
                          "lineage_message_count": statistics["message_count"],
                          "lineage_attachment_count": statistics["attachment_count"]})
    return inherited
