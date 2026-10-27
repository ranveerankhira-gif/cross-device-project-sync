#!/usr/bin/env python3
"""Durable, account-bound app-operation queue. No app/database writes here.

Claim BEFORE dispatch. An interrupted claim is never automatically dispatched
again: reconcile it against an observed task carrying the exact receipt marker.
The app has no create idempotency API, so absence alone cannot prove failure.
"""
from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile

FORMAT = "cross-device-receive-queue-v2"
RESULTS_FORMAT = "cross-device-conversation-ledger-results-v2"
DONE = {"registered-lazy", "context-loaded", "skipped", "retained-hidden", "superseded-divergence"}
WRITES = {
    "register-new", "update-existing", "apply-retained-update",
    "resolve-divergence-incoming", "resolve-divergence-retained",
}
NO_WRITES = {"skip-unchanged": "skipped", "keep-hidden-already-downloaded": "skipped",
             "download-update-keep-hidden": "retained-hidden", "preserve-pending-source-diverged": "blocked-divergence",
             "skip-resolved-divergence": "skipped"}


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def valid_sha(value, label="SHA-256"):
    text = str(value or "").lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ValueError(f"invalid {label}")
    return text


def context_load_marker(task, evidence_report_sha256):
    evidence = valid_sha(evidence_report_sha256, "context evidence report SHA-256")
    return "[cross-device-context-loaded:" + digest({
        "operation_key": task.get("operation_key"),
        "conversation_sha256": task.get("conversation_sha256"),
        "context_evidence_report_sha256": evidence,
    }) + "]"


def context_evidence_file(caller_home, path_value, expected_sha256):
    expected = valid_sha(expected_sha256, "context evidence report SHA-256")
    path = Path(str(path_value or ""))
    if not path.is_absolute():
        raise ValueError("context evidence report path must be absolute")
    root, resolved = Path(caller_home).resolve(), path.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError("context evidence report must be inside the calling CODEX_HOME")
    if not resolved.is_file():
        raise ValueError("context evidence report is missing")
    info = resolved.lstat()
    if stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ):
        raise ValueError("context evidence report must not be a reparse point")
    actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError("context evidence report SHA-256 mismatch")
    return str(resolved)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def unsigned(value, field):
    return {key: item for key, item in value.items() if key != field}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as stream:
        name = stream.name
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            temporary = Path(name).resolve()
            if temporary.parent != path.parent.resolve() or temporary.is_symlink():
                raise ValueError("refusing to remove an unexpected atomic-write temporary file")
            os.unlink(temporary)  # noqa: SEC-AUDITOR -- NamedTemporaryFile in the validated destination parent


def same_path(left, right):
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))


def require_account(path, caller_home):
    root = Path(caller_home).resolve()
    resolved = Path(path).resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError("receive queue must be inside the calling CODEX_HOME")


@contextmanager
def locked(path):
    # OS advisory locks release on process death, unlike durable app claims.
    lock = Path(str(path) + ".mutex")
    lock.parent.mkdir(parents=True, exist_ok=True)
    stream = lock.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise ValueError("receive queue is being changed by another process") from None
    try:
        yield
    finally:
        stream.close()


def save(path, value):
    value["queue_sha256"] = digest(unsigned(value, "queue_sha256"))
    write(path, value)


def load(path, caller_home):
    require_account(path, caller_home)
    value = read(path)
    if value.get("format") != FORMAT or value.get("queue_sha256") != digest(unsigned(value, "queue_sha256")):
        raise ValueError("receive queue hash or format is invalid")
    if not same_path(value["destination_codex_home"], caller_home):
        raise ValueError("receive queue belongs to a different account")
    return value


def counts(value):
    result = {}
    for task in value["tasks"]:
        result[task["status"]] = result.get(task["status"], 0) + 1
    return result


