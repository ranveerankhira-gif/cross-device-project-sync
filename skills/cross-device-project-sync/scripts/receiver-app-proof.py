#!/usr/bin/env python3
"""Capture and verify live Codex task evidence for a completed receive queue.

The queue's ``last_observation`` is orchestration state, not independent proof.
This helper reads the calling account's SQLite task inventory in a read-only
transaction and verifies the task ID, project binding, archive disposition and
deterministic receipt marker before the receiver ledger may be committed.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
from typing import Any


FORMAT = "cross-device-receiver-app-proof-v2"
WRITE_ACTIONS = {
    "register-new", "update-existing", "apply-retained-update",
    "resolve-divergence-incoming", "resolve-divergence-retained",
}
ROLLOUT_RECEIPT_ACTIONS = WRITE_ACTIONS - {"register-new"}
DONE_WRITE_STATES = {"registered-lazy", "context-loaded"}
VISIBLE_SOURCES = {"vscode", "exec", "cli", "app-server"}


class AppProofError(ValueError):
    pass


def canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def load_local(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, Path(__file__).with_name(filename))
    if spec is None or spec.loader is None:
        raise AppProofError(f"cannot load required helper: {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def same_path(left: Any, right: Any) -> bool:
    return os.path.normcase(str(Path(str(left)).resolve())) == os.path.normcase(str(Path(str(right)).resolve()))


def is_reparse(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def normalize_rollout_path(value: Any) -> Path:
    text = str(value or "").strip()
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    return Path(text)


def inside(path: Path, parent: Path) -> bool:
    path, parent = path.resolve(), parent.resolve()
    return path == parent or parent in path.parents


def stable_rollout_bytes(path: Path) -> bytes:
    snapshots = []
    for _ in range(2):
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after or len(data) != after.st_size:
            raise AppProofError(f"receiving task rollout changed during verification: {path}")
        snapshots.append((identity_after, data))
    if snapshots[0] != snapshots[1]:
        raise AppProofError(f"receiving task rollout was not stable across reads: {path}")
    return snapshots[0][1]


def read_context_evidence(caller_home: Path, raw_path: Any, expected_sha256: str) -> dict[str, str]:
    path = Path(str(raw_path or ""))
    if not path.is_absolute():
        raise AppProofError("context evidence report path must be absolute")
    resolved = path.resolve(strict=True)
    if not inside(resolved, caller_home) or not resolved.is_file() or is_reparse(resolved):
        raise AppProofError("context evidence report is missing, unsafe, or belongs to another Codex account")
    data = stable_rollout_bytes(resolved)
    actual = digest(data)
    if actual != expected_sha256:
        raise AppProofError("context evidence report SHA-256 mismatch")
    return {"path": str(resolved), "sha256": actual}


def read_rollout_evidence(caller_home: Path, raw_path: Any, expected_thread_id: str) -> dict[str, Any]:
    rollout = normalize_rollout_path(raw_path)
    if not str(rollout):
        raise AppProofError(f"receiving task has no rollout path: {expected_thread_id}")
    if not rollout.is_absolute():
        raise AppProofError(f"receiving task rollout path is not absolute: {rollout}")
    lexical = Path(os.path.abspath(str(rollout)))
    raw_roots = [caller_home / "sessions", caller_home / "archived_sessions"]
    raw_owner = None
    for root in raw_roots:
        try:
            lexical.relative_to(root)
            raw_owner = root
            break
        except ValueError:
            continue
    if raw_owner is None or not raw_owner.exists():
        raise AppProofError(f"receiving task rollout is outside the calling CODEX_HOME session roots: {lexical}")
    current = raw_owner
    if is_reparse(current):
        raise AppProofError(f"receiving task session root is a reparse point: {current}")
    for component in lexical.relative_to(raw_owner).parts:
        current = current / component
        if is_reparse(current):
            raise AppProofError(f"receiving task rollout path contains a reparse point: {current}")
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise AppProofError(f"receiving task rollout is missing: {rollout}: {exc}") from exc
    owner = next((root.resolve() for root in raw_roots if root.exists() and inside(resolved, root)), None)
    if owner is None:
        raise AppProofError(f"receiving task rollout is outside the calling CODEX_HOME session roots: {resolved}")
    if not resolved.is_file() or is_reparse(resolved):
        raise AppProofError(f"receiving task rollout is missing or unsafe: {resolved}")
    data = stable_rollout_bytes(resolved)
    session_ids: set[str] = set()
    user_messages: list[str] = []
    for line_number, raw_line in enumerate(data.splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line.decode("utf-8-sig" if line_number == 1 else "utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise AppProofError(f"invalid receiving task rollout JSONL at line {line_number}: {resolved}") from exc
        if not isinstance(record, dict):
            raise AppProofError(f"invalid receiving task rollout record at line {line_number}: {resolved}")
        payload = record.get("payload")
        if record.get("type") == "session_meta" and isinstance(payload, dict):
            session_id = str(payload.get("id") or "").strip()
            if session_id:
                session_ids.add(session_id.casefold())
        if (
            record.get("type") != "response_item" or not isinstance(payload, dict)
            or payload.get("type") != "message" or payload.get("role") != "user"
        ):
            continue
        parts = payload.get("content")
        if not isinstance(parts, list):
            raise AppProofError(f"receiving task user message has invalid content at line {line_number}: {resolved}")
        texts = []
        for part in parts:
            if isinstance(part, dict) and part.get("type") in {"input_text", "text"} and isinstance(part.get("text"), str):
                texts.append(part["text"])
        user_messages.append("\n".join(texts))
    if session_ids != {expected_thread_id.casefold()}:
        raise AppProofError(f"receiving task rollout is not bound to its exact task ID: {expected_thread_id}")
    return {
        "rollout_path": str(resolved),
        "rollout_sha256": digest(data),
        "rollout_user_message_count": len(user_messages),
        "user_messages": user_messages,
    }


def read_live_threads(caller_home: Path) -> dict[str, dict[str, Any]]:
    database = caller_home / "state_5.sqlite"
    if not database.is_file() or is_reparse(database):
        raise AppProofError(f"calling account task database is missing or unsafe: {database}")
    uri = database.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
        required = {"id", "source", "archived", "project_id", "first_user_message"}
        if not required.issubset(columns):
            raise AppProofError("unsupported Codex task database schema for live receiver verification")
        selected = ["id", "source", "archived", "project_id", "first_user_message"]
        has_rollout_path = "rollout_path" in columns
        if has_rollout_path:
            selected.append("rollout_path")
        if "has_user_event" in columns:
            selected.append("has_user_event")
        if "cwd" in columns:
            selected.append("cwd")
        rows = connection.execute(f"SELECT {', '.join(selected)} FROM threads ORDER BY id").fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            thread_id = str(row[0] or "").strip()
            if not thread_id or thread_id.casefold() in result:
                raise AppProofError("Codex task database contains an empty or duplicate task ID")
            result[thread_id.casefold()] = {
                "thread_id": thread_id,
                "source": str(row[1] or ""),
                "receiver_disposition": "archived" if bool(row[2]) else "active",
                "project_id": None if row[3] is None else str(row[3]),
                "first_user_message": str(row[4] or ""),
                "rollout_path": None if not has_rollout_path else str(row[5] or ""),
                "has_user_event": None if "has_user_event" not in selected or row[selected.index("has_user_event")] is None else bool(row[selected.index("has_user_event")]),
                "cwd": None if "cwd" not in selected else str(row[selected.index("cwd")] or ""),
            }
        return result


def read_app_project_bindings(caller_home: Path) -> dict[str, Any]:
    """Read the desktop app's current project membership projection.

    Newer desktop builds keep local project membership in global state while
    leaving ``threads.project_id`` NULL.  The global state is therefore the
    authoritative source when present; SQLite remains a legacy fallback.
    """
    path = caller_home / ".codex-global-state.json"
    if not path.exists():
        return {"available": False, "assignments": {}, "projectless": set(), "projects": {}}
    if not path.is_file() or is_reparse(path):
        raise AppProofError(f"calling account global state is missing or unsafe: {path}")
    before = path.stat()
    data = path.read_bytes()
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    ) or len(data) != after.st_size:
        raise AppProofError("calling account global state changed during verification; retry")
    try:
        state = json.loads(data.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AppProofError("calling account global state is invalid JSON") from exc
    if not isinstance(state, dict):
        raise AppProofError("calling account global state must be an object")
    assignments = state.get("thread-project-assignments") or {}
    projectless = state.get("projectless-thread-ids") or []
    projects = state.get("local-projects") or {}
    if not isinstance(assignments, dict) or not isinstance(projectless, list) or not isinstance(projects, dict):
        raise AppProofError("calling account global state has an unsupported project-membership schema")
    normalized_assignments: dict[str, str] = {}
    for thread_id, value in assignments.items():
        if not isinstance(value, dict):
            raise AppProofError("calling account global state contains an invalid thread project assignment")
        project_kind = str(value.get("projectKind") or "")
        project_id = str(value.get("projectId") or "").strip()
        if project_kind != "local" or not project_id:
            continue
        normalized_assignments[str(thread_id).casefold()] = project_id
    normalized_projectless = {str(thread_id).casefold() for thread_id in projectless if str(thread_id).strip()}
    if set(normalized_assignments) & normalized_projectless:
        raise AppProofError("calling account global state marks a thread as both project-bound and Projectless")
    return {
        "available": True,
        "assignments": normalized_assignments,
        "projectless": normalized_projectless,
        "projects": projects,
    }


def verified_project_binding(
    app_state: dict[str, Any], row: dict[str, Any], task: dict[str, Any], thread_id: str,
) -> str:
    target_kind = str(task.get("target_kind") or "")
    target_project_id = task.get("target_project_id")
    database_project_id = row.get("project_id")
    key = thread_id.casefold()
    assigned_project_id = app_state["assignments"].get(key) if app_state["available"] else None
    explicit_projectless = key in app_state["projectless"] if app_state["available"] else False
    if assigned_project_id and database_project_id is not None and assigned_project_id != str(database_project_id):
        raise AppProofError(f"receiving task project sources disagree: {thread_id}")
    effective_project_id = assigned_project_id or (None if database_project_id is None else str(database_project_id))
    if target_kind == "project":
        if not target_project_id or effective_project_id != str(target_project_id):
            raise AppProofError(f"receiving task project binding mismatch: {thread_id}")
        if app_state["available"]:
            project = app_state["projects"].get(str(target_project_id))
            roots = project.get("rootPaths") if isinstance(project, dict) else None
            restored_root = str(task.get("restored_project_root") or "").strip()
            if not isinstance(roots, list) or not restored_root or not any(same_path(root, restored_root) for root in roots):
                raise AppProofError(f"receiving task project root is not registered to the target project: {thread_id}")
            if row.get("cwd") and not same_path(row["cwd"], restored_root):
                raise AppProofError(f"receiving task working directory does not match the restored project: {thread_id}")
            return "global-state-thread-project-assignment"
        return "sqlite-project-id"
    if target_kind == "projectless":
        if target_project_id is not None or effective_project_id is not None:
            raise AppProofError(f"Projectless receiving task is bound to a project: {thread_id}")
        if app_state["available"] and not explicit_projectless:
            raise AppProofError(f"receiving task is not explicitly registered as Projectless: {thread_id}")
        return "global-state-projectless-inventory" if app_state["available"] else "sqlite-null-project-id"
    raise AppProofError(f"unsupported receiving task target kind: {target_kind!r}")


def proof_projection(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "destination_codex_home": value.get("destination_codex_home"),
        "destination_instance_id": value.get("destination_instance_id"),
        "queue_sha256": value.get("queue_sha256"),
        "app_write_count": value.get("app_write_count"),
        "tasks": value.get("tasks"),
    }


def build_proof(queue_path: Path, caller_home: Path) -> dict[str, Any]:
    queue_module = load_local("receive-queue-engine.py", "app_proof_queue")
    queue = queue_module.load(queue_path, str(caller_home))
    # This also proves the complete queue can deterministically produce ledger
    # results; unfinished or contradictory tasks stop before the DB is trusted.
    queue_module.ledger_results(queue_path, str(caller_home))
    live = read_live_threads(caller_home)
    app_state = read_app_project_bindings(caller_home)
    tasks = []
    seen: set[str] = set()
    for task in queue.get("tasks") or []:
        if task.get("action") not in WRITE_ACTIONS:
            continue
        if task.get("status") not in DONE_WRITE_STATES:
            raise AppProofError("app-write queue item is not complete")
        thread_id = str(task.get("receiving_thread_id") or "").strip()
        if not thread_id:
            raise AppProofError("app-write queue item has no receiving task ID")
        key = thread_id.casefold()
        if key in seen:
            raise AppProofError("one live receiving task ID is assigned to multiple queue items")
        seen.add(key)
        row = live.get(key)
        if row is None:
            raise AppProofError(f"receiving task does not exist in the calling account: {thread_id}")
        if row["source"] not in VISIBLE_SOURCES:
            raise AppProofError(f"receiving task is not a verified user-visible Codex task: {thread_id}")
        marker = str(task.get("receipt_marker") or "")
        if not marker:
            raise AppProofError(f"receiving task has no exact receipt marker: {thread_id}")
        action = str(task.get("action") or "")
        status = str(task.get("status") or "")
        rollout_evidence = None
        if action in ROLLOUT_RECEIPT_ACTIONS or status == "context-loaded":
            rollout_evidence = read_rollout_evidence(caller_home, row.get("rollout_path"), thread_id)
        if action == "register-new":
            if marker in row["first_user_message"]:
                receipt_evidence_type = "sqlite-first-user-message"
            else:
                rollout_evidence = read_rollout_evidence(caller_home, row.get("rollout_path"), thread_id)
                if not any(marker in text for text in rollout_evidence["user_messages"]):
                    raise AppProofError(f"new receiving task has no role-parsed user message containing its sync receipt marker: {thread_id}")
                receipt_evidence_type = "rollout-user-message"
        else:
            if rollout_evidence is None or not any(marker in text for text in rollout_evidence["user_messages"]):
                raise AppProofError(f"receiving task rollout has no user message containing this sync receipt marker: {thread_id}")
            receipt_evidence_type = "rollout-user-message"
        expected_disposition = str(task.get("expected_disposition") or "")
        if expected_disposition not in {"active", "archived"} or row["receiver_disposition"] != expected_disposition:
            raise AppProofError(f"receiving task archive disposition mismatch: {thread_id}")
        target_kind = str(task.get("target_kind") or "")
        target_project_id = task.get("target_project_id")
        project_binding_evidence_type = verified_project_binding(app_state, row, task, thread_id)
        proof_task = {
            "operation_key": task.get("operation_key"),
            "action": action,
            "status": status,
            "receiving_thread_id": thread_id,
            "receipt_marker_sha256": digest(marker.encode("utf-8")),
            "receipt_evidence_type": receipt_evidence_type,
            "target_kind": target_kind,
            "target_project_id": target_project_id,
            "receiver_disposition": row["receiver_disposition"],
            "source": row["source"],
            "project_binding_evidence_type": project_binding_evidence_type,
            "has_user_event": row["has_user_event"],
            "first_user_message_sha256": digest(row["first_user_message"].encode("utf-8")),
        }
        if rollout_evidence is not None:
            proof_task.update({
                "rollout_path": rollout_evidence["rollout_path"],
                "rollout_sha256": rollout_evidence["rollout_sha256"],
                "rollout_user_message_count": rollout_evidence["rollout_user_message_count"],
            })
        if status == "context-loaded":
            observation = task.get("last_observation") or {}
            context_report_sha256 = str(observation.get("context_evidence_report_sha256") or "").lower()
            context_marker = str(observation.get("context_load_marker") or "")
            expected_context_marker = queue_module.context_load_marker(task, context_report_sha256)
            if context_marker != expected_context_marker:
                raise AppProofError(f"context-loaded task has an invalid independent context marker: {thread_id}")
            if rollout_evidence is None or not any(context_marker in text for text in rollout_evidence["user_messages"]):
                raise AppProofError(f"context-loaded task rollout has no user message containing its context marker: {thread_id}")
            context_evidence = read_context_evidence(
                caller_home, observation.get("context_evidence_report_path"), context_report_sha256,
            )
            proof_task.update({
                "context_evidence_type": "rollout-user-message",
                "context_evidence_report_sha256": context_report_sha256,
                "context_evidence_report_path": context_evidence["path"],
                "context_load_marker_sha256": digest(context_marker.encode("utf-8")),
            })
        tasks.append(proof_task)
    tasks.sort(key=lambda item: str(item["operation_key"]))
    proof = {
        "format": FORMAT,
        "status": "verified-live-app-state",
        "captured_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "destination_codex_home": str(caller_home.resolve()),
        "destination_instance_id": queue.get("destination_instance_id"),
        "queue_sha256": queue.get("queue_sha256"),
        "app_write_count": len(tasks),
        "tasks": tasks,
    }
    proof["app_proof_sha256"] = digest(proof)
    return proof


def validate_proof(proof: dict[str, Any], queue_path: Path, caller_home: Path, *, verify_live: bool = True) -> dict[str, Any]:
    if not isinstance(proof, dict) or proof.get("format") != FORMAT or proof.get("status") != "verified-live-app-state":
        raise AppProofError("unsupported or incomplete receiver app proof")
    claimed = str(proof.get("app_proof_sha256") or "").lower()
    unsigned = {key: value for key, value in proof.items() if key != "app_proof_sha256"}
    if len(claimed) != 64 or claimed != digest(unsigned):
        raise AppProofError("receiver app proof hash mismatch")
    if not same_path(proof.get("destination_codex_home"), caller_home):
        raise AppProofError("receiver app proof belongs to another Codex account")
    queue_module = load_local("receive-queue-engine.py", "app_proof_verify_queue")
    queue = queue_module.load(queue_path, str(caller_home))
    if proof.get("destination_instance_id") != queue.get("destination_instance_id") or proof.get("queue_sha256") != queue.get("queue_sha256"):
        raise AppProofError("receiver app proof is not bound to the exact queue")
    if verify_live:
        current = build_proof(queue_path, caller_home)
        if proof_projection(proof) != proof_projection(current):
            raise AppProofError("live Codex task state changed after the receiver app proof")
    return proof


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    queue_module = load_local("receive-queue-engine.py", "app_proof_writer")
    queue_module.write(path, value)


def main() -> int:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command", required=True)
    capture = subs.add_parser("capture")
    capture.add_argument("--queue", type=Path, required=True)
    capture.add_argument("--output", type=Path, required=True)
    verify = subs.add_parser("verify")
    verify.add_argument("--queue", type=Path, required=True)
    verify.add_argument("--proof", type=Path, required=True)
    args = parser.parse_args()
    caller_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).resolve()
    try:
        if args.command == "capture":
            output = args.output.resolve()
            if caller_home not in output.parents or output.exists():
                raise AppProofError("app proof output must be a new file inside the calling CODEX_HOME")
            value = build_proof(args.queue.resolve(), caller_home)
            atomic_write(output, value)
            result = {"status": value["status"], "proof": str(output), "app_proof_sha256": value["app_proof_sha256"], "app_write_count": value["app_write_count"]}
        else:
            value = json.loads(args.proof.read_text(encoding="utf-8-sig"))
            validate_proof(value, args.queue.resolve(), caller_home, verify_live=True)
            result = {"status": "verified-live-app-state", "app_proof_sha256": value["app_proof_sha256"], "app_write_count": value["app_write_count"]}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, UnicodeError, json.JSONDecodeError, sqlite3.Error, AppProofError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
