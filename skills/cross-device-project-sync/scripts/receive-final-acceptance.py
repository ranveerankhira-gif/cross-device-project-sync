#!/usr/bin/env python3
"""Fail-closed final acceptance for a caller-only whole-device receive."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any


FORMAT = "cross-device-project-sync-receive-acceptance-v2"
PREPARED_FORMAT = "cross-device-project-sync-receive-acceptance-prepared-v1"
RECEIPT_FORMAT = "cross-device-conversation-ledger-commit-receipt-v2"
READINESS_FORMAT = "cross-device-project-sync-device-readiness-v2"
CANONICAL_REPOSITORY = os.environ.get("CROSS_DEVICE_SYNC_REPOSITORY", "example-owner/example-private-backups").strip()
INDEX_FORMATS = {
    "cross-device-project-sync-incremental-device-index-v1",
    "cross-device-project-sync-incremental-device-index-v2",
}
CATALOG_FORMATS = {
    "cross-device-receiving-task-catalog-v1",
    "cross-device-receiving-task-catalog-v2",
}
PLAN_FORMAT = "cross-device-conversation-sync-plan-v3"
QUEUE_FORMAT = "cross-device-receive-queue-v2"
GOOD_QUEUE_STATES = {"registered-lazy", "context-loaded", "skipped", "retained-hidden"}
WRITE_ACTIONS = {
    "register-new", "update-existing", "apply-retained-update",
    "resolve-divergence-incoming", "resolve-divergence-retained",
}
NO_WRITE_STATUS = {
    "skip-unchanged": "skipped",
    "keep-hidden-already-downloaded": "skipped",
    "download-update-keep-hidden": "retained-hidden",
    "skip-resolved-divergence": "skipped",
}
TASK_IDENTITY_FIELDS = (
    "kind", "project_index", "project_id", "target_kind", "original_thread_id",
    "conversation_sha256", "messages_sha256", "lineage_id", "branch_id", "source_disposition",
)


class AcceptanceError(ValueError):
    pass


def canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def unsigned(value: dict[str, Any], field: str) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != field}


def load_local(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, Path(__file__).with_name(filename))
    if spec is None or spec.loader is None:
        raise AcceptanceError(f"cannot load required helper: {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def require_hash(value: dict[str, Any], field: str, expected_format: str | set[str], format_field: str = "format") -> None:
    formats = {expected_format} if isinstance(expected_format, str) else expected_format
    if value.get(format_field) not in formats:
        raise AcceptanceError(f"unsupported {field} format")
    claimed = str(value.get(field) or "").lower()
    if len(claimed) != 64 or claimed != digest(unsigned(value, field)):
        raise AcceptanceError(f"{field} does not match its content")


def same_path(left: Any, right: Any) -> bool:
    return os.path.normcase(str(Path(str(left)).resolve())) == os.path.normcase(str(Path(str(right)).resolve()))


def inside(path: Path, parent: Path) -> bool:
    path, parent = path.resolve(), parent.resolve()
    return path == parent or parent in path.parents


def project_map_by_path(value: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = value.get("projects")
    if not isinstance(rows, list):
        raise AcceptanceError("project map must contain a projects array")
    result = {}
    ids = set()
    for row in rows:
        if not isinstance(row, dict):
            raise AcceptanceError("project map row must be an object")
        raw_path = str(row.get("path") or "").strip()
        if not raw_path:
            raise AcceptanceError("project map contains an empty path")
        raw = Path(raw_path)
        if not raw.is_absolute():
            raise AcceptanceError("project map path must be absolute")
        resolved = raw.resolve()
        if not resolved.is_dir():
            raise AcceptanceError("project map path is missing or is not a directory")
        path = os.path.normcase(str(resolved))
        project_id = str(row.get("project_id") or "").strip()
        if not path or not project_id or path in result or project_id in ids:
            raise AcceptanceError("project map contains an empty or duplicate path/project ID")
        result[path] = row
        ids.add(project_id)
    return result


def require_distinct_paths(output: Path, paths: dict[str, Path]) -> None:
    output_key = os.path.normcase(str(output.resolve()))
    matches = [name for name, path in paths.items() if os.path.normcase(str(path.resolve())) == output_key]
    if matches:
        raise AcceptanceError("final acceptance output must be a new path distinct from every input: " + ", ".join(matches))
    if output.exists():
        try:
            existing = read_json(output)
        except Exception as exc:
            raise AcceptanceError("final acceptance output already exists and is not a recoverable report") from exc
        prepared_path = output.with_name(output.name + ".prepared")
        if existing.get("status") != "blocked" or not prepared_path.is_file():
            raise AcceptanceError("final acceptance output already exists; use a new path")


def task_identity(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row.get(field) for field in TASK_IDENTITY_FIELDS)


def require_receive_hash(receive: dict[str, Any]) -> None:
    claimed = str(receive.get("receive_report_sha256") or "").lower()
    if len(claimed) != 64 or claimed != digest(unsigned(receive, "receive_report_sha256")):
        raise AcceptanceError("receive report hash does not match its content")


def validate_readiness(
    receive: dict[str, Any], registry_path: Path, registry: dict[str, Any], caller: dict[str, Any],
    caller_home: Path, readiness_module: Any, packager: Any,
) -> tuple[dict[str, Any], str]:
    report_path = Path(str(receive.get("readiness_report_path") or "")).resolve()
    if not inside(report_path, Path(registry["machine_state_root"])):
        raise AcceptanceError("readiness report is outside machine_state_root")
    report = read_json(report_path)
    require_hash(report, "readiness_sha256", READINESS_FORMAT)
    if report.get("status") != "ready" or receive.get("readiness_sha256") != report["readiness_sha256"]:
        raise AcceptanceError("receive report is not bound to a ready device report")
    expected = {
        "registry_path": str(registry_path.resolve()),
        "registry_sha256": digest(registry_path.read_bytes()),
        "device_label": registry["device_label"],
        "computer_name": registry["computer_name"],
        "caller_codex_home": str(caller_home),
        "caller_instance_id": caller["instance_id"],
    }
    for field, value in expected.items():
        actual = report.get(field)
        if field in {"registry_path", "caller_codex_home"}:
            if not same_path(actual, value):
                raise AcceptanceError(f"readiness report {field} is stale or belongs elsewhere")
        elif actual != value:
            raise AcceptanceError(f"readiness report {field} is stale or mismatched")
    skill_root = Path(str(receive.get("installed_skill_root") or "")).resolve()
    if skill_root != readiness_module.canonical_public_skill_root():
        raise AcceptanceError("receive report does not use the canonical public Skill")
    records, tree_sha256, version = readiness_module.skill_inventory(skill_root, packager)
    del records
    if receive.get("installed_skill_tree_sha256") != tree_sha256 or receive.get("installed_skill_version") != version:
        raise AcceptanceError("public Skill changed after device readiness")
    skill_check = next((row for row in report.get("checks") or [] if row.get("name") == "public-skill-integrity"), None)
    detail = (skill_check or {}).get("detail") or {}
    if (skill_check or {}).get("status") != "passed" or detail.get("tree_sha256") != tree_sha256 or detail.get("version") != version:
        raise AcceptanceError("readiness report is not bound to the current public Skill tree")
    for row in registry["instances"]:
        config = read_json(Path(row["codex_home"]) / "cross-device-project-sync-device.json")
        if (
            set(config) != {"device_label", "computer_name"}
            or config.get("device_label") != registry["device_label"]
            or str(config.get("computer_name") or "").casefold() != str(registry["computer_name"]).casefold()
        ):
            raise AcceptanceError("an allow-listed instance device label changed after readiness")
    return report, tree_sha256


def validate_ledger_entry(
    plan: dict[str, Any], decision: dict[str, Any], queue_task: dict[str, Any],
    entry: dict[str, Any], committed_utc: str,
) -> None:
    def require_fields(expected: dict[str, Any]) -> None:
        for field, value in expected.items():
            if entry.get(field) != value:
                raise AcceptanceError(f"receiver ledger {field} does not match the exact plan")

    action = decision["action"]
    require_fields({
        "source_device": plan["source_device"],
        "source_computer": decision.get("origin_computer") or plan["source_computer"],
        "last_source_computer": plan["source_computer"],
        "original_thread_id": decision["original_thread_id"],
        "origin_computer": decision.get("origin_computer") or plan["source_computer"],
        "origin_instance_id": decision.get("origin_instance_id") or decision.get("source_instance_id") or "legacy-unknown",
        "origin_thread_id": decision.get("origin_thread_id") or decision["original_thread_id"],
        "origin_title": decision.get("thread_title"),
        "lineage_id": decision.get("lineage_id"),
        "branch_id": decision.get("branch_id") or "main",
        "kind": decision.get("kind") or "conversation",
        "receiver_disposition": decision.get("receiver_disposition"),
    })
    if action in {"download-update-keep-hidden", "keep-hidden-already-downloaded"}:
        require_fields({
            "pending_conversation_sha256": decision["conversation_sha256"],
            "pending_messages_sha256": decision.get("messages_sha256"),
            "pending_message_count": decision["message_count"],
            "pending_attachment_count": decision["attachment_count"],
            "pending_cutoff_timestamp": decision.get("cutoff_timestamp"),
        })
        if action == "download-update-keep-hidden":
            require_fields({"last_downloaded_utc": committed_utc})
        else:
            require_fields({"last_seen_utc": committed_utc})
    elif action in {"resolve-divergence-incoming", "resolve-divergence-retained"}:
        require_fields({
            "conversation_sha256": decision.get("apply_conversation_sha256"),
            "applied_state_version": 2,
            "messages_sha256": decision.get("apply_messages_sha256"),
            "message_count": decision.get("apply_message_count"),
            "attachment_count": decision.get("apply_attachment_count"),
            "cutoff_timestamp": decision.get("apply_cutoff_timestamp"),
            "last_applied_utc": committed_utc,
        })
        resolution_sha256 = decision.get("divergence_resolution_sha256")
        if entry.get("last_divergence_resolution_sha256") != resolution_sha256:
            raise AcceptanceError("receiver ledger is missing the exact divergence resolution receipt")
        histories = entry.get("divergent_histories")
        expected_history = (decision.get("divergent_history") or {}).get("conversation_sha256")
        if not isinstance(histories, list) or not any(
            isinstance(item, dict)
            and item.get("conversation_sha256") == expected_history
            and item.get("resolution_sha256") == resolution_sha256
            for item in histories
        ):
            raise AcceptanceError("receiver ledger is missing the non-selected divergent history")
        if action == "resolve-divergence-retained" and entry.get("suppressed_incoming_conversation_sha256") != decision.get("conversation_sha256"):
            raise AcceptanceError("receiver ledger did not preserve the non-selected incoming branch")
    elif action == "skip-resolved-divergence":
        if entry.get("suppressed_incoming_conversation_sha256") != decision.get("conversation_sha256"):
            raise AcceptanceError("receiver ledger resolved-divergence suppression hash mismatch")
        require_fields({"last_seen_utc": committed_utc})
    elif action == "preserve-pending-source-diverged":
        require_fields({"last_seen_utc": committed_utc})
    else:
        require_fields({
            "conversation_sha256": decision["conversation_sha256"],
            "applied_state_version": 2,
            "messages_sha256": decision.get("messages_sha256"),
            "message_count": decision["message_count"],
            "attachment_count": decision["attachment_count"],
            "cutoff_timestamp": decision.get("cutoff_timestamp"),
            ("last_seen_utc" if action == "skip-unchanged" else "last_applied_utc"): committed_utc,
        })
    if action not in {
        "download-update-keep-hidden", "keep-hidden-already-downloaded",
        "preserve-pending-source-diverged",
    }:
        pending_fields = (
            "pending_conversation_sha256", "pending_messages_sha256",
            "pending_message_count", "pending_attachment_count",
            "pending_cutoff_timestamp", "last_downloaded_utc",
        )
        present = [field for field in pending_fields if field in entry]
        if present:
            raise AcceptanceError(
                "receiver ledger applied entry retains impossible pending state: " + ", ".join(present)
            )
    expected_handoff_path = (
        decision.get("apply_conversation_path")
        if action in {"resolve-divergence-incoming", "resolve-divergence-retained"}
        else decision.get("conversation_path")
    )
    if (
        action not in {"preserve-pending-source-diverged", "skip-resolved-divergence"}
        and expected_handoff_path
        and entry.get("retained_handoff_path") != expected_handoff_path
    ):
        raise AcceptanceError("receiver ledger retained_handoff_path does not match the exact plan")
    planned_working_directories = decision.get("retained_working_directories") or []
    if (
        (action == "register-new" or planned_working_directories)
        and entry.get("retained_working_directories") != planned_working_directories
    ):
        raise AcceptanceError("receiver ledger retained_working_directories do not match the exact plan")
    receiving_id = queue_task.get("receiving_thread_id") or decision.get("receiving_thread_id")
    if entry.get("receiving_thread_id") != receiving_id:
        raise AcceptanceError("receiver ledger task ID does not match the queue")


def build_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    multi = load_local("multi-instance-sync.py", "acceptance_multi_instance")
    queue_module = load_local("receive-queue-engine.py", "acceptance_receive_queue")
    ledger_module = load_local("conversation-sync-ledger.py", "acceptance_ledger")
    receiver_module = load_local("device-index-receiver.py", "acceptance_device_receiver")
    readiness_module = load_local("device-readiness.py", "acceptance_readiness")
    packager = load_local("package-installed-skill.py", "acceptance_skill_packager")
    catalog_builder = load_local("build-receiving-task-catalog.py", "acceptance_catalog_builder")
    project_transaction = load_local("project-receive-transaction.py", "acceptance_project_transaction")
    app_proof_module = load_local("receiver-app-proof.py", "acceptance_app_proof")
    caller_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).resolve()
    output = Path(args.output).resolve()
    if not inside(output, caller_home):
        raise AcceptanceError("final acceptance report must be inside the calling CODEX_HOME")
    registry_path = Path(args.registry).resolve()
    registry, caller = multi.validate_registry(registry_path, str(caller_home), require_paths=True)

    paths = {
        "device_index": Path(args.device_index).resolve(),
        "receive_report": Path(args.receive_report).resolve(),
        "project_map": Path(args.project_map).resolve(),
        "catalog": Path(args.catalog).resolve(),
        "plan": Path(args.plan).resolve(),
        "queue": Path(args.queue).resolve(),
        "results": Path(args.results).resolve(),
        "app_proof": Path(args.app_proof).resolve(),
        "ledger": Path(args.ledger).resolve(),
        "commit_receipt": Path(args.commit_receipt).resolve(),
    }
    require_distinct_paths(output, paths)
    for name in ("project_map", "catalog", "plan", "queue", "results", "app_proof", "ledger", "commit_receipt"):
        if not inside(paths[name], caller_home):
            raise AcceptanceError(f"{name} must be inside the calling CODEX_HOME")

    index = read_json(paths["device_index"])
    receive = read_json(paths["receive_report"])
    project_map = read_json(paths["project_map"])
    catalog = read_json(paths["catalog"])
    plan = read_json(paths["plan"])
    queue = queue_module.load(paths["queue"], str(caller_home))
    results = read_json(paths["results"])
    app_proof = read_json(paths["app_proof"])
    ledger = ledger_module.load_ledger(paths["ledger"])
    receipt = read_json(paths["commit_receipt"])

    require_hash(index, "index_sha256", INDEX_FORMATS)
    require_hash(catalog, "catalog_sha256", CATALOG_FORMATS)
    require_hash(plan, "plan_sha256", PLAN_FORMAT)
    require_hash(receipt, "commit_receipt_sha256", RECEIPT_FORMAT)
    require_receive_hash(receive)
    if queue.get("format") != QUEUE_FORMAT:
        raise AcceptanceError("unsupported receive queue")

    readiness_report, current_skill_tree_sha256 = validate_readiness(
        receive, registry_path, registry, caller, caller_home, readiness_module, packager,
    )
    repository = str(receive.get("repository") or "")
    if repository != CANONICAL_REPOSITORY or index.get("repository") != CANONICAL_REPOSITORY:
        raise AcceptanceError("device index and receive report must use the canonical private repository")
    selection_path = Path(str(receive.get("selection_file") or "")).resolve()
    if not inside(selection_path, caller_home):
        raise AcceptanceError("selection file must belong to the calling account")
    selection = read_json(selection_path)
    selection_hash = str(selection.get("discovery_sha256") or "")
    if selection_hash != digest(unsigned(selection, "discovery_sha256")):
        raise AcceptanceError("selection catalog hash does not match its content")
    if receive.get("selection_discovery_sha256") != selection_hash or selection.get("repository") != CANONICAL_REPOSITORY or selection.get("release_inventory_complete") is not True:
        raise AcceptanceError("receive report is not bound to a complete canonical selection catalog")
    release_tag = str(receive.get("index_release_tag") or "")
    candidates = [
        row for row in selection.get("device_index_releases") or []
        if row.get("release_tag") == release_tag and row.get("verified") is True
    ]
    if len(candidates) != 1 or candidates[0].get("identity_sha256") != receive.get("selected_release_identity_sha256"):
        raise AcceptanceError("selected Release is not the exact verified user selection")
    bound_release = receiver_module.validate_bound_release(
        Path(str(receive.get("index_release_metadata_path") or "")).resolve(),
        Path(str(receive.get("index_assets_path") or "")).resolve(),
        CANONICAL_REPOSITORY, "device-index", expected_tag=release_tag,
    )
    discovered = receiver_module.discover_index(
        bound_release, Path(str(receive.get("index_assets_path") or "")).resolve(), CANONICAL_REPOSITORY,
    )
    if not same_path(discovered.get("index_path"), paths["device_index"]):
        raise AcceptanceError("final acceptance device index is not the selected Release asset")
    if bound_release.get("identity_sha256") != index.get("index_sha256") or candidates[0].get("identity_sha256") != index.get("index_sha256"):
        raise AcceptanceError("selected Release identity does not match the exact device index")
    if discovered.get("required_skill_version") != index.get("required_skill_version") or receive.get("required_skill_version") != index.get("required_skill_version"):
        raise AcceptanceError("required Skill version is not bound across Release, index, and receive report")
    lock_operation = str(receive.get("machine_lock_operation_id") or "")
    lock_token = str(receive.get("machine_lock_token") or "")
    lock_state = multi.require_lock_or_released(registry, lock_operation, lock_token, "download")
    lock = lock_state["lock"]
    if lock.get("caller_instance_id") != caller["instance_id"] or not same_path(lock.get("caller_codex_home"), caller_home):
        raise AcceptanceError("machine receive lock belongs to another account")
    if receive.get("machine_lock_token_sha256") != lock.get("lock_token_sha256"):
        raise AcceptanceError("receive report machine lock token binding is invalid")

    if not same_path(receive.get("destination_codex_home"), caller_home) or receive.get("destination_instance_id") != caller["instance_id"]:
        raise AcceptanceError("receive report belongs to another calling account")
    if not same_path(queue.get("destination_codex_home"), caller_home) or queue.get("destination_instance_id") != caller["instance_id"]:
        raise AcceptanceError("receive queue belongs to another calling account")
    if receive.get("status") != "restored-verified-not-registered":
        raise AcceptanceError("receive report is not a verified byte-restore report")
    for field in ("migration_id", "source_device", "source_computer", "index_sha256"):
        if receive.get(field) != index.get(field):
            raise AcceptanceError(f"receive report and device index disagree on {field}")
    if catalog.get("migration_id") != index.get("migration_id") or catalog.get("source_device") != index.get("source_device") or catalog.get("source_computer") != index.get("source_computer"):
        raise AcceptanceError("receiving catalog belongs to a different device snapshot")
    if plan.get("catalog_sha256") != catalog["catalog_sha256"] or queue.get("catalog_sha256") != catalog["catalog_sha256"]:
        raise AcceptanceError("catalog, plan, and queue hashes are not bound together")
    if queue.get("plan_sha256") != plan["plan_sha256"] or results.get("plan_sha256") != plan["plan_sha256"]:
        raise AcceptanceError("plan, queue, and ledger results hashes are not bound together")
    if (queue.get("divergence_resolution_sha256s") or []) != (plan.get("divergence_resolution_sha256s") or []):
        raise AcceptanceError("receive queue is not bound to the plan's exact divergence resolutions")

    index_projects = index.get("projects")
    receive_projects = receive.get("projects")
    catalog_projects = catalog.get("projects")
    if not all(isinstance(rows, list) for rows in (index_projects, receive_projects, catalog_projects)):
        raise AcceptanceError("project inventories must be arrays")
    if receive.get("content_group_count") != len(index_projects) or len(receive_projects) != len(index_projects) or len(catalog_projects) != len(index_projects):
        raise AcceptanceError("device index, restore report, and catalog group counts differ")
    by_source_id = {}
    source_positions = {}
    for position, row in enumerate(index_projects, 1):
        key = str(row.get("project_id") or "")
        if not key or key in by_source_id:
            raise AcceptanceError("device index has an empty or duplicate project ID")
        if row.get("conversation_count") != len(row.get("conversations") or []):
            raise AcceptanceError("device index conversation count does not match its inventory")
        by_source_id[key] = row
        source_positions[key] = position
    report_by_id = {str(row.get("project_id") or ""): row for row in receive_projects}
    catalog_by_id = {str(row.get("source_project_id") or ""): row for row in catalog_projects}
    catalog_by_index = {row.get("index"): row for row in catalog_projects}
    if len(report_by_id) != len(receive_projects) or len(catalog_by_id) != len(catalog_projects):
        raise AcceptanceError("restore report or catalog has duplicate project IDs")
    if set(report_by_id) != set(by_source_id) or set(catalog_by_id) != set(by_source_id):
        raise AcceptanceError("restored/catalog project IDs do not match the device index")
    if set(catalog_by_index) != set(range(1, len(index_projects) + 1)):
        raise AcceptanceError("catalog project indexes are incomplete or duplicated")

    mapped = project_map_by_path(project_map)
    project_rows = []
    for source_id, source in by_source_id.items():
        restored = report_by_id[source_id]
        catalog_project = catalog_by_id[source_id]
        kind = source.get("kind") or "registered-project"
        has_payload = source.get("has_project_payload", True)
        expected_index = source_positions[source_id]
        if (catalog_project.get("kind") or "registered-project") != kind or catalog_project.get("has_project_payload", True) != has_payload:
            raise AcceptanceError(f"catalog group {source_id} kind does not match the device index")
        if restored.get("index") != expected_index or catalog_project.get("index") != expected_index:
            raise AcceptanceError(f"group {source_id} index does not match the device index order")
        for left, right, label in (
            (restored.get("kind") or "registered-project", kind, "kind"),
            (restored.get("has_project_payload", True), has_payload, "payload kind"),
            (restored.get("project_release_tag"), (source.get("project_release") or {}).get("tag"), "project Release"),
            (restored.get("project_manifest_sha256"), (source.get("project_release") or {}).get("manifest_sha256"), "project manifest"),
            (restored.get("conversation_release_tag"), (source.get("conversation_release") or {}).get("tag"), "conversation Release"),
            (restored.get("conversation_manifest_sha256"), (source.get("conversation_release") or {}).get("manifest_sha256"), "conversation manifest"),
        ):
            if left != right:
                raise AcceptanceError(f"restored group {source_id} does not match index {label}")
        root_text = restored.get("restored_project_root")
        registered_id = catalog_project.get("registered_project_id")
        if bool(root_text) != bool(catalog_project.get("restored_project_root")) or (
            root_text and not same_path(root_text, catalog_project.get("restored_project_root"))
        ):
            raise AcceptanceError(f"catalog group {source_id} restored root does not match the receive report")
        pack_root = Path(str(restored.get("conversation_pack_path") or "")).resolve()
        try:
            catalog_builder.validate_pack_manifest(pack_root, restored)
        except Exception as exc:
            raise AcceptanceError(f"conversation pack changed after catalog creation for {source_id}: {exc}") from exc
        if has_payload:
            if not root_text:
                raise AcceptanceError(f"payload group {source_id} has no restored project root")
            root = Path(str(root_text)).resolve()
            if not root.is_dir():
                raise AcceptanceError(f"restored project root is missing for {source_id}")
            installation = restored.get("project_installation")
            if not isinstance(installation, dict) or installation.get("verified") is not True:
                raise AcceptanceError(f"restored project {source_id} lacks a verified installation receipt")
            if not same_path(installation.get("destination"), root):
                raise AcceptanceError(f"restored project {source_id} installation destination mismatch")
            installed_hash = str(installation.get("installed_tree_sha256") or "")
            if len(installed_hash) != 64 or project_transaction.fingerprint(root) != installed_hash:
                raise AcceptanceError(f"restored project {source_id} changed after verified installation")
        elif root_text is not None or restored.get("project_installation") is not None:
            raise AcceptanceError(f"conversation-only group {source_id} unexpectedly has project bytes")
        if kind == "registered-project" and has_payload:
            key = os.path.normcase(str(Path(str(root_text)).resolve()))
            current = mapped.get(key)
            if current is None or current.get("project_id") != registered_id or not registered_id:
                raise AcceptanceError("registered project lacks an exact current-account path mapping")
        else:
            if registered_id is not None:
                raise AcceptanceError("Projectless content was registered as a project")
            if root_text:
                key = os.path.normcase(str(Path(str(root_text)).resolve()))
                if key in mapped:
                    raise AcceptanceError("Projectless working directory appears in the project map")
        project_rows.append({
            "source_project_id": source_id, "kind": kind, "has_project_payload": has_payload,
            "restored_project_root": root_text, "registered_project_id": registered_id,
        })

    receiving_tasks = catalog.get("receiving_tasks")
    if not isinstance(receiving_tasks, list):
        raise AcceptanceError("receiving catalog tasks must be an array")
    if catalog.get("target_receiving_task_count") != len(receiving_tasks):
        raise AcceptanceError("catalog target task count does not match its task inventory")
    indexed_conversations = Counter()
    source_dispositions = Counter()
    catalog_conversation_total = 0
    for position, source in enumerate(index_projects, 1):
        expected_group = Counter()
        for row in source.get("conversations") or []:
            identity = (
                str(row.get("conversation_sha256") or ""), str(row.get("lineage_id") or ""),
                str(row.get("branch_id") or "main"), str(row.get("thread_id") or ""),
                str(row.get("source_disposition") or "active"),
            )
            expected_group[identity] += 1
            indexed_conversations[identity] += 1
            source_dispositions[str(row.get("source_disposition") or "active")] += 1
        actual_group = Counter(
            (
                str(row.get("conversation_sha256") or ""), str(row.get("lineage_id") or ""),
                str(row.get("branch_id") or "main"), str(row.get("original_thread_id") or ""),
                str(row.get("source_disposition") or "active"),
            )
            for row in receiving_tasks if row.get("kind") == "conversation" and row.get("project_index") == position
        )
        if expected_group != actual_group:
            raise AcceptanceError(f"catalog conversation inventory does not match device index group {source.get('project_id')}")
        catalog_conversation_total += sum(actual_group.values())
    if any(
        not isinstance(row.get("project_index"), int) or row.get("project_index") < 1 or row.get("project_index") > len(index_projects)
        for row in receiving_tasks
    ):
        raise AcceptanceError("catalog task has an invalid project_index")
    if catalog.get("user_visible_conversation_count") != sum(indexed_conversations.values()):
        raise AcceptanceError("catalog visible conversation count does not reconcile to the device index")
    if catalog_conversation_total != sum(indexed_conversations.values()):
        raise AcceptanceError("catalog per-group conversation counts do not reconcile")
    bootstrap_count = sum(row.get("kind") == "project-bootstrap" for row in receiving_tasks)
    if catalog.get("bootstrap_task_count") != bootstrap_count:
        raise AcceptanceError("catalog bootstrap count does not match its task inventory")

    for task in receiving_tasks:
        target_kind = task.get("target_kind") or ("project" if task.get("project_id") else "projectless")
        project = catalog_by_index[task["project_index"]]
        expected_project_id = project.get("registered_project_id") if target_kind == "project" else None
        if target_kind == "projectless" and task.get("project_id") is not None:
            raise AcceptanceError("Projectless task is bound to a fake project")
        if target_kind == "project" and not task.get("project_id"):
            raise AcceptanceError("project task is missing its exact project ID")
        if task.get("project_id") != expected_project_id:
            raise AcceptanceError("catalog task project binding does not match its source group")

    queue_tasks = queue.get("tasks") or []
    bad_states = Counter(str(row.get("status") or "missing") for row in queue_tasks if row.get("status") not in GOOD_QUEUE_STATES)
    if bad_states:
        raise AcceptanceError("receive queue is unfinished: " + json.dumps(bad_states, sort_keys=True))
    decisions = plan.get("decisions")
    if not isinstance(decisions, list):
        raise AcceptanceError("receive plan decisions are invalid")
    decisions_by_key = {str(row.get("source_key") or ""): row for row in decisions}
    queue_by_key = {str(row.get("operation_key") or ""): row for row in queue_tasks}
    proof_tasks = app_proof.get("tasks")
    if not isinstance(proof_tasks, list):
        raise AcceptanceError("live app proof task inventory is invalid")
    app_proof_by_key = {str(row.get("operation_key") or ""): row for row in proof_tasks}
    if len(app_proof_by_key) != len(proof_tasks):
        raise AcceptanceError("live app proof contains duplicate operation keys")
    catalog_by_key = {}
    for row in receiving_tasks:
        lineage = str(row.get("lineage_id") or "")
        if not lineage:
            raise AcceptanceError("v2 catalog task is missing lineage_id")
        key = ledger_module.lineage_source_key(lineage, str(row.get("branch_id") or "main"))
        if key in catalog_by_key:
            raise AcceptanceError("receiving catalog contains duplicate stable source identities")
        catalog_by_key[key] = row
    expected_task_count = int(catalog.get("target_receiving_task_count") or -1)
    if not (
        len(decisions_by_key) == len(decisions) == len(queue_by_key) == len(queue_tasks) == len(catalog_by_key) == expected_task_count
    ) or set(catalog_by_key) != set(decisions_by_key) or set(decisions_by_key) != set(queue_by_key):
        raise AcceptanceError("catalog, plan, and queue task inventories differ")
    receiving_ids = set()
    for key, catalog_task in catalog_by_key.items():
        decision = decisions_by_key[key]
        queue_task = queue_by_key[key]
        if task_identity(catalog_task) != task_identity(decision) or task_identity(catalog_task) != task_identity(queue_task):
            raise AcceptanceError("catalog, plan, and queue task identity fields differ")
        if queue_task.get("action") != decision.get("action"):
            raise AcceptanceError("queue action does not match the exact plan")
        expected_target_kind = catalog_task.get("target_kind") or ("project" if catalog_task.get("project_id") else "projectless")
        expected_target_id = catalog_task.get("project_id") if expected_target_kind == "project" else None
        if queue_task.get("target_kind") != expected_target_kind or queue_task.get("target_project_id") != expected_target_id:
            raise AcceptanceError("queue target binding does not match the catalog")
        if queue_task.get("expected_disposition") != decision.get("receiver_disposition"):
            raise AcceptanceError("queue archive disposition does not match the plan")
        marker_identity = {
            "source_key": key, "conversation_sha256": catalog_task.get("conversation_sha256"),
        }
        if decision.get("action") in {"resolve-divergence-incoming", "resolve-divergence-retained"}:
            marker_identity.update({
                "divergence_resolution_sha256": decision.get("divergence_resolution_sha256"),
                "apply_conversation_sha256": decision.get("apply_conversation_sha256"),
            })
        marker = "[cross-device-receipt:" + queue_module.digest(marker_identity) + "]"
        if queue_task.get("receipt_marker") != marker or not str(queue_task.get("initial_prompt") or "").startswith(marker + "\n"):
            raise AcceptanceError("queue receipt marker or lazy prompt is not deterministic")
        action = decision.get("action")
        status = queue_task.get("status")
        observation = queue_task.get("last_observation")
        if action in WRITE_ACTIONS:
            if status not in {"registered-lazy", "context-loaded"} or not isinstance(observation, dict):
                raise AcceptanceError("app-write queue item lacks a verified app observation")
            required_observation = {
                "status": status,
                "destination_codex_home": str(caller_home),
                "receiving_thread_id": queue_task.get("receiving_thread_id"),
                "receipt_marker": marker,
                "receiver_disposition": queue_task.get("expected_disposition"),
                "target_kind": expected_target_kind,
                "target_project_id": expected_target_id,
            }
            for field, expected in required_observation.items():
                actual = observation.get(field)
                if field == "destination_codex_home":
                    if not same_path(actual, expected):
                        raise AcceptanceError("app observation belongs to another account")
                elif actual != expected:
                    raise AcceptanceError(f"app observation {field} does not match the queue")
            if not queue_task.get("receiving_thread_id") or int(queue_task.get("attempts") or 0) < 1:
                raise AcceptanceError("app-write queue item has no real task ID or recorded attempt")
            if status == "context-loaded" and observation.get("full_context_verified") is not True:
                raise AcceptanceError("context-loaded queue item lacks complete-context verification")
            proof_task = app_proof_by_key.get(key)
            if not isinstance(proof_task, dict):
                raise AcceptanceError("app-write queue item is missing from the live app proof")
            expected_receipt_evidence = "sqlite-first-user-message" if action == "register-new" else "rollout-user-message"
            if proof_task.get("receipt_evidence_type") != expected_receipt_evidence:
                raise AcceptanceError("live app proof uses the wrong receipt evidence type")
            if action != "register-new" and not str(proof_task.get("rollout_sha256") or ""):
                raise AcceptanceError("updated receiving task lacks rollout evidence")
            if status == "context-loaded":
                evidence_sha256 = str(observation.get("context_evidence_report_sha256") or "").lower()
                try:
                    expected_context_marker = queue_module.context_load_marker(queue_task, evidence_sha256)
                except ValueError as exc:
                    raise AcceptanceError(str(exc)) from exc
                if observation.get("context_load_marker") != expected_context_marker:
                    raise AcceptanceError("context-loaded queue observation has no exact independent marker")
                if (
                    proof_task.get("context_evidence_type") != "rollout-user-message"
                    or proof_task.get("context_evidence_report_sha256") != evidence_sha256
                    or not same_path(
                        proof_task.get("context_evidence_report_path"),
                        observation.get("context_evidence_report_path"),
                    )
                    or proof_task.get("context_load_marker_sha256") != digest(expected_context_marker.encode("utf-8"))
                    or not str(proof_task.get("rollout_sha256") or "")
                ):
                    raise AcceptanceError("context-loaded task lacks independently verified rollout evidence")
            if action in {"resolve-divergence-incoming", "resolve-divergence-retained"}:
                resolution_path = Path(str(decision.get("divergence_resolution_path") or "")).resolve()
                if not inside(resolution_path, caller_home) or not resolution_path.is_file():
                    raise AcceptanceError("divergence resolution artifact is missing or belongs to another account")
                resolution = ledger_module.validate_resolution_artifact(read_json(resolution_path))
                if resolution.get("resolution_sha256") != decision.get("divergence_resolution_sha256"):
                    raise AcceptanceError("divergence resolution artifact changed after planning")
        elif action in NO_WRITE_STATUS:
            if status != NO_WRITE_STATUS[action] or observation is not None:
                raise AcceptanceError("no-write queue item has an impossible status or app observation")
        else:
            raise AcceptanceError("unsupported or divergent receive action cannot be accepted")
        receiving_id = str(queue_task.get("receiving_thread_id") or "")
        if receiving_id:
            if receiving_id in receiving_ids:
                raise AcceptanceError("one receiving task ID is owned by multiple lineages")
            receiving_ids.add(receiving_id)
    if set(app_proof_by_key) != {key for key, row in decisions_by_key.items() if row.get("action") in WRITE_ACTIONS}:
        raise AcceptanceError("live app proof inventory differs from the exact app-write plan")
    expected_results = queue_module.ledger_results(paths["queue"], str(caller_home))
    if canonical(expected_results) != canonical(results):
        raise AcceptanceError("ledger results do not come from the exact completed queue")
    ledger_module.validate_results_envelope(
        results, str(plan.get("plan_sha256") or ""), str(queue.get("queue_sha256") or ""),
    )
    app_proof_module.validate_proof(app_proof, paths["queue"], caller_home, verify_live=True)

    try:
        expected_receipt_summary = ledger_module.expected_commit_summary(plan, results)
    except ledger_module.LedgerError as exc:
        raise AcceptanceError(str(exc)) from exc
    actual_receipt_summary = {
        key: receipt.get(key) for key in ("updated", "skipped", "failed")
    }
    if (
        any(type(receipt.get(key)) is not int for key in expected_receipt_summary)
        or actual_receipt_summary != expected_receipt_summary
        or receipt.get("status") != ("committed" if expected_receipt_summary["failed"] == 0 else "incomplete")
    ):
        raise AcceptanceError("receiver ledger commit receipt summary does not match the exact plan and results")
    if expected_receipt_summary["failed"] != 0:
        raise AcceptanceError("receiver ledger commit is incomplete")
    committed_utc = str(receipt.get("committed_utc") or "")
    if not committed_utc or committed_utc != str(ledger.get("updated_utc") or ""):
        raise AcceptanceError("receiver ledger commit receipt timestamp does not match the committed ledger")
    receipt_bindings = {
        "destination_codex_home": str(caller_home),
        "destination_instance_id": caller["instance_id"],
        "source_device": plan["source_device"],
        "source_computer": plan["source_computer"],
        "plan_sha256": plan["plan_sha256"],
        "catalog_sha256": catalog["catalog_sha256"],
        "receiver_state_sha256": plan["receiver_state_sha256"],
        "results_sha256": results["results_sha256"],
        "queue_sha256": queue["queue_sha256"],
        "app_proof_sha256": app_proof["app_proof_sha256"],
        "ledger_sha256_before": plan["ledger_sha256"],
        "ledger_sha256_after": ledger_module.ledger_sha256(ledger),
    }
    for field, expected in receipt_bindings.items():
        actual = receipt.get(field)
        if field == "destination_codex_home":
            if not same_path(actual, expected):
                raise AcceptanceError("ledger commit receipt belongs to another account")
        elif actual != expected:
            raise AcceptanceError(f"ledger commit receipt {field} mismatch")
    entries = ledger.get("entries") or {}
    for key, decision in decisions_by_key.items():
        entry = entries.get(key)
        if not isinstance(entry, dict):
            raise AcceptanceError("receiver ledger is missing a planned lineage")
        validate_ledger_entry(plan, decision, queue_by_key[key], entry, committed_utc)

    state_counts = Counter(str(row["status"]) for row in queue_tasks)
    counts = {
        "content_groups": len(index_projects),
        "registered_projects": sum(row["kind"] == "registered-project" and row["has_project_payload"] for row in project_rows),
        "projectless_directories": sum(row["kind"] == "projectless-root" for row in project_rows),
        "conversation_only_groups": sum(not row["has_project_payload"] for row in project_rows),
        "user_visible_conversations": sum(indexed_conversations.values()),
        "bootstrap_tasks": sum(row.get("kind") == "project-bootstrap" for row in catalog.get("receiving_tasks") or []),
        "source_active": source_dispositions["active"],
        "source_archived": source_dispositions["archived"],
        "source_deleted": source_dispositions["deleted"],
        "registered_lazy": state_counts["registered-lazy"],
        "context_loaded": state_counts["context-loaded"],
        "skipped": state_counts["skipped"],
        "retained_hidden": state_counts["retained-hidden"],
        "excluded_internal_runtime": int(catalog.get("excluded_internal_session_count") or 0),
        "source_exclusions": len(index.get("exclusions") or []),
        "conversation_exclusions": len(index.get("conversation_exclusions") or []),
        "cloud_managed_references": len(index.get("cloud_managed_projects") or []),
        "pending": 0,
        "failed": 0,
    }
    report = {
        "format": FORMAT,
        "status": "receive-complete-verified",
        "accepted_utc": receipt.get("committed_utc"),
        "destination_codex_home": str(caller_home),
        "destination_instance_id": caller["instance_id"],
        "device_label": registry["device_label"],
        "computer_name": registry["computer_name"],
        "source_device": index["source_device"],
        "source_computer": index["source_computer"],
        "migration_id": index["migration_id"],
        "index_release_tag": receive.get("index_release_tag"),
        "index_sha256": index["index_sha256"],
        "repository": CANONICAL_REPOSITORY,
        "required_skill_version": index.get("required_skill_version"),
        "readiness_sha256": readiness_report["readiness_sha256"],
        "public_skill_tree_sha256": current_skill_tree_sha256,
        "input_hashes": {
            "receive_report_sha256": digest(paths["receive_report"].read_bytes()),
            "project_map_sha256": digest(paths["project_map"].read_bytes()),
            "catalog_sha256": catalog["catalog_sha256"],
            "plan_sha256": plan["plan_sha256"],
            "queue_sha256": queue["queue_sha256"],
            "results_sha256": results["results_sha256"],
            "app_proof_sha256": app_proof["app_proof_sha256"],
            "ledger_sha256": ledger_module.ledger_sha256(ledger),
            "commit_receipt_sha256": receipt["commit_receipt_sha256"],
        },
        "counts": counts,
        "projects": project_rows,
        "verification": {
            "caller_only": True,
            "exact_project_bindings": True,
            "projectless_not_registered": True,
            "queue_complete": True,
            "ledger_commit_bound": True,
            "index_inventory_reconciled": True,
            "release_selection_bound": True,
            "restored_trees_current": True,
            "app_observations_verified": True,
            "live_app_state_verified": True,
            "machine_lock_released": False,
        },
    }
    prepared = {
        "format": PREPARED_FORMAT,
        "status": "prepared-before-lock-release",
        "output": str(output),
        "machine_lock_operation_id": lock_operation,
        "machine_lock_token_sha256": lock.get("lock_token_sha256"),
        "report": report,
    }
    prepared["prepared_sha256"] = digest(prepared)
    prepared_path = output.with_name(output.name + ".prepared")
    if prepared_path.exists():
        existing_prepared = read_json(prepared_path)
        if canonical(existing_prepared) != canonical(prepared):
            raise AcceptanceError("existing prepared acceptance does not match the current verified inputs")
    else:
        multi.atomic_write_json(prepared_path, prepared)
    released = multi.release_lock(registry, lock_operation, lock_token)
    report["machine_lock_release"] = {
        "evidence_path": released["release_evidence_path"],
        "evidence_sha256": released["release_evidence_sha256"],
        "already_released": released["already_released"],
    }
    report["verification"]["machine_lock_released"] = True
    report["prepared_acceptance_path"] = str(prepared_path)
    report["prepared_acceptance_sha256"] = prepared["prepared_sha256"]
    report["acceptance_sha256"] = digest(report)
    multi.atomic_write_json(output, report)
    return report


def write_blocked(output: Path, error: Exception) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    value = {"format": FORMAT, "status": "blocked", "errors": [str(error)]}
    value["acceptance_sha256"] = digest(value)
    temporary = output.with_name(output.name + ".new")
    temporary.write_bytes(canonical(value))
    os.replace(temporary, output)


def validation_lock_is_still_held(args: argparse.Namespace, caller_home: Path) -> bool:
    """Blocked output is valid only while the exact download lock is held."""
    try:
        multi = load_local("multi-instance-sync.py", "acceptance_blocked_lock_check")
        registry, caller = multi.validate_registry(Path(args.registry).resolve(), str(caller_home), require_paths=True)
        receive = read_json(Path(args.receive_report).resolve())
        operation_id = str(receive.get("machine_lock_operation_id") or "")
        token = str(receive.get("machine_lock_token") or "")
        lock = multi.require_lock(registry, operation_id, token, "download")
        return (
            caller.get("instance_id") == lock.get("caller_instance_id")
            and same_path(lock.get("caller_codex_home"), caller_home)
        )
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", required=True)
    parser.add_argument("--device-index", required=True)
    parser.add_argument("--receive-report", required=True)
    parser.add_argument("--project-map", required=True)
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--results", required=True)
    parser.add_argument("--app-proof", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--commit-receipt", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        report = build_acceptance(args)
        print(json.dumps({
            "status": report["status"], "report": str(Path(args.output).resolve()),
            "acceptance_sha256": report["acceptance_sha256"], "counts": report["counts"],
        }, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        output = Path(args.output).resolve()
        caller = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).resolve()
        inputs = [
            Path(getattr(args, name)).resolve() for name in (
                "registry", "device_index", "receive_report", "project_map", "catalog", "plan",
                "queue", "results", "app_proof", "ledger", "commit_receipt",
            )
        ]
        aliases_input = any(same_path(output, path) for path in inputs)
        prepared_exists = output.with_name(output.name + ".prepared").is_file()
        if (
            inside(output, caller) and not output.exists() and not aliases_input
            and not prepared_exists and validation_lock_is_still_held(args, caller)
        ):
            write_blocked(output, exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