def build(catalog_path, caller, output, plan_path):
    require_account(output, caller["codex_home"])
    catalog, plan = read(catalog_path), read(plan_path)
    if catalog.get("format") not in {"cross-device-receiving-task-catalog-v1", "cross-device-receiving-task-catalog-v2"}:
        raise ValueError("queue requires a filtered receiving catalog")
    if catalog.get("catalog_sha256") != digest(unsigned(catalog, "catalog_sha256")):
        raise ValueError("receiving catalog hash mismatch")
    if plan.get("format") != "cross-device-conversation-sync-plan-v3" or plan.get("plan_sha256") != digest(unsigned(plan, "plan_sha256")):
        raise ValueError("receive plan hash or format mismatch")
    if plan.get("catalog_sha256") != catalog["catalog_sha256"]:
        raise ValueError("receive plan belongs to a different catalog")
    ledger_path = Path(caller["codex_home"]) / "cross-device-conversation-sync-ledger.json"
    current_ledger = read(ledger_path) if ledger_path.exists() else {"format": "cross-device-conversation-sync-ledger-v2", "entries": {}}
    if plan.get("ledger_sha256") != digest(current_ledger):
        raise ValueError("receiver ledger changed since planning; do not dispatch a stale queue")
    # Plans are built by the ledger helper after reading active AND archived state.
    decisions = {row["source_key"]: row for row in plan["decisions"]}
    if len(decisions) != len(plan["decisions"]):
        raise ValueError("duplicate source keys in receive plan")
    tasks, seen = [], set()
    for row in catalog["receiving_tasks"]:
        lineage = row.get("lineage_id")
        key = digest({"lineage_id": lineage, "branch_id": row.get("branch_id") or "main"}) if lineage else digest({
            "source_computer": str(catalog["source_computer"]).casefold(), "original_thread_id": row["original_thread_id"]})
        decision = decisions.get(key)
        if decision is None:
            # v1 catalog against a migrated ledger uses a stable lineage key.
            matches = [item for item in decisions.values() if item["original_thread_id"] == row["original_thread_id"]]
            if len(matches) != 1:
                raise ValueError("catalog row has no unique ledger decision")
            decision = matches[0]
            key = decision["source_key"]
        if decision["conversation_sha256"] != row["conversation_sha256"]:
            raise ValueError("plan conversation hash mismatch")
        if key in seen:
            raise ValueError("duplicate source identity")
        seen.add(key)
        action = decision["action"]
        if action not in WRITES and action not in NO_WRITES:
            raise ValueError("unsupported receive action")
        marker_identity = {"source_key": key, "conversation_sha256": row["conversation_sha256"]}
        if action in {"resolve-divergence-incoming", "resolve-divergence-retained"}:
            marker_identity.update({
                "divergence_resolution_sha256": decision.get("divergence_resolution_sha256"),
                "apply_conversation_sha256": decision.get("apply_conversation_sha256"),
            })
        marker = "[cross-device-receipt:" + digest(marker_identity) + "]"
        prompt = str(row.get("initial_prompt") or "").strip()
        if action == "resolve-divergence-retained":
            retained_path = str(decision.get("apply_conversation_path") or "").strip()
            if not retained_path:
                raise ValueError("retained divergence resolution has no verified handoff path")
            prompt = (
                "Apply the explicitly selected retained conversation handoff from this verified local path:\n"
                + retained_path
                + "\nPreserve the incoming version as divergent history; do not merge the two histories automatically."
            )
        if action in WRITES and not prompt:
            raise ValueError("app-write task requires a lazy registration prompt")
        title = str(row.get("display_title") or row["original_thread_id"])
        source = str(row.get("source_instance_name") or row.get("source_instance_id") or "legacy-unknown")
        task = {
            **row, "operation_key": key, "action": action,
            "title": f"[{catalog['source_device']}·{source}] {title}"[:120],
            "receipt_marker": marker, "initial_prompt": marker + "\n" + prompt,
            "target_kind": row.get("target_kind") or ("project" if row.get("project_id") else "projectless"),
            "target_project_id": row.get("project_id") if row.get("target_kind") != "projectless" else None,
            "status": "pending" if action in WRITES else NO_WRITES[action],
            "receiving_thread_id": decision.get("receiving_thread_id"),
            "expected_disposition": decision["receiver_disposition"],
            "claim_token": None, "attempts": 0,
        }
        for field in (
            "divergence_resolution_path", "divergence_resolution_sha256", "divergence_choice",
            "apply_conversation_sha256", "apply_messages_sha256", "apply_message_count",
            "apply_attachment_count", "apply_cutoff_timestamp", "apply_conversation_path",
            "divergent_history",
        ):
            if field in decision:
                task[field] = decision[field]
        tasks.append(task)
    if seen != set(decisions):
        raise ValueError("catalog and plan task inventories differ")
    value = {"format": FORMAT, "catalog_sha256": catalog["catalog_sha256"], "plan_sha256": plan["plan_sha256"],
             "divergence_resolution_sha256s": plan.get("divergence_resolution_sha256s") or [],
             "destination_codex_home": caller["codex_home"], "destination_instance_id": caller["instance_id"],
             "created_utc": now(), "max_batch_size": 8, "tasks": tasks}
    operation_path = Path(caller["codex_home"]) / "cross-device-active-receive-queue.json"
    with locked(operation_path), locked(output):
        if operation_path.exists():
            active = read(operation_path)
            active_queue = Path(active["queue"])
            if not same_path(active_queue, output):
                prior = load(active_queue, caller["codex_home"])
                if any(row["status"] not in DONE for row in prior["tasks"]):
                    raise ValueError("this account has another unfinished receive queue; resume it first")
                for task in prior["tasks"]:
                    if task["action"] in WRITES:
                        applied = current_ledger.get("entries", {}).get(task["operation_key"], {})
                        if applied.get("receiving_thread_id") != task["receiving_thread_id"] or applied.get("conversation_sha256") != task["conversation_sha256"]:
                            raise ValueError("previous queue app writes have not been committed to the receiver ledger")
        if Path(output).exists():
            old = load(output, caller["codex_home"])
            if old["catalog_sha256"] != value["catalog_sha256"] or old["plan_sha256"] != value["plan_sha256"]:
                raise ValueError("existing queue belongs to another plan; do not overwrite it")
            write(operation_path, {"queue": str(Path(output).resolve()), "plan_sha256": old["plan_sha256"]})
            return {"queue": str(output), "reused": True, "task_count": len(old["tasks"]), "counts": counts(old)}
        save(output, value)
        write(operation_path, {"queue": str(Path(output).resolve()), "plan_sha256": value["plan_sha256"]})
    return {"queue": str(output), "reused": False, "task_count": len(tasks), "batch_size": 8}


