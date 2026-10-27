#!/usr/bin/env python3
"""Build and compare content-addressed project/conversation inventories.

This helper deliberately separates immutable project snapshots from conversation
packs.  A new device index may therefore reuse an older project Release while
publishing only a changed conversation pack (or the reverse).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from typing import Any


CATALOG_FORMAT_V1 = "cross-device-project-sync-content-catalog-v1"
CATALOG_FORMAT_V2 = "cross-device-project-sync-content-catalog-v2"
CATALOG_FORMATS = {CATALOG_FORMAT_V1, CATALOG_FORMAT_V2}
CATALOG_FORMAT = CATALOG_FORMAT_V1
PLAN_FORMAT_V1 = "cross-device-project-sync-incremental-plan-v1"
PLAN_FORMAT_V2 = "cross-device-project-sync-incremental-plan-v2"
PLAN_FORMATS = {PLAN_FORMAT_V1, PLAN_FORMAT_V2}
PLAN_FORMAT = PLAN_FORMAT_V1
INDEX_FORMAT_V1 = "cross-device-project-sync-incremental-device-index-v1"
INDEX_FORMAT_V2 = "cross-device-project-sync-incremental-device-index-v2"
INDEX_FORMATS = {INDEX_FORMAT_V1, INDEX_FORMAT_V2}
INDEX_FORMAT = INDEX_FORMAT_V1
CANONICAL_REPOSITORY = os.environ.get("CROSS_DEVICE_SYNC_REPOSITORY", "example-owner/example-private-backups").strip()
REQUIRED_SKILL_VERSION = "20261019-v48"
SCAN_CHECKPOINT_FORMAT = "cross-device-project-sync-scan-checkpoint-v3"
CONVERSATION_PACK_GENERATION = "cross-device-project-sync-conversation-generation-20261013-v38"
CONVERSATION_PACK_GENERATION_V2 = "cross-device-project-sync-conversation-generation-20261014-v42"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SyncError(ValueError):
    pass


class NonTransferableConversation(ValueError):
    """A session contains no genuine user-visible request after filtering."""

    pass


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_reparse(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def normalize_relative(value: str) -> str:
    pure = PurePosixPath(value.replace("\\", "/"))
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise SyncError(f"invalid relative path: {value}")
    return pure.as_posix()


def exclusion_match(relative: str, exclusions: set[str]) -> bool:
    return any(relative == item or relative.startswith(item + "/") for item in exclusions)


def project_inventory(root: Path, exclusions: list[str], status_path: Path | None = None) -> dict[str, Any]:
    root = root.absolute()
    if not root.is_dir() or is_reparse(root):
        raise SyncError(f"project root is missing, not a directory, or a reparse point: {root}")
    excluded = {normalize_relative(item) for item in exclusions}
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    stack = [(root, "")]
    seen_casefold: dict[str, str] = {}
    while stack:
        current, relative = stack.pop()
        if status_path is not None:
            status_path.parent.mkdir(parents=True, exist_ok=True)
            status_path.write_text(str(current), encoding="utf-8")
        if relative and exclusion_match(relative, excluded):
            continue
        if relative and is_reparse(current):
            raise SyncError(f"unexcluded reparse point: {current}")
        info = current.stat(follow_symlinks=False)
        key = relative.casefold()
        prior = seen_casefold.get(key)
        if prior is not None and prior != relative:
            raise SyncError(f"Windows path collision: {prior!r} and {relative!r}")
        seen_casefold[key] = relative
        mtime = round(info.st_mtime, 6)
        if current.is_dir():
            entries.append({"path": relative or ".", "type": "dir", "mtime": mtime})
            children = sorted(current.iterdir(), key=lambda item: item.name.casefold(), reverse=True)
            for child in children:
                child_rel = f"{relative}/{child.name}" if relative else child.name
                stack.append((child, PurePosixPath(child_rel).as_posix()))
        elif current.is_file():
            digest = sha256_file(current)
            entries.append({
                "path": relative,
                "type": "file",
                "size": info.st_size,
                "mtime": mtime,
                "sha256": digest,
            })
            total_bytes += info.st_size
        else:
            raise SyncError(f"unsupported filesystem entry: {current}")
    entries.sort(key=lambda item: item["path"].casefold())
    identity = {"excluded_relative_paths": sorted(excluded, key=str.casefold), "entries": entries}
    return {
        "tree_sha256": hashlib.sha256(canonical_bytes(identity)).hexdigest(),
        "entry_count": len(entries),
        "file_count": sum(item["type"] == "file" for item in entries),
        "total_bytes": total_bytes,
        "excluded_relative_paths": identity["excluded_relative_paths"],
        "entries": entries,
    }


def load_exporter(script_dir: Path):
    path = script_dir / "export-conversation-handoff.py"
    spec = importlib.util.spec_from_file_location("cross_device_conversation_exporter", path)
    if spec is None or spec.loader is None:
        raise SyncError("could not load conversation exporter")
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.path.insert(0, str(script_dir))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def inherited_history_inventory(paths: list[str]) -> list[dict[str, Any]]:
    histories = []
    seen: set[str] = set()
    for raw in paths:
        root = Path(raw).resolve(strict=True)
        key = os.path.normcase(str(root))
        if key in seen:
            continue
        seen.add(key)
        if not root.is_dir() or is_reparse(root):
            raise SyncError(f"inherited conversation history is missing or unsafe: {root}")
        files = []
        total_bytes = 0
        for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix().casefold()):
            if is_reparse(path):
                raise SyncError(f"inherited conversation history contains a reparse point: {path}")
            if path.is_file():
                relative = path.relative_to(root).as_posix()
                size = path.stat().st_size
                files.append({"path": relative, "size": size, "sha256": sha256_file(path)})
                total_bytes += size
        if not files or not any(item["path"] == "FULL_TRANSCRIPT.jsonl" for item in files):
            raise SyncError(f"inherited conversation history lacks FULL_TRANSCRIPT.jsonl: {root}")
        histories.append({
            "source_path": str(root),
            "tree_sha256": hashlib.sha256(canonical_bytes(files)).hexdigest(),
            "file_count": len(files),
            "total_bytes": total_bytes,
            "files": files,
        })
    histories.sort(key=lambda item: item["tree_sha256"])
    return histories


def portable_identity():
    spec = importlib.util.spec_from_file_location("portable_conversation_identity", Path(__file__).with_name("conversation_identity.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def semantic_message_fingerprint(message: dict[str, Any]) -> str:
    """Hash user-visible meaning without local record IDs, time, or sequence."""
    content = []
    for part in message.get("content") or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            content.append({"type": "text", "text": str(part.get("text") or "")})
        elif part.get("type") == "image":
            content.append({
                "type": "image",
                "mime_type": str(part.get("mime_type") or ""),
                "size": int(part.get("size") or 0),
                "sha256": str(part.get("sha256") or "").lower(),
            })
    identity = {"role": str(message.get("role") or ""), "phase": message.get("phase"), "content": content}
    return hashlib.sha256(canonical_bytes(identity)).hexdigest()


def inherited_message_fingerprints(paths: list[str]) -> list[str]:
    """Flatten retained transcript history once, in deterministic path order."""
    fingerprints: list[str] = []
    seen_transcripts: set[str] = set()
    for root_text in paths:
        root = Path(root_text).resolve(strict=True)
        transcripts = [root] if root.is_file() and root.name == "FULL_TRANSCRIPT.jsonl" else sorted(
            root.rglob("FULL_TRANSCRIPT.jsonl"), key=lambda item: item.relative_to(root).as_posix().casefold()
        )
        for transcript in transcripts:
            raw = transcript.read_bytes()
            transcript_sha = hashlib.sha256(raw).hexdigest()
            if transcript_sha in seen_transcripts:
                continue
            seen_transcripts.add(transcript_sha)
            for line_number, line in enumerate(raw.splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise SyncError(f"invalid inherited transcript {transcript}:{line_number}") from exc
                if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
                    raise SyncError(f"invalid inherited transcript record {transcript}:{line_number}")
                fingerprints.append(semantic_message_fingerprint(message))
    return fingerprints


def conversation_inventory(exporter, session_log: Path, thread_id: str, title: str | None,
                           original_session_log: Path | None = None,
                           inherited_handoff_paths: list[str] | None = None,
                           generation: str = CONVERSATION_PACK_GENERATION,
                           lineage: dict[str, Any] | None = None) -> dict[str, Any]:
    session_data = exporter.read_stable(session_log)
    with tempfile.TemporaryDirectory(prefix="codex-conversation-fingerprint-") as raw:
        work = Path(raw)
        try:
            messages, stats = exporter.export_messages(session_data, work, thread_id)
        except ValueError as exc:
            if str(exc) == "No user-visible conversation messages were found.":
                raise NonTransferableConversation(str(exc)) from exc
            raise
        transcript = work / "FULL_TRANSCRIPT.jsonl"
        attachments = []
        attachment_root = work / "attachments"
        if attachment_root.exists():
            for path in sorted(attachment_root.rglob("*")):
                if path.is_file():
                    attachments.append({
                        "path": path.relative_to(work).as_posix(),
                        "size": path.stat().st_size,
                        "sha256": sha256_file(path),
                    })
        inherited_paths = inherited_handoff_paths or []
        inherited_histories = inherited_history_inventory(inherited_paths)
        message_fingerprints = portable_identity().combined(inherited_paths, messages)
        lineage = dict(lineage or {})
        identity = {
            "generator": generation,
            **(
                {"lineage_id": lineage["lineage_id"], "branch_id": lineage.get("branch_id") or "main"}
                if lineage.get("lineage_id") else {"thread_id": thread_id}
            ),
            "messages_sha256": sha256_file(transcript),
            "attachments": attachments,
            "inherited_histories": [
                {"tree_sha256": item["tree_sha256"], "file_count": item["file_count"], "total_bytes": item["total_bytes"]}
                for item in inherited_histories
            ],
        }
        first_user_text = ""
        for message in messages:
            if message["role"] == "user":
                first_user_text = " ".join(
                    part["text"] for part in message["content"] if part["type"] == "text"
                ).strip()
                if first_user_text:
                    break
        resolved_title = (title or first_user_text or thread_id).strip()[:200]
        return {
            "thread_id": thread_id,
            "thread_title": resolved_title,
            "session_log": str(original_session_log or session_log),
            "snapshot_session_log": str(session_log),
            "conversation_sha256": (portable_identity().content_hash(lineage["lineage_id"], lineage.get("branch_id"), message_fingerprints, generation)
                                    if lineage.get("lineage_id") else hashlib.sha256(canonical_bytes(identity)).hexdigest()),
            "messages_sha256": identity["messages_sha256"],
            "evidence_sha256": portable_identity().evidence_hash(identity["messages_sha256"], attachments, inherited_histories),
            "message_count": len(messages),
            "attachment_count": stats["attachment_count"],
            "cutoff_timestamp": messages[-1].get("timestamp"),
            "attachments": attachments,
            "inherited_histories": inherited_histories,
            "message_fingerprints": message_fingerprints,
            **{field: lineage.get(field) for field in LINEAGE_FIELDS if lineage.get(field) is not None},
        }


def validate_discovery(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SyncError("discovery must be a JSON object")
    projects = payload.get("projects")
    if not isinstance(projects, list) or not projects:
        raise SyncError("discovery.projects must be a non-empty array")
    for field in ("source_device", "source_computer"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise SyncError(f"discovery.{field} is required")
    if payload.get("format") == "cross-device-project-sync-multi-instance-discovery-v2":
        claimed = str(payload.get("discovery_sha256") or "").lower()
        unsigned = dict(payload)
        unsigned.pop("discovery_sha256", None)
        if claimed != hashlib.sha256(canonical_bytes(unsigned)).hexdigest():
            raise SyncError("multi-instance discovery_sha256 does not match the discovery content")
        instances = payload.get("instances")
        if not isinstance(instances, list) or not instances:
            raise SyncError("multi-instance discovery must contain instances")
    return payload


LINEAGE_FIELDS = (
    "evidence_sha256",
    "projectless_directory_id",
    "projectless_directory_ids",
    "source_thread_id",
    "source_instance_id", "source_instance_name", "source_disposition",
    "origin_computer", "origin_instance_id", "origin_thread_id",
    "lineage_id", "branch_id", "cwd",
)


def _is_prefix(left: list[str], right: list[str]) -> bool:
    return len(left) <= len(right) and left == right[:len(left)]


def conversation_content_sha(conversation: dict[str, Any], generation: str) -> str:
    if conversation.get("lineage_id"):
        return portable_identity().content_hash(conversation["lineage_id"], conversation.get("branch_id"), conversation["message_fingerprints"], generation)
    identity = {
        "generator": generation,
        **(
            {
                "lineage_id": conversation["lineage_id"],
                "branch_id": conversation.get("branch_id") or "main",
            }
            if conversation.get("lineage_id") else {"thread_id": conversation["thread_id"]}
        ),
        "messages_sha256": conversation["messages_sha256"],
        "attachments": conversation.get("attachments") or [],
        "inherited_histories": [
            {
                "tree_sha256": item["tree_sha256"],
                "file_count": item["file_count"],
                "total_bytes": item["total_bytes"],
            }
            for item in conversation.get("inherited_histories") or []
        ],
    }
    return hashlib.sha256(canonical_bytes(identity)).hexdigest()


def reconcile_lineage_branches(projects: list[dict[str, Any]], exclusions: list[dict[str, Any]]) -> None:
    """Deduplicate equal/ancestor copies and preserve true divergences as branches."""
    grouped: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for project in projects:
        for conversation in project.get("conversations") or []:
            lineage = str(conversation.get("lineage_id") or "")
            branch = str(conversation.get("branch_id") or "main")
            if lineage:
                grouped.setdefault((lineage, branch), []).append((project, conversation))
    for (lineage, parent_branch), candidates in grouped.items():
        if len(candidates) < 2:
            continue
        candidates.sort(key=lambda item: (
            item[1].get("source_disposition") == "archived",
            str(item[1].get("source_instance_id") or "").casefold(),
            str(item[1].get("thread_id") or "").casefold(),
        ))
        fingerprints = [list(item[1].get("message_fingerprints") or []) for item in candidates]
        winner_indexes: set[int] = set(range(len(candidates)))
        for left_index, left in enumerate(fingerprints):
            for right_index, right in enumerate(fingerprints):
                if left_index == right_index or left_index not in winner_indexes:
                    continue
                if left == right and right_index < left_index:
                    winner_indexes.discard(left_index)
                elif len(left) < len(right) and _is_prefix(left, right):
                    winner_indexes.discard(left_index)
        kept = [index for index in sorted(winner_indexes)]
        removed = [index for index in range(len(candidates)) if index not in winner_indexes]
        for index in removed:
            project, conversation = candidates[index]
            project["conversations"].remove(conversation)
            reason = "duplicate conversation lineage content"
            if any(_is_prefix(fingerprints[index], fingerprints[other]) and len(fingerprints[index]) < len(fingerprints[other]) for other in kept):
                reason = "conversation lineage is a strict ancestor of a retained descendant"
            exclusion = {
                "project_id": project["project_id"],
                "thread_id": conversation["thread_id"],
                "source_instance_id": conversation.get("source_instance_id"),
                "source_disposition": conversation.get("source_disposition"),
                "original_session_log": conversation.get("session_log"),
                "reason": reason,
                "lineage_id": lineage,
                "branch_id": parent_branch,
            }
            exclusions.append(exclusion)
            project.setdefault("conversation_exclusions", []).append(exclusion)
        survivors = [candidates[index] for index in kept]
        if len(survivors) < 2:
            continue
        survivor_fingerprints = [list(item[1].get("message_fingerprints") or []) for item in survivors]
        if all(
            _is_prefix(survivor_fingerprints[left], survivor_fingerprints[right])
            or _is_prefix(survivor_fingerprints[right], survivor_fingerprints[left])
            for left in range(len(survivors)) for right in range(left + 1, len(survivors))
        ):
            continue
        for project, conversation in survivors:
            sequence = conversation["message_fingerprints"]
            # The distinguishing PREFIX, not the task/instance ID, defines a
            # branch. Copying or extending it keeps the same stable branch ID.
            prefix_length = 1
            while prefix_length < len(sequence) and any(other != sequence and other[:prefix_length] == sequence[:prefix_length] for other in survivor_fingerprints):
                prefix_length += 1
            branch_identity = {
                "lineage_id": lineage,
                "parent_branch_id": parent_branch,
                "distinguishing_prefix": sequence[:prefix_length],
            }
            conversation["parent_branch_id"] = parent_branch
            conversation["branch_id"] = "branch-" + hashlib.sha256(canonical_bytes(branch_identity)).hexdigest()[:24]


def conversation_set_identity(project: dict[str, Any], generation: str, is_v2: bool) -> dict[str, Any]:
    rows = []
    for item in project.get("conversations") or []:
        if is_v2:
            rows.append(portable_identity().set_row(item))
        else:
            rows.append({"thread_id": item["thread_id"], "conversation_sha256": item["conversation_sha256"]})
    return {"generator": generation, "conversations": rows}


def apply_branch_state(projects, path):
    value = json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else {"format": "cross-device-lineage-branch-state-v1", "branches": []}
    claimed = value.pop("state_sha256", None)
    if claimed is not None and claimed != hashlib.sha256(canonical_bytes(value)).hexdigest():
        raise SyncError("local lineage branch registry hash mismatch")
    if value.get("format") != "cross-device-lineage-branch-state-v1":
        raise SyncError("unsupported local lineage branch registry")
    for project in projects:
        for row in project["conversations"]:
            matches = [old for old in value["branches"] if old["lineage_id"] == row["lineage_id"] and _is_prefix(old["prefix"], row["message_fingerprints"])]
            if matches:
                longest = max(len(old["prefix"]) for old in matches)
                exact = {old["branch_id"] for old in matches if len(old["prefix"]) == longest}
                if len(exact) != 1:
                    raise SyncError("ambiguous persisted lineage branch")
                row["branch_id"] = exact.pop()
    return value


def persist_branch_state(projects, path, value):
    for project in projects:
        for row in project["conversations"]:
            if row.get("branch_id") == "main":
                continue
            entry = {"lineage_id": row["lineage_id"], "branch_id": row["branch_id"], "prefix": row["message_fingerprints"]}
            if entry not in value["branches"]:
                value["branches"].append(entry)
    value["state_sha256"] = hashlib.sha256(canonical_bytes(value)).hexdigest()
    # This file is local machine state, never copied into a catalog or package.
    import tempfile
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=target.parent, delete=False) as stream:
        temporary = stream.name
        stream.write(canonical_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)


def command_scan(args: argparse.Namespace) -> dict[str, Any]:
    discovery = validate_discovery(json.loads(Path(args.discovery).read_text(encoding="utf-8")))
    is_v2 = discovery.get("format") == "cross-device-project-sync-multi-instance-discovery-v2"
    catalog_format = CATALOG_FORMAT_V2 if is_v2 else CATALOG_FORMAT_V1
    generation = CONVERSATION_PACK_GENERATION_V2 if is_v2 else CONVERSATION_PACK_GENERATION
    exporter = load_exporter(Path(__file__).resolve().parent)
    projects: list[dict[str, Any]] = []
    input_conversation_exclusions = list(discovery.get("conversation_exclusions") or [])
    conversation_exclusions: list[dict[str, Any]] = []
    preclassified_by_project: dict[str, list[dict[str, Any]]] = {}
    for exclusion in input_conversation_exclusions:
        if not isinstance(exclusion, dict):
            raise SyncError("conversation exclusion must be an object")
        exclusion_project_id = str(exclusion.get("project_id") or "").strip()
        if exclusion_project_id:
            preclassified_by_project.setdefault(exclusion_project_id.casefold(), []).append(exclusion)
        else:
            conversation_exclusions.append(exclusion)
    checkpoint_dir = Path(args.checkpoint_dir).absolute() if args.checkpoint_dir else None
    status_path = Path(args.status_file).absolute() if args.status_file else None
    if checkpoint_dir is not None:
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    checkpoints: list[tuple[Path, str, dict[str, Any]]] = []
    for raw_project in discovery["projects"]:
        if not isinstance(raw_project, dict):
            raise SyncError("discovery project must be an object")
        project_id = str(raw_project.get("project_id") or "").strip()
        label = str(raw_project.get("label") or "").strip()
        project_kind = str(raw_project.get("kind") or "registered-project").strip()
        source_path = str(raw_project.get("source_path") or "").strip()
        has_project_payload = bool(source_path)
        if not project_id or project_id.casefold() in seen_ids:
            raise SyncError(f"missing or duplicate project_id: {project_id}")
        if not has_project_payload and project_kind != "projectless-conversation-group":
            raise SyncError(f"only a projectless-conversation-group may omit source_path: {project_id}")
        seen_ids.add(project_id.casefold())
        root: Path | None = None
        if has_project_payload:
            root = Path(source_path).absolute()
            path_identity = os.path.normcase(os.path.normpath(str(root)))
            if path_identity in seen_paths:
                raise SyncError(f"duplicate project source path: {root}")
            seen_paths.add(path_identity)
        checkpoint_binding = hashlib.sha256(canonical_bytes({"format": SCAN_CHECKPOINT_FORMAT, "project": raw_project})).hexdigest()
        checkpoint_path = checkpoint_dir / (hashlib.sha256(project_id.encode("utf-8")).hexdigest() + ".json") if checkpoint_dir else None
        print(f"scan-start:{project_id}:{source_path or '[conversation-only]'}", file=os.sys.stderr, flush=True)
        tree = project_inventory(root, list(raw_project.get("exclude_relative_paths") or []), status_path) if root else {
            "tree_sha256": None,
            "entry_count": 0,
            "file_count": 0,
            "total_bytes": 0,
            "entries": [],
            "excluded_relative_paths": [],
        }
        project = {
            "project_id": project_id,
            "kind": project_kind,
            "has_project_payload": has_project_payload,
            "label": label or (root.name if root else project_id),
            "source_path": str(root) if root else None,
            "registrations": list(raw_project.get("registrations") or []),
            **tree,
            "exclusion_records": list(raw_project.get("exclusion_records") or []),
        }
        inventory = raw_project.get("conversation_inventory")
        inventory_complete = isinstance(inventory, dict) and inventory.get("complete") is True
        expected_thread_ids = list(inventory.get("expected_thread_ids") or []) if isinstance(inventory, dict) else []
        preclassified_exclusions = preclassified_by_project.pop(project_id.casefold(), [])
        preclassified_ids = [str(item.get("thread_id") or "") for item in preclassified_exclusions]
        if any(not item.strip() for item in preclassified_ids):
            raise SyncError(f"preclassified conversation exclusion lacks thread ID for {project_id}")
        if len({item.casefold() for item in preclassified_ids}) != len(preclassified_ids):
            raise SyncError(f"duplicate preclassified conversation exclusion for {project_id}")
        if inventory_complete:
            if any(not isinstance(item, str) or not item.strip() for item in expected_thread_ids):
                raise SyncError(f"invalid expected conversation thread ID for {project_id}")
            expected_keys = [item.casefold() for item in expected_thread_ids]
            if not is_v2 and len(expected_keys) != len(set(expected_keys)):
                raise SyncError(f"duplicate expected conversation thread ID for {project_id}")
            discovered_ids = [str(item.get("thread_id") or "") for item in raw_project.get("conversations") or []]
            accounted_ids = discovered_ids + preclassified_ids
            if {item.casefold() for item in accounted_ids} != set(expected_keys) or len(accounted_ids) != len(expected_thread_ids):
                raise SyncError(f"conversation discovery inventory mismatch for {project_id}")
        conversations = []
        project_conversation_exclusions = list(preclassified_exclusions)
        for raw_conversation in raw_project.get("conversations") or []:
            snapshot_log = Path(str(raw_conversation.get("snapshot_session_log") or raw_conversation["session_log"])).resolve(strict=True)
            original_log = Path(str(raw_conversation.get("original_session_log") or raw_conversation["session_log"])).absolute()
            thread_id = str(raw_conversation["thread_id"])
            lineage = {field: raw_conversation.get(field) for field in LINEAGE_FIELDS}
            if is_v2:
                if not SHA256_RE.fullmatch(str(lineage.get("lineage_id") or "")):
                    raise SyncError(f"v2 conversation lacks a valid lineage_id: {thread_id}")
                if not str(lineage.get("source_instance_id") or "").strip():
                    raise SyncError(f"v2 conversation lacks source_instance_id: {thread_id}")
                if lineage.get("source_disposition") not in {"active", "archived"}:
                    raise SyncError(f"v2 conversation has invalid source_disposition: {thread_id}")
            try:
                conversations.append(conversation_inventory(
                    exporter, snapshot_log, thread_id, raw_conversation.get("thread_title"), original_log,
                    list(raw_conversation.get("inherited_handoff_paths") or []), generation, lineage,
                ))
            except NonTransferableConversation:
                project_conversation_exclusions.append({
                    "project_id": project_id,
                    "thread_id": thread_id,
                    "source_instance_id": raw_conversation.get("source_instance_id"),
                    "original_session_log": str(original_log),
                    "reason": "zero user-visible messages after required system, reasoning, tool, and injected-context filtering",
                })
        conversations.sort(key=lambda item: (str(item.get("lineage_id") or ""), str(item.get("branch_id") or ""), str(item.get("source_instance_id") or "").casefold(), item["thread_id"].casefold()))
        exported_keys = {item["thread_id"].casefold() for item in conversations}
        excluded_keys = {str(item.get("thread_id") or "").casefold() for item in project_conversation_exclusions}
        from collections import Counter
        accounted = Counter([item["thread_id"].casefold() for item in conversations] + [str(item.get("thread_id") or "").casefold() for item in project_conversation_exclusions])
        if inventory_complete and accounted != Counter(item.casefold() for item in expected_thread_ids):
            raise SyncError(f"conversation inventory is not fully accounted for: {project_id}")
        if is_v2 and len({(item.get("source_instance_id"), item["thread_id"]) for item in conversations}) != len(conversations):
            raise SyncError("duplicate thread inside one source instance")
        project.update({
            "conversation_pack_generation": generation,
            "conversation_count": len(conversations),
            "conversations": conversations,
            "conversation_exclusions": project_conversation_exclusions,
            "conversation_inventory_complete": inventory_complete,
            "expected_conversation_count": len(expected_thread_ids) if inventory_complete else None,
            "accounted_conversation_count": len(exported_keys | excluded_keys),
        })
        conversation_exclusions.extend(project_conversation_exclusions)
        projects.append(project)
        if checkpoint_path is not None:
            checkpoints.append((checkpoint_path, checkpoint_binding, project))
    if preclassified_by_project:
        unknown = sorted(preclassified_by_project, key=str.casefold)
        raise SyncError(f"conversation exclusions reference unknown project IDs: {', '.join(unknown)}")
    if is_v2:
        branch_path = getattr(args, "branch_state", None)
        branch_state = apply_branch_state(projects, branch_path) if branch_path else None
        reconcile_lineage_branches(projects, conversation_exclusions)
        if branch_path:
            persist_branch_state(projects, branch_path, branch_state)
    for project in projects:
        project["conversations"].sort(key=lambda item: (str(item.get("lineage_id") or ""), str(item.get("branch_id") or ""), str(item.get("source_instance_id") or "").casefold(), item["thread_id"].casefold()))
        for conversation in project["conversations"]:
            conversation["conversation_sha256"] = conversation_content_sha(conversation, generation)
            if is_v2:
                conversation["source_thread_id"] = conversation.get("source_thread_id") or conversation["thread_id"]
                conversation["thread_id"] = hashlib.sha256(canonical_bytes({"lineage_id": conversation["lineage_id"], "branch_id": conversation["branch_id"]})).hexdigest()
        project["conversation_count"] = len(project["conversations"])
        project["conversation_set_sha256"] = hashlib.sha256(canonical_bytes(conversation_set_identity(project, generation, is_v2))).hexdigest()
        project["accounted_conversation_count"] = project["conversation_count"] + len(project["conversation_exclusions"])
        print(f"scan-complete:{project['project_id']}:{project['file_count']}:{project['total_bytes']}", file=os.sys.stderr, flush=True)
    for checkpoint_path, checkpoint_binding, project in checkpoints:
        write_json(checkpoint_path, {"format": SCAN_CHECKPOINT_FORMAT, "discovery_binding_sha256": checkpoint_binding, "project": project})
    projects.sort(key=lambda item: item["project_id"].casefold())
    catalog = {
        "format": catalog_format,
        "source_device": discovery["source_device"],
        "source_computer": discovery["source_computer"],
        **({
            "instances": discovery.get("instances") or [],
            "instance_registry_sha256": discovery.get("instance_registry_sha256"),
            "discovery_sha256": discovery.get("discovery_sha256"),
        } if is_v2 else {}),
        "projects": projects,
        "cloud_managed_projects": discovery.get("cloud_managed_projects") or [],
        **({"exclusions": discovery.get("exclusions") or []} if is_v2 else {}),
        "conversation_exclusions": conversation_exclusions,
        "conversation_inventory_complete": discovery.get("conversation_inventory_complete") is not False and all(item["conversation_inventory_complete"] for item in projects),
        "expected_conversation_count": sum(item["expected_conversation_count"] or 0 for item in projects),
    }
    catalog["catalog_sha256"] = hashlib.sha256(canonical_bytes(catalog)).hexdigest()
    write_json(Path(args.output), catalog)
    return {
        "catalog": str(Path(args.output).resolve()),
        "catalog_sha256": catalog["catalog_sha256"],
        "project_count": sum(item["has_project_payload"] for item in projects),
        "conversation_only_group_count": sum(not item["has_project_payload"] for item in projects),
        "conversation_count": sum(item["conversation_count"] for item in projects),
        "total_bytes": sum(item["total_bytes"] for item in projects),
    }


def load_hashed_document(path: Path, expected_format: str | set[str], hash_field: str) -> dict[str, Any]:
    raw = path.read_bytes()
    payload = json.loads(raw)
    expected = {expected_format} if isinstance(expected_format, str) else expected_format
    if not isinstance(payload, dict) or payload.get("format") not in expected:
        raise SyncError(f"unsupported {expected_format} document: {path}")
    claimed = str(payload.get(hash_field) or "").lower()
    unsigned = dict(payload)
    unsigned.pop(hash_field, None)
    actual = hashlib.sha256(canonical_bytes(unsigned)).hexdigest()
    if claimed != actual or raw != canonical_bytes(payload):
        raise SyncError(f"{hash_field} or canonical encoding is invalid: {path}")
    return payload


def load_catalog(path: Path) -> dict[str, Any]:
    payload = load_hashed_document(path, CATALOG_FORMATS, "catalog_sha256")
    if payload.get("format") not in CATALOG_FORMATS or not isinstance(payload.get("projects"), list):
        raise SyncError(f"unsupported content catalog: {path}")
    return payload


def load_previous(path: Path) -> dict[str, Any]:
    raw_payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw_payload, dict):
        raise SyncError(f"unsupported previous catalog or index: {path}")
    if raw_payload.get("format") in CATALOG_FORMATS:
        payload = load_hashed_document(path, CATALOG_FORMATS, "catalog_sha256")
    elif raw_payload.get("format") in INDEX_FORMATS:
        payload = load_hashed_document(path, INDEX_FORMATS, "index_sha256")
        if payload.get("repository") != CANONICAL_REPOSITORY:
            raise SyncError("previous device index repository mismatch")
    else:
        raise SyncError(f"unsupported previous catalog or index: {path}")
    if not isinstance(payload.get("projects"), list):
        raise SyncError(f"unsupported previous catalog or index: {path}")
    return payload


def command_plan(args: argparse.Namespace) -> dict[str, Any]:
    current = load_catalog(Path(args.current))
    previous = load_previous(Path(args.previous)) if args.previous else {"projects": []}
    if previous.get("projects") and (
        str(previous.get("source_device") or "") != str(current["source_device"])
        or str(previous.get("source_computer") or "").casefold() != str(current["source_computer"]).casefold()
    ):
        raise SyncError("previous catalog or index belongs to another source device")
    prior = {item["project_id"].casefold(): item for item in previous["projects"]}
    decisions = []
    for item in current["projects"]:
        old = prior.get(item["project_id"].casefold())
        project_action = "upload" if item.get("has_project_payload", True) else "none"
        conversation_action = "upload"
        if project_action != "none" and old and old.get("tree_sha256") == item["tree_sha256"] and old.get("project_release"):
            project_action = "reuse"
        if old and old.get("conversation_set_sha256") == item["conversation_set_sha256"] and old.get("conversation_release"):
            conversation_action = "reuse"
        decisions.append({
            "project_id": item["project_id"],
            "kind": item.get("kind") or "registered-project",
            "has_project_payload": item.get("has_project_payload", True),
            "label": item["label"],
            "source_path": item["source_path"],
            "tree_sha256": item["tree_sha256"],
            "conversation_set_sha256": item["conversation_set_sha256"],
            "conversation_pack_generation": item["conversation_pack_generation"],
            "project_action": project_action,
            "conversation_action": conversation_action,
            "project_release": old.get("project_release") if old and project_action == "reuse" else None,
            "conversation_release": old.get("conversation_release") if old and conversation_action == "reuse" else None,
        })
    result = {
        "format": PLAN_FORMAT_V2 if current.get("format") == CATALOG_FORMAT_V2 else PLAN_FORMAT_V1,
        "source_device": current["source_device"],
        "source_computer": current["source_computer"],
        "current_catalog_sha256": current["catalog_sha256"],
        "decisions": decisions,
    }
    result["plan_sha256"] = hashlib.sha256(canonical_bytes(result)).hexdigest()
    write_json(Path(args.output), result)
    return {
        "plan": str(Path(args.output).resolve()),
        "plan_sha256": result["plan_sha256"],
        "project_upload_count": sum(item["project_action"] == "upload" for item in decisions),
        "project_reuse_count": sum(item["project_action"] == "reuse" for item in decisions),
        "conversation_upload_count": sum(item["conversation_action"] == "upload" for item in decisions),
        "conversation_reuse_count": sum(item["conversation_action"] == "reuse" for item in decisions),
    }


def load_plan(path: Path) -> dict[str, Any]:
    payload = load_hashed_document(path, PLAN_FORMATS, "plan_sha256")
    if not isinstance(payload.get("decisions"), list):
        raise SyncError("incremental plan decisions must be an array")
    return payload


def command_verify_plan(args: argparse.Namespace) -> dict[str, Any]:
    catalog = load_catalog(Path(args.catalog))
    plan = load_plan(Path(args.plan))
    if (
        plan.get("current_catalog_sha256") != catalog.get("catalog_sha256")
        or plan.get("source_device") != catalog.get("source_device")
        or str(plan.get("source_computer") or "").casefold() != str(catalog.get("source_computer") or "").casefold()
    ):
        raise SyncError("incremental plan does not bind the supplied catalog and source device")
    catalog_by_id = {str(item.get("project_id") or "").casefold(): item for item in catalog["projects"]}
    if len(catalog_by_id) != len(catalog["projects"]):
        raise SyncError("catalog contains duplicate project IDs")
    seen: set[str] = set()
    for decision in plan["decisions"]:
        if not isinstance(decision, dict):
            raise SyncError("incremental plan decision must be an object")
        project_id = str(decision.get("project_id") or "")
        key = project_id.casefold()
        if not project_id or key in seen or key not in catalog_by_id:
            raise SyncError(f"missing, duplicate, or unknown plan project ID: {project_id!r}")
        seen.add(key)
        project = catalog_by_id[key]
        for field in ("label", "source_path", "tree_sha256", "conversation_set_sha256", "conversation_pack_generation"):
            if decision.get(field) != project.get(field):
                raise SyncError(f"plan decision does not match catalog field {field}: {project_id}")
        for action_field, release_field in (
            ("project_action", "project_release"),
            ("conversation_action", "conversation_release"),
        ):
            action = decision.get(action_field)
            release = decision.get(release_field)
            allowed = {"upload", "reuse", "none"} if action_field == "project_action" else {"upload", "reuse"}
            if action not in allowed:
                raise SyncError(f"invalid {action_field} for {project_id}")
            if action == "reuse":
                validate_release_reference(release, f"{project_id}.{release_field}")
            elif release is not None:
                raise SyncError(f"upload decision must not carry {release_field}: {project_id}")
            if action_field == "project_action" and action == "none" and project.get("has_project_payload", True):
                raise SyncError(f"project payload cannot use project_action=none: {project_id}")
    if seen != set(catalog_by_id):
        raise SyncError("incremental plan omits one or more catalog projects")
    return {
        "plan": str(Path(args.plan).resolve()),
        "plan_sha256": plan["plan_sha256"],
        "catalog_sha256": catalog["catalog_sha256"],
        "project_count": len(seen),
        "verified": True,
    }


def validate_release_reference(value: Any, field: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise SyncError(f"{field} must be an object")
    tag = str(value.get("tag") or "")
    manifest_sha256 = str(value.get("manifest_sha256") or "").lower()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,127}", tag):
        raise SyncError(f"{field}.tag is invalid")
    if not SHA256_RE.fullmatch(manifest_sha256):
        raise SyncError(f"{field}.manifest_sha256 is invalid")
    return {"tag": tag, "manifest_sha256": manifest_sha256}


def validate_release(value: Any, field: str) -> dict[str, Any]:
    reference = validate_release_reference(value, field)
    reused = value.get("reused")
    if not isinstance(reused, bool):
        raise SyncError(f"{field}.reused must be Boolean")
    return {**reference, "reused": reused}


def command_build_index(args: argparse.Namespace) -> dict[str, Any]:
    catalog = load_catalog(Path(args.catalog))
    is_v2 = catalog.get("format") == CATALOG_FORMAT_V2
    if catalog.get("conversation_inventory_complete") is not True:
        raise SyncError("content catalog does not prove a complete conversation inventory")
    bindings = json.loads(Path(args.bindings).read_text(encoding="utf-8"))
    by_id = {str(item.get("project_id") or "").casefold(): item for item in bindings.get("projects") or []}
    if len(by_id) != len(bindings.get("projects") or []):
        raise SyncError("bindings contain duplicate project IDs")
    projects = []
    for project in catalog["projects"]:
        binding = by_id.pop(project["project_id"].casefold(), None)
        if binding is None:
            raise SyncError(f"missing Release bindings for {project['project_id']}")
        conversations = []
        for item in project["conversations"]:
            conversation = {
                "thread_id": item["thread_id"],
                "thread_title": item["thread_title"],
                "conversation_sha256": item["conversation_sha256"],
                "messages_sha256": item["messages_sha256"],
                "message_count": item["message_count"],
                "attachment_count": item["attachment_count"],
                "cutoff_timestamp": item["cutoff_timestamp"],
            }
            if is_v2:
                conversation.update({field: item.get(field) for field in LINEAGE_FIELDS if item.get(field) is not None})
                if item.get("parent_branch_id"):
                    conversation["parent_branch_id"] = item["parent_branch_id"]
            conversations.append(conversation)
        has_project_payload = project.get("has_project_payload", True)
        project_record = {
            "project_id": project["project_id"],
            "kind": project.get("kind") or "registered-project",
            "has_project_payload": has_project_payload,
            "label": project["label"],
            "source_path": project["source_path"],
            "tree_sha256": project["tree_sha256"],
            "entry_count": project["entry_count"],
            "file_count": project["file_count"],
            "total_bytes": project["total_bytes"],
            "excluded_relative_paths": project["excluded_relative_paths"],
            "exclusion_records": project.get("exclusion_records") or [],
            "registrations": project.get("registrations") or [],
            "project_release": (
                validate_release(binding.get("project_release"), f"{project['project_id']}.project_release")
                if has_project_payload else None
            ),
            "conversation_set_sha256": project["conversation_set_sha256"],
            "conversation_pack_generation": project["conversation_pack_generation"],
            "conversation_count": project["conversation_count"],
            "conversations": conversations,
            "conversation_release": validate_release(binding.get("conversation_release"), f"{project['project_id']}.conversation_release"),
        }
        if not has_project_payload and binding.get("project_release") is not None:
            raise SyncError(f"conversation-only group must not carry a project Release: {project['project_id']}")
        projects.append(project_record)
    if by_id:
        raise SyncError("bindings contain unknown project IDs: " + ", ".join(sorted(by_id)))
    index = {
        "format": INDEX_FORMAT_V2 if is_v2 else INDEX_FORMAT_V1,
        "migration_id": str(bindings.get("migration_id") or ""),
        "repository": CANONICAL_REPOSITORY,
        "required_skill_version": REQUIRED_SKILL_VERSION,
        "skill_release": validate_release(bindings.get("skill_release"), "skill_release"),
        "source_device": catalog["source_device"],
        "source_computer": catalog["source_computer"],
        "content_catalog_sha256": catalog["catalog_sha256"],
        **({
            "instances": catalog.get("instances") or [],
            "instance_registry_sha256": catalog.get("instance_registry_sha256"),
            "exclusions": catalog.get("exclusions") or [],
        } if is_v2 else {}),
        "projects": projects,
        "cloud_managed_projects": catalog.get("cloud_managed_projects") or [],
        "conversation_exclusions": catalog.get("conversation_exclusions") or [],
    }
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,127}", index["migration_id"]):
        raise SyncError("bindings.migration_id is invalid")
    index["index_sha256"] = hashlib.sha256(canonical_bytes(index)).hexdigest()
    write_json(Path(args.output), index)
    handoff = Path(args.handoff)
    lines = [
        "# 接收设备恢复前必读",
        "",
        f"- 源设备：{index['source_device']}（{index['source_computer']}）",
        f"- 目标私有仓库：`{CANONICAL_REPOSITORY}`",
        f"- 迁移ID：`{index['migration_id']}`",
        f"- 设备索引SHA-256：`{index['index_sha256']}`",
        f"- 必需skill版本：`{REQUIRED_SKILL_VERSION}`",
        f"- skill Release：`{index['skill_release']['tag']}`，清单SHA-256：`{index['skill_release']['manifest_sha256']}`",
        "",
        "## 接收顺序",
        "",
        "1. 先下载并校验本索引及同Release中的本文件。",
        "2. 下载并校验上面绑定的skill版本，把它暂存为本次迁移专用的隔离运行时；不要覆盖公共skill，也不要使用其他版本解释新索引。",
        "3. 按下表逐项下载项目Release与对话包Release。`复用`表示本次没有重复上传字节，但该旧Release已重新写入本次索引，仍是本次迁移的组成部分。",
        "4. 每个项目恢复到新的空目录，注册为本地项目；为下表中的每个对话创建或恢复一个绑定到该项目的任务。不要声称旧聊天气泡或原任务ID被导入。",
        "5. 所有项目、对话、排除项和云端托管引用全部核对后，才能报告同步完成。",
        "",
        "## 项目与对话总账",
        "",
        "|项目|原路径|项目文件与Release|对话|对话包Release|明确排除|",
        "|---|---|---|---:|---|---|",
    ]
    for item in projects:
        project_state = ("复用" if item["project_release"]["reused"] else "本次新上传") if item["project_release"] else "仅对话，不上传目录"
        conversation_state = "复用" if item["conversation_release"]["reused"] else "本次新上传"
        exclusion_records = item.get("exclusion_records") or [
            {"path": path, "reason": "excluded from archive"} for path in item["excluded_relative_paths"]
        ]
        excluded = "<br>".join(f"`{record['path']}`：{record['reason']}" for record in exclusion_records) or "无"
        project_release_text = (
            f"{project_state}；`{item['project_release']['tag']}`；清单 `{item['project_release']['manifest_sha256']}`；树指纹 `{item['tree_sha256']}`"
            if item["project_release"] else project_state
        )
        lines.append(f"|{item['label']}|`{item['source_path'] or '(none)'}`|{project_release_text}|"
                     f"{item['conversation_count']}|{conversation_state}；`{item['conversation_release']['tag']}`；集合指纹 `{item['conversation_set_sha256']}`|{excluded}|")
    lines.extend([
        "",
        "## 没有作为本地归档上传的内容",
        "",
        "- 上表列出的目录连接点：只记录路径和原因，不跟随到项目外部运行时缓存。",
        "- ChatGPT账号托管项目：仅在索引中记录ID并在接收账号核验，不伪装成本地文件夹上传。",
        "- Codex系统/开发者指令、隐藏推理、工具调用参数和原始工具输出：不属于可迁移对话记录。",
        f"- 整个 `.codex` 数据库、登录凭据和{index['source_device']}本地设备标签文件：不复制到接收设备。",
        "- 每个对话只覆盖其记录的截止时间；截止时间之后的新消息不可能已经存在于本快照。",
        "",
        "## 明确未打包的空壳对话日志",
        "",
    ])
    if index["conversation_exclusions"]:
        for item in index["conversation_exclusions"]:
            lines.append(f"- 项目 `{item.get('project_id')}` · 任务 `{item.get('thread_id')}` · `{item.get('original_session_log')}`：{item.get('reason')}")
    else:
        lines.append("- 无")
    lines.extend([
        "",
        "## 云端托管项目引用",
        "",
    ])
    if index["cloud_managed_projects"]:
        for item in index["cloud_managed_projects"]:
            lines.append(f"- `{item.get('project_id')}`：{item.get('label')}（账号云端核验，不上传本地归档）")
    else:
        lines.append("- 无")
    lines.extend([
        "",
        "## 对话截止清单",
        "",
    ])
    for item in projects:
        lines.append(f"### {item['label']}")
        lines.append("")
        for conversation in item["conversations"]:
            source = f"{conversation.get('source_instance_id') or 'legacy-unknown'} / {conversation.get('source_disposition') or 'unknown'}"
            lines.append(f"- `{conversation['thread_id']}` · {source} · {conversation['thread_title']} · 截止 `{conversation['cutoff_timestamp']}` · `{conversation['conversation_sha256']}`")
        if not item["conversations"]:
            lines.append("- 无对话")
        lines.append("")
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return {
        "index": str(Path(args.output).resolve()),
        "handoff": str(handoff.resolve()),
        "index_sha256": index["index_sha256"],
        "project_count": sum(item["has_project_payload"] for item in projects),
        "conversation_only_group_count": sum(not item["has_project_payload"] for item in projects),
        "conversation_count": sum(item["conversation_count"] for item in projects),
        "reused_project_count": sum(bool(item["project_release"] and item["project_release"]["reused"]) for item in projects),
        "reused_conversation_pack_count": sum(item["conversation_release"]["reused"] for item in projects),
    }


def command_verify_index(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.index)
    raw = path.read_bytes()
    payload = json.loads(raw)
    if payload.get("format") not in INDEX_FORMATS or not isinstance(payload.get("projects"), list):
        raise SyncError("unsupported incremental device index")
    is_v2 = payload.get("format") == INDEX_FORMAT_V2
    claimed = payload.pop("index_sha256", None)
    actual = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    if claimed != actual or raw != canonical_bytes({**payload, "index_sha256": claimed}):
        raise SyncError("device index hash or canonical encoding is invalid")
    for field in ("migration_id", "source_device", "source_computer"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise SyncError(f"device index {field} is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,127}", payload["migration_id"]):
        raise SyncError("device index migration_id is invalid")
    if not SHA256_RE.fullmatch(str(payload.get("content_catalog_sha256") or "")):
        raise SyncError("device index content_catalog_sha256 is invalid")
    for field in ("cloud_managed_projects", "conversation_exclusions"):
        if not isinstance(payload.get(field), list):
            raise SyncError(f"device index {field} must be an array")
    for position, exclusion in enumerate(payload["conversation_exclusions"]):
        if not isinstance(exclusion, dict) or not str(exclusion.get("thread_id") or "").strip() or not str(exclusion.get("reason") or "").strip():
            raise SyncError(f"device index conversation exclusion {position} is invalid")
        if not str(exclusion.get("original_session_log") or exclusion.get("session_log") or "").strip():
            raise SyncError(f"device index conversation exclusion {position} lacks session provenance")
    if is_v2:
        for field in ("instances", "exclusions"):
            if not isinstance(payload.get(field), list):
                raise SyncError(f"v2 device index {field} must be an array")
        if not SHA256_RE.fullmatch(str(payload.get("instance_registry_sha256") or "")):
            raise SyncError("v2 device index instance_registry_sha256 is invalid")
    seen: set[str] = set()
    seen_threads: set[str] = set()
    version_ok = payload.get("required_skill_version") == REQUIRED_SKILL_VERSION if is_v2 else bool(re.fullmatch(r"\d{8}-v\d+", str(payload.get("required_skill_version") or "")))
    if payload.get("repository") != CANONICAL_REPOSITORY or not version_ok:
        raise SyncError("device index repository or required skill version mismatch")
    validate_release(payload.get("skill_release"), "skill_release")
    for project in payload["projects"]:
        project_id = str(project.get("project_id") or "")
        if not project_id or project_id.casefold() in seen:
            raise SyncError("device index contains a missing or duplicate project ID")
        seen.add(project_id.casefold())
        if not isinstance(project.get("label"), str) or not project["label"].strip():
            raise SyncError(f"device index project {project_id} lacks label")
        has_project_payload = project.get("has_project_payload", True)
        if not isinstance(has_project_payload, bool):
            raise SyncError(f"device index project {project_id} has invalid has_project_payload")
        if has_project_payload:
            if not isinstance(project.get("source_path"), str) or not project["source_path"].strip():
                raise SyncError(f"device index project {project_id} lacks source_path")
            if not SHA256_RE.fullmatch(str(project.get("tree_sha256") or "")):
                raise SyncError(f"device index project {project_id} has invalid tree_sha256")
            validate_release(project.get("project_release"), f"{project_id}.project_release")
        elif project.get("source_path") is not None or project.get("tree_sha256") is not None or project.get("project_release") is not None:
            raise SyncError(f"conversation-only project {project_id} carries project payload fields")
        if not SHA256_RE.fullmatch(str(project.get("conversation_set_sha256") or "")):
            raise SyncError(f"device index project {project_id} has invalid conversation_set_sha256")
        for field in ("entry_count", "file_count", "total_bytes", "conversation_count"):
            if not isinstance(project.get(field), int) or isinstance(project.get(field), bool) or project[field] < 0:
                raise SyncError(f"device index project {project_id} has invalid {field}")
        if project["file_count"] > project["entry_count"]:
            raise SyncError(f"device index project {project_id} file_count exceeds entry_count")
        expected_generation = CONVERSATION_PACK_GENERATION_V2 if is_v2 else CONVERSATION_PACK_GENERATION
        if project.get("conversation_pack_generation") != expected_generation:
            raise SyncError(f"device index project {project_id} conversation generator mismatch")
        for field in ("excluded_relative_paths", "exclusion_records", "conversations"):
            if not isinstance(project.get(field), list):
                raise SyncError(f"device index project {project_id} {field} must be an array")
        if project["conversation_count"] != len(project["conversations"]):
            raise SyncError(f"device index project {project_id} conversation_count mismatch")
        conversations = []
        local_threads: set[str] = set()
        for position, conversation in enumerate(project["conversations"]):
            if not isinstance(conversation, dict):
                raise SyncError(f"device index project {project_id} conversation {position} is invalid")
            thread_id = str(conversation.get("thread_id") or "")
            key = thread_id.casefold()
            if not thread_id or key in local_threads or key in seen_threads:
                raise SyncError(f"device index contains a missing or duplicate thread ID: {thread_id!r}")
            local_threads.add(key)
            seen_threads.add(key)
            if not isinstance(conversation.get("thread_title"), str) or not conversation["thread_title"].strip():
                raise SyncError(f"device index conversation {thread_id} lacks thread_title")
            if is_v2:
                if not SHA256_RE.fullmatch(str(conversation.get("lineage_id") or "")):
                    raise SyncError(f"device index conversation {thread_id} lacks lineage_id")
                if not str(conversation.get("branch_id") or "").strip() or not str(conversation.get("source_instance_id") or "").strip():
                    raise SyncError(f"device index conversation {thread_id} lacks branch or source instance")
                if conversation.get("source_disposition") not in {"active", "archived"}:
                    raise SyncError(f"device index conversation {thread_id} has invalid source disposition")
            for field in ("conversation_sha256", "messages_sha256"):
                if not SHA256_RE.fullmatch(str(conversation.get(field) or "")):
                    raise SyncError(f"device index conversation {thread_id} has invalid {field}")
            for field in ("message_count", "attachment_count"):
                if not isinstance(conversation.get(field), int) or isinstance(conversation.get(field), bool) or conversation[field] < 0:
                    raise SyncError(f"device index conversation {thread_id} has invalid {field}")
            cutoff = conversation.get("cutoff_timestamp")
            if cutoff is not None and not isinstance(cutoff, str):
                raise SyncError(f"device index conversation {thread_id} has invalid cutoff_timestamp")
            conversations.append(
                portable_identity().set_row(conversation)
                if is_v2 else {"thread_id": thread_id, "conversation_sha256": conversation["conversation_sha256"]}
            )
        expected_set = hashlib.sha256(canonical_bytes({"generator": expected_generation, "conversations": conversations})).hexdigest()
        if expected_set != project["conversation_set_sha256"]:
            raise SyncError(f"device index project {project_id} conversation_set_sha256 mismatch")
        validate_release(project.get("conversation_release"), f"{project_id}.conversation_release")
    return {
        "index": str(path.resolve()), "index_sha256": claimed,
        "project_count": sum(item.get("has_project_payload", True) for item in payload["projects"]),
        "conversation_only_group_count": sum(not item.get("has_project_payload", True) for item in payload["projects"]),
        "verified": True,
    }


def command_manifest_fingerprint(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.manifest)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("format") != "split-tar-pax-v2" or not isinstance(manifest.get("entries"), list):
        raise SyncError("unsupported project backup manifest")
    source_name = str(manifest.get("source_name") or "")
    if not source_name:
        raise SyncError("project backup manifest is missing source_name")
    normalized = []
    prefix = source_name + "/"
    for item in manifest["entries"]:
        archive_path = str(item.get("path") or "")
        if archive_path == source_name:
            relative = "."
        elif archive_path.startswith(prefix):
            relative = archive_path[len(prefix):]
        else:
            raise SyncError(f"manifest entry is outside source root: {archive_path}")
        raw_type = str(item.get("type"))
        if raw_type == "5":
            normalized.append({"path": relative, "type": "dir", "mtime": round(float(item["mtime"]), 6)})
        elif raw_type in {"0", "\x00"}:
            normalized.append({
                "path": relative,
                "type": "file",
                "size": int(item["size"]),
                "mtime": round(float(item["mtime"]), 6),
                "sha256": str(item["sha256"]).lower(),
            })
        else:
            raise SyncError(f"unsupported manifest entry type: {raw_type}")
    normalized.sort(key=lambda item: item["path"].casefold())
    identity = {
        "excluded_relative_paths": sorted((str(item) for item in manifest.get("excluded_source_paths") or []), key=str.casefold),
        "entries": normalized,
    }
    tree_sha256 = hashlib.sha256(canonical_bytes(identity)).hexdigest()
    if args.expected and tree_sha256 != args.expected.lower():
        raise SyncError(f"project manifest tree fingerprint mismatch: expected {args.expected.lower()}, got {tree_sha256}")
    return {
        "manifest": str(path.resolve()),
        "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "tree_sha256": tree_sha256,
        "entry_count": len(normalized),
        "verified": True,
    }


def command_verify(args: argparse.Namespace) -> dict[str, Any]:
    catalog_path = Path(args.catalog)
    raw = catalog_path.read_bytes()
    payload = load_catalog(catalog_path)
    claimed = payload.pop("catalog_sha256", None)
    actual = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    if claimed != actual or raw != canonical_bytes({**payload, "catalog_sha256": claimed}):
        raise SyncError("catalog hash or canonical encoding is invalid")
    for project in payload["projects"]:
        if project.get("has_project_payload", True) and not SHA256_RE.fullmatch(str(project.get("tree_sha256") or "")):
            raise SyncError(f"invalid tree_sha256 for {project.get('project_id')}")
        if not project.get("has_project_payload", True) and project.get("tree_sha256") is not None:
            raise SyncError(f"conversation-only group carries tree_sha256: {project.get('project_id')}")
        if not SHA256_RE.fullmatch(str(project.get("conversation_set_sha256") or "")):
            raise SyncError(f"invalid conversation_set_sha256 for {project.get('project_id')}")
    return {"catalog": str(catalog_path.resolve()), "catalog_sha256": claimed, "verified": True}


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan")
    scan.add_argument("--discovery", required=True)
    scan.add_argument("--output", required=True)
    scan.add_argument("--checkpoint-dir")
    scan.add_argument("--status-file")
    scan.add_argument("--branch-state", help="machine-local branch registry; caller must hold upload lock")
    scan.set_defaults(handler=command_scan)
    plan = sub.add_parser("plan")
    plan.add_argument("--current", required=True)
    plan.add_argument("--previous")
    plan.add_argument("--output", required=True)
    plan.set_defaults(handler=command_plan)
    verify_plan = sub.add_parser("verify-plan")
    verify_plan.add_argument("--plan", required=True)
    verify_plan.add_argument("--catalog", required=True)
    verify_plan.set_defaults(handler=command_verify_plan)
    verify = sub.add_parser("verify")
    verify.add_argument("--catalog", required=True)
    verify.set_defaults(handler=command_verify)
    build_index = sub.add_parser("build-index")
    build_index.add_argument("--catalog", required=True)
    build_index.add_argument("--bindings", required=True)
    build_index.add_argument("--output", required=True)
    build_index.add_argument("--handoff", required=True)
    build_index.set_defaults(handler=command_build_index)
    verify_index = sub.add_parser("verify-index")
    verify_index.add_argument("--index", required=True)
    verify_index.set_defaults(handler=command_verify_index)
    manifest_fingerprint = sub.add_parser("manifest-fingerprint")
    manifest_fingerprint.add_argument("--manifest", required=True)
    manifest_fingerprint.add_argument("--expected")
    manifest_fingerprint.set_defaults(handler=command_manifest_fingerprint)
    args = parser.parse_args()
    try:
        print(json.dumps(args.handler(args), ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, KeyError, json.JSONDecodeError, SyncError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
