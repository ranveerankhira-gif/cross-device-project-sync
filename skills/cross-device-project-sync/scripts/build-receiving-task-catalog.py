#!/usr/bin/env python3
"""Build a path-bound, filtered catalog for restored Codex receiving tasks."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
from typing import Any


FORMAT_V1 = "cross-device-receiving-task-catalog-v1"
FORMAT_V2 = "cross-device-receiving-task-catalog-v2"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def conversation_fingerprint(
    thread_dir: Path, thread_id: str, generator: str,
    lineage_id: str | None = None, branch_id: str | None = None,
) -> tuple[str, str]:
    transcript = thread_dir / "FULL_TRANSCRIPT.jsonl"
    messages_sha256 = sha256_file(transcript)
    if lineage_id:
        spec = importlib.util.spec_from_file_location("catalog_identity", Path(__file__).with_name("conversation_identity.py"))
        portable = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(portable)
        return portable.content_hash(lineage_id, branch_id, portable.flatten(thread_dir), generator), messages_sha256
    attachments = []
    attachment_root = thread_dir / "attachments"
    if attachment_root.is_dir():
        for path in sorted(
            (item for item in attachment_root.rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(thread_dir).as_posix().casefold(),
        ):
            attachments.append({
                "path": path.relative_to(thread_dir).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            })
    identity = {
        "generator": generator,
        **(
            {"lineage_id": lineage_id, "branch_id": branch_id or "main"}
            if lineage_id else {"thread_id": thread_id}
        ),
        "messages_sha256": messages_sha256,
        "attachments": attachments,
        "inherited_histories": [],
    }
    history_root = thread_dir / "history"
    if history_root.is_dir():
        for root in sorted((item for item in history_root.iterdir() if item.is_dir()), key=lambda item: item.name):
            files = [
                {"path": path.relative_to(root).as_posix(), "size": path.stat().st_size, "sha256": sha256_file(path)}
                for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.relative_to(root).as_posix().casefold())
            ]
            tree_sha256 = hashlib.sha256(canonical_json(files)).hexdigest()
            if root.name != tree_sha256:
                raise ValueError(f"Inherited history directory identity mismatch: {root}")
            identity["inherited_histories"].append({
                "tree_sha256": tree_sha256,
                "file_count": len(files),
                "total_bytes": sum(item["size"] for item in files),
            })
    return hashlib.sha256(canonical_json(identity)).hexdigest(), messages_sha256


def normalized_path(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("Project map path must be a non-empty absolute path.")
    path = Path(text)
    if not path.is_absolute():
        raise ValueError("Project map path must be absolute.")
    return os.path.normcase(str(path.resolve()))


def load_injected_classifier(script_dir: Path):
    sys.path.insert(0, str(script_dir))
    exporter = script_dir / "export-conversation-handoff.py"
    spec = importlib.util.spec_from_file_location("cross_device_conversation_exporter", exporter)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load conversation exporter: {exporter}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.is_injected_user_record


def transcript_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(path.read_bytes().split(b"\n"), 1):
        if not raw_line:
            continue
        try:
            record = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid transcript JSONL at {path}:{line_number}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"Transcript record is not an object at {path}:{line_number}")
        records.append(record)
    return records


def validate_pack_manifest(pack_root: Path, project: dict[str, Any]) -> dict[str, Any]:
    manifest_value = str(project.get("conversation_pack_manifest_path") or "").strip()
    candidates = [Path(manifest_value)] if manifest_value else [
        pack_root / "CONVERSATION_PACK_MANIFEST.json",
        pack_root.parent / "CONVERSATION_PACK_MANIFEST.json",
    ]
    manifest_paths = [path for path in candidates if path.is_file()]
    if len(manifest_paths) != 1:
        raise ValueError(f"Expected exactly one conversation pack manifest for {pack_root}; found {len(manifest_paths)}")
    manifest_path = manifest_paths[0]
    expected_manifest_hash = str(project.get("conversation_manifest_sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_hash) or sha256_file(manifest_path) != expected_manifest_hash:
        raise ValueError(f"Conversation pack manifest hash mismatch: {manifest_path}")
    manifest = read_json(manifest_path)
    if manifest.get("format") != "cross-device-project-sync-conversation-pack-v1":
        raise ValueError(f"Unsupported conversation pack manifest: {manifest_path}")
    records = manifest.get("files")
    if not isinstance(records, list):
        raise ValueError("Conversation pack manifest files must be an array")
    expected = {str(record.get("path") or ""): record for record in records if isinstance(record, dict)}
    if len(expected) != len(records) or "" in expected:
        raise ValueError("Conversation pack manifest has missing or duplicate file paths")
    actual = {
        path.relative_to(pack_root).as_posix(): path
        for path in pack_root.rglob("*") if path.is_file() and path.name != "CONVERSATION_PACK_MANIFEST.json"
    }
    if set(actual) != set(expected):
        raise ValueError("Extracted conversation pack inventory does not match its manifest")
    for name, path in actual.items():
        record = expected[name]
        if path.stat().st_size != record.get("size") or sha256_file(path) != record.get("sha256"):
            raise ValueError(f"Extracted conversation pack file hash mismatch: {name}")
    conversations = manifest.get("conversations")
    if not isinstance(conversations, list) or manifest.get("conversation_count") != len(conversations):
        raise ValueError("Conversation pack manifest conversation inventory is invalid")
    return manifest


def text_parts(record: dict[str, Any]) -> list[str]:
    return [
        str(part.get("text") or "")
        for part in record.get("content") or []
        if isinstance(part, dict) and part.get("type") == "text"
    ]


def attachment_count(records: list[dict[str, Any]]) -> int:
    return sum(
        1
        for record in records
        for part in record.get("content") or []
        if isinstance(part, dict) and part.get("type") == "image"
    )


def extract_title(path: Path, thread_id: str) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"(?m)^Original task title:\s*(.+?)\s*$", text)
    title = match.group(1).strip() if match else ""
    return title if title and title != "(not recorded)" else thread_id


def display_title(original_title: str, genuine_users: list[dict[str, Any]]) -> str:
    generic = (
        original_title.startswith("# Files mentioned by the user:"),
        original_title.startswith("## Referenced ChatGPT conversation:"),
        original_title.startswith("The following is the Codex agent history"),
    )
    candidate = original_title
    if any(generic):
        first_text = "\n".join(text_parts(genuine_users[0])).strip()
        request = re.search(r"(?ims)^## My request(?: for Codex)?:\s*\n?\s*(.+)$", first_text)
        if request:
            candidate = next((line.strip() for line in request.group(1).splitlines() if line.strip()), candidate)
        else:
            referenced = re.search(r'"title"\s*:\s*"([^"]+)"', first_text)
            if referenced:
                candidate = referenced.group(1).strip()
    candidate = re.sub(r"^[#>*\s]+", "", candidate).strip()
    return candidate[:80] or original_title[:80]


def operation_key(
    migration_id: str, project: dict[str, Any], thread_id: str,
    lineage_id: str | None = None, branch_id: str | None = None,
) -> str:
    if lineage_id:
        return hashlib.sha256(canonical_json({
            "format": FORMAT_V2,
            "lineage_id": lineage_id,
            "branch_id": branch_id or "main",
        })).hexdigest()
    identity = {
        "format": FORMAT_V1,
        "migration_id": migration_id,
        "project_manifest_sha256": project["project_manifest_sha256"],
        "restored_project_root": normalized_path(project["restored_project_root"]),
        "original_thread_id": thread_id,
    }
    return hashlib.sha256(canonical_json(identity)).hexdigest()


def parse_existing(groups: list[tuple[str, list[str]]]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for state, values in groups:
        for value in values:
            original, separator, receiving = value.partition("=")
            if not separator or not original.strip() or not receiving.strip():
                raise ValueError("Receiving-task mappings must be ORIGINAL_THREAD_ID=RECEIVING_THREAD_ID")
            current = result.get(original)
            candidate = {"receiving_thread_id": receiving, "status": state}
            if current is not None and current != candidate:
                raise ValueError(f"Conflicting receiving tasks for original thread {original}")
            result[original] = candidate
    return result


def build(args: argparse.Namespace) -> dict[str, Any]:
    report = read_json(args.restore_report)
    project_map_data = read_json(args.project_map)
    existing = parse_existing([
        ("verified-existing", args.existing_receiving_task),
        ("context-loaded", args.context_loaded_receiving_task),
        ("registered-lazy", args.lazy_receiving_task),
    ])
    classify_injected = load_injected_classifier(Path(__file__).resolve().parent)

    mapped: dict[str, dict[str, Any]] = {}
    for item in project_map_data.get("projects") or []:
        key = normalized_path(str(item.get("path") or ""))
        if not key or key in mapped:
            raise ValueError("Project map contains an empty or duplicate normalized path.")
        mapped[key] = item

    migration_id = str(report.get("migration_id") or "").strip()
    projects = report.get("projects") or []
    is_v2 = report.get("format") == "cross-device-project-sync-incremental-receive-report-v2"
    expected_groups = report.get("content_group_count", report.get("project_count"))
    if not migration_id or len(projects) != int(expected_groups if expected_groups is not None else -1):
        raise ValueError("Restore report migration identity or project count is invalid.")

    catalog_projects: list[dict[str, Any]] = []
    excluded_sessions: list[dict[str, Any]] = []
    receiving_tasks: list[dict[str, Any]] = []
    seen_threads: set[tuple[str, str]] = set()
    projectless_directories = {project["project_id"]: project.get("restored_project_root") for project in projects if project.get("kind") == "projectless-root"}

    for project in projects:
        has_project_payload = project.get("has_project_payload", True)
        is_project_task = has_project_payload and project.get("kind") not in {"projectless-root", "projectless-conversation-group"}
        restored_value = str(project.get("restored_project_root") or "").strip()
        restored_root = Path(restored_value) if restored_value else None
        pack_root = Path(project["conversation_pack_path"])
        if (has_project_payload and (restored_root is None or not restored_root.is_dir())) or not pack_root.is_dir():
            raise ValueError(f"Restored project or conversation pack is missing: {restored_root}")
        pack_manifest = validate_pack_manifest(pack_root, project)
        mapped_project = mapped.get(normalized_path(str(restored_root))) if restored_root else None
        if is_project_task and mapped_project is None:
            raise ValueError(f"No exact registered project path match for {restored_root}")

        user_tasks: list[dict[str, Any]] = []
        threads_root = pack_root / "threads"
        thread_dirs = (
            sorted((path for path in threads_root.iterdir() if path.is_dir()), key=lambda path: path.name)
            if threads_root.is_dir()
            else []
        )
        manifest_threads = {str(item.get("thread_id") or ""): item for item in pack_manifest["conversations"] if isinstance(item, dict)}
        if len(manifest_threads) != len(pack_manifest["conversations"]) or set(manifest_threads) != {path.name for path in thread_dirs}:
            raise ValueError(f"Extracted conversation thread inventory does not match its manifest: {pack_root}")
        for thread_dir in thread_dirs:
            thread_id = thread_dir.name
            transcript = thread_dir / "FULL_TRANSCRIPT.jsonl"
            handoff = thread_dir / "DETAILED_HANDOFF.md"
            start = thread_dir / "RECEIVER_START.md"
            index = thread_dir / "CONVERSATION_INDEX.json"
            for required in (transcript, handoff, start, index):
                if not required.is_file():
                    raise ValueError(f"Conversation thread is missing {required.name}: {thread_dir}")
            records = transcript_records(transcript)
            genuine_users = [
                record for record in records
                if record.get("role") == "user" and not classify_injected(text_parts(record))
            ]
            if not genuine_users:
                excluded_sessions.append({
                    "project_index": project["index"],
                    "project_id": mapped_project["project_id"] if mapped_project else None,
                    "original_thread_id": thread_id,
                    "reason": "internal-runtime-session",
                    "conversation_path": str(thread_dir),
                })
                continue

            identity = (normalized_path(str(restored_root)) if restored_root else f"projectless:{project['project_id']}", thread_id)
            if identity in seen_threads:
                raise ValueError(f"Duplicate project/thread identity: {restored_root} / {thread_id}")
            seen_threads.add(identity)
            original_title = extract_title(handoff, thread_id)
            generator = str(pack_manifest.get("generator") or "")
            if not generator:
                raise ValueError("Conversation pack manifest is missing generator identity")
            manifest_thread = manifest_threads[thread_id]
            lineage_id = str(manifest_thread.get("lineage_id") or "").strip() or None
            branch_id = str(manifest_thread.get("branch_id") or "main")
            conversation_sha256, messages_sha256 = conversation_fingerprint(
                thread_dir, thread_id, generator, lineage_id, branch_id,
            )
            for field, actual_value in (
                ("conversation_sha256", conversation_sha256),
                ("messages_sha256", messages_sha256),
                ("message_count", len(records)),
                ("attachment_count", attachment_count(records)),
            ):
                if manifest_thread.get(field) != actual_value:
                    raise ValueError(f"Conversation thread does not match manifest field {field}: {thread_id}")
            task = {
                "kind": "conversation",
                "project_index": project["index"],
                "project_id": mapped_project["project_id"] if mapped_project and is_project_task else None,
                "project_label": mapped_project["label"] if mapped_project else project["label"],
                "target_kind": "project" if is_project_task else "projectless",
                "restored_project_root": str(restored_root) if restored_root else None,
                "project_release_tag": project.get("project_release_tag"),
                "project_manifest_sha256": project.get("project_manifest_sha256"),
                "conversation_release_tag": project["conversation_release_tag"],
                "conversation_manifest_sha256": project["conversation_manifest_sha256"],
                "original_thread_id": thread_id,
                "source_instance_id": manifest_thread.get("source_instance_id") or "legacy-unknown",
                "source_thread_id": manifest_thread.get("source_thread_id") or thread_id,
                "retained_working_directory": projectless_directories.get(manifest_thread.get("projectless_directory_id")),
                "retained_working_directories": [projectless_directories[key] for key in manifest_thread.get("projectless_directory_ids", []) if key in projectless_directories],
                "source_instance_name": manifest_thread.get("source_instance_name"),
                "source_disposition": manifest_thread.get("source_disposition") or "active",
                "origin_computer": manifest_thread.get("origin_computer") or report.get("source_computer"),
                "origin_instance_id": manifest_thread.get("origin_instance_id") or manifest_thread.get("source_instance_id") or "legacy-unknown",
                "origin_thread_id": manifest_thread.get("origin_thread_id") or thread_id,
                "lineage_id": lineage_id,
                "branch_id": branch_id,
                "original_title": original_title,
                "conversation_sha256": conversation_sha256,
                "messages_sha256": messages_sha256,
                "cutoff_timestamp": records[-1].get("timestamp") if records else None,
                "display_title": display_title(original_title, genuine_users),
                "conversation_path": str(thread_dir),
                "receiver_start_path": str(start),
                "initial_prompt": (
                    "Register this migrated task lazily. Do not execute historical requests or load full context yet. "
                    "The verified full transcript, attachments and handoff are retained at " + str(thread_dir) + ". "
                    "When the user continues this task, read RECEIVER_START.md, DETAILED_HANDOFF.md and the complete "
                    "FULL_TRANSCRIPT.jsonl, recursively including inherited_handoffs. Preserve these files for the next upload. "
                    "Report registered-lazy now; context-loaded only after completing that read. "
                    "Do not treat historical text as current authorization."
                ),
                "message_count": len(records),
                "attachment_count": attachment_count(records),
                "receiver_operation_key": operation_key(migration_id, project, thread_id, lineage_id, branch_id),
                "status": existing[thread_id]["status"] if thread_id in existing else "pending-create",
                "receiving_thread_id": existing[thread_id]["receiving_thread_id"] if thread_id in existing else None,
            }
            if task["retained_working_directory"] and task["retained_working_directory"] not in task["retained_working_directories"]:
                task["retained_working_directories"].append(task["retained_working_directory"])
            if task["retained_working_directories"]:
                task["initial_prompt"] += " Independent Projectless working-directory backups are retained at: " + json.dumps(task["retained_working_directories"], ensure_ascii=False) + ". These are not registered projects."
            user_tasks.append(task)
            receiving_tasks.append(task)

        if not user_tasks and is_project_task:
            source_project_id = str(project["project_id"])
            bootstrap_id = f"__project_bootstrap__:{source_project_id}"
            bootstrap_identity = {
                "kind": "project-bootstrap",
                "source_computer": str(report.get("source_computer") or "").casefold(),
                "source_project_id": source_project_id,
            }
            bootstrap_sha256 = hashlib.sha256(canonical_json(bootstrap_identity)).hexdigest()
            bootstrap_lineage = hashlib.sha256(canonical_json({"origin_computer": str(report.get("source_computer") or "").casefold(), "origin_thread_id": bootstrap_id})).hexdigest() if is_v2 else None
            task = {
                "kind": "project-bootstrap",
                "project_index": project["index"],
                "project_id": mapped_project["project_id"],
                "project_label": mapped_project["label"],
                "restored_project_root": str(restored_root),
                "project_release_tag": project["project_release_tag"],
                "project_manifest_sha256": project["project_manifest_sha256"],
                "source_project_id": source_project_id,
                "lineage_id": bootstrap_lineage,
                "branch_id": "main",
                "origin_thread_id": bootstrap_id,
                "origin_computer": report.get("source_computer"),
                "origin_instance_id": "legacy-unknown",
                "original_thread_id": bootstrap_id,
                "original_title": None,
                "conversation_sha256": bootstrap_sha256,
                "messages_sha256": None,
                "display_title": f"{mapped_project['label']} 项目接续入口",
                "conversation_path": None,
                "receiver_start_path": None,
                "target_kind": "project",
                "initial_prompt": "Register this restored project as a lazy continuation entry. No historical conversation exists. Do not start project work until the user asks.",
                "message_count": 0,
                "attachment_count": 0,
                "receiver_operation_key": operation_key(migration_id, project, bootstrap_id, bootstrap_lineage, "main"),
                "status": existing[bootstrap_id]["status"] if bootstrap_id in existing else "pending-create",
                "receiving_thread_id": existing[bootstrap_id]["receiving_thread_id"] if bootstrap_id in existing else None,
            }
            receiving_tasks.append(task)

        catalog_projects.append({
            "index": project["index"],
            "source_project_id": project["project_id"],
            "kind": project.get("kind") or "registered-project",
            "has_project_payload": has_project_payload,
            "registered_project_id": mapped_project["project_id"] if mapped_project else None,
            "label": mapped_project["label"] if mapped_project else project["label"],
            "restored_project_root": str(restored_root) if restored_root else None,
            "raw_session_count": len(thread_dirs),
            "user_visible_task_count": len(user_tasks),
            "excluded_internal_session_count": len(thread_dirs) - len(user_tasks),
            "requires_bootstrap": not user_tasks and is_project_task,
        })

    pending = sum(task["status"] == "pending-create" for task in receiving_tasks)
    completed = len(receiving_tasks) - pending
    context_loaded = sum(task["status"] == "context-loaded" for task in receiving_tasks)
    registered_lazy = sum(task["status"] == "registered-lazy" for task in receiving_tasks)
    catalog = {
        "format": FORMAT_V2 if is_v2 else FORMAT_V1,
        "migration_id": migration_id,
        "source_device": report.get("source_device"),
        "source_computer": report.get("source_computer"),
        "restore_root": report.get("restore_root"),
        "project_count": len(catalog_projects),
        "raw_session_count": sum(project["raw_session_count"] for project in catalog_projects),
        "user_visible_conversation_count": sum(task["kind"] == "conversation" for task in receiving_tasks),
        "excluded_internal_session_count": len(excluded_sessions),
        "bootstrap_task_count": sum(task["kind"] == "project-bootstrap" for task in receiving_tasks),
        "target_receiving_task_count": len(receiving_tasks),
        "verified_existing_task_count": completed,
        "context_loaded_task_count": context_loaded,
        "registered_lazy_task_count": registered_lazy,
        "pending_task_count": pending,
        "projects": catalog_projects,
        "receiving_tasks": receiving_tasks,
        "excluded_sessions": excluded_sessions,
    }
    catalog["catalog_sha256"] = hashlib.sha256(canonical_json(catalog)).hexdigest()
    return catalog


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--restore-report", type=Path, required=True)
    parser.add_argument("--project-map", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--existing-receiving-task", action="append", default=[])
    parser.add_argument("--context-loaded-receiving-task", action="append", default=[])
    parser.add_argument("--lazy-receiving-task", action="append", default=[])
    args = parser.parse_args()
    try:
        catalog = build(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_bytes(canonical_json(catalog))
        temporary.replace(args.output)
        print(json.dumps({
            "output": str(args.output.resolve()),
            "project_count": catalog["project_count"],
            "raw_session_count": catalog["raw_session_count"],
            "user_visible_conversation_count": catalog["user_visible_conversation_count"],
            "excluded_internal_session_count": catalog["excluded_internal_session_count"],
            "bootstrap_task_count": catalog["bootstrap_task_count"],
            "target_receiving_task_count": catalog["target_receiving_task_count"],
            "verified_existing_task_count": catalog["verified_existing_task_count"],
            "context_loaded_task_count": catalog["context_loaded_task_count"],
            "registered_lazy_task_count": catalog["registered_lazy_task_count"],
            "pending_task_count": catalog["pending_task_count"],
        }, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
