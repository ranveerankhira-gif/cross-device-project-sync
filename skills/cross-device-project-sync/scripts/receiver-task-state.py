#!/usr/bin/env python3
"""Create and verify durable state for one automatic receiving task."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time


PLAN_FORMAT = "cross-device-project-sync-receiver-task-plan-v2"
RECEIPT_FORMAT = "cross-device-project-sync-receiver-task-receipt-v1"
SHA256 = re.compile(r"[0-9a-f]{64}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object: " + str(path))
    return value


def load_json_bytes(path: Path) -> tuple[dict, bytes]:
    payload = path.read_bytes()
    value = json.loads(payload.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object: " + str(path))
    return value, payload


def validate_plan(path: Path) -> tuple[dict, str, Path]:
    path = path.resolve()
    plan, payload = load_json_bytes(path)
    if plan.get("format") != PLAN_FORMAT or plan.get("status") != "pending-app-task-creation":
        raise ValueError("Receiving task plan format or status is invalid.")
    required_text = (
        "operation_key", "restored_project_root", "conversation_handoff_path",
        "receiver_bootstrap_path", "release_tag", "project_manifest_sha256",
        "conversation_manifest_sha256", "original_thread_id", "initial_prompt",
        "receipt_path",
    )
    for field in required_text:
        if not isinstance(plan.get(field), str) or not plan[field].strip():
            raise ValueError("Receiving task plan is missing: " + field)
    if not SHA256.fullmatch(plan["operation_key"]):
        raise ValueError("Receiving operation key is invalid.")
    for field in ("project_manifest_sha256", "conversation_manifest_sha256"):
        if not SHA256.fullmatch(plan[field].lower()):
            raise ValueError("Receiving task plan has an invalid " + field + ".")
    for field in ("lineage_task_count", "lineage_message_count", "lineage_attachment_count"):
        if not isinstance(plan.get(field), int) or isinstance(plan[field], bool) or plan[field] < 0:
            raise ValueError("Receiving task plan has an invalid " + field + ".")
    if plan["operation_key"] not in plan["initial_prompt"]:
        raise ValueError("Receiving prompt is not bound to its operation key.")
    receipt = Path(plan["receipt_path"]).resolve()
    expected_receipt = path.with_name("_codex-receiving-task-receipt.json")
    if receipt != expected_receipt:
        raise ValueError("Receiving receipt path is not beside the plan.")
    project_root = Path(plan["restored_project_root"]).resolve()
    handoff_root = Path(plan["conversation_handoff_path"]).resolve()
    bootstrap = Path(plan["receiver_bootstrap_path"]).resolve()
    if path.parent != project_root.parent or path.parent != handoff_root.parent:
        raise ValueError("Receiving plan, project, and handoff must share the restore destination.")
    if not project_root.is_dir() or not handoff_root.is_dir() or not bootstrap.is_file():
        raise ValueError("Receiving plan references a missing project, handoff, or bootstrap file.")
    if bootstrap != handoff_root and handoff_root not in bootstrap.parents:
        raise ValueError("Receiving bootstrap file is outside the verified handoff directory.")
    return plan, hashlib.sha256(payload).hexdigest(), receipt


def validate_receipt(receipt: dict, plan: dict, plan_sha256: str) -> None:
    if receipt.get("format") != RECEIPT_FORMAT or receipt.get("status") != "verified-complete":
        raise ValueError("Receiving task receipt format or status is invalid.")
    expected = {
        "operation_key": plan["operation_key"],
        "plan_sha256": plan_sha256,
        "release_tag": plan["release_tag"],
        "project_manifest_sha256": plan["project_manifest_sha256"],
        "conversation_manifest_sha256": plan["conversation_manifest_sha256"],
        "restored_project_root": plan["restored_project_root"],
        "original_thread_id": plan["original_thread_id"],
    }
    for field, value in expected.items():
        if receipt.get(field) != value:
            raise ValueError("Receiving task receipt does not match the plan: " + field)
    for field in ("receiving_thread_id", "project_id"):
        if not isinstance(receipt.get(field), str) or not receipt[field].strip():
            raise ValueError("Receiving task receipt is missing: " + field)
    if receipt.get("semantic_review_approved") is not True or receipt.get("exact_project_association_verified") is not True:
        raise ValueError("Receiving task receipt lacks required approvals.")
    if not SHA256.fullmatch(str(receipt.get("evidence_report_sha256") or "")):
        raise ValueError("Receiving task receipt has an invalid evidence report hash.")


def inspect(args: argparse.Namespace) -> None:
    plan, plan_sha256, receipt_path = validate_plan(Path(args.plan))
    result = {
        "state": "no-receipt",
        "operation_key": plan["operation_key"],
        "plan_sha256": plan_sha256,
        "receipt_path": str(receipt_path),
    }
    if receipt_path.exists():
        receipt = load_json(receipt_path)
        validate_receipt(receipt, plan, plan_sha256)
        result.update({
            "state": "verified-complete",
            "receiving_thread_id": receipt["receiving_thread_id"],
            "project_id": receipt["project_id"],
            "evidence_report_sha256": receipt["evidence_report_sha256"],
        })
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


def complete(args: argparse.Namespace) -> None:
    if not args.semantic_review_approved or not args.exact_project_association_verified:
        raise ValueError("Both semantic review and exact project association must be explicitly approved.")
    plan, plan_sha256, receipt_path = validate_plan(Path(args.plan))
    evidence_path = Path(args.evidence_report).resolve()
    project_root = Path(plan["restored_project_root"]).resolve()
    handoff_root = Path(plan["conversation_handoff_path"]).resolve()
    if evidence_path in {project_root, handoff_root} or project_root in evidence_path.parents or handoff_root in evidence_path.parents:
        raise ValueError("Evidence report must remain outside the verified project and handoff directories.")
    report, evidence_payload = load_json_bytes(evidence_path)
    expected = {
        "thread_id": plan["original_thread_id"],
        "receiving_thread_id": args.receiving_thread_id,
        "release_tag": plan["release_tag"],
        "project_manifest_sha256": plan["project_manifest_sha256"],
        "restored_project_root": plan["restored_project_root"],
        "conversation_cutoff": plan["conversation_cutoff"],
        "conversation_source_device": plan["conversation_source_device"],
        "lineage_task_count": plan["lineage_task_count"],
        "lineage_message_count": plan["lineage_message_count"],
        "lineage_attachment_count": plan["lineage_attachment_count"],
    }
    for field, value in expected.items():
        if report.get(field) != value:
            raise ValueError("Evidence report does not match the receiving plan: " + field)
    verifier_path = Path(__file__).with_name("verify-handoff-evidence.py")
    spec = importlib.util.spec_from_file_location("cross_device_handoff_evidence", verifier_path)
    if not spec or not spec.loader:
        raise ValueError("Installed handoff evidence verifier could not be loaded.")
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    verifier.verify(
        Path(plan["conversation_handoff_path"]),
        report,
        plan["release_tag"],
        plan["project_manifest_sha256"],
        plan["restored_project_root"],
        args.receiving_thread_id,
    )
    receipt = {
        "format": RECEIPT_FORMAT,
        "status": "verified-complete",
        "completed_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "operation_key": plan["operation_key"],
        "plan_sha256": plan_sha256,
        "release_tag": plan["release_tag"],
        "project_manifest_sha256": plan["project_manifest_sha256"],
        "conversation_manifest_sha256": plan["conversation_manifest_sha256"],
        "restored_project_root": plan["restored_project_root"],
        "original_thread_id": plan["original_thread_id"],
        "receiving_thread_id": args.receiving_thread_id,
        "project_id": args.project_id,
        "evidence_report_path": str(evidence_path),
        "evidence_report_sha256": hashlib.sha256(evidence_payload).hexdigest(),
        "semantic_review_approved": True,
        "exact_project_association_verified": True,
    }
    payload = (json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError("Receiving task receipt already exists; inspect it instead of creating another task.") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        receipt_path.unlink(missing_ok=True)
        raise
    validate_receipt(load_json(receipt_path), plan, plan_sha256)
    print(json.dumps({
        "state": "verified-complete",
        "receipt_path": str(receipt_path),
        "receipt_sha256": sha256_file(receipt_path),
        "receiving_thread_id": args.receiving_thread_id,
        "project_id": args.project_id,
    }, ensure_ascii=False, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--plan", required=True)
    inspect_parser.set_defaults(func=inspect)
    complete_parser = subparsers.add_parser("complete")
    complete_parser.add_argument("--plan", required=True)
    complete_parser.add_argument("--evidence-report", required=True)
    complete_parser.add_argument("--receiving-thread-id", required=True)
    complete_parser.add_argument("--project-id", required=True)
    complete_parser.add_argument("--semantic-review-approved", action="store_true")
    complete_parser.add_argument("--exact-project-association-verified", action="store_true")
    complete_parser.set_defaults(func=complete)
    args = parser.parse_args()
    try:
        args.func(args)
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