def batch(path, caller_home, claim=False):
    with locked(path):
        value = load(path, caller_home)
        outstanding = [row for row in value["tasks"] if row["status"] in {"in-flight", "uncertain"}]
        eligible = [row for row in value["tasks"] if row["status"] in {"pending", "failed"}]
        # One batch at a time: pending asynchronous creations must settle first.
        selected = [] if outstanding else eligible[:8]
        if claim:
            for row in selected:
                row.update(status="in-flight", claim_token=secrets.token_hex(24), claimed_utc=now(), attempts=row["attempts"] + 1)
            save(path, value)
        return {"queue": str(path), "destination_instance_id": value["destination_instance_id"],
                "destination_codex_home": value["destination_codex_home"], "batch_size": len(selected),
                "tasks": selected, "unresolved_claims": outstanding, "counts": counts(value),
                "complete": all(row["status"] in DONE for row in value["tasks"])}


def record(path, caller_home, key, claim_token, observation):
    """Observation must come from the current account's verified app task read.

    An uncertain call is retained indefinitely. To retry without a duplicate,
    the adapter must prove *definitive* non-creation, not merely list absence.
    """
    with locked(path):
        value = load(path, caller_home)
        matches = [row for row in value["tasks"] if row["operation_key"] == key]
        if len(matches) != 1:
            raise ValueError("unknown queue operation key")
        task = matches[0]
        if not claim_token or task.get("claim_token") != claim_token:
            raise ValueError("queue claim token mismatch")
        if not same_path(observation.get("destination_codex_home") or "/", caller_home):
            raise ValueError("app observation belongs to another account")
        status = observation.get("status")
        if status in {"registered-lazy", "context-loaded"}:
            thread = str(observation.get("receiving_thread_id") or "").strip()
            if not thread or observation.get("receipt_marker") != task["receipt_marker"]:
                raise ValueError("verified task ID and exact receipt marker are required")
            if observation.get("receiver_disposition") != task["expected_disposition"]:
                raise ValueError("source or preserved receiver archive state has not been verified")
            if observation.get("target_kind") != task["target_kind"] or observation.get("target_project_id") != task["target_project_id"]:
                raise ValueError("receiving task project binding mismatch")
            if task.get("receiving_thread_id") not in {None, thread}:
                raise ValueError("cannot replace the receiving task ID")
            if any(row is not task and row.get("receiving_thread_id") == thread for row in value["tasks"]):
                raise ValueError("receiving task ID is already owned by another lineage")
            if task["status"] == "context-loaded" and status != "context-loaded":
                raise ValueError("cannot downgrade context-loaded")
            if status == "context-loaded":
                if observation.get("full_context_verified") is not True:
                    raise ValueError("context-loaded requires explicit complete-context verification")
                evidence_sha256 = valid_sha(
                    observation.get("context_evidence_report_sha256"),
                    "context evidence report SHA-256",
                )
                expected_context_marker = context_load_marker(task, evidence_sha256)
                if observation.get("context_load_marker") != expected_context_marker:
                    raise ValueError("context-loaded requires the exact independent context-load marker")
                observation["context_evidence_report_path"] = context_evidence_file(
                    caller_home, observation.get("context_evidence_report_path"), evidence_sha256,
                )
            task["receiving_thread_id"] = thread
        elif status in {"failed", "uncertain"}:
            if task["status"] not in {"in-flight", "uncertain"}:
                raise ValueError("only an outstanding claim can fail")
            if status == "failed" and (observation.get("definitive_no_side_effect") is not True or not observation.get("failure_evidence")):
                raise ValueError("retry requires definitive no-side-effect evidence; use uncertain otherwise")
        else:
            raise ValueError("unsupported queue result status")
        task.update(status=status, updated_utc=now(), last_observation=observation)
        save(path, value)
        return {"queue": str(path), "counts": counts(value), "complete": all(row["status"] in DONE for row in value["tasks"])}


