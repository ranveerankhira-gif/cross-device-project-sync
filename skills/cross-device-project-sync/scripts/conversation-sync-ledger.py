#!/usr/bin/env python3
"""Plan and record idempotent cross-device conversation receiving.

The ledger is receiver-local.  It prevents an unchanged source conversation from
being registered again and preserves a user's local archived state.  Source
advancement is proved by conversation_sha256, never by title or timestamp alone.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import sys
from typing import Any

LEDGER_FORMAT_V1 = "cross-device-conversation-sync-ledger-v1"
LEDGER_FORMAT_V2 = "cross-device-conversation-sync-ledger-v2"
LEDGER_FORMATS = {LEDGER_FORMAT_V1, LEDGER_FORMAT_V2}
LEDGER_FORMAT = LEDGER_FORMAT_V2
PLAN_FORMAT = "cross-device-conversation-sync-plan-v3"
RESULTS_FORMAT = "cross-device-conversation-ledger-results-v2"
COMMIT_RECEIPT_FORMAT = "cross-device-conversation-ledger-commit-receipt-v2"
DIVERGENCE_RESOLUTION_FORMAT = "cross-device-conversation-divergence-resolution-v1"
RECEIVING_CATALOG_FORMAT_V1 = "cross-device-receiving-task-catalog-v1"
RECEIVING_CATALOG_FORMAT_V2 = "cross-device-receiving-task-catalog-v2"
RECEIVING_CATALOG_FORMATS = {RECEIVING_CATALOG_FORMAT_V1, RECEIVING_CATALOG_FORMAT_V2}
RECEIVING_CATALOG_FORMAT = RECEIVING_CATALOG_FORMAT_V1
RECEIVER_STATE_FORMAT_V1 = "cross-device-receiver-state-v1"
RECEIVER_STATE_FORMAT_V2 = "cross-device-receiver-state-v2"
RECEIVER_STATE_FORMATS = {RECEIVER_STATE_FORMAT_V1, RECEIVER_STATE_FORMAT_V2}
RECEIVER_STATE_FORMAT = RECEIVER_STATE_FORMAT_V1
SHA256_LEN = 64
APPLIED_STATE_VERSION = 2
LEDGER_LOCK_TIMEOUT_SECONDS = 30.0
RECEIVER_STATE_MAX_AGE_SECONDS = 3600.0
RECEIVER_STATE_MAX_FUTURE_SKEW_SECONDS = 300.0


class LedgerError(ValueError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=path.name + ".",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(canonical_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def load_local(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, Path(__file__).with_name(filename))
    if spec is None or spec.loader is None:
        raise LedgerError(f"cannot load required helper: {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_results_envelope(results: dict[str, Any], plan_sha256: str, queue_sha256: str) -> None:
    if not isinstance(results, dict) or results.get("format") != RESULTS_FORMAT:
        raise LedgerError("ledger results must use the queue-bound v2 format")
    claimed = str(results.get("results_sha256") or "").lower()
    unsigned = {key: value for key, value in results.items() if key != "results_sha256"}
    if len(claimed) != SHA256_LEN or claimed != hashlib.sha256(canonical_bytes(unsigned)).hexdigest():
        raise LedgerError("results_sha256 does not match the ledger results content")
    if results.get("plan_sha256") != plan_sha256 or results.get("queue_sha256") != queue_sha256:
        raise LedgerError("ledger results are not bound to the exact plan and receive queue")


def validate_plan_envelope(plan: dict[str, Any]) -> str:
    if not isinstance(plan, dict) or plan.get("format") != PLAN_FORMAT:
        raise LedgerError("unsupported plan")
    claimed_plan_sha256 = str(plan.get("plan_sha256") or "").lower()
    unsigned_plan = {key: value for key, value in plan.items() if key != "plan_sha256"}
    actual_plan_sha256 = hashlib.sha256(canonical_bytes(unsigned_plan)).hexdigest()
    if len(claimed_plan_sha256) != SHA256_LEN or claimed_plan_sha256 != actual_plan_sha256:
        raise LedgerError("plan_sha256 does not match the plan content")
    for field in ("catalog_sha256", "receiver_state_sha256"):
        value = str(plan.get(field) or "").lower()
        if len(value) != SHA256_LEN or any(ch not in "0123456789abcdef" for ch in value):
            raise LedgerError(f"plan {field} is missing or invalid")
    if not str(plan.get("receiver_state_captured_at_utc") or "").strip():
        raise LedgerError("plan receiver_state_captured_at_utc is missing")
    planned_ledger_sha256 = str(plan.get("ledger_sha256") or "").lower()
    if len(planned_ledger_sha256) != SHA256_LEN or any(ch not in "0123456789abcdef" for ch in planned_ledger_sha256):
        raise LedgerError("plan ledger_sha256 is missing or invalid")
    if not isinstance(plan.get("decisions"), list):
        raise LedgerError("plan decisions must be an array")
    return claimed_plan_sha256


def expected_commit_summary(plan: dict[str, Any], results: dict[str, Any]) -> dict[str, int]:
    decisions = plan["decisions"]
    decision_keys = {str(decision.get("source_key") or "") for decision in decisions}
    if "" in decision_keys or len(decision_keys) != len(decisions):
        raise LedgerError("commit receipt cannot validate missing or duplicate plan source keys")
    rows = results.get("results")
    if not isinstance(rows, list):
        raise LedgerError("commit receipt cannot validate results without a results array")
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise LedgerError("commit receipt cannot validate a non-object result row")
        key = str(row.get("source_key") or "")
        if not key or key in by_key or key not in decision_keys:
            raise LedgerError("commit receipt results contain a missing, duplicate, or unknown source key")
        by_key[key] = row
    no_write_actions = {
        "skip-unchanged", "keep-hidden-already-downloaded",
        "preserve-pending-source-diverged", "skip-resolved-divergence",
    }
    updated = skipped = failed = 0
    for decision in decisions:
        action = decision.get("action")
        if action in no_write_actions:
            if decision["source_key"] in by_key:
                raise LedgerError("commit receipt results include an app-write row for a no-write decision")
            skipped += 1
            continue
        result = by_key.get(decision["source_key"])
        expected_status = "downloaded-kept-hidden" if action == "download-update-keep-hidden" else "applied"
        if result is None or result.get("status") != expected_status:
            failed += 1
        else:
            updated += 1
    return {"updated": updated, "skipped": skipped, "failed": failed}


@contextmanager
def exclusive_file_lock(path: Path, timeout_seconds: float = LEDGER_LOCK_TIMEOUT_SECONDS):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    acquired = False
    started = time.monotonic()
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        while not acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() - started >= timeout_seconds:
                    raise LedgerError(f"timed out waiting for receiver ledger lock: {path}")
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def valid_sha(value: Any) -> str:
    text = str(value or "").lower()
    if len(text) != SHA256_LEN or any(ch not in "0123456789abcdef" for ch in text):
        raise LedgerError(f"invalid conversation_sha256: {value!r}")
    return text


def optional_sha(value: Any) -> str | None:
    return None if value in {None, ""} else valid_sha(value)


def source_key(source_computer: str, thread_id: str) -> str:
    identity = {"source_computer": source_computer.casefold(), "original_thread_id": thread_id}
    return hashlib.sha256(canonical_bytes(identity)).hexdigest()


def lineage_id(origin_computer: str, origin_thread_id: str) -> str:
    identity = {"origin_computer": origin_computer.casefold(), "origin_thread_id": origin_thread_id}
    return hashlib.sha256(canonical_bytes(identity)).hexdigest()


def lineage_source_key(lineage: str, branch: str) -> str:
    return hashlib.sha256(canonical_bytes({"lineage_id": lineage, "branch_id": branch})).hexdigest()


def row_source_key(source_computer: str, row: dict[str, Any]) -> str:
    lineage = str(row.get("lineage_id") or "").strip()
    if lineage:
        if len(lineage) != SHA256_LEN or any(ch not in "0123456789abcdef" for ch in lineage):
            raise LedgerError("invalid lineage_id")
        branch = str(row.get("branch_id") or "main").strip()
        if not branch:
            raise LedgerError("branch_id is required with lineage_id")
        return lineage_source_key(lineage, branch)
    return source_key(source_computer, str(row.get("original_thread_id") or ""))


def bootstrap_identity(source_project_id: str) -> str:
    value = str(source_project_id or "").strip()
    if not value:
        raise LedgerError("project bootstrap source_project_id is required")
    return "__project_bootstrap__:" + value


def bootstrap_sha256(source_computer: str, source_project_id: str) -> str:
    identity = {
        "kind": "project-bootstrap",
        "source_computer": source_computer.casefold(),
        "source_project_id": str(source_project_id).strip(),
    }
    return hashlib.sha256(canonical_bytes(identity)).hexdigest()


def load_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"format": LEDGER_FORMAT, "entries": {}}
    value = read_json(path)
    if value.get("format") not in LEDGER_FORMATS or not isinstance(value.get("entries"), dict):
        raise LedgerError(f"unsupported ledger: {path}")
    return value


def ledger_sha256(ledger: dict[str, Any]) -> str:
    if ledger.get("format") not in LEDGER_FORMATS or not isinstance(ledger.get("entries"), dict):
        raise LedgerError("unsupported receiver ledger content")
    return hashlib.sha256(canonical_bytes(ledger)).hexdigest()


def require_inside(path: Path, parent: Path, label: str) -> Path:
    resolved, root = path.resolve(), parent.resolve()
    if resolved == root or root not in resolved.parents:
        raise LedgerError(f"{label} must be inside the calling CODEX_HOME")
    return resolved


def resolution_unsigned(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "resolution_sha256" and not key.startswith("_")}


def validate_resolution_artifact(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("format") != DIVERGENCE_RESOLUTION_FORMAT:
        raise LedgerError("unsupported divergence resolution artifact")
    claimed = str(value.get("resolution_sha256") or "").lower()
    if len(claimed) != SHA256_LEN or claimed != hashlib.sha256(canonical_bytes(resolution_unsigned(value))).hexdigest():
        raise LedgerError("divergence resolution SHA-256 does not match its content")
    if value.get("choice") not in {"incoming", "retained-pending"}:
        raise LedgerError("divergence resolution choice is invalid")
    for field in ("source_key", "catalog_sha256", "ledger_sha256", "blocked_plan_sha256"):
        valid_sha(value.get(field))
    for name in ("applied", "pending", "incoming"):
        snapshot = value.get(name)
        if not isinstance(snapshot, dict):
            raise LedgerError(f"divergence resolution {name} snapshot is missing")
        valid_sha(snapshot.get("conversation_sha256"))
    if len(str(value.get("reason") or "").strip()) < 8:
        raise LedgerError("divergence resolution requires an audit reason of at least 8 characters")
    return value


def load_resolution_files(paths: list[Path] | None, caller_home: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in paths or []:
        resolved = require_inside(path, caller_home, "divergence resolution")
        value = validate_resolution_artifact(read_json(resolved))
        if os.path.normcase(str(Path(str(value.get("destination_codex_home") or "")).resolve())) != os.path.normcase(str(caller_home.resolve())):
            raise LedgerError("divergence resolution belongs to another Codex account")
        key = str(value["source_key"])
        if key in result:
            raise LedgerError(f"duplicate divergence resolution for source_key: {key}")
        result[key] = {**value, "_resolution_path": str(resolved)}
    return result


def divergence_snapshot(entry: dict[str, Any], prefix: str, *, source: str, path: Any = None) -> dict[str, Any]:
    field = "conversation_sha256" if not prefix else prefix + "_conversation_sha256"
    snapshot = {
        "conversation_sha256": valid_sha(entry.get(field)),
        "messages_sha256": optional_sha(entry.get("messages_sha256" if not prefix else prefix + "_messages_sha256")),
        "message_count": int(entry.get("message_count" if not prefix else prefix + "_message_count") or 0),
        "attachment_count": int(entry.get("attachment_count" if not prefix else prefix + "_attachment_count") or 0),
        "cutoff_timestamp": entry.get("cutoff_timestamp" if not prefix else prefix + "_cutoff_timestamp"),
        "conversation_path": path,
        "source": source,
    }
    return snapshot


def create_divergence_resolution(
    plan: dict[str, Any], ledger: dict[str, Any], source: str, choice: str, reason: str,
    destination_codex_home: Path,
) -> dict[str, Any]:
    if plan.get("format") != PLAN_FORMAT:
        raise LedgerError("unsupported blocked divergence plan")
    claimed = str(plan.get("plan_sha256") or "").lower()
    unsigned = {key: item for key, item in plan.items() if key != "plan_sha256"}
    if claimed != hashlib.sha256(canonical_bytes(unsigned)).hexdigest():
        raise LedgerError("blocked divergence plan hash mismatch")
    if plan.get("ledger_sha256") != ledger_sha256(ledger):
        raise LedgerError("receiver ledger changed after the blocked divergence plan")
    matches = [row for row in plan.get("decisions") or [] if row.get("source_key") == source]
    if len(matches) != 1 or matches[0].get("action") != "preserve-pending-source-diverged":
        raise LedgerError("source_key is not one unresolved blocked divergence in this plan")
    decision = matches[0]
    entry = ledger.get("entries", {}).get(source)
    if not isinstance(entry, dict):
        raise LedgerError("blocked divergence is missing from the receiver ledger")
    applied = divergence_snapshot(entry, "", source="receiver-ledger-applied", path=entry.get("retained_handoff_path"))
    pending = divergence_snapshot(entry, "pending", source="receiver-ledger-pending", path=entry.get("retained_handoff_path"))
    incoming = {
        "conversation_sha256": valid_sha(decision.get("conversation_sha256")),
        "messages_sha256": optional_sha(decision.get("messages_sha256")),
        "message_count": int(decision.get("message_count") or 0),
        "attachment_count": int(decision.get("attachment_count") or 0),
        "cutoff_timestamp": decision.get("cutoff_timestamp"),
        "conversation_path": decision.get("conversation_path"),
        "source": "selected-device-snapshot",
        "source_device": plan.get("source_device"),
        "source_computer": plan.get("source_computer"),
        "source_instance_id": decision.get("source_instance_id"),
    }
    if pending["conversation_sha256"] == incoming["conversation_sha256"]:
        raise LedgerError("blocked divergence no longer contains two distinct versions")
    value = {
        "format": DIVERGENCE_RESOLUTION_FORMAT,
        "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "destination_codex_home": str(destination_codex_home.resolve()),
        "source_key": source,
        "choice": choice,
        "reason": reason.strip(),
        "catalog_sha256": valid_sha(plan.get("catalog_sha256")),
        "ledger_sha256": ledger_sha256(ledger),
        "blocked_plan_sha256": valid_sha(plan.get("plan_sha256")),
        "applied": applied,
        "pending": pending,
        "incoming": incoming,
    }
    validate_resolution_artifact({
        **value,
        "resolution_sha256": hashlib.sha256(canonical_bytes(value)).hexdigest(),
    })
    value["resolution_sha256"] = hashlib.sha256(canonical_bytes(value)).hexdigest()
    return value


def resolution_for_divergence(
    resolution: dict[str, Any], source: str, catalog_sha256: str,
    ledger: dict[str, Any], entry: dict[str, Any], row: dict[str, Any],
) -> dict[str, Any]:
    validate_resolution_artifact(resolution)
    if resolution.get("source_key") != source or resolution.get("catalog_sha256") != catalog_sha256:
        raise LedgerError("divergence resolution belongs to another source or catalog")
    if resolution.get("ledger_sha256") != ledger_sha256(ledger):
        raise LedgerError("divergence resolution is stale for the current receiver ledger")
    expected = {
        "applied": valid_sha(entry.get("conversation_sha256")),
        "pending": valid_sha(entry.get("pending_conversation_sha256")),
        "incoming": valid_sha(row.get("conversation_sha256")),
    }
    for name, expected_hash in expected.items():
        if (resolution.get(name) or {}).get("conversation_sha256") != expected_hash:
            raise LedgerError(f"divergence resolution {name} hash no longer matches")
    return resolution


def require_distinct_commit_paths(paths: dict[str, Path]) -> None:
    owners: dict[str, str] = {}
    for name, path in paths.items():
        key = os.path.normcase(str(path.resolve()))
        prior = owners.get(key)
        if prior is not None:
            raise LedgerError(f"commit paths must be pairwise distinct: {prior} and {name}")
        owners[key] = name


def receiver_states(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    value = read_json(path)
    if not isinstance(value, dict) or value.get("format") not in RECEIVER_STATE_FORMATS:
        raise LedgerError("unsupported receiver state format")
    is_v2 = value.get("format") == RECEIVER_STATE_FORMAT_V2
    source_computer = str(value.get("source_computer") or "").strip()
    if not source_computer:
        raise LedgerError("receiver state source_computer is required")
    if value.get("complete") is not True:
        raise LedgerError("receiver state must declare complete=true")
    if value.get("active_inventory_complete") is not True or value.get("archived_inventory_complete") is not True:
        raise LedgerError("receiver state must prove complete active and archived inventories")
    captured_at = str(value.get("captured_at_utc") or "").strip()
    try:
        captured = dt.datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LedgerError("receiver state captured_at_utc is invalid") from exc
    if captured.tzinfo is None:
        raise LedgerError("receiver state captured_at_utc must include a timezone")
    captured_utc = captured.astimezone(dt.timezone.utc)
    now_utc = dt.datetime.now(dt.timezone.utc)
    if (captured_utc - now_utc).total_seconds() > RECEIVER_STATE_MAX_FUTURE_SKEW_SECONDS:
        raise LedgerError("receiver state captured_at_utc is unreasonably in the future")
    if (now_utc - captured_utc).total_seconds() > RECEIVER_STATE_MAX_AGE_SECONDS:
        raise LedgerError("receiver state is stale; read the app state again")
    rows = value.get("threads")
    if not isinstance(rows, list):
        raise LedgerError("receiver state must contain a threads array")
    if type(value.get("thread_count")) is not int or value["thread_count"] != len(rows):
        raise LedgerError("receiver state thread_count does not match threads")
    result: dict[str, dict[str, Any]] = {}
    receiving_owners: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise LedgerError("receiver thread state must be an object")
        original = str(row.get("original_thread_id") or "").strip()
        if not original:
            raise LedgerError("receiver original_thread_id is required")
        if is_v2:
            identity_key = row_source_key(source_computer, row)
        else:
            identity_key = original
        if identity_key in result:
            raise LedgerError(f"duplicate receiver source identity: {identity_key!r}")
        disposition = str(row.get("receiver_disposition") or "active")
        if disposition not in {"active", "archived", "deleted"}:
            raise LedgerError(f"invalid receiver disposition for {original}: {disposition}")
        receiving_id = str(row.get("receiving_thread_id") or "").strip()
        if disposition in {"active", "archived"} and not receiving_id:
            raise LedgerError(f"receiver thread ID is required for {disposition} task {original}")
        if receiving_id:
            prior = receiving_owners.get(receiving_id)
            if prior is not None and prior != identity_key:
                raise LedgerError(f"duplicate receiving_thread_id {receiving_id!r}: {prior}, {identity_key}")
            receiving_owners[receiving_id] = identity_key
        result[identity_key] = {
            "original_thread_id": original,
            "lineage_id": row.get("lineage_id"),
            "branch_id": row.get("branch_id") or "main",
            "receiving_thread_id": receiving_id or None,
            "receiver_disposition": disposition,
        }
    return result, {
        "source_computer": source_computer,
        "captured_at_utc": captured_at,
        "receiver_state_sha256": hashlib.sha256(canonical_bytes(value)).hexdigest(),
        "identity_mode": "lineage-branch" if is_v2 else "legacy-thread",
    }


def receiving_id_owners(ledger: dict[str, Any]) -> dict[str, str]:
    owners: dict[str, str] = {}
    for key, entry in ledger["entries"].items():
        receiving_id = str(entry.get("receiving_thread_id") or "").strip()
        if not receiving_id:
            continue
        prior = owners.get(receiving_id)
        if prior is not None and prior != key:
            raise LedgerError(f"receiver ledger reuses receiving_thread_id {receiving_id!r}")
        owners[receiving_id] = key
    return owners


def catalog_conversations(catalog: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    if not isinstance(catalog, dict) or catalog.get("format") not in RECEIVING_CATALOG_FORMATS:
        raise LedgerError("only a filtered receiving-task catalog is accepted")
    is_v2 = catalog.get("format") == RECEIVING_CATALOG_FORMAT_V2
    claimed_catalog_sha256 = str(catalog.get("catalog_sha256") or "").lower()
    unsigned_catalog = dict(catalog)
    unsigned_catalog.pop("catalog_sha256", None)
    actual_catalog_sha256 = hashlib.sha256(canonical_bytes(unsigned_catalog)).hexdigest()
    if claimed_catalog_sha256 != actual_catalog_sha256:
        raise LedgerError("receiving-task catalog_sha256 does not match the catalog content")
    source_device = str(catalog.get("source_device") or "").strip()
    source_computer = str(catalog.get("source_computer") or "").strip()
    if not source_device or not source_computer:
        raise LedgerError("catalog source_device and source_computer are required")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    tasks = catalog.get("receiving_tasks")
    projects = catalog.get("projects")
    excluded_sessions = catalog.get("excluded_sessions")
    if not isinstance(tasks, list) or not isinstance(projects, list) or not isinstance(excluded_sessions, list):
        raise LedgerError("receiving-task catalog arrays are invalid")
    expected_counts = {
        "project_count": len(projects),
        "raw_session_count": sum(int(project.get("raw_session_count") or 0) for project in projects),
        "user_visible_conversation_count": sum(task.get("kind") == "conversation" for task in tasks),
        "excluded_internal_session_count": len(excluded_sessions),
        "bootstrap_task_count": sum(task.get("kind") == "project-bootstrap" for task in tasks),
        "target_receiving_task_count": len(tasks),
    }
    for field, expected in expected_counts.items():
        if type(catalog.get(field)) is not int or catalog[field] != expected:
            raise LedgerError(f"receiving-task catalog {field} does not match its inventory")
    sources = []
    for task in tasks:
        if not isinstance(task, dict):
            raise LedgerError("receiving task must be an object")
        kind = task.get("kind")
        if kind == "conversation":
            sources.append((task, {
                "project_id": task.get("project_id"),
                "project_label": task.get("project_label"),
                "kind": kind,
            }))
        elif kind == "project-bootstrap":
            source_project_id = str(task.get("source_project_id") or "").strip()
            identity = bootstrap_identity(source_project_id)
            expected_sha = bootstrap_sha256(source_computer, source_project_id)
            if task.get("original_thread_id") != identity or task.get("conversation_sha256") != expected_sha:
                raise LedgerError("project bootstrap identity or hash is invalid")
            sources.append((task, {
                "project_id": task.get("project_id"),
                "project_label": task.get("project_label"),
                "kind": kind,
            }))
        else:
            raise LedgerError(f"unsupported receiving task kind: {kind!r}")
    for conversation, project in sources:
        thread_id = str(conversation.get("thread_id") or conversation.get("original_thread_id") or "").strip()
        if not thread_id or thread_id in seen:
            raise LedgerError(f"missing or duplicate source thread_id: {thread_id!r}")
        seen.add(thread_id)
        rows.append({
            "original_thread_id": thread_id,
            "thread_title": str(conversation.get("thread_title") or conversation.get("original_title") or thread_id),
            "conversation_sha256": valid_sha(conversation.get("conversation_sha256")),
            "messages_sha256": optional_sha(conversation.get("messages_sha256")),
            "message_count": int(conversation.get("message_count") or 0),
            "attachment_count": int(conversation.get("attachment_count") or 0),
            "cutoff_timestamp": conversation.get("cutoff_timestamp"),
            "project_id": project.get("project_id"),
            "project_label": project.get("project_label"),
            "conversation_path": conversation.get("conversation_path"),
            "retained_working_directories": conversation.get("retained_working_directories") or [],
            "kind": project.get("kind") or "conversation",
            "source_instance_id": conversation.get("source_instance_id") or "legacy-unknown",
            "source_disposition": conversation.get("source_disposition") or "active",
            "origin_computer": conversation.get("origin_computer") or source_computer,
            "origin_instance_id": conversation.get("origin_instance_id") or conversation.get("source_instance_id") or "legacy-unknown",
            "origin_thread_id": conversation.get("origin_thread_id") or thread_id,
            "lineage_id": conversation.get("lineage_id"),
            "branch_id": conversation.get("branch_id") or "main",
            "target_kind": conversation.get("target_kind") or ("project" if conversation.get("project_id") else "projectless"),
        })
    if is_v2:
        identity_keys = [row_source_key(source_computer, row) for row in rows]
        if len(identity_keys) != len(set(identity_keys)):
            raise LedgerError("receiving catalog contains duplicate lineage branches")
    rows.sort(key=lambda row: (str(row.get("lineage_id") or ""), str(row.get("branch_id") or ""), row["original_thread_id"].casefold()))
    return source_device, source_computer, rows


def build_plan(
    catalog: dict[str, Any],
    ledger: dict[str, Any],
    states: dict[str, dict[str, Any]],
    receiver_state_info: dict[str, str] | None = None,
    divergence_resolutions: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    source_device, source_computer, conversations = catalog_conversations(catalog)
    is_v2 = catalog.get("format") == RECEIVING_CATALOG_FORMAT_V2
    if receiver_state_info is None:
        synthetic_state = {
            "source_computer": source_computer,
            "threads": states,
        }
        receiver_state_info = {
            "source_computer": source_computer,
            "captured_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "receiver_state_sha256": hashlib.sha256(canonical_bytes(synthetic_state)).hexdigest(),
        }
    if receiver_state_info.get("source_computer", "").casefold() != source_computer.casefold():
        raise LedgerError("receiver state source_computer does not match the catalog")
    known_state_keys = {
        (key if is_v2 else str(entry.get("original_thread_id") or ""))
        for key, entry in ledger["entries"].items()
        if (
            str(entry.get("last_source_computer") or entry.get("source_computer") or "").casefold()
            == source_computer.casefold()
            or (is_v2 and key in {row_source_key(source_computer, row) for row in conversations})
        )
    }
    missing_states = sorted(item for item in known_state_keys if item and item not in states)
    if missing_states:
        raise LedgerError(f"receiver state is incomplete for ledger tasks: {', '.join(missing_states)}")
    entries = ledger["entries"]
    # A migrated ledger must still accept a v1 snapshot. Resolve by original
    # provenance, never by the current source device or receiving task ID.
    legacy_aliases = {}
    for stored_key, entry in entries.items():
        if entry.get("lineage_id"):
            legacy_key = source_key(str(entry.get("origin_computer") or entry.get("source_computer") or ""),
                                    str(entry.get("origin_thread_id") or entry.get("original_thread_id") or ""))
            if entry.get("branch_id", "main") == "main":
                if legacy_key in legacy_aliases:
                    raise LedgerError("ambiguous migrated legacy identity")
                legacy_aliases[legacy_key] = stored_key
    receiving_owners = receiving_id_owners(ledger)
    for state_identity, live in states.items():
        receiving_id = str(live.get("receiving_thread_id") or "").strip()
        if not receiving_id:
            continue
        if is_v2:
            key = state_identity
        else:
            legacy_key = source_key(source_computer, state_identity)
            key = legacy_aliases.get(legacy_key, legacy_key)
        prior = receiving_owners.get(receiving_id)
        if prior is not None and prior != key:
            raise LedgerError(f"receiving_thread_id {receiving_id!r} is already bound to another source")
        receiving_owners[receiving_id] = key
    resolutions = divergence_resolutions or {}
    decisions = []
    used_resolutions: set[str] = set()
    current_keys: set[str] = set()
    for row in conversations:
        key = row_source_key(source_computer, row)
        if not is_v2 and key in legacy_aliases:
            key = legacy_aliases[key]
            migrated = entries[key]
            row = {**row, **{field: migrated.get(field) for field in (
                "lineage_id", "branch_id", "origin_computer", "origin_instance_id", "origin_thread_id")}}
        current_keys.add(key)
        old = entries.get(key)
        live = states.get(key if is_v2 else row["original_thread_id"], {})
        old_receiving_id = str((old or {}).get("receiving_thread_id") or "").strip()
        live_receiving_id = str(live.get("receiving_thread_id") or "").strip()
        if old_receiving_id and live_receiving_id and old_receiving_id != live_receiving_id:
            raise LedgerError(
                f"receiver thread identity conflict for {row['original_thread_id']}: "
                f"ledger={old_receiving_id}, live={live_receiving_id}"
            )
        disposition = str(live.get("receiver_disposition") or (old or {}).get("receiver_disposition") or row.get("source_disposition") or "active")
        receiving_id = live_receiving_id or old_receiving_id or None
        applied_sha256 = (old or {}).get("conversation_sha256")
        pending_sha256 = (old or {}).get("pending_conversation_sha256")
        suppressed_incoming_sha256 = (old or {}).get("suppressed_incoming_conversation_sha256")
        legacy_hidden_state = bool(
            old is not None
            and not (old or {}).get("applied_state_version")
            and pending_sha256 is None
            and disposition in {"archived", "deleted"}
        )
        if old is None and receiving_id and disposition in {"archived", "deleted"}:
            action = "download-update-keep-hidden"
            reason = "existing-hidden-thread-not-in-receiver-ledger"
        elif old is None and receiving_id:
            action = "update-existing"
            reason = "existing-active-thread-not-in-receiver-ledger"
        elif old is None:
            action = "register-new"
            reason = "source-thread-not-in-receiver-ledger"
        elif pending_sha256 and pending_sha256 != row["conversation_sha256"]:
            resolution = resolutions.get(key)
            if resolution is None:
                action = "preserve-pending-source-diverged"
                reason = "source-hash-differs-from-unapplied-pending-hash"
            else:
                resolution_for_divergence(resolution, key, catalog["catalog_sha256"], ledger, old or {}, row)
                used_resolutions.add(key)
                if resolution["choice"] == "incoming":
                    action = "resolve-divergence-incoming"
                    reason = "explicit-resolution-adopt-incoming-preserve-pending-history"
                    applied_snapshot = resolution["incoming"]
                    divergent_snapshot = resolution["pending"]
                else:
                    action = "resolve-divergence-retained"
                    reason = "explicit-resolution-apply-retained-preserve-incoming-history"
                    applied_snapshot = resolution["pending"]
                    divergent_snapshot = resolution["incoming"]
        elif pending_sha256 == row["conversation_sha256"] and disposition in {"archived", "deleted"}:
            action = "keep-hidden-already-downloaded"
            reason = f"source-already-downloaded-receiver-{disposition}"
        elif pending_sha256 == row["conversation_sha256"]:
            action = "apply-retained-update"
            reason = "receiver-active-with-retained-update"
        elif legacy_hidden_state:
            action = "download-update-keep-hidden"
            reason = "legacy-ledger-cannot-prove-hidden-hash-was-applied"
        elif applied_sha256 == row["conversation_sha256"]:
            action = "skip-unchanged"
            reason = "same-conversation-sha256"
        elif suppressed_incoming_sha256 == row["conversation_sha256"]:
            action = "skip-resolved-divergence"
            reason = "incoming-version-was-explicitly-preserved-as-divergent-history"
        elif disposition in {"archived", "deleted"}:
            action = "download-update-keep-hidden"
            reason = f"source-advanced-receiver-{disposition}"
        else:
            action = "update-existing"
            reason = "source-conversation-sha256-advanced"
        decision = {
            **row,
            "source_key": key,
            "prior_conversation_sha256": (old or {}).get("conversation_sha256"),
            "pending_conversation_sha256": pending_sha256,
            "receiving_thread_id": receiving_id,
            "receiver_disposition": disposition,
            "action": action,
            "reason": reason,
        }
        if action in {"resolve-divergence-incoming", "resolve-divergence-retained"}:
            decision.update({
                "divergence_resolution_path": resolution.get("_resolution_path"),
                "divergence_resolution_sha256": resolution["resolution_sha256"],
                "divergence_choice": resolution["choice"],
                "apply_conversation_sha256": applied_snapshot["conversation_sha256"],
                "apply_messages_sha256": applied_snapshot.get("messages_sha256"),
                "apply_message_count": applied_snapshot.get("message_count"),
                "apply_attachment_count": applied_snapshot.get("attachment_count"),
                "apply_cutoff_timestamp": applied_snapshot.get("cutoff_timestamp"),
                "apply_conversation_path": applied_snapshot.get("conversation_path"),
                "divergent_history": divergent_snapshot,
            })
        decisions.append(decision)
    unused_resolutions = sorted(set(resolutions) - used_resolutions)
    if unused_resolutions:
        raise LedgerError("divergence resolution does not match a current unresolved divergence: " + ", ".join(unused_resolutions))
    removed = [
        {"source_key": key, **value, "action": "retain-local-no-source-delete"}
        for key, value in entries.items()
        if str(value.get("last_source_computer") or value.get("source_computer") or "").casefold() == source_computer.casefold() and key not in current_keys
    ]
    plan = {
        "format": PLAN_FORMAT,
        "source_device": source_device,
        "source_computer": source_computer,
        "catalog_sha256": catalog["catalog_sha256"],
        "receiver_state_sha256": receiver_state_info.get("receiver_state_sha256"),
        "receiver_state_captured_at_utc": receiver_state_info.get("captured_at_utc"),
        "ledger_sha256": ledger_sha256(ledger),
        "divergence_resolution_sha256s": sorted(
            resolution["resolution_sha256"] for resolution in resolutions.values()
        ),
        "decisions": decisions,
        "source_removed": removed,
    }
    plan["plan_sha256"] = hashlib.sha256(canonical_bytes(plan)).hexdigest()
    return plan


def commit_plan(
    plan: dict[str, Any], ledger: dict[str, Any], results: dict[str, Any],
    committed_utc: str | None = None,
) -> dict[str, Any]:
    claimed_plan_sha256 = validate_plan_envelope(plan)
    planned_ledger_sha256 = str(plan.get("ledger_sha256") or "").lower()
    if planned_ledger_sha256 != ledger_sha256(ledger):
        raise LedgerError("receiver ledger changed after planning; build a fresh plan")
    if not isinstance(results, dict) or str(results.get("plan_sha256") or "").lower() != claimed_plan_sha256:
        raise LedgerError("result plan_sha256 does not match the exact plan")
    decisions = plan.get("decisions")
    if not isinstance(decisions, list):
        raise LedgerError("plan decisions must be an array")
    decision_keys: set[str] = set()
    receiving_owners = receiving_id_owners(ledger)
    allowed_actions = {
        "register-new", "update-existing", "skip-unchanged",
        "download-update-keep-hidden", "keep-hidden-already-downloaded",
        "apply-retained-update", "preserve-pending-source-diverged",
        "resolve-divergence-incoming", "resolve-divergence-retained",
        "skip-resolved-divergence",
    }
    for decision in decisions:
        if not isinstance(decision, dict):
            raise LedgerError("plan decision must be an object")
        key = str(decision.get("source_key") or "")
        if not key or key in decision_keys:
            raise LedgerError(f"missing or duplicate plan source_key: {key!r}")
        if decision.get("action") not in allowed_actions:
            raise LedgerError(f"unsupported plan action: {decision.get('action')!r}")
        expected_key = row_source_key(str(plan.get("source_computer") or ""), decision)
        if key != expected_key:
            raise LedgerError("plan source_key does not match its source identity")
        planned_receiving_id = str(decision.get("receiving_thread_id") or "").strip()
        if planned_receiving_id:
            prior = receiving_owners.get(planned_receiving_id)
            if prior is not None and prior != key:
                raise LedgerError(f"receiving_thread_id {planned_receiving_id!r} is already bound to another source")
            receiving_owners[planned_receiving_id] = key
        decision_keys.add(key)
    result_rows = results.get("results") if isinstance(results, dict) else None
    if not isinstance(result_rows, list):
        raise LedgerError("result must contain a results array")
    by_key: dict[str, dict[str, Any]] = {}
    for row in result_rows:
        if not isinstance(row, dict):
            raise LedgerError("result row must be an object")
        key = str(row.get("source_key") or "")
        if not key or key in by_key:
            raise LedgerError(f"missing or duplicate result source_key: {key!r}")
        if key not in decision_keys:
            raise LedgerError(f"result source_key is not present in the plan: {key}")
        by_key[key] = row
    validated_resolutions: dict[str, dict[str, Any]] = {}
    for decision in decisions:
        if decision.get("action") not in {"resolve-divergence-incoming", "resolve-divergence-retained"}:
            continue
        resolution_path = Path(str(decision.get("divergence_resolution_path") or ""))
        if not resolution_path.is_file():
            raise LedgerError("divergence resolution artifact is missing during commit")
        resolution = validate_resolution_artifact(read_json(resolution_path))
        if resolution.get("resolution_sha256") != decision.get("divergence_resolution_sha256"):
            raise LedgerError("divergence resolution artifact changed after planning")
        current_entry = ledger["entries"].get(decision["source_key"]) or {}
        resolution_for_divergence(
            resolution, decision["source_key"], plan["catalog_sha256"], ledger,
            current_entry, decision,
        )
        validated_resolutions[decision["source_key"]] = resolution
    now = committed_utc or dt.datetime.now(dt.timezone.utc).isoformat()
    updated = skipped = failed = 0
    for decision in decisions:
        action = decision["action"]
        if action in {
            "skip-unchanged", "keep-hidden-already-downloaded",
            "preserve-pending-source-diverged", "skip-resolved-divergence",
        }:
            if decision["source_key"] in by_key:
                raise LedgerError(f"{action} must not have an app-write result")
            existing = dict(ledger["entries"].get(decision["source_key"]) or {})
            observed = {
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
                "receiving_thread_id": decision.get("receiving_thread_id"),
                "receiver_disposition": decision["receiver_disposition"],
                "last_seen_utc": now,
            }
            if action not in {"preserve-pending-source-diverged", "skip-resolved-divergence"} and decision.get("conversation_path"):
                observed["retained_handoff_path"] = decision["conversation_path"]
            if action == "skip-unchanged":
                observed.update({
                    "conversation_sha256": decision["conversation_sha256"],
                    "applied_state_version": APPLIED_STATE_VERSION,
                    "message_count": decision["message_count"],
                    "attachment_count": decision["attachment_count"],
                    "cutoff_timestamp": decision.get("cutoff_timestamp"),
                })
                if decision.get("messages_sha256") is not None:
                    observed["messages_sha256"] = decision["messages_sha256"]
                for field in (
                    "pending_conversation_sha256", "pending_messages_sha256",
                    "pending_message_count", "pending_attachment_count",
                    "pending_cutoff_timestamp", "last_downloaded_utc",
                ):
                    existing.pop(field, None)
            if action == "skip-resolved-divergence":
                if existing.get("suppressed_incoming_conversation_sha256") != decision["conversation_sha256"]:
                    raise LedgerError("resolved divergence suppression hash does not match the current source")
                histories = existing.get("divergent_histories")
                if not isinstance(histories, list) or not any(
                    isinstance(item, dict) and item.get("conversation_sha256") == decision["conversation_sha256"]
                    for item in histories
                ):
                    raise LedgerError("resolved divergence history is missing the preserved incoming version")
            existing.update(observed)
            ledger["entries"][decision["source_key"]] = existing
            skipped += 1
            continue
        result = by_key.get(decision["source_key"])
        expected_status = "downloaded-kept-hidden" if action == "download-update-keep-hidden" else "applied"
        if not result or result.get("status") != expected_status:
            failed += 1
            continue
        if result.get("action") != action:
            raise LedgerError("result action does not match the exact plan decision")
        if action in {"resolve-divergence-incoming", "resolve-divergence-retained"}:
            if result.get("divergence_resolution_sha256") != decision.get("divergence_resolution_sha256"):
                raise LedgerError("result divergence resolution SHA-256 does not match the exact plan decision")
        expected_applied_sha256 = (
            decision.get("apply_conversation_sha256")
            if action in {"resolve-divergence-incoming", "resolve-divergence-retained"}
            else decision["conversation_sha256"]
        )
        if valid_sha(result.get("conversation_sha256")) != expected_applied_sha256:
            raise LedgerError("result conversation_sha256 does not match the exact plan decision")
        disposition = str(result.get("receiver_disposition") or decision["receiver_disposition"])
        if disposition not in {"active", "archived", "deleted"}:
            raise LedgerError(f"invalid result receiver_disposition: {disposition}")
        if disposition != decision["receiver_disposition"]:
            raise LedgerError("result receiver disposition does not match the exact plan decision")
        if action == "download-update-keep-hidden" and disposition not in {"archived", "deleted"}:
            raise LedgerError("hidden receiver update cannot be recorded as active")
        prior_receiving_id = str(decision.get("receiving_thread_id") or "").strip()
        result_receiving_id = str(result.get("receiving_thread_id") or "").strip()
        if action == "register-new":
            if not result_receiving_id:
                raise LedgerError("register-new result requires receiving_thread_id")
            receiving_id = result_receiving_id
        elif action in {
            "update-existing", "apply-retained-update",
            "resolve-divergence-incoming", "resolve-divergence-retained",
        }:
            if not prior_receiving_id:
                raise LedgerError(f"{action} decision is missing receiving_thread_id")
            if result_receiving_id and result_receiving_id != prior_receiving_id:
                raise LedgerError(f"{action} result cannot replace the receiving thread")
            receiving_id = prior_receiving_id
        else:
            if disposition == "archived" and not prior_receiving_id:
                raise LedgerError("archived hidden update is missing receiving_thread_id")
            if result_receiving_id and result_receiving_id != prior_receiving_id:
                raise LedgerError("hidden update cannot create or replace the receiving thread")
            receiving_id = prior_receiving_id or None
        if receiving_id:
            prior_owner = receiving_owners.get(receiving_id)
            if prior_owner is not None and prior_owner != decision["source_key"]:
                raise LedgerError(f"receiving_thread_id {receiving_id!r} is already bound to another source")
            receiving_owners[receiving_id] = decision["source_key"]
        existing = dict(ledger["entries"].get(decision["source_key"]) or {})
        if action == "download-update-keep-hidden":
            existing.update({
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
                "receiving_thread_id": receiving_id,
                "receiver_disposition": disposition,
                "pending_conversation_sha256": decision["conversation_sha256"],
                "pending_messages_sha256": decision.get("messages_sha256"),
                "pending_message_count": decision["message_count"],
                "pending_attachment_count": decision["attachment_count"],
                "pending_cutoff_timestamp": decision.get("cutoff_timestamp"),
                "last_downloaded_utc": now,
            })
            if decision.get("conversation_path"):
                existing["retained_handoff_path"] = decision["conversation_path"]
            existing["retained_working_directories"] = decision.get("retained_working_directories") or existing.get("retained_working_directories") or []
            ledger["entries"][decision["source_key"]] = existing
            updated += 1
            continue
        applied_conversation_sha256 = expected_applied_sha256
        applied_messages_sha256 = (
            decision.get("apply_messages_sha256")
            if action in {"resolve-divergence-incoming", "resolve-divergence-retained"}
            else decision.get("messages_sha256")
        )
        applied_message_count = (
            decision.get("apply_message_count")
            if action in {"resolve-divergence-incoming", "resolve-divergence-retained"}
            else decision["message_count"]
        )
        applied_attachment_count = (
            decision.get("apply_attachment_count")
            if action in {"resolve-divergence-incoming", "resolve-divergence-retained"}
            else decision["attachment_count"]
        )
        applied_cutoff_timestamp = (
            decision.get("apply_cutoff_timestamp")
            if action in {"resolve-divergence-incoming", "resolve-divergence-retained"}
            else decision.get("cutoff_timestamp")
        )
        entry = {
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
            "conversation_sha256": applied_conversation_sha256,
            "applied_state_version": APPLIED_STATE_VERSION,
            "messages_sha256": applied_messages_sha256,
            "message_count": applied_message_count,
            "attachment_count": applied_attachment_count,
            "cutoff_timestamp": applied_cutoff_timestamp,
            "receiving_thread_id": receiving_id,
            "receiver_disposition": disposition,
            "last_applied_utc": now,
        }
        applied_path = (
            decision.get("apply_conversation_path")
            if action in {"resolve-divergence-incoming", "resolve-divergence-retained"}
            else decision.get("conversation_path")
        )
        if applied_path:
            entry["retained_handoff_path"] = applied_path
        entry["retained_working_directories"] = decision.get("retained_working_directories") or existing.get("retained_working_directories") or []
        if applied_messages_sha256 is not None:
            entry["messages_sha256"] = applied_messages_sha256
        if action in {"resolve-divergence-incoming", "resolve-divergence-retained"}:
            resolution = validated_resolutions[decision["source_key"]]
            history = dict(decision.get("divergent_history") or {})
            valid_sha(history.get("conversation_sha256"))
            history.update({
                "resolution_sha256": resolution["resolution_sha256"],
                "resolution_choice": resolution["choice"],
                "recorded_utc": now,
            })
            prior_histories = existing.get("divergent_histories") or []
            if not isinstance(prior_histories, list):
                raise LedgerError("receiver ledger divergent_histories is invalid")
            entry["divergent_histories"] = [*prior_histories, history]
            entry["last_divergence_resolution_sha256"] = resolution["resolution_sha256"]
            if action == "resolve-divergence-retained":
                entry["suppressed_incoming_conversation_sha256"] = decision["conversation_sha256"]
            else:
                entry.pop("suppressed_incoming_conversation_sha256", None)
        for field in (
            "pending_conversation_sha256", "pending_messages_sha256",
            "pending_message_count", "pending_attachment_count",
            "pending_cutoff_timestamp", "last_downloaded_utc",
        ):
            entry.pop(field, None)
        ledger["entries"][decision["source_key"]] = entry
        updated += 1
    if any(decision.get("lineage_id") for decision in decisions):
        ledger["format"] = LEDGER_FORMAT_V2
    ledger["updated_utc"] = now
    return {"ledger": ledger, "updated": updated, "skipped": skipped, "failed": failed}


def build_commit_receipt(
    value: dict[str, Any], caller_home: Path, destination_instance_id: str,
    plan_value: dict[str, Any], queue_value: dict[str, Any], result_value: dict[str, Any],
    app_proof_value: dict[str, Any], prior_ledger_sha256: str,
) -> dict[str, Any]:
    receipt = {
        "format": COMMIT_RECEIPT_FORMAT,
        "status": "committed" if value["failed"] == 0 else "incomplete",
        "committed_utc": value["ledger"].get("updated_utc"),
        "destination_codex_home": str(caller_home),
        "destination_instance_id": destination_instance_id,
        "source_device": plan_value.get("source_device"),
        "source_computer": plan_value.get("source_computer"),
        "plan_sha256": plan_value.get("plan_sha256"),
        "catalog_sha256": plan_value.get("catalog_sha256"),
        "receiver_state_sha256": plan_value.get("receiver_state_sha256"),
        "queue_sha256": queue_value.get("queue_sha256"),
        "results_sha256": result_value.get("results_sha256"),
        "app_proof_sha256": app_proof_value.get("app_proof_sha256"),
        "ledger_sha256_before": prior_ledger_sha256,
        "ledger_sha256_after": ledger_sha256(value["ledger"]),
        "updated": value["updated"],
        "skipped": value["skipped"],
        "failed": value["failed"],
    }
    receipt["commit_receipt_sha256"] = hashlib.sha256(canonical_bytes(receipt)).hexdigest()
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command", required=True)
    plan = subs.add_parser("plan")
    plan.add_argument("--catalog", type=Path, required=True)
    plan.add_argument("--ledger", type=Path, required=True)
    plan.add_argument("--receiver-state", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--divergence-resolution", type=Path, action="append", default=[])
    commit = subs.add_parser("commit")
    commit.add_argument("--plan", type=Path, required=True)
    commit.add_argument("--ledger", type=Path, required=True)
    commit.add_argument("--result", type=Path, required=True)
    commit.add_argument("--queue", type=Path, required=True)
    commit.add_argument("--app-proof", type=Path, required=True)
    commit.add_argument("--receipt", type=Path)
    commit.add_argument("--destination-instance-id")
    resolve = subs.add_parser("resolve-divergence")
    resolve.add_argument("--plan", type=Path, required=True)
    resolve.add_argument("--ledger", type=Path, required=True)
    resolve.add_argument("--queue", type=Path, required=True)
    resolve.add_argument("--source-key", required=True)
    resolve.add_argument("--choice", choices=("incoming", "retained-pending"), required=True)
    resolve.add_argument("--reason", required=True)
    resolve.add_argument("--output", type=Path, required=True)
    try:
        args = parser.parse_args()
        caller_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").resolve()
        if caller_home not in args.ledger.resolve().parents:
            raise LedgerError("receiver ledger must be inside the calling CODEX_HOME")
        if args.command == "plan" and caller_home not in args.output.resolve().parents:
            raise LedgerError("receiver plan must be inside the calling CODEX_HOME")
        if args.command == "resolve-divergence":
            for name in ("plan", "ledger", "queue", "output"):
                require_inside(getattr(args, name), caller_home, name.replace("_", "-"))
            lock_path = Path(str(args.ledger) + ".lock")
            require_distinct_commit_paths({
                "plan": args.plan, "ledger": args.ledger, "queue": args.queue,
                "resolution": args.output, "ledger-lock": lock_path,
            })
            queue_module = load_local("receive-queue-engine.py", "ledger_divergence_queue")
            with exclusive_file_lock(lock_path):
                plan_value = read_json(args.plan)
                ledger_value = load_ledger(args.ledger)
                resolution_value = create_divergence_resolution(
                    plan_value, ledger_value, args.source_key, args.choice, args.reason, caller_home,
                )
                if args.output.exists():
                    existing = validate_resolution_artifact(read_json(args.output))
                    stable_existing = dict(existing)
                    stable_new = dict(resolution_value)
                    stable_new["created_utc"] = stable_existing.get("created_utc")
                    stable_new["resolution_sha256"] = hashlib.sha256(
                        canonical_bytes(resolution_unsigned(stable_new))
                    ).hexdigest()
                    if canonical_bytes(existing) != canonical_bytes(stable_new):
                        raise LedgerError("existing divergence resolution does not match this exact choice")
                    resolution_value = existing
                else:
                    write_json(args.output, resolution_value)
                retired = queue_module.resolve_blocked_divergence(
                    args.queue, str(caller_home), args.source_key, resolution_value,
                )
            print(json.dumps({
                "status": "divergence-resolved-awaiting-fresh-plan",
                "resolution": str(args.output.resolve()),
                "resolution_sha256": resolution_value["resolution_sha256"],
                "choice": resolution_value["choice"],
                "old_queue_complete": retired["complete"],
            }, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "commit" and args.receipt is not None and caller_home not in args.receipt.resolve().parents:
            raise LedgerError("ledger commit receipt must be inside the calling CODEX_HOME")
        if args.command == "commit" and args.receipt is not None and not str(args.destination_instance_id or "").strip():
            raise LedgerError("--destination-instance-id is required with --receipt")
        if args.command == "commit":
            for name in ("plan", "result", "queue", "app_proof"):
                if caller_home not in getattr(args, name).resolve().parents:
                    raise LedgerError(f"{name.replace('_', '-')} must be inside the calling CODEX_HOME")
        if args.command == "plan":
            states, state_info = receiver_states(args.receiver_state)
            value = build_plan(
                read_json(args.catalog), load_ledger(args.ledger), states, state_info,
                load_resolution_files(args.divergence_resolution, caller_home),
            )
            write_json(args.output, value)
            counts: dict[str, int] = {}
            for row in value["decisions"]:
                counts[row["action"]] = counts.get(row["action"], 0) + 1
            print(json.dumps({"plan": str(args.output.resolve()), "counts": counts, "source_removed": len(value["source_removed"])}, ensure_ascii=False, sort_keys=True))
            return 0
        lock_path = Path(str(args.ledger) + ".lock")
        commit_paths = {
            "plan": args.plan, "ledger": args.ledger, "result": args.result,
            "queue": args.queue, "app-proof": args.app_proof, "ledger-lock": lock_path,
        }
        if args.receipt is not None:
            commit_paths["receipt"] = args.receipt
        require_distinct_commit_paths(commit_paths)
        with exclusive_file_lock(lock_path):
            plan_value = read_json(args.plan)
            validate_plan_envelope(plan_value)
            result_value = read_json(args.result)
            queue_module = load_local("receive-queue-engine.py", "ledger_commit_queue")
            app_proof_module = load_local("receiver-app-proof.py", "ledger_commit_app_proof")
            queue_value = queue_module.load(args.queue, str(caller_home))
            validate_results_envelope(result_value, str(plan_value.get("plan_sha256") or ""), str(queue_value.get("queue_sha256") or ""))
            expected_result_value = queue_module.ledger_results(args.queue, str(caller_home))
            if canonical_bytes(result_value) != canonical_bytes(expected_result_value):
                raise LedgerError("ledger results do not come from the exact completed queue")
            app_proof_value = read_json(args.app_proof)
            app_proof_module.validate_proof(app_proof_value, args.queue, caller_home, verify_live=True)
            if queue_value.get("plan_sha256") != plan_value.get("plan_sha256"):
                raise LedgerError("receive queue is not bound to the exact ledger plan")
            prior_ledger = load_ledger(args.ledger)
            prior_ledger_sha256 = ledger_sha256(prior_ledger)
            existing_receipt = None
            committed_utc = None
            if args.receipt is not None and args.receipt.exists():
                existing_receipt = read_json(args.receipt)
                if (
                    not isinstance(existing_receipt, dict)
                    or existing_receipt.get("format") != COMMIT_RECEIPT_FORMAT
                    or existing_receipt.get("commit_receipt_sha256")
                    != hashlib.sha256(canonical_bytes({key: item for key, item in existing_receipt.items() if key != "commit_receipt_sha256"})).hexdigest()
                ):
                    raise LedgerError("existing commit receipt is invalid")
                committed_utc = str(existing_receipt.get("committed_utc") or "")
                if not committed_utc:
                    raise LedgerError("existing commit receipt has no committed timestamp")
                if prior_ledger_sha256 == existing_receipt.get("ledger_sha256_after"):
                    expected_bindings = {
                        "destination_codex_home": str(caller_home),
                        "destination_instance_id": str(args.destination_instance_id).strip(),
                        "source_device": plan_value.get("source_device"),
                        "source_computer": plan_value.get("source_computer"),
                        "plan_sha256": plan_value.get("plan_sha256"),
                        "catalog_sha256": plan_value.get("catalog_sha256"),
                        "receiver_state_sha256": plan_value.get("receiver_state_sha256"),
                        "queue_sha256": queue_value.get("queue_sha256"),
                        "results_sha256": result_value.get("results_sha256"),
                        "app_proof_sha256": app_proof_value.get("app_proof_sha256"),
                        "ledger_sha256_before": plan_value.get("ledger_sha256"),
                    }
                    if any(existing_receipt.get(key) != expected for key, expected in expected_bindings.items()):
                        raise LedgerError("existing commit receipt belongs to another exact commit")
                    summary = expected_commit_summary(plan_value, result_value)
                    receipt_summary = {key: existing_receipt.get(key) for key in ("updated", "skipped", "failed")}
                    expected_status = "committed" if summary["failed"] == 0 else "incomplete"
                    if (
                        any(type(existing_receipt.get(key)) is not int for key in summary)
                        or receipt_summary != summary
                        or existing_receipt.get("status") != expected_status
                    ):
                        raise LedgerError("existing commit receipt summary does not match the exact plan and results")
                    if committed_utc != str(prior_ledger.get("updated_utc") or ""):
                        raise LedgerError("existing commit receipt timestamp does not match the committed receiver ledger")
                    summary.update(receipt=str(args.receipt.resolve()), commit_receipt_sha256=existing_receipt["commit_receipt_sha256"], idempotent=True)
                    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
                    return 2 if existing_receipt["failed"] else 0
                if prior_ledger_sha256 != existing_receipt.get("ledger_sha256_before"):
                    raise LedgerError("existing commit receipt does not match the current receiver ledger")
            value = commit_plan(plan_value, prior_ledger, result_value, committed_utc=committed_utc)
            receipt_value = None
            if args.receipt is not None:
                receipt_value = build_commit_receipt(
                    value, caller_home, str(args.destination_instance_id).strip(), plan_value,
                    queue_value, result_value, app_proof_value, prior_ledger_sha256,
                )
                # Write the receipt first. If the process stops before the ledger
                # replacement, its after-hash cannot validate and the same commit
                # can be retried safely against the unchanged pre-commit ledger.
                if existing_receipt is not None:
                    if canonical_bytes(existing_receipt) != canonical_bytes(receipt_value):
                        raise LedgerError("existing commit receipt content is not identical to this retry")
                else:
                    write_json(args.receipt, receipt_value)
            write_json(args.ledger, value["ledger"])
        summary = {key: value[key] for key in ("updated", "skipped", "failed")}
        if args.receipt is not None:
            summary.update(receipt=str(args.receipt.resolve()), commit_receipt_sha256=receipt_value["commit_receipt_sha256"])
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 2 if value["failed"] else 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
