#!/usr/bin/env python3
"""Track, resume, build, and verify a whole-device project migration."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import ntpath
import os
import pathlib
import re
import sys
from typing import Any


FINAL_FORMAT = "cross-device-project-sync-device-migration-v2"
STATE_FORMAT = "cross-device-project-sync-device-migration-state-v1"
LEGACY_FORMAT = "cross-device-project-sync-device-migration-v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
LOCAL_DISPOSITIONS = {
    "pending",
    "completed",
    "failed",
    "excluded-broad-root",
    "excluded-inaccessible",
    "excluded-duplicate-alias",
}
EXCLUDED_DISPOSITIONS = {
    "excluded-broad-root",
    "excluded-inaccessible",
    "excluded-duplicate-alias",
}
PROJECT_FIELDS = (
    "project_id",
    "label",
    "source_path",
    "source_device",
    "source_computer",
    "project_manifest_format",
    "release_tag",
    "project_manifest_sha256",
    "snapshot_created_utc",
    "conversation",
)
CONVERSATION_FIELDS = (
    "thread_id",
    "thread_title",
    "cutoff_timestamp",
    "conversation_manifest_sha256",
)


class ManifestError(ValueError):
    pass


def require_text(value: Any, field: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{field} must be a non-empty string")
    text = value.strip()
    if len(text) > maximum or any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise ManifestError(f"{field} contains unsupported characters or is too long")
    return text


def require_sha256(value: Any, field: str) -> str:
    text = require_text(value, field, maximum=64).lower()
    if not SHA256_RE.fullmatch(text):
        raise ManifestError(f"{field} must be 64 lowercase hexadecimal characters")
    return text


def require_timestamp(value: Any, field: str, *, utc: bool = False) -> str:
    text = require_text(value, field, maximum=64)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ManifestError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ManifestError(f"{field} must include a timezone")
    if utc and parsed.utcoffset() != dt.timedelta(0):
        raise ManifestError(f"{field} must use UTC")
    return text


def normalize_windows_path(value: Any, field: str) -> tuple[str, str]:
    text = require_text(value, field, maximum=32766).replace("/", "\\")
    drive, tail = ntpath.splitdrive(text)
    if not drive or not tail.startswith("\\"):
        raise ManifestError(f"{field} must be an absolute Windows path")
    normalized = ntpath.normpath(text)
    if normalized in (".", "\\"):
        raise ManifestError(f"{field} must identify a project directory")
    return normalized, normalized.casefold()


def validate_conversation(value: Any, prefix: str) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ManifestError(f"{prefix} must be null or an object")
    missing = [field for field in CONVERSATION_FIELDS if field not in value]
    if missing:
        raise ManifestError(f"{prefix} is missing: {', '.join(missing)}")
    return {
        "thread_id": require_text(value["thread_id"], f"{prefix}.thread_id"),
        "thread_title": require_text(value["thread_title"], f"{prefix}.thread_title"),
        "cutoff_timestamp": require_timestamp(value["cutoff_timestamp"], f"{prefix}.cutoff_timestamp"),
        "conversation_manifest_sha256": require_sha256(
            value["conversation_manifest_sha256"], f"{prefix}.conversation_manifest_sha256"
        ),
    }


def validate_identity(payload: dict[str, Any]) -> tuple[str, str, str, str]:
    migration_id = require_text(payload.get("migration_id"), "migration_id", maximum=128)
    if not ID_RE.fullmatch(migration_id):
        raise ManifestError("migration_id contains unsupported characters")
    source_device = require_text(payload.get("source_device"), "source_device", maximum=80)
    source_computer = require_text(payload.get("source_computer"), "source_computer", maximum=80)
    created_utc = require_timestamp(payload.get("created_utc"), "created_utc", utc=True)
    return migration_id, source_device, source_computer, created_utc


def canonicalize_inventory(value: Any, *, final: bool) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ManifestError("inventory must contain every discovered project")
    seen_ids: set[str] = set()
    path_groups: dict[str, list[dict[str, Any]]] = {}
    inventory: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        prefix = f"inventory[{index}]"
        if not isinstance(item, dict):
            raise ManifestError(f"{prefix} must be an object")
        project_id = require_text(item.get("project_id"), f"{prefix}.project_id")
        key = project_id.casefold()
        if key in seen_ids:
            raise ManifestError(f"duplicate project_id: {project_id}")
        seen_ids.add(key)
        label = require_text(item.get("label"), f"{prefix}.label")
        project_kind = require_text(item.get("project_kind"), f"{prefix}.project_kind", maximum=16)
        disposition = require_text(item.get("disposition"), f"{prefix}.disposition", maximum=64)
        reason_value = item.get("reason")
        canonical_project_id = item.get("canonical_project_id")

        if project_kind == "chatgpt":
            if disposition != "cloud-managed":
                raise ManifestError(f"{prefix} ChatGPT projects must use disposition cloud-managed")
            if item.get("source_path") is not None:
                raise ManifestError(f"{prefix}.source_path must be null for a ChatGPT project")
            if reason_value is not None or canonical_project_id is not None:
                raise ManifestError(f"{prefix} cloud-managed entries cannot have exclusion metadata")
            normalized = {
                "project_id": project_id,
                "label": label,
                "project_kind": "chatgpt",
                "source_path": None,
                "disposition": "cloud-managed",
                "reason": None,
                "canonical_project_id": None,
            }
        elif project_kind == "local":
            if disposition not in LOCAL_DISPOSITIONS:
                raise ManifestError(f"{prefix} has an unsupported local disposition")
            if final and disposition in {"pending", "failed"}:
                raise ManifestError(f"{prefix} is not complete")
            source_path, path_key = normalize_windows_path(item.get("source_path"), f"{prefix}.source_path")
            reason = None
            canonical = None
            if disposition in EXCLUDED_DISPOSITIONS or disposition == "failed":
                reason = require_text(reason_value, f"{prefix}.reason", maximum=2000)
            elif reason_value is not None:
                raise ManifestError(f"{prefix}.reason must be null unless the entry is failed or excluded")
            if disposition == "excluded-duplicate-alias":
                canonical = require_text(canonical_project_id, f"{prefix}.canonical_project_id")
            elif canonical_project_id is not None:
                raise ManifestError(f"{prefix}.canonical_project_id is only valid for duplicate aliases")
            normalized = {
                "project_id": project_id,
                "label": label,
                "project_kind": "local",
                "source_path": source_path,
                "disposition": disposition,
                "reason": reason,
                "canonical_project_id": canonical,
            }
            path_groups.setdefault(path_key, []).append(normalized)
        else:
            raise ManifestError(f"{prefix}.project_kind must be local or chatgpt")
        inventory.append(normalized)

    for entries in path_groups.values():
        duplicate_entries = [item for item in entries if item["disposition"] == "excluded-duplicate-alias"]
        primary_entries = [item for item in entries if item["disposition"] != "excluded-duplicate-alias"]
        if len(entries) == 1 and duplicate_entries:
            raise ManifestError("a duplicate alias must share its source_path with one canonical project")
        if len(entries) > 1:
            if len(primary_entries) != 1 or len(duplicate_entries) != len(entries) - 1:
                raise ManifestError("duplicate source_path entries require one canonical project and duplicate aliases")
            primary_id = primary_entries[0]["project_id"]
            for duplicate in duplicate_entries:
                if duplicate["canonical_project_id"].casefold() != primary_id.casefold():
                    raise ManifestError("duplicate alias canonical_project_id does not identify its canonical project")

    inventory.sort(
        key=lambda item: (
            item["project_kind"],
            (item["source_path"] or "").casefold(),
            item["project_id"].casefold(),
        )
    )
    return inventory


def canonicalize_project(
    item: Any,
    prefix: str,
    *,
    source_device: str,
    source_computer: str,
) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ManifestError(f"{prefix} must be an object")
    missing = [field for field in PROJECT_FIELDS if field not in item]
    if missing:
        raise ManifestError(f"{prefix} is missing: {', '.join(missing)}")
    project_id = require_text(item["project_id"], f"{prefix}.project_id")
    label = require_text(item["label"], f"{prefix}.label")
    source_path, _ = normalize_windows_path(item["source_path"], f"{prefix}.source_path")
    project_device = require_text(item["source_device"], f"{prefix}.source_device", maximum=80)
    project_computer = require_text(item["source_computer"], f"{prefix}.source_computer", maximum=80)
    if project_device != source_device or project_computer.casefold() != source_computer.casefold():
        raise ManifestError(f"{prefix} source identity does not match the migration")
    if item["project_manifest_format"] != "split-tar-pax-v2":
        raise ManifestError(f"{prefix}.project_manifest_format must be split-tar-pax-v2")
    return {
        "project_id": project_id,
        "label": label,
        "source_path": source_path,
        "source_device": project_device,
        "source_computer": project_computer,
        "project_manifest_format": "split-tar-pax-v2",
        "release_tag": require_text(item["release_tag"], f"{prefix}.release_tag"),
        "project_manifest_sha256": require_sha256(
            item["project_manifest_sha256"], f"{prefix}.project_manifest_sha256"
        ),
        "snapshot_created_utc": require_timestamp(
            item["snapshot_created_utc"], f"{prefix}.snapshot_created_utc", utc=True
        ),
        "conversation": validate_conversation(item["conversation"], f"{prefix}.conversation"),
    }


def canonicalize_projects(
    value: Any,
    *,
    inventory: list[dict[str, Any]],
    source_device: str,
    source_computer: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ManifestError("projects must be an array")
    inventory_by_id = {item["project_id"].casefold(): item for item in inventory}
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    seen_tags: set[str] = set()
    projects = []
    for index, item in enumerate(value):
        project = canonicalize_project(
            item,
            f"projects[{index}]",
            source_device=source_device,
            source_computer=source_computer,
        )
        project_id_key = project["project_id"].casefold()
        _, path_key = normalize_windows_path(project["source_path"], f"projects[{index}].source_path")
        tag_key = project["release_tag"].casefold()
        if project_id_key in seen_ids:
            raise ManifestError(f"duplicate completed project_id: {project['project_id']}")
        if path_key in seen_paths:
            raise ManifestError(f"duplicate completed source_path: {project['source_path']}")
        if tag_key in seen_tags:
            raise ManifestError(f"duplicate release_tag: {project['release_tag']}")
        inventory_item = inventory_by_id.get(project_id_key)
        if inventory_item is None or inventory_item["disposition"] != "completed":
            raise ManifestError(f"completed project is not marked completed in inventory: {project['project_id']}")
        if inventory_item["label"] != project["label"] or inventory_item["source_path"].casefold() != project["source_path"].casefold():
            raise ManifestError(f"completed project identity does not match inventory: {project['project_id']}")
        seen_ids.add(project_id_key)
        seen_paths.add(path_key)
        seen_tags.add(tag_key)
        projects.append(project)

    completed_ids = {
        item["project_id"].casefold() for item in inventory if item["disposition"] == "completed"
    }
    if seen_ids != completed_ids:
        raise ManifestError("completed inventory and project results do not match")
    projects.sort(key=lambda item: item["source_path"].casefold())
    return projects


def counts(inventory: list[dict[str, Any]], projects: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "discovered_project_count": len(inventory),
        "selected_local_project_count": sum(
            item["disposition"] in {"pending", "completed", "failed"} for item in inventory
        ),
        "completed_project_count": len(projects),
        "failed_project_count": sum(item["disposition"] == "failed" for item in inventory),
        "excluded_project_count": sum(item["disposition"] in EXCLUDED_DISPOSITIONS for item in inventory),
        "cloud_project_count": sum(item["disposition"] == "cloud-managed" for item in inventory),
    }


def derive_state_status(inventory: list[dict[str, Any]]) -> str:
    if any(item["disposition"] == "failed" for item in inventory):
        return "failed"
    if any(item["disposition"] == "pending" for item in inventory):
        return "in-progress"
    return "ready-to-finalize"


def canonicalize_state(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("format") != STATE_FORMAT:
        raise ManifestError(f"state format must be {STATE_FORMAT}")
    migration_id, source_device, source_computer, created_utc = validate_identity(payload)
    inventory = canonicalize_inventory(payload.get("inventory"), final=False)
    projects = canonicalize_projects(
        payload.get("projects"),
        inventory=inventory,
        source_device=source_device,
        source_computer=source_computer,
    )
    status = derive_state_status(inventory)
    if payload.get("status") != status:
        raise ManifestError("state status does not match project dispositions")
    return {
        "format": STATE_FORMAT,
        "status": status,
        "migration_id": migration_id,
        "source_device": source_device,
        "source_computer": source_computer,
        "created_utc": created_utc,
        **counts(inventory, projects),
        "inventory": inventory,
        "projects": projects,
    }


def canonicalize_final(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("format") != FINAL_FORMAT:
        raise ManifestError(f"format must be {FINAL_FORMAT}")
    if payload.get("status") != "complete":
        raise ManifestError("status must be complete")
    migration_id, source_device, source_computer, created_utc = validate_identity(payload)
    inventory = canonicalize_inventory(payload.get("inventory"), final=True)
    projects = canonicalize_projects(
        payload.get("projects"),
        inventory=inventory,
        source_device=source_device,
        source_computer=source_computer,
    )
    expected_counts = counts(inventory, projects)
    for field, value in expected_counts.items():
        if payload.get(field) != value:
            raise ManifestError(f"{field} does not match the inventory")
    if expected_counts["selected_local_project_count"] != expected_counts["completed_project_count"]:
        raise ManifestError("not every selected local project is complete")
    return {
        "format": FINAL_FORMAT,
        "status": "complete",
        "migration_id": migration_id,
        "source_device": source_device,
        "source_computer": source_computer,
        "created_utc": created_utc,
        **expected_counts,
        "inventory": inventory,
        "projects": projects,
    }


def canonicalize_legacy(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("format") != LEGACY_FORMAT or payload.get("status") != "complete":
        raise ManifestError(f"legacy format must be {LEGACY_FORMAT} with complete status")
    migration_id, source_device, source_computer, created_utc = validate_identity(payload)
    value = payload.get("projects")
    if not isinstance(value, list) or not value:
        raise ManifestError("legacy projects must be a non-empty array")
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    seen_tags: set[str] = set()
    projects = []
    for index, item in enumerate(value):
        prefix = f"projects[{index}]"
        if not isinstance(item, dict):
            raise ManifestError(f"{prefix} must be an object")
        project_id = require_text(item.get("project_id"), f"{prefix}.project_id")
        label = require_text(item.get("label"), f"{prefix}.label")
        source_path, path_key = normalize_windows_path(item.get("source_path"), f"{prefix}.source_path")
        release_tag = require_text(item.get("release_tag"), f"{prefix}.release_tag")
        if project_id.casefold() in seen_ids or path_key in seen_paths or release_tag.casefold() in seen_tags:
            raise ManifestError("legacy manifest contains a duplicate project identity")
        seen_ids.add(project_id.casefold())
        seen_paths.add(path_key)
        seen_tags.add(release_tag.casefold())
        projects.append(
            {
                "project_id": project_id,
                "label": label,
                "source_path": source_path,
                "release_tag": release_tag,
                "project_manifest_sha256": require_sha256(
                    item.get("project_manifest_sha256"), f"{prefix}.project_manifest_sha256"
                ),
                "snapshot_created_utc": require_timestamp(
                    item.get("snapshot_created_utc"), f"{prefix}.snapshot_created_utc"
                ),
                "conversation": validate_conversation(item.get("conversation"), f"{prefix}.conversation"),
            }
        )
    projects.sort(key=lambda item: item["source_path"].casefold())
    if payload.get("project_count") != len(projects):
        raise ManifestError("legacy project_count does not match projects length")
    return {
        "format": LEGACY_FORMAT,
        "status": "complete",
        "migration_id": migration_id,
        "source_device": source_device,
        "source_computer": source_computer,
        "created_utc": created_utc,
        "project_count": len(projects),
        "projects": projects,
    }


def canonical_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def load_json(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read JSON {path}: {exc}") from exc


def write_new(path: pathlib.Path, value: dict[str, Any]) -> None:
    if path.exists():
        raise ManifestError(f"refusing to overwrite existing output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(canonical_bytes(value))
        handle.flush()
        os.fsync(handle.fileno())


def replace_state(path: pathlib.Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("xb") as handle:
            handle.write(canonical_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_state(path: pathlib.Path) -> dict[str, Any]:
    raw = path.read_bytes()
    state = canonicalize_state(json.loads(raw.decode("utf-8")))
    if raw != canonical_bytes(state):
        raise ManifestError("migration state is not in canonical UTF-8 JSON form")
    return state


def result_summary(value: dict[str, Any], state: str) -> dict[str, Any]:
    return {
        "migration_id": value["migration_id"],
        "state": state,
        "status": value["status"],
        "discovered_project_count": value["discovered_project_count"],
        "selected_local_project_count": value["selected_local_project_count"],
        "completed_project_count": value["completed_project_count"],
        "failed_project_count": value["failed_project_count"],
        "excluded_project_count": value["excluded_project_count"],
        "cloud_project_count": value["cloud_project_count"],
    }


def command_init(args: argparse.Namespace) -> int:
    payload = load_json(pathlib.Path(args.input).resolve())
    if not isinstance(payload, dict):
        raise ManifestError("discovery input must be a JSON object")
    migration_id, source_device, source_computer, created_utc = validate_identity(payload)
    inventory = canonicalize_inventory(payload.get("inventory"), final=False)
    if any(item["disposition"] in {"completed", "failed"} for item in inventory):
        raise ManifestError("initial discovery may contain pending, excluded, and cloud-managed entries only")
    state = {
        "format": STATE_FORMAT,
        "status": derive_state_status(inventory),
        "migration_id": migration_id,
        "source_device": source_device,
        "source_computer": source_computer,
        "created_utc": created_utc,
        **counts(inventory, []),
        "inventory": inventory,
        "projects": [],
    }
    state = canonicalize_state(state)
    output = pathlib.Path(args.output).resolve()
    write_new(output, state)
    summary = result_summary(state, "initialized")
    summary["state_path"] = str(output)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


def command_record_success(args: argparse.Namespace) -> int:
    path = pathlib.Path(args.state).resolve()
    state = load_state(path)
    raw_result = load_json(pathlib.Path(args.result).resolve())
    project = canonicalize_project(
        raw_result,
        "result",
        source_device=state["source_device"],
        source_computer=state["source_computer"],
    )
    inventory_by_id = {item["project_id"].casefold(): item for item in state["inventory"]}
    target = inventory_by_id.get(project["project_id"].casefold())
    if target is None or target["project_kind"] != "local":
        raise ManifestError("result project_id is not a discovered local project")
    if target["disposition"] in EXCLUDED_DISPOSITIONS:
        raise ManifestError("an excluded project cannot be recorded as completed")
    if target["label"] != project["label"] or target["source_path"].casefold() != project["source_path"].casefold():
        raise ManifestError("result project identity does not match discovery")

    existing = [item for item in state["projects"] if item["project_id"].casefold() == project["project_id"].casefold()]
    if existing:
        if existing[0] != project or target["disposition"] != "completed":
            raise ManifestError("completed project result cannot be replaced with different data")
        summary = result_summary(state, "already-recorded")
        summary["state_path"] = str(path)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0

    target["disposition"] = "completed"
    target["reason"] = None
    state["projects"].append(project)
    state["status"] = derive_state_status(state["inventory"])
    state.update(counts(state["inventory"], state["projects"]))
    state = canonicalize_state(state)
    replace_state(path, state)
    summary = result_summary(state, "success-recorded")
    summary["state_path"] = str(path)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


def command_record_failure(args: argparse.Namespace) -> int:
    path = pathlib.Path(args.state).resolve()
    state = load_state(path)
    project_id = require_text(args.project_id, "project_id")
    error = require_text(args.error, "error", maximum=2000)
    target = next(
        (item for item in state["inventory"] if item["project_id"].casefold() == project_id.casefold()),
        None,
    )
    if target is None or target["project_kind"] != "local":
        raise ManifestError("project_id is not a discovered local project")
    if target["disposition"] == "completed":
        raise ManifestError("a completed project cannot be changed to failed")
    if target["disposition"] in EXCLUDED_DISPOSITIONS:
        raise ManifestError("an excluded project cannot be changed to failed")
    target["disposition"] = "failed"
    target["reason"] = error
    state["status"] = derive_state_status(state["inventory"])
    state.update(counts(state["inventory"], state["projects"]))
    state = canonicalize_state(state)
    replace_state(path, state)
    summary = result_summary(state, "failure-recorded")
    summary["state_path"] = str(path)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


def command_inspect(args: argparse.Namespace) -> int:
    path = pathlib.Path(args.state).resolve()
    state = load_state(path)
    pending = [item["project_id"] for item in state["inventory"] if item["disposition"] == "pending"]
    failed = [item["project_id"] for item in state["inventory"] if item["disposition"] == "failed"]
    summary = result_summary(state, "inspected")
    summary.update(
        {
            "state_path": str(path),
            "pending_project_ids": pending,
            "failed_project_ids": failed,
            "next_project_id": (failed + pending)[0] if failed or pending else None,
            "completed_release_tags": [item["release_tag"] for item in state["projects"]],
        }
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


def command_build(args: argparse.Namespace) -> int:
    state_path = pathlib.Path(args.state).resolve()
    state = load_state(state_path)
    if state["status"] != "ready-to-finalize":
        raise ManifestError("migration state still has pending or failed projects")
    manifest = {
        "format": FINAL_FORMAT,
        "status": "complete",
        "migration_id": state["migration_id"],
        "source_device": state["source_device"],
        "source_computer": state["source_computer"],
        "created_utc": state["created_utc"],
        **counts(state["inventory"], state["projects"]),
        "inventory": state["inventory"],
        "projects": state["projects"],
    }
    manifest = canonicalize_final(manifest)
    output = pathlib.Path(args.output).resolve()
    write_new(output, manifest)
    raw = canonical_bytes(manifest)
    result = {
        "manifest_path": str(output),
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "migration_id": manifest["migration_id"],
        "project_count": manifest["completed_project_count"],
        "discovered_project_count": manifest["discovered_project_count"],
        "state": "built-complete",
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def command_verify(args: argparse.Namespace) -> int:
    path = pathlib.Path(args.manifest).resolve()
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    if payload.get("format") == FINAL_FORMAT:
        manifest = canonicalize_final(payload)
        verification_state = "verified-complete"
        project_count = manifest["completed_project_count"]
        if raw != canonical_bytes(manifest):
            raise ManifestError("manifest is not in canonical UTF-8 JSON form")
    elif payload.get("format") == LEGACY_FORMAT:
        manifest = canonicalize_legacy(payload)
        verification_state = "verified-complete-legacy-inventory-unproven"
        project_count = manifest["project_count"]
    else:
        raise ManifestError("unsupported device migration manifest format")
    if args.expected_device and manifest["source_device"] != args.expected_device:
        raise ManifestError("source_device does not match expected device")
    if args.expected_computer and manifest["source_computer"].casefold() != args.expected_computer.casefold():
        raise ManifestError("source_computer does not match expected computer")
    result = {
        "manifest_path": str(path),
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "migration_id": manifest["migration_id"],
        "project_count": project_count,
        "release_tags": [project["release_tag"] for project in manifest["projects"]],
        "state": verification_state,
    }
    if manifest["format"] == FINAL_FORMAT:
        result.update(counts(manifest["inventory"], manifest["projects"]))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("init", help="create durable migration state from discovery")
    initialize.add_argument("--input", required=True)
    initialize.add_argument("--output", required=True)
    initialize.set_defaults(func=command_init)

    success = subparsers.add_parser("record-success", help="atomically record one verified project Release")
    success.add_argument("--state", required=True)
    success.add_argument("--result", required=True)
    success.set_defaults(func=command_record_success)

    failure = subparsers.add_parser("record-failure", help="atomically record one failed project")
    failure.add_argument("--state", required=True)
    failure.add_argument("--project-id", required=True)
    failure.add_argument("--error", required=True)
    failure.set_defaults(func=command_record_failure)

    inspect = subparsers.add_parser("inspect", help="show resumable migration progress")
    inspect.add_argument("--state", required=True)
    inspect.set_defaults(func=command_inspect)

    build = subparsers.add_parser("build", help="build a final index from ready migration state")
    build.add_argument("--state", required=True)
    build.add_argument("--output", required=True)
    build.set_defaults(func=command_build)

    verify = subparsers.add_parser("verify", help="verify a final device migration manifest")
    verify.add_argument("manifest")
    verify.add_argument("--expected-device")
    verify.add_argument("--expected-computer")
    verify.set_defaults(func=command_verify)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except (ManifestError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