def ledger_results(path, caller_home):
    value = load(path, caller_home)
    if any(row["status"] not in DONE for row in value["tasks"]):
        raise ValueError("queue has unfinished or unresolved items; ledger commit is not ready")
    rows = []
    for task in value["tasks"]:
        if task["action"] in WRITES:
            observation = task.get("last_observation")
            if task.get("status") not in {"registered-lazy", "context-loaded"} or not isinstance(observation, dict):
                raise ValueError("app-write queue item lacks a verified app observation")
            if observation.get("status") != task["status"] or observation.get("receiving_thread_id") != task.get("receiving_thread_id"):
                raise ValueError("app-write observation does not match the completed queue item")
            if observation.get("receipt_marker") != task.get("receipt_marker"):
                raise ValueError("app-write observation receipt marker mismatch")
            if observation.get("target_kind") != task.get("target_kind") or observation.get("target_project_id") != task.get("target_project_id"):
                raise ValueError("app-write observation project binding mismatch")
        elif task["action"] in NO_WRITES:
            expected = NO_WRITES[task["action"]]
            if task.get("status") != expected or task.get("last_observation") is not None:
                raise ValueError("no-write queue item has an impossible completed state")
        else:
            raise ValueError("unsupported receive action")
        if task["action"] in WRITES or task["action"] == "download-update-keep-hidden":
            applied_sha256 = task.get("apply_conversation_sha256") or task["conversation_sha256"]
            rows.append({"source_key": task["operation_key"], "action": task["action"],
                         "conversation_sha256": applied_sha256,
                         "status": "downloaded-kept-hidden" if task["action"] == "download-update-keep-hidden" else "applied",
                         "receiving_thread_id": task.get("receiving_thread_id"),
                         "receiver_disposition": task["expected_disposition"],
                         **({"divergence_resolution_sha256": task.get("divergence_resolution_sha256")}
                            if task["action"] in {"resolve-divergence-incoming", "resolve-divergence-retained"} else {})})
    result = {
        "format": RESULTS_FORMAT,
        "plan_sha256": value["plan_sha256"],
        "queue_sha256": value["queue_sha256"],
        "results": rows,
    }
    result["results_sha256"] = digest(unsigned(result, "results_sha256"))
    return result


def resolve_blocked_divergence(path, caller_home, operation_key, resolution):
    """Retire exactly one old blocked Queue item after an audited resolution.

    The resolution is applied only by a freshly rebuilt plan and Queue. This
    function merely prevents the old, now-superseded Queue from owning the
    account forever.
    """
    with locked(path):
        value = load(path, caller_home)
        matches = [row for row in value["tasks"] if row.get("operation_key") == operation_key]
        if len(matches) != 1:
            raise ValueError("unknown divergence queue operation key")
        task = matches[0]
        resolution_sha256 = str(resolution.get("resolution_sha256") or "")
        if task.get("status") == "superseded-divergence":
            if task.get("divergence_resolution_sha256") != resolution_sha256:
                raise ValueError("blocked Queue was superseded by another divergence resolution")
            return {"queue": str(path), "counts": counts(value), "complete": all(row["status"] in DONE for row in value["tasks"]), "reused": True}
        if task.get("action") != "preserve-pending-source-diverged" or task.get("status") != "blocked-divergence":
            raise ValueError("queue item is not an unresolved blocked divergence")
        if value.get("plan_sha256") != resolution.get("blocked_plan_sha256"):
            raise ValueError("divergence resolution belongs to another blocked Queue plan")
        if task.get("conversation_sha256") != (resolution.get("incoming") or {}).get("conversation_sha256"):
            raise ValueError("divergence resolution incoming hash does not match the blocked Queue")
        task.update({
            "status": "superseded-divergence",
            "divergence_resolution_sha256": resolution_sha256,
            "divergence_choice": resolution.get("choice"),
            "updated_utc": now(),
        })
        save(path, value)
        return {"queue": str(path), "counts": counts(value), "complete": all(row["status"] in DONE for row in value["tasks"]), "reused": False}
