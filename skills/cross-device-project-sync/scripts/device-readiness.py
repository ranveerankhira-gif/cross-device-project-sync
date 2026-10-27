#!/usr/bin/env python3
"""Produce a hash-bound, read-only readiness report for one sync device."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any


FORMAT = "cross-device-project-sync-device-readiness-v2"
CANONICAL_REPOSITORY = os.environ.get("CROSS_DEVICE_SYNC_REPOSITORY", "").strip()
REPORT_MAX_AGE_SECONDS = 900.0
REPORT_MAX_FUTURE_SKEW_SECONDS = 120.0


def canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def load_local(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, Path(__file__).with_name(filename))
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load required helper: {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def actual_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).resolve()


def canonical_public_skill_root() -> Path:
    profile = Path(os.environ.get("USERPROFILE") or Path.home()).resolve()
    return (profile / ".agents" / "skills" / "cross-device-project-sync").resolve()


def is_within(path: Path, parent: Path) -> bool:
    resolved, root = path.resolve(), parent.resolve()
    return resolved == root or root in resolved.parents


def add_check(checks: list[dict[str, Any]], name: str, passed: bool, detail: Any) -> None:
    checks.append({"name": name, "status": "passed" if passed else "failed", "detail": detail})


def skill_inventory(skill_root: Path, packager) -> tuple[list[dict[str, Any]], str, str]:
    skill_text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    match = re.search(r"(?m)^\s*version:\s*['\"]?([^'\"\s]+)", skill_text)
    if not match:
        raise ValueError("public Skill metadata.version is missing")
    records = []
    for path in packager.source_files(skill_root):
        data = path.read_bytes()
        records.append({
            "path": path.relative_to(skill_root).as_posix(),
            "size": len(data),
            "sha256": digest(data),
        })
    return records, digest(records), match.group(1)


def verify_expected_package(package_root: Path, skill_root: Path, records: list[dict[str, Any]], version: str, packager) -> dict[str, Any]:
    manifest = packager.verify(argparse.Namespace(package=str(package_root)))
    expected = []
    prefix = skill_root.name + "/"
    for row in manifest["files"]:
        path = str(row["path"])
        if not path.startswith(prefix):
            raise ValueError("Skill package inventory has an unexpected root")
        expected.append({"path": path[len(prefix):], "size": row["size"], "sha256": row["sha256"]})
    if expected != records or manifest["version"] != version:
        raise ValueError("installed public Skill does not match the expected exact package")
    return {
        "path": str(package_root.resolve()),
        "version": manifest["version"],
        "payload_sha256": manifest["payload_sha256"],
        "manifest_sha256": digest((package_root / packager.MANIFEST_NAME).read_bytes()),
    }


def resolve_gh(requested: str | None, caller_home: Path) -> str:
    if requested:
        path = Path(requested).resolve()
        if not path.is_file():
            raise ValueError(f"GitHub CLI does not exist: {path}")
        return str(path)
    found = shutil.which("gh.exe") or shutil.which("gh")
    if found:
        return found
    config_path = caller_home / "cross-device-project-sync-github.json"
    config = read_json(config_path)
    if config.get("hostname") != "github.com":
        raise ValueError("local GitHub configuration must use github.com")
    path = Path(str(config.get("github_cli") or "")).resolve()
    if not path.is_file():
        raise ValueError("configured GitHub CLI does not exist")
    return str(path)


def run_gh(gh: str, arguments: list[str]) -> str:
    completed = subprocess.run(
        [gh, *arguments], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    if completed.returncode != 0:
        raise ValueError(f"GitHub CLI check failed: {' '.join(arguments[:3])}")
    return completed.stdout


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    multi = load_local("multi-instance-sync.py", "readiness_multi_instance")
    packager = load_local("package-installed-skill.py", "readiness_skill_packager")
    registry_path = Path(args.registry).resolve()
    public_root = Path(args.installed_skill_root).resolve()
    canonical_root = canonical_public_skill_root()
    output = Path(args.output).resolve()
    caller_home = actual_codex_home()
    checks: list[dict[str, Any]] = []
    errors: list[str] = []

    registry = caller = None
    try:
        registry, caller = multi.validate_registry(registry_path, str(caller_home), require_paths=True)
        add_check(checks, "registry-and-caller", True, {
            "caller_instance_id": caller["instance_id"], "caller_codex_home": caller["codex_home"],
            "instance_count": len(registry["instances"]),
        })
    except Exception as exc:
        errors.append(str(exc))
        add_check(checks, "registry-and-caller", False, str(exc))
        try:
            raw = read_json(registry_path)
            if isinstance(raw, dict):
                registry = raw
        except Exception:
            pass

    machine_state_root = Path(str((registry or {}).get("machine_state_root") or registry_path.parent)).resolve()
    if not is_within(output, machine_state_root):
        raise ValueError("readiness report must be stored under machine_state_root")

    actual_computer = multi.machine_name()
    expected_computer = str((registry or {}).get("computer_name") or "")
    expected_label = str((registry or {}).get("device_label") or "")
    identity_ok = bool(expected_computer and expected_label and actual_computer.casefold() == expected_computer.casefold())
    identity_detail = {
        "actual_computer": actual_computer,
        "registry_computer": expected_computer,
        "registry_device_label": expected_label,
    }
    add_check(checks, "device-identity", identity_ok, identity_detail)
    if not identity_ok:
        errors.append("computer name, device label, and registry identity do not agree")

    instance_rows = []
    labels_ok = True
    duplicates = []
    for row in (registry or {}).get("instances") or []:
        home = Path(str(row.get("codex_home") or "")).resolve()
        config_path = home / "cross-device-project-sync-device.json"
        label = None
        issue = None
        try:
            config = read_json(config_path)
            if not isinstance(config, dict) or set(config) != {"device_label", "computer_name"}:
                raise ValueError("device identity file must contain only device_label and computer_name")
            label = str(config.get("device_label") or "").strip()
            if label != expected_label:
                raise ValueError(f"device label is {label!r}, expected {expected_label!r}")
            if str(config.get("computer_name") or "").strip().casefold() != expected_computer.casefold():
                raise ValueError("device identity computer_name does not match the registry")
        except Exception as exc:
            labels_ok = False
            issue = str(exc)
        private_skill = home / "skills" / "cross-device-project-sync"
        if private_skill.exists():
            duplicates.append(str(private_skill))
        try:
            state = read_json(home / ".codex-global-state.json")
            for project in (state.get("local-projects") or {}).values():
                if not isinstance(project, dict):
                    continue
                for root_text in project.get("rootPaths") or []:
                    candidate = Path(str(root_text)).resolve() / ".agents" / "skills" / "cross-device-project-sync"
                    if candidate.exists() and candidate.resolve() != canonical_root:
                        duplicates.append(str(candidate.resolve()))
        except Exception as exc:
            labels_ok = False
            issue = f"cannot inspect project-local Skill discovery paths: {exc}"
        instance_rows.append({
            "instance_id": row.get("instance_id"), "display_name": row.get("display_name"),
            "codex_home": str(home), "exists": home.is_dir(), "device_label_file": str(config_path),
            "device_label": label, "identity_issue": issue,
        })
    add_check(checks, "all-instance-device-labels", labels_ok, instance_rows)
    if not labels_ok:
        errors.append("one or more allow-listed Codex homes lack the exact device identity file")
    duplicates = sorted(set(duplicates), key=str.casefold)
    add_check(checks, "no-private-skill-duplicates", not duplicates, {"duplicates": duplicates})
    if duplicates:
        errors.append("private Skill copies remain in account-specific discovery paths")

    skill_info: dict[str, Any] = {"path": str(public_root)}
    skill_ok = True
    try:
        if public_root != canonical_root:
            raise ValueError(f"installed public Skill root must be the canonical shared path: {canonical_root}")
        if public_root.name != "cross-device-project-sync" or not public_root.is_dir():
            raise ValueError("installed public Skill root is invalid")
        records, tree_sha256, version = skill_inventory(public_root, packager)
        skill_info.update(version=version, file_count=len(records), tree_sha256=tree_sha256)
        if args.expected_skill_version and version != args.expected_skill_version:
            raise ValueError(f"public Skill version is {version}, expected {args.expected_skill_version}")
        if args.expected_skill_package:
            skill_info["expected_package"] = verify_expected_package(
                Path(args.expected_skill_package).resolve(), public_root, records, version, packager,
            )
            skill_info["integrity_level"] = "exact-package-bound"
        else:
            skill_info["integrity_level"] = "self-hash-for-cross-device-comparison"
    except Exception as exc:
        skill_ok = False
        errors.append(str(exc))
        skill_info["error"] = str(exc)
    add_check(checks, "public-skill-integrity", skill_ok, skill_info)

    lock_ok = True
    lock_info: dict[str, Any]
    try:
        if registry is None or registry.get("format") != multi.REGISTRY_FORMAT:
            raise ValueError("machine lock cannot be inspected without a valid registry")
        lock_info = multi.inspect_lock(registry)
        lock_ok = not lock_info["locked"]
    except Exception as exc:
        lock_ok = False
        lock_info = {"error": str(exc)}
    add_check(checks, "machine-operation-lock-free", lock_ok, lock_info)
    if not lock_ok:
        errors.append("machine-wide sync lock is occupied or unreadable")

    cloud_info: dict[str, Any] = {"requested": bool(args.check_cloud), "repository": args.repository}
    cloud_ok = True
    if args.check_cloud:
        try:
            gh = resolve_gh(args.gh, caller_home)
            run_gh(gh, ["auth", "status", "--hostname", "github.com"])
            repository = json.loads(run_gh(gh, ["repo", "view", args.repository, "--json", "nameWithOwner,isPrivate"] ))
            if repository.get("nameWithOwner") != args.repository or repository.get("isPrivate") is not True:
                raise ValueError("canonical repository identity or privacy does not match")
            cloud_info.update(status="verified", github_cli=str(Path(gh).resolve()), private=True)
        except Exception as exc:
            cloud_ok = False
            cloud_info.update(status="failed", error=str(exc))
            errors.append(str(exc))
    else:
        cloud_info["status"] = "not-requested"
    add_check(checks, "github-private-repository", cloud_ok, cloud_info)

    report = {
        "format": FORMAT,
        "status": "ready" if not errors else "not-ready",
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "registry_path": str(registry_path),
        "registry_sha256": digest(registry_path.read_bytes()) if registry_path.is_file() else None,
        "machine_state_root": str(machine_state_root),
        "device_label": expected_label or None,
        "computer_name": actual_computer or None,
        "caller_codex_home": str(caller_home),
        "caller_instance_id": (caller or {}).get("instance_id"),
        "checks": checks,
        "errors": errors,
    }
    report["readiness_sha256"] = digest(report)
    multi.atomic_write_json(output, report)
    return report


def verify_report(args: argparse.Namespace) -> dict[str, Any]:
    report_path = Path(args.verify_report).resolve()
    report = read_json(report_path)
    claimed = str(report.get("readiness_sha256") or "").lower()
    unsigned = {key: value for key, value in report.items() if key != "readiness_sha256"}
    if report.get("format") != FORMAT or len(claimed) != 64 or claimed != digest(unsigned):
        raise ValueError("device readiness report format or hash is invalid")
    if report.get("status") != "ready" or report.get("errors"):
        raise ValueError("device readiness report is not ready")
    generated = dt.datetime.fromisoformat(str(report.get("generated_utc") or "").replace("Z", "+00:00"))
    if generated.tzinfo is None:
        raise ValueError("device readiness timestamp has no timezone")
    age = (dt.datetime.now(dt.timezone.utc) - generated.astimezone(dt.timezone.utc)).total_seconds()
    if age > REPORT_MAX_AGE_SECONDS or age < -REPORT_MAX_FUTURE_SKEW_SECONDS:
        raise ValueError("device readiness report is stale or from the future")
    multi = load_local("multi-instance-sync.py", "readiness_verify_multi")
    packager = load_local("package-installed-skill.py", "readiness_verify_packager")
    registry_path = Path(args.registry).resolve()
    caller_home = actual_codex_home()
    registry, caller = multi.validate_registry(registry_path, str(caller_home), require_paths=True)
    if caller is None:
        raise ValueError("calling CODEX_HOME is not allow-listed")
    expected = {
        "registry_path": str(registry_path),
        "registry_sha256": digest(registry_path.read_bytes()),
        "device_label": registry["device_label"],
        "computer_name": registry["computer_name"],
        "caller_codex_home": str(caller_home),
        "caller_instance_id": caller["instance_id"],
    }
    for field, value in expected.items():
        actual = report.get(field)
        if field in {"registry_path", "caller_codex_home"}:
            if Path(str(actual)).resolve() != Path(str(value)).resolve():
                raise ValueError(f"device readiness {field} is stale or mismatched")
        elif actual != value:
            raise ValueError(f"device readiness {field} is stale or mismatched")
    skill_root = Path(args.installed_skill_root).resolve()
    if skill_root != canonical_public_skill_root():
        raise ValueError("installed Skill root is not the canonical shared path")
    records, tree_sha256, version = skill_inventory(skill_root, packager)
    del records
    if args.expected_skill_version and version != args.expected_skill_version:
        raise ValueError(f"public Skill version is {version}, expected {args.expected_skill_version}")
    skill_check = next((row for row in report.get("checks") or [] if row.get("name") == "public-skill-integrity"), None)
    detail = (skill_check or {}).get("detail") or {}
    if (skill_check or {}).get("status") != "passed" or detail.get("tree_sha256") != tree_sha256 or detail.get("version") != version:
        raise ValueError("public Skill changed after the readiness report")
    if any(row.get("status") != "passed" for row in report.get("checks") or []):
        raise ValueError("device readiness report contains a failed check")
    for instance in registry["instances"]:
        config = read_json(Path(instance["codex_home"]) / "cross-device-project-sync-device.json")
        if set(config) != {"device_label", "computer_name"} or config.get("device_label") != registry["device_label"] or str(config.get("computer_name") or "").casefold() != str(registry["computer_name"]).casefold():
            raise ValueError("an allow-listed instance device label changed after readiness")
    if args.operation_id or args.lock_token or args.kind:
        if not args.operation_id or not args.lock_token or not args.kind:
            raise ValueError("operation-id, lock-token, and kind must be supplied together")
        held = multi.require_lock(registry, args.operation_id, args.lock_token, args.kind)
        if held.get("caller_instance_id") != caller["instance_id"] or Path(held.get("caller_codex_home")).resolve() != caller_home:
            raise ValueError("current machine lock belongs to another calling account")
    return {
        "status": "verified-current-readiness",
        "report": str(report_path),
        "readiness_sha256": claimed,
        "registry_sha256": expected["registry_sha256"],
        "skill_version": version,
        "skill_tree_sha256": tree_sha256,
        "caller_instance_id": caller["instance_id"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", required=True)
    parser.add_argument("--installed-skill-root", required=True)
    parser.add_argument("--output")
    parser.add_argument("--expected-skill-version")
    parser.add_argument("--expected-skill-package")
    parser.add_argument("--check-cloud", action="store_true")
    parser.add_argument("--repository", default=CANONICAL_REPOSITORY or None, help="Private owner/repository; or set CROSS_DEVICE_SYNC_REPOSITORY")
    parser.add_argument("--gh")
    parser.add_argument("--verify-report")
    parser.add_argument("--operation-id")
    parser.add_argument("--lock-token")
    parser.add_argument("--kind", choices=("upload", "download"))
    args = parser.parse_args()
    try:
        if args.verify_report:
            report = verify_report(args)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0
        if not args.output:
            raise ValueError("--output is required when creating a readiness report")
        report = build_report(args)
        print(json.dumps({
            "status": report["status"], "report": str(Path(args.output).resolve()),
            "readiness_sha256": report["readiness_sha256"], "error_count": len(report["errors"]),
        }, ensure_ascii=False, sort_keys=True))
        return 0 if report["status"] == "ready" else 2
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
