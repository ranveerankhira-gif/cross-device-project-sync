#!/usr/bin/env python3
"""Machine-wide coordination for multiple local Codex instances.

This helper deliberately reads only the allow-listed Codex homes.  It never
copies authentication material or databases.  Upload/download network work is
still performed by the existing immutable Release helpers after this module
has produced a complete, hash-bound discovery or receiving queue.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager, closing
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
import time
import sys
from typing import Any


REGISTRY_FORMAT = "cross-device-codex-instance-registry-v1"
DISCOVERY_FORMAT = "cross-device-project-sync-multi-instance-discovery-v2"
LEDGER_V1 = "cross-device-conversation-sync-ledger-v1"
LEDGER_V2 = "cross-device-conversation-sync-ledger-v2"
QUEUE_FORMAT = "cross-device-receive-queue-v1"
LOCK_FORMAT = "cross-device-machine-operation-lock-v1"
PROJECT_LEDGER_FORMAT = "cross-device-project-receiver-ledger-v1"
SHA256_LEN = 64
MAX_BATCH_SIZE = 8


class MultiInstanceError(ValueError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def digest(value: Any) -> str:
    data = value if isinstance(value, bytes) else canonical_bytes(value)
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            handle.write(canonical_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary:
            try:
                Path(temporary).unlink()
            except FileNotFoundError:
                pass


def machine_name() -> str:
    return str(os.environ.get("COMPUTERNAME") or platform.node()).strip()


def path_key(value: Path | str) -> str:
    text = os.fspath(value)
    if os.name == "nt":
        # SQLite rollout_path values may use Win32 extended-length syntax while
        # pathlib enumeration returns the ordinary DOS/UNC spelling.  They are
        # the same file identity and must compare equal.
        folded = text.casefold()
        if folded.startswith("\\\\?\\unc\\"):
            text = "\\\\" + text[8:]
        elif folded.startswith("\\\\?\\"):
            text = text[4:]
    # realpath also expands an existing Windows 8.3 alias (for example
    # RUNNER~1) so registry and caller spellings compare by filesystem identity.
    return os.path.normpath(os.path.realpath(os.path.abspath(text))).casefold()


def is_within(path: Path, parent: Path) -> bool:
    key = path_key(path)
    parent_key = path_key(parent).rstrip("\\/")
    return key == parent_key or key.startswith(parent_key + os.sep.casefold())


def is_reparse(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def require_sha(value: Any, field: str) -> str:
    text = str(value or "").lower()
    if len(text) != SHA256_LEN or any(char not in "0123456789abcdef" for char in text):
        raise MultiInstanceError(f"{field} must be a lowercase SHA-256")
    return text


def validate_registry(path: Path, caller_home: str | None = None, require_paths: bool = True) -> tuple[dict[str, Any], dict[str, Any] | None]:
    value = read_json(path)
    if not isinstance(value, dict) or value.get("format") != REGISTRY_FORMAT:
        raise MultiInstanceError(f"unsupported instance registry: {path}")
    allowed_top = {"format", "device_label", "computer_name", "machine_state_root", "instances"}
    if set(value) - allowed_top:
        raise MultiInstanceError("instance registry contains unsupported fields")
    device_label = str(value.get("device_label") or "").strip()
    computer = str(value.get("computer_name") or "").strip()
    root_text = str(value.get("machine_state_root") or "").strip()
    if not device_label or not computer or not root_text:
        raise MultiInstanceError("instance registry device identity and machine_state_root are required")
    actual = machine_name()
    if actual and actual.casefold() != computer.casefold():
        raise MultiInstanceError(f"instance registry computer mismatch: registry={computer}, actual={actual}")
    state_root_raw = Path(root_text)
    if not state_root_raw.is_absolute():
        raise MultiInstanceError("machine_state_root must be absolute")
    state_root = state_root_raw.absolute()
    instances = value.get("instances")
    if not isinstance(instances, list) or not instances:
        raise MultiInstanceError("instance registry must contain at least one instance")
    ids: set[str] = set()
    homes: set[str] = set()
    caller: dict[str, Any] | None = None
    normalized = []
    for row in instances:
        if not isinstance(row, dict) or set(row) - {"instance_id", "display_name", "codex_home"}:
            raise MultiInstanceError("instance registry row is invalid")
        instance_id = str(row.get("instance_id") or "").strip()
        display_name = str(row.get("display_name") or "").strip()
        home_text = str(row.get("codex_home") or "").strip()
        if not instance_id or not display_name or not home_text:
            raise MultiInstanceError("instance_id, display_name, and codex_home are required")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", instance_id):
            raise MultiInstanceError("instance_id must be a safe directory identifier")
        if instance_id.casefold() in ids:
            raise MultiInstanceError(f"duplicate instance_id: {instance_id}")
        home_raw = Path(home_text)
        if not home_raw.is_absolute():
            raise MultiInstanceError(f"codex_home must be absolute: {home_text}")
        home = home_raw.absolute()
        home_key = path_key(home)
        if home_key in homes:
            raise MultiInstanceError(f"duplicate codex_home: {home}")
        if is_within(state_root, home) or is_within(home, state_root):
            raise MultiInstanceError("machine_state_root and codex_home must not contain one another")
        if require_paths:
            if not home.is_dir() or not (home / ".codex-global-state.json").is_file() or not (home / "sessions").is_dir():
                raise MultiInstanceError(f"registered Codex home is incomplete: {home}")
            if is_reparse(home):
                raise MultiInstanceError(f"registered Codex home cannot be a reparse point: {home}")
        normalized_row = {"instance_id": instance_id, "display_name": display_name, "codex_home": str(home)}
        normalized.append(normalized_row)
        ids.add(instance_id.casefold())
        homes.add(home_key)
        if caller_home and home_key == path_key(caller_home):
            caller = normalized_row
    if caller_home and caller is None:
        raise MultiInstanceError(f"calling CODEX_HOME is not allow-listed: {Path(caller_home).absolute()}")
    return {
        "format": REGISTRY_FORMAT,
        "device_label": device_label,
        "computer_name": computer,
        "machine_state_root": str(state_root),
        "instances": normalized,
    }, caller


def lock_path(registry: dict[str, Any]) -> Path:
    return Path(registry["machine_state_root"]) / "operation.lock.json"


def acquire_lock(registry: dict[str, Any], caller: dict[str, Any], operation_id: str, kind: str) -> dict[str, Any]:
    path = lock_path(registry)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            existing = read_json(path)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MultiInstanceError(
                f"machine-wide operation lock exists but is unreadable; use explicit recover-lock after inspection: {exc}"
            ) from exc
        if not isinstance(existing, dict) or existing.get("format") != LOCK_FORMAT:
            raise MultiInstanceError("machine-wide operation lock exists but has an unsupported format; use explicit recover-lock")
        raise MultiInstanceError(
            f"another machine-wide sync lock is held: {existing.get('kind')} {existing.get('operation_id')} "
            f"from {existing.get('caller_instance_id')}; creator PID lifetime is not used to steal locks"
        )
    token = secrets.token_hex(32)
    value = {
        "format": LOCK_FORMAT,
        "operation_id": operation_id,
        "kind": kind,
        "lock_token_sha256": hashlib.sha256(token.encode("ascii")).hexdigest(),
        "caller_instance_id": caller["instance_id"],
        "caller_codex_home": caller["codex_home"],
        "computer_name": registry["computer_name"],
        "device_label": registry["device_label"],
        "creator_pid": os.getpid(),
        "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, canonical_bytes(value))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return {**value, "lock_token": token, "lock_path": str(path)}


def require_lock(registry: dict[str, Any], operation_id: str, token: str, kind: str | None = None) -> dict[str, Any]:
    path = lock_path(registry)
    if not path.is_file():
        raise MultiInstanceError("machine-wide operation lock is missing")
    value = read_json(path)
    if not isinstance(value, dict) or value.get("format") != LOCK_FORMAT:
        raise MultiInstanceError("machine-wide operation lock is invalid")
    if value.get("operation_id") != operation_id:
        raise MultiInstanceError("machine-wide operation lock belongs to another operation")
    if kind and value.get("kind") != kind:
        raise MultiInstanceError("machine-wide operation lock kind mismatch")
    if value.get("lock_token_sha256") != hashlib.sha256(token.encode("ascii")).hexdigest():
        raise MultiInstanceError("machine-wide operation lock token mismatch")
    return value


def released_lock_path(registry: dict[str, Any], operation_id: str, token: str) -> Path:
    token_sha256 = hashlib.sha256(token.encode("ascii")).hexdigest()
    identity = digest({"operation_id": operation_id, "lock_token_sha256": token_sha256})
    return Path(registry["machine_state_root"]) / "released-locks" / f"{identity}.lock.json"


def require_lock_or_released(
    registry: dict[str, Any], operation_id: str, token: str, kind: str | None = None,
) -> dict[str, Any]:
    path = lock_path(registry)
    if path.is_file():
        return {"state": "held", "lock": require_lock(registry, operation_id, token, kind), "path": str(path)}
    archived = released_lock_path(registry, operation_id, token)
    if not archived.is_file():
        raise MultiInstanceError("machine-wide operation lock and deterministic release evidence are both missing")
    value = read_json(archived)
    if not isinstance(value, dict) or value.get("format") != LOCK_FORMAT:
        raise MultiInstanceError("deterministic released-lock evidence is invalid")
    if value.get("operation_id") != operation_id or (kind and value.get("kind") != kind):
        raise MultiInstanceError("released-lock evidence belongs to another operation")
    if value.get("lock_token_sha256") != hashlib.sha256(token.encode("ascii")).hexdigest():
        raise MultiInstanceError("released-lock evidence token mismatch")
    return {
        "state": "released", "lock": value, "path": str(archived),
        "sha256": hashlib.sha256(archived.read_bytes()).hexdigest(),
    }


def release_lock(registry: dict[str, Any], operation_id: str, token: str) -> dict[str, Any]:
    path = lock_path(registry)
    archived = released_lock_path(registry, operation_id, token)
    if not path.exists():
        evidence = require_lock_or_released(registry, operation_id, token)
        return {
            "released": True, "already_released": True, "operation_id": operation_id,
            "lock_path": str(path), "release_evidence_path": evidence["path"],
            "release_evidence_sha256": evidence["sha256"],
        }
    require_lock(registry, operation_id, token)
    archived.parent.mkdir(parents=True, exist_ok=True)
    if archived.exists():
        raise MultiInstanceError("deterministic released-lock evidence already exists while the lock is still held")
    os.replace(path, archived)
    evidence_sha256 = hashlib.sha256(archived.read_bytes()).hexdigest()
    return {
        "released": True, "already_released": False, "operation_id": operation_id,
        "lock_path": str(path), "release_evidence_path": str(archived),
        "release_evidence_sha256": evidence_sha256,
    }


def inspect_lock(registry: dict[str, Any]) -> dict[str, Any]:
    path = lock_path(registry)
    if not path.exists():
        return {"locked": False, "lock_path": str(path)}
    value = read_json(path)
    if not isinstance(value, dict) or value.get("format") != LOCK_FORMAT:
        raise MultiInstanceError("machine-wide operation lock has an unsupported format")
    public = {key: value.get(key) for key in (
        "format", "operation_id", "kind", "caller_instance_id", "caller_codex_home",
        "computer_name", "device_label", "creator_pid", "created_utc",
    )}
    return {"locked": True, "lock_path": str(path), "lock": public}


def recover_lock(
    registry: dict[str, Any], caller: dict[str, Any], expected_operation_id: str, reason: str,
) -> dict[str, Any]:
    """Explicitly archive an abandoned lock; never infer abandonment from a PID."""
    reason = reason.strip()
    if len(reason) < 8:
        raise MultiInstanceError("recover-lock requires an audit reason of at least 8 characters")
    path = lock_path(registry)
    if not path.is_file():
        raise MultiInstanceError("machine-wide operation lock is missing")
    value = read_json(path)
    if not isinstance(value, dict) or value.get("format") != LOCK_FORMAT:
        raise MultiInstanceError("machine-wide operation lock has an unsupported format")
    if value.get("operation_id") != expected_operation_id:
        raise MultiInstanceError("recover-lock expected_operation_id does not match the held lock")
    recovery_root = Path(registry["machine_state_root"]) / "recovered-locks"
    recovery_root.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archived = recovery_root / f"{stamp}-{secrets.token_hex(4)}-{expected_operation_id}.lock.json"
    os.replace(path, archived)
    receipt = {
        "format": "cross-device-machine-operation-lock-recovery-v1",
        "recovered_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "recovered_by_instance_id": caller["instance_id"],
        "recovered_by_codex_home": caller["codex_home"],
        "reason": reason,
        "archived_lock": str(archived),
        "archived_lock_sha256": hashlib.sha256(archived.read_bytes()).hexdigest(),
        "operation_id": expected_operation_id,
    }
    receipt_path = archived.with_suffix(".recovery.json")
    atomic_write_json(receipt_path, receipt)
    return {"recovered": True, "archived_lock": str(archived), "recovery_receipt": str(receipt_path)}


def stable_copy(source: Path, destination: Path, attempts: int = 10) -> bytes:
    last = "source changed during capture"
    for _ in range(attempts):
        try:
            before = source.stat()
            data = source.read_bytes()
            after = source.stat()
        except OSError as exc:
            last = str(exc)
            time.sleep(0.02)
            continue
        if before.st_size == after.st_size == len(data) and before.st_mtime_ns == after.st_mtime_ns:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            if destination.read_bytes() != data:
                raise MultiInstanceError(f"snapshot verification failed: {destination}")
            return data
        last = "source changed during capture"
        time.sleep(0.02)
    raise MultiInstanceError(f"could not capture a stable file after {attempts} attempts: {source}: {last}")


def freeze_history(source: Path, destination: Path) -> Path:
    if not source.is_dir() or is_reparse(source) or not (source / "FULL_TRANSCRIPT.jsonl").is_file():
        raise MultiInstanceError("retained conversation history is missing or unsafe")
    paths = sorted(source.rglob("*"), key=lambda path: str(path).casefold())
    hashes = {}
    for path in paths:
        if is_reparse(path):
            raise MultiInstanceError("retained history contains a reparse point")
        if path.is_file():
            relative = path.relative_to(source)
            data = stable_copy(path, destination / relative)
            hashes[str(relative)] = hashlib.sha256(data).hexdigest()
    after = {str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest() for path in source.rglob("*") if path.is_file()}
    if hashes != after:
        raise MultiInstanceError("retained history changed during capture; use a new snapshot")
    return destination


def first_session_payload(data: bytes, source: Path) -> dict[str, Any]:
    try:
        first = data.splitlines()[0].decode("utf-8-sig")
        row = json.loads(first)
    except (IndexError, UnicodeError, json.JSONDecodeError) as exc:
        raise MultiInstanceError(f"invalid session metadata: {source}: {exc}") from exc
    payload = row.get("payload") if isinstance(row, dict) and row.get("type") == "session_meta" else None
    if not isinstance(payload, dict):
        raise MultiInstanceError(f"session log lacks first-line session_meta payload: {source}")
    return payload


def lineage_id(origin_computer: str, origin_thread_id: str) -> str:
    return digest({"origin_computer": origin_computer.casefold(), "origin_thread_id": origin_thread_id})


def source_key_v2(lineage: str, branch: str) -> str:
    return digest({"lineage_id": lineage, "branch_id": branch})


def reverse_ledger(home: Path) -> dict[str, dict[str, Any]]:
    path = home / "cross-device-conversation-sync-ledger.json"
    if not path.is_file():
        return {}
    value = read_json(path)
    if not isinstance(value, dict) or value.get("format") not in {LEDGER_V1, LEDGER_V2} or not isinstance(value.get("entries"), dict):
        raise MultiInstanceError(f"unsupported receiver ledger: {path}")
    result: dict[str, dict[str, Any]] = {}
    for entry in value["entries"].values():
        receiving = str(entry.get("receiving_thread_id") or "").strip()
        if receiving:
            if receiving.casefold() in result:
                raise MultiInstanceError(f"receiver ledger contains duplicate receiving_thread_id: {receiving}")
            result[receiving.casefold()] = entry
    return result


def broad_root_reason(path: Path) -> str | None:
    absolute = path.absolute()
    if absolute.parent == absolute:
        return "drive root is a broad container"
    home = Path(os.environ.get("USERPROFILE") or Path.home()).absolute()
    candidates = {
        path_key(home): "user profile is a broad container",
        path_key(home / "Desktop"): "Desktop is a broad container",
        path_key(home / "Documents"): "Documents is a broad container",
        path_key(home / "Downloads"): "Downloads is a broad container",
    }
    # Include redirected Desktop/Documents/Downloads from Windows Known Folders.
    import importlib.util
    spec = importlib.util.spec_from_file_location("broad_discovery", Path(__file__).with_name("build-local-discovery.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    candidates.update(module.broad_root_map())
    return candidates.get(path_key(absolute))


def read_thread_metadata(home: Path) -> dict[str, dict[str, Any]]:
    """Read a transaction-consistent metadata projection, never copy the DB.

    No model config, auth, first messages, or database files enter the snapshot.
    The state DB is the app's task inventory; orphan logs cannot prove visibility.
    """
    database = home / "state_5.sqlite"
    if not database.is_file() or is_reparse(database):
        raise MultiInstanceError(f"cannot verify user-visible task inventory: {database}")
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
        if not {"id", "rollout_path", "source", "archived", "title"}.issubset(columns):
            raise MultiInstanceError("unsupported app task metadata schema; do not infer visibility from logs")
        selected = ["id", "rollout_path", "source", "archived", "title"]
        if "has_user_event" in columns:
            selected.append("has_user_event")
        rows = connection.execute(f"SELECT {', '.join(selected)} FROM threads ORDER BY id").fetchall()
        result = {}
        for row in rows:
            thread, rollout, source, archived, title = row[:5]
            has_user_event = None if len(row) == 5 or row[5] is None else bool(row[5])
            result[str(thread).casefold()] = {"thread_id": thread, "rollout_path": rollout, "source": source,
                                             "source_disposition": "archived" if archived else "active", "title": title,
                                             "has_user_event": has_user_event}
        return result


def visible_source(source: str) -> bool:
    # Current app uses vscode/exec/cli; subagent/guardian records are internal.
    return source in {"vscode", "exec", "cli", "app-server"}


def state_projection(state):
    return {key: state.get(key) for key in ("local-projects", "thread-project-assignments", "projectless-thread-ids",
                                          "thread-workspace-root-hints", "thread-projectless-output-directories")}


def protected_roots(registry):
    state = Path(registry["machine_state_root"]).resolve()
    return [Path(row["codex_home"]).resolve() for row in registry["instances"]] + [
        state, state.parent / "migrations", state.parent / "skill-backups", Path(__file__).resolve().parents[2]]


def overlaps(left, right):
    return is_within(left, right) or is_within(right, left)


def validate_work_root(registry, root: Path):
    root = root.resolve()
    state_root = Path(registry["machine_state_root"]).resolve()
    if is_within(root, state_root) or root == state_root.parent:
        raise MultiInstanceError("work root must not be the machine state or sync storage container")
    for instance in registry["instances"]:
        home = Path(instance["codex_home"])
        if overlaps(root, home):
            raise MultiInstanceError("work root overlaps a Codex home")
        state = read_json(home / ".codex-global-state.json")
        for project in (state.get("local-projects") or {}).values():
            for source in project.get("rootPaths") or []:
                if overlaps(root, Path(source).resolve()):
                    raise MultiInstanceError(f"work root overlaps a registered project: {source}")
    if overlaps(root, Path(__file__).resolve().parents[2]):
        raise MultiInstanceError("work root overlaps shared Skills")


def project_id_for_root(root: Path) -> str:
    return "path-" + hashlib.sha256(path_key(root).encode("utf-8")).hexdigest()[:32]


def projectless_group_id(instance_id: str) -> str:
    return "projectless-conversations-" + hashlib.sha256(instance_id.casefold().encode("utf-8")).hexdigest()[:20]


def snapshot_discovery(registry: dict[str, Any], operation_id: str, token: str, snapshot_root: Path, output: Path) -> dict[str, Any]:
    require_lock(registry, operation_id, token, "upload")
    snapshot_root = snapshot_root.absolute()
    validate_work_root(registry, snapshot_root)
    for instance in registry["instances"]:
        if is_within(snapshot_root, Path(instance["codex_home"])):
            raise MultiInstanceError("snapshot root must stay outside every Codex home")
    if output.exists():
        raise MultiInstanceError(f"discovery output already exists: {output}")
    snapshot_root.mkdir(parents=True, exist_ok=True)
    root_records: dict[str, dict[str, Any]] = {}
    local_project_roots: dict[tuple[str, str], list[str]] = {}
    session_rows: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    instance_records = []
    cloud_references = []
    for instance in registry["instances"]:
        instance_id = instance["instance_id"]
        home = Path(instance["codex_home"])
        target = snapshot_root / "instances" / instance_id
        state_source = home / ".codex-global-state.json"
        state_data = stable_copy(state_source, target / ".codex-global-state.json")
        state = json.loads(state_data.decode("utf-8-sig"))
        metadata = read_thread_metadata(home)
        explicit_projectless = set(state.get("projectless-thread-ids") or [])
        session_paths_before = sorted(str(path) for area in ("sessions", "archived_sessions") for path in (home / area).rglob("*.jsonl"))
        if not isinstance(state, dict) or not isinstance(state.get("local-projects"), dict):
            raise MultiInstanceError(f"Codex global state lacks local-projects: {state_source}")
        for local_id, raw in state["local-projects"].items():
            if not isinstance(raw, dict):
                raise MultiInstanceError(f"invalid local project record {local_id} in {state_source}")
            roots = raw.get("rootPaths")
            if not isinstance(roots, list) or not roots:
                raise MultiInstanceError(f"local project {local_id} has no rootPaths")
            normalized_roots = []
            for root_text in roots:
                root = Path(str(root_text)).resolve()
                reason = broad_root_reason(root)
                if not root.is_dir() or is_reparse(root):
                    reason = "registered project root is missing, inaccessible, or a reparse point"
                if any(overlaps(root.resolve(), protected) for protected in protected_roots(registry)):
                    reason = "registered project overlaps protected Codex, sync storage or shared Skill state"
                if reason:
                    exclusions.append({
                        "source_instance_id": instance_id,
                        "project_id": str(local_id),
                        "source_path": str(root),
                        "reason": reason,
                        "kind": "excluded-broad-root",
                    })
                    continue
                key = path_key(root)
                record = root_records.setdefault(key, {
                    "project_id": project_id_for_root(root),
                    "kind": "registered-project",
                    "label": str(raw.get("name") or root.name),
                    "source_path": str(root),
                    "registrations": [],
                    "conversations": [],
                    "exclude_relative_paths": [],
                    "exclusion_records": [],
                })
                record["registrations"].append({
                    "source_instance_id": instance_id,
                    "source_project_id": str(local_id),
                    "label": str(raw.get("name") or root.name),
                })
                normalized_roots.append(key)
            local_project_roots[(instance_id.casefold(), str(local_id).casefold())] = normalized_roots
        assignments = state.get("thread-project-assignments")
        assignments = assignments if isinstance(assignments, dict) else {}
        for assignment in assignments.values():
            if isinstance(assignment, dict) and assignment.get("projectKind") not in {None, "local"}:
                reference = {"source_instance_id": instance_id, "project_id": assignment.get("projectId"),
                             "kind": assignment.get("projectKind"), "status": "account-managed-reference-not-migrated"}
                if reference not in cloud_references:
                    cloud_references.append(reference)
        ledger_by_receiver = reverse_ledger(home)
        seen_instance_threads: set[str] = set()
        counts = {"active": 0, "archived": 0}
        for area, disposition in (("sessions", "active"), ("archived_sessions", "archived")):
            source_root = home / area
            if not source_root.is_dir():
                continue
            for session in sorted(source_root.rglob("*.jsonl"), key=lambda item: str(item).casefold()):
                relative = session.relative_to(home)
                snap = target / relative
                try:
                    data = stable_copy(session, snap)
                    payload = first_session_payload(data, session)
                    thread_id = str(payload.get("id") or "").strip()
                    cwd_text = str(payload.get("cwd") or "").strip()
                    if not thread_id or not cwd_text:
                        raise MultiInstanceError("session_meta is missing id or cwd")
                    if thread_id.casefold() in seen_instance_threads:
                        raise MultiInstanceError(f"duplicate thread ID inside instance {instance_id}: {thread_id}")
                    seen_instance_threads.add(thread_id.casefold())
                    meta = metadata.get(thread_id.casefold())
                    if meta is None:
                        exclusions.append({"source_instance_id": instance_id, "thread_id": thread_id,
                                           "kind": "unverifiable-or-deleted-task", "reason": "log is not bound to a current app task record"})
                        continue
                    if not meta.get("rollout_path") or path_key(meta["rollout_path"]) != path_key(session):
                        exclusions.append({"source_instance_id": instance_id, "thread_id": thread_id,
                                           "kind": "unreadable-session", "reason": "visible app task rollout path does not match the captured log"})
                        continue
                    if not visible_source(str(meta["source"])):
                        exclusions.append({"source_instance_id": instance_id, "thread_id": thread_id,
                                           "kind": "internal-runtime-session", "reason": "app source is internal or not a verified user-visible task source"})
                        continue
                    disposition = meta["source_disposition"]
                    counts[disposition] += 1
                    received = ledger_by_receiver.get(thread_id.casefold())
                    if received:
                        origin_computer = str(received.get("origin_computer") or received.get("source_computer") or "").strip()
                        origin_thread = str(received.get("origin_thread_id") or received.get("original_thread_id") or "").strip()
                        origin_instance = str(received.get("origin_instance_id") or received.get("source_instance_id") or "legacy-unknown")
                        lineage = str(received.get("lineage_id") or lineage_id(origin_computer, origin_thread))
                        branch = str(received.get("branch_id") or "main")
                        inherited = str(received.get("retained_handoff_path") or received.get("conversation_path") or "").strip()
                        if inherited:
                            inherited = str(freeze_history(Path(inherited), target / "retained-histories" / digest(thread_id)))
                    else:
                        origin_computer = registry["computer_name"]
                        origin_thread = thread_id
                        origin_instance = instance_id
                        lineage = lineage_id(origin_computer, origin_thread)
                        branch = "main"
                        inherited = ""
                    assignment = assignments.get(thread_id)
                    assignment = assignment if isinstance(assignment, dict) else {}
                    session_rows.append({
                        "thread_id": thread_id,
                        "thread_title": (received or {}).get("origin_title") or meta["title"],
                        "explicit_projectless": thread_id in explicit_projectless,
                        "source_instance_id": instance_id,
                        "source_instance_name": instance["display_name"],
                        "source_disposition": disposition,
                        "source_project_id": str(assignment.get("projectId") or ""),
                        "source_project_kind": str(assignment.get("projectKind") or ""),
                        "cwd": str(Path(cwd_text).absolute()),
                        "session_log": str(snap),
                        "snapshot_session_log": str(snap),
                        "original_session_log": str(session.absolute()),
                        "origin_computer": origin_computer,
                        "origin_instance_id": origin_instance,
                        "origin_thread_id": origin_thread,
                        "lineage_id": lineage,
                        "branch_id": branch,
                        "inherited_handoff_paths": [inherited] if inherited else [],
                        "inherited_working_directories": list((received or {}).get("retained_working_directories") or []),
                    })
                except (OSError, UnicodeError, json.JSONDecodeError, MultiInstanceError) as exc:
                    exclusions.append({
                        "source_instance_id": instance_id,
                        "session_log": str(session.absolute()),
                        "reason": str(exc),
                        "kind": "unreadable-session",
                    })
        missing = [row for key, row in metadata.items() if key not in seen_instance_threads and visible_source(str(row["source"]))]
        for row in missing:
            if row.get("has_user_event") is False:
                exclusions.append({"source_instance_id": instance_id, "thread_id": row["thread_id"],
                                   "kind": "empty-task-no-user-event", "reason": "app task has no user event and no conversation rollout"})
            else:
                exclusions.append({"source_instance_id": instance_id, "thread_id": row["thread_id"],
                                   "kind": "unreadable-session", "reason": "visible app task with user content has no captured rollout"})
        session_paths_after = sorted(str(path) for area in ("sessions", "archived_sessions") for path in (home / area).rglob("*.jsonl"))
        if session_paths_before != session_paths_after or metadata != read_thread_metadata(home) or state_projection(state) != state_projection(read_json(state_source)):
            raise MultiInstanceError("task/project inventory changed during capture; retry with a new snapshot directory")
        instance_records.append({
            "instance_id": instance_id,
            "display_name": instance["display_name"],
            "codex_home": str(home),
            "global_state_sha256": hashlib.sha256(state_data).hexdigest(),
            "app_task_metadata_sha256": digest(metadata),
            "app_task_count": len(metadata),
            "active_session_count": counts["active"],
            "archived_session_count": counts["archived"],
        })

    roots = [Path(record["source_path"]) for record in root_records.values()]
    roots_by_depth = sorted(roots, key=lambda item: (len(path_key(item)), path_key(item)))
    for index, parent in enumerate(roots_by_depth):
        for child in roots_by_depth[index + 1:]:
            if is_within(child, parent):
                raise MultiInstanceError(f"overlapping registered project roots must be resolved: {parent} contains {child}")

    unassigned: list[dict[str, Any]] = []
    for row in session_rows:
        project_keys = local_project_roots.get((row["source_instance_id"].casefold(), row["source_project_id"].casefold()), [])
        cwd = Path(row["cwd"])
        candidates = [key for key in project_keys if key == path_key(cwd) or is_within(cwd, Path(root_records[key]["source_path"]))]
        if row.get("explicit_projectless"):
            candidates = []
        elif not candidates:
            candidates = [key for key, record in root_records.items() if is_within(cwd, Path(record["source_path"]))]
        if candidates:
            selected = max(candidates, key=len)
            root_records[selected]["conversations"].append(row)
        else:
            unassigned.append(row)

    cwd_groups: dict[str, list[dict[str, Any]]] = {}
    cwd_paths: dict[str, Path] = {}
    for row in unassigned:
        directories = [row["cwd"], *row.get("inherited_working_directories", [])]
        for directory in dict.fromkeys(directories):
            key = path_key(directory)
            cwd_paths[key] = Path(directory)
            cwd_groups.setdefault(key, []).append(row)
    unsafe_keys: dict[str, str] = {}
    for key, cwd in cwd_paths.items():
        reason = broad_root_reason(cwd)
        if reason:
            unsafe_keys[key] = reason
            continue
        if not cwd.is_dir():
            unsafe_keys[key] = "projectless working directory is missing or inaccessible"
            continue
        if is_reparse(cwd):
            unsafe_keys[key] = "projectless working directory is a reparse point"
            continue
        if any(overlaps(cwd.resolve(), protected) for protected in protected_roots(registry) + roots + [snapshot_root]):
            unsafe_keys[key] = "projectless working directory overlaps registered project or protected local state"
            continue
        for other_key, other in cwd_paths.items():
            if other_key != key and (is_within(cwd, other) or is_within(other, cwd)):
                unsafe_keys[key] = "projectless working directory overlaps another projectless working directory"
                break
    projectless_groups: dict[str, dict[str, Any]] = {}
    for key, rows in cwd_groups.items():
        if key not in unsafe_keys:
            cwd = cwd_paths[key]
            record = root_records.setdefault(key, {
                "project_id": project_id_for_root(cwd),
                "kind": "projectless-root",
                "label": cwd.name,
                "source_path": str(cwd),
                "registrations": [],
                "conversations": [],
                "exclude_relative_paths": [],
                "exclusion_records": [],
            })
            # Directory bytes deduplicate globally, but Projectless dialogue
            # packs remain per source instance. Never invent a project binding.
            for row in rows:
                row.setdefault("projectless_directory_ids", []).append(record["project_id"])
        for row in rows:
            instance_id = row["source_instance_id"]
            group = projectless_groups.setdefault(instance_id, {
                "project_id": projectless_group_id(instance_id),
                "kind": "projectless-conversation-group",
                "label": f"{row['source_instance_name']} projectless conversations",
                "source_path": None,
                "registrations": [{"source_instance_id": instance_id, "source_project_id": None, "label": None}],
                "conversations": [],
                "exclude_relative_paths": [],
                "exclusion_records": [],
            })
            if row not in group["conversations"]:
                group["conversations"].append(row)
            if key not in unsafe_keys:
                continue
            exclusions.append({
                "source_instance_id": instance_id,
                "thread_id": row["thread_id"],
                "working_directory": str(cwd_paths[key]),
                "reason": unsafe_keys[key] + "; conversation retained without directory backup",
                "kind": "projectless-directory-not-backed-up",
            })

    projects = list(root_records.values()) + list(projectless_groups.values())
    for project in projects:
        project["registrations"] = sorted(project["registrations"], key=lambda item: (str(item["source_instance_id"]).casefold(), str(item.get("source_project_id") or "").casefold()))
        project["conversations"] = sorted(project["conversations"], key=lambda item: (item["source_instance_id"].casefold(), item["thread_id"].casefold()))
        expected = [item["thread_id"] for item in project["conversations"]]
        project["conversation_inventory"] = {"complete": True, "expected_thread_ids": expected}
    projects.sort(key=lambda item: item["project_id"].casefold())
    result = {
        "format": DISCOVERY_FORMAT,
        "operation_id": operation_id,
        "source_device": registry["device_label"],
        "source_computer": registry["computer_name"],
        "captured_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "instance_registry_sha256": digest(registry),
        "instances": sorted(instance_records, key=lambda item: item["instance_id"].casefold()),
        "projects": projects,
        "cloud_managed_projects": cloud_references,
        "exclusions": sorted(exclusions, key=lambda item: (str(item.get("source_instance_id") or "").casefold(), str(item.get("thread_id") or item.get("session_log") or item.get("source_path") or "").casefold())),
        "conversation_exclusions": [],
        "conversation_inventory_complete": not any(item.get("kind") == "unreadable-session" for item in exclusions),
        "expected_conversation_count": len(session_rows),
    }
    result["discovery_sha256"] = digest(result)
    atomic_write_json(output, result)
    return {
        "discovery": str(output.resolve()),
        "discovery_sha256": result["discovery_sha256"],
        "instance_count": len(instance_records),
        "project_count": sum(item["kind"] == "registered-project" for item in projects),
        "projectless_root_count": sum(item["kind"] == "projectless-root" for item in projects),
        "projectless_group_count": sum(item["kind"] == "projectless-conversation-group" for item in projects),
        "conversation_count": len(session_rows),
        "exclusion_count": len(exclusions),
        "complete": result["conversation_inventory_complete"],
    }


def migrate_ledger(path: Path, destination_instance_id: str, backup_dir: Path) -> dict[str, Any]:
    value = read_json(path) if path.exists() else {"format": LEDGER_V2, "entries": {}}
    if not isinstance(value, dict) or value.get("format") not in {LEDGER_V1, LEDGER_V2} or not isinstance(value.get("entries"), dict):
        raise MultiInstanceError(f"unsupported receiver ledger: {path}")
    if value.get("format") == LEDGER_V2:
        return {"ledger": str(path.resolve()), "format": LEDGER_V2, "entry_count": len(value["entries"]), "migrated": False}
    migrated: dict[str, Any] = {}
    for old_key, raw in value["entries"].items():
        if not isinstance(raw, dict):
            raise MultiInstanceError(f"invalid legacy ledger entry: {old_key}")
        origin_computer = str(raw.get("source_computer") or "").strip()
        origin_thread = str(raw.get("original_thread_id") or "").strip()
        if not origin_computer or not origin_thread:
            raise MultiInstanceError(f"legacy ledger entry lacks source identity: {old_key}")
        lineage = lineage_id(origin_computer, origin_thread)
        branch = "main"
        key = source_key_v2(lineage, branch)
        if key in migrated:
            raise MultiInstanceError("legacy ledger migration produced a duplicate lineage branch")
        migrated[key] = {
            **raw,
            "origin_computer": origin_computer,
            "origin_instance_id": str(raw.get("source_instance_id") or "legacy-unknown"),
            "origin_thread_id": origin_thread,
            "lineage_id": lineage,
            "branch_id": branch,
            "destination_instance_id": destination_instance_id,
            "legacy_source_key": old_key,
        }
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir / f"{path.name}.v1-{stamp}-{hashlib.sha256(path.read_bytes()).hexdigest()[:12]}.json"
    if backup.exists():
        raise MultiInstanceError(f"ledger backup already exists: {backup}")
    shutil.copy2(path, backup)
    if hashlib.sha256(backup.read_bytes()).digest() != hashlib.sha256(path.read_bytes()).digest():
        raise MultiInstanceError("legacy ledger backup verification failed")
    output = {
        "format": LEDGER_V2,
        "migrated_from": LEDGER_V1,
        "destination_instance_id": destination_instance_id,
        "entries": migrated,
        "updated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    atomic_write_json(path, output)
    return {"ledger": str(path.resolve()), "backup": str(backup.resolve()), "format": LEDGER_V2, "entry_count": len(migrated), "migrated": True}


def queue_engine():
    import importlib.util
    spec = importlib.util.spec_from_file_location("receive_queue_engine", Path(__file__).with_name("receive-queue-engine.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def project_transaction():
    import importlib.util
    spec = importlib.util.spec_from_file_location("project_receive_transaction", Path(__file__).with_name("project-receive-transaction.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def actual_caller_home() -> str:
    return str(Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).resolve())


def require_actual_caller(requested: str) -> None:
    if path_key(Path(requested).resolve()) != path_key(actual_caller_home()):
        raise MultiInstanceError("caller-home does not match this process CODEX_HOME")


def build_queue(catalog_path: Path, registry: dict[str, Any], caller: dict[str, Any], output: Path, plan_path: Path | None = None) -> dict[str, Any]:
    if plan_path is None:
        raise MultiInstanceError("receive queue requires a verified ledger plan; catalog-only creation is unsafe")
    return queue_engine().build(catalog_path, caller, output, plan_path)


def build_receiver_state(catalog_path, registry, caller, output):
    queue_engine().require_account(output, caller["codex_home"])
    catalog = read_json(catalog_path)
    if catalog.get("catalog_sha256") != digest({key: value for key, value in catalog.items() if key != "catalog_sha256"}):
        raise MultiInstanceError("receiving catalog hash mismatch")
    if catalog.get("format") != "cross-device-receiving-task-catalog-v2":
        raise MultiInstanceError("automatic receiver-state capture requires a v2 catalog")
    home = Path(caller["codex_home"])
    metadata = read_thread_metadata(home)
    ledger_path = home / "cross-device-conversation-sync-ledger.json"
    ledger = read_json(ledger_path) if ledger_path.exists() else {"format": LEDGER_V2, "entries": {}}
    if ledger.get("format") != LEDGER_V2:
        raise MultiInstanceError("migrate the current account's v1 ledger before capturing v2 receiver state")
    rows, receiver_owners = {}, {}
    for key, entry in ledger["entries"].items():
        lineage = entry.get("lineage_id") or lineage_id(entry["source_computer"], entry["original_thread_id"])
        branch = entry.get("branch_id") or "main"
        receiver = str(entry.get("receiving_thread_id") or "")
        live = metadata.get(receiver.casefold()) if receiver else None
        identity_key = source_key_v2(lineage, branch)
        if receiver and receiver in receiver_owners:
            raise MultiInstanceError("receiver ledger has duplicate task ownership")
        if receiver:
            receiver_owners[receiver] = identity_key
        rows[identity_key] = {"original_thread_id": entry.get("original_thread_id") or entry.get("origin_thread_id"),
                              "lineage_id": lineage, "branch_id": branch, "receiving_thread_id": receiver or None,
                              "receiver_disposition": live["source_disposition"] if live else "deleted"}
    # Native origin tasks must be recognized when their descendants return home.
    for live in metadata.values():
        if not visible_source(str(live["source"])) or live["thread_id"] in receiver_owners:
            continue
        lineage = lineage_id(registry["computer_name"], live["thread_id"])
        key = source_key_v2(lineage, "main")
        if key in rows and rows[key]["receiving_thread_id"] != live["thread_id"]:
            raise MultiInstanceError("native task and received copy conflict for one lineage")
        rows[key] = {"original_thread_id": live["thread_id"], "lineage_id": lineage, "branch_id": "main",
                     "receiving_thread_id": live["thread_id"], "receiver_disposition": live["source_disposition"]}
    if metadata != read_thread_metadata(home):
        raise MultiInstanceError("receiver metadata changed during capture; retry")
    value = {"format": "cross-device-receiver-state-v2", "source_computer": catalog["source_computer"],
             "destination_codex_home": caller["codex_home"], "destination_instance_id": caller["instance_id"],
             "captured_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "complete": True,
             "active_inventory_complete": True, "archived_inventory_complete": True,
             "thread_count": len(rows), "threads": [rows[key] for key in sorted(rows)]}
    atomic_write_json(output, value)
    return {"receiver_state": str(output), "thread_count": len(rows), "destination_instance_id": caller["instance_id"]}


def next_queue_batch(path: Path, caller_home: str | None = None, claim: bool = False) -> dict[str, Any]:
    return queue_engine().batch(path, caller_home or actual_caller_home(), claim)


def record_queue_result(path: Path, operation_key: str, claim_token: str, observation: dict[str, Any], caller_home: str | None = None) -> dict[str, Any]:
    return queue_engine().record(path, caller_home or actual_caller_home(), operation_key, claim_token, observation)


def plan_project_destination(ledger_path: Path, project_key: str, target_tree_sha256: str,
                             current_tree_sha256: str | None, base_restore_root: Path,
                             source_name: str, source_device: str) -> dict[str, Any]:
    target = require_sha(target_tree_sha256, "target_tree_sha256")
    current = require_sha(current_tree_sha256, "current_tree_sha256") if current_tree_sha256 else None
    ledger = read_json(ledger_path) if ledger_path.exists() else {"format": PROJECT_LEDGER_FORMAT, "projects": {}}
    if not isinstance(ledger, dict) or ledger.get("format") != PROJECT_LEDGER_FORMAT or not isinstance(ledger.get("projects"), dict):
        raise MultiInstanceError("unsupported project receiver ledger")
    old = ledger["projects"].get(project_key)
    if old and current == target:
        action = "reuse-unchanged"
        destination = str(old.get("restored_project_root") or "")
    elif old and current and current == old.get("last_applied_tree_sha256"):
        action = "transactional-safe-replace"
        destination = str(old.get("restored_project_root") or "")
    else:
        action = "restore-side-by-side"
        destination = str(project_transaction().choose_new_destination(
            base_restore_root, source_name, source_device, project_key, target
        ).absolute())
    return {
        "project_key": project_key,
        "action": action,
        "destination": destination,
        "target_tree_sha256": target,
        "current_tree_sha256": current,
        "rollback_required": action == "transactional-safe-replace",
        "must_not_overlay": action == "restore-side-by-side",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command", required=True)
    validate = subs.add_parser("validate-registry")
    validate.add_argument("--registry", type=Path, required=True)
    validate.add_argument("--caller-home")
    work = subs.add_parser("validate-work-root")
    work.add_argument("--registry", type=Path, required=True)
    work.add_argument("--work-root", type=Path, required=True)
    acquire = subs.add_parser("acquire-lock")
    acquire.add_argument("--registry", type=Path, required=True)
    acquire.add_argument("--caller-home", required=True)
    acquire.add_argument("--operation-id", required=True)
    acquire.add_argument("--kind", choices=("upload", "download"), required=True)
    release = subs.add_parser("release-lock")
    release.add_argument("--registry", type=Path, required=True)
    release.add_argument("--operation-id", required=True)
    release.add_argument("--lock-token", required=True)
    validate_lock = subs.add_parser("validate-lock")
    validate_lock.add_argument("--registry", type=Path, required=True)
    validate_lock.add_argument("--operation-id", required=True)
    validate_lock.add_argument("--lock-token", required=True)
    validate_lock.add_argument("--kind", choices=("upload", "download"))
    inspect = subs.add_parser("inspect-lock")
    inspect.add_argument("--registry", type=Path, required=True)
    recover = subs.add_parser("recover-lock")
    recover.add_argument("--registry", type=Path, required=True)
    recover.add_argument("--caller-home", required=True)
    recover.add_argument("--expected-operation-id", required=True)
    recover.add_argument("--reason", required=True)
    snapshot = subs.add_parser("snapshot-discovery")
    snapshot.add_argument("--registry", type=Path, required=True)
    snapshot.add_argument("--operation-id", required=True)
    snapshot.add_argument("--lock-token", required=True)
    snapshot.add_argument("--snapshot-root", type=Path, required=True)
    snapshot.add_argument("--output", type=Path, required=True)
    migrate = subs.add_parser("migrate-ledger")
    migrate.add_argument("--ledger", type=Path, required=True)
    migrate.add_argument("--destination-instance-id", required=True)
    migrate.add_argument("--backup-dir", type=Path, required=True)
    queue = subs.add_parser("build-receive-queue")
    queue.add_argument("--registry", type=Path, required=True)
    queue.add_argument("--caller-home", required=True)
    queue.add_argument("--catalog", type=Path, required=True)
    queue.add_argument("--plan", type=Path, required=True)
    queue.add_argument("--output", type=Path, required=True)
    receiver_state = subs.add_parser("build-receiver-state")
    receiver_state.add_argument("--registry", type=Path, required=True)
    receiver_state.add_argument("--catalog", type=Path, required=True)
    receiver_state.add_argument("--output", type=Path, required=True)
    record = subs.add_parser("record-queue-result")
    record.add_argument("--queue", type=Path, required=True)
    record.add_argument("--operation-key", required=True)
    record.add_argument("--claim-token", required=True)
    record.add_argument("--observation", type=Path, required=True)
    next_batch = subs.add_parser("next-receive-batch")
    next_batch.add_argument("--queue", type=Path, required=True)
    next_batch.add_argument("--claim", action="store_true")
    results = subs.add_parser("queue-ledger-results")
    results.add_argument("--queue", type=Path, required=True)
    results.add_argument("--output", type=Path, required=True)
    restore_recover = subs.add_parser("recover-project-update")
    restore_recover.add_argument("--registry", type=Path, required=True)
    restore_recover.add_argument("--journal", type=Path, required=True)
    destination = subs.add_parser("plan-project-destination")
    destination.add_argument("--ledger", type=Path, required=True)
    destination.add_argument("--project-key", required=True)
    destination.add_argument("--target-tree-sha256", required=True)
    destination.add_argument("--current-tree-sha256")
    destination.add_argument("--base-restore-root", type=Path, required=True)
    destination.add_argument("--source-name", required=True)
    destination.add_argument("--source-device", required=True)
    args = parser.parse_args()
    try:
        if getattr(args, "caller_home", None):
            require_actual_caller(args.caller_home)
        if args.command == "validate-registry":
            registry, caller = validate_registry(args.registry, args.caller_home)
            result = {"valid": True, "registry": str(args.registry.resolve()), "device_label": registry["device_label"], "computer_name": registry["computer_name"], "instance_count": len(registry["instances"]), "caller_instance_id": caller["instance_id"] if caller else None}
        elif args.command == "acquire-lock":
            registry, caller = validate_registry(args.registry, args.caller_home)
            assert caller is not None
            result = acquire_lock(registry, caller, args.operation_id, args.kind)
        elif args.command == "validate-work-root":
            registry, _ = validate_registry(args.registry, actual_caller_home())
            validate_work_root(registry, args.work_root)
            result = {"valid": True, "work_root": str(args.work_root.resolve())}
        elif args.command == "release-lock":
            registry, _ = validate_registry(args.registry)
            require_actual_caller(require_lock(registry, args.operation_id, args.lock_token)["caller_codex_home"])
            result = release_lock(registry, args.operation_id, args.lock_token)
        elif args.command == "validate-lock":
            registry, _ = validate_registry(args.registry)
            held = require_lock(registry, args.operation_id, args.lock_token, args.kind)
            require_actual_caller(held["caller_codex_home"])
            result = {"valid": True, "operation_id": held["operation_id"]}
        elif args.command == "inspect-lock":
            registry, _ = validate_registry(args.registry)
            result = inspect_lock(registry)
        elif args.command == "recover-lock":
            registry, caller = validate_registry(args.registry, args.caller_home)
            assert caller is not None
            result = recover_lock(registry, caller, args.expected_operation_id, args.reason)
        elif args.command == "snapshot-discovery":
            registry, _ = validate_registry(args.registry)
            require_actual_caller(require_lock(registry, args.operation_id, args.lock_token)["caller_codex_home"])
            result = snapshot_discovery(registry, args.operation_id, args.lock_token, args.snapshot_root, args.output)
        elif args.command == "migrate-ledger":
            result = migrate_ledger(args.ledger, args.destination_instance_id, args.backup_dir)
        elif args.command == "build-receive-queue":
            registry, caller = validate_registry(args.registry, args.caller_home)
            assert caller is not None
            result = build_queue(args.catalog, registry, caller, args.output, args.plan)
        elif args.command == "build-receiver-state":
            registry, caller = validate_registry(args.registry, actual_caller_home())
            result = build_receiver_state(args.catalog, registry, caller, args.output)
        elif args.command == "record-queue-result":
            result = record_queue_result(args.queue, args.operation_key, args.claim_token, read_json(args.observation))
        elif args.command == "next-receive-batch":
            result = next_queue_batch(args.queue, claim=args.claim)
        elif args.command == "queue-ledger-results":
            queue_engine().require_account(args.output, actual_caller_home())
            result = queue_engine().ledger_results(args.queue, actual_caller_home())
            atomic_write_json(args.output, result)
        elif args.command == "recover-project-update":
            registry, caller = validate_registry(args.registry, actual_caller_home())
            import importlib.util
            spec = importlib.util.spec_from_file_location("recover_project", Path(__file__).with_name("project-receive-transaction.py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            base = Path(registry["machine_state_root"]).parent / "receivers" / digest(actual_caller_home().casefold())[:24]
            result = module.recover(args.journal, actual_caller_home(), base)
        else:
            result = plan_project_destination(
                args.ledger, args.project_key, args.target_tree_sha256,
                args.current_tree_sha256, args.base_restore_root,
                args.source_name, args.source_device,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, UnicodeError, json.JSONDecodeError, MultiInstanceError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
