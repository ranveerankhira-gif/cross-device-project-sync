#!/usr/bin/env python3
"""Discover and validate device indexes, then bootstrap their exact bound Skill.

The bootstrap contract intentionally understands only the release envelope,
the two supported index families, and the Skill package format.  An index bound
to any different version is interpreted only through its exact verified,
isolated Skill runtime; the shared installation is never replaced.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any
import zipfile
import owned_temp


CANONICAL_REPOSITORY = os.environ.get("CROSS_DEVICE_SYNC_REPOSITORY", "example-owner/example-private-backups").strip()
CURRENT_SKILL_VERSION = "20261019-v48"
BINDING_FORMAT = "cross-device-project-sync-content-release-binding-v1"
INCREMENTAL_FORMATS = {
    "cross-device-project-sync-incremental-device-index-v1",
    "cross-device-project-sync-incremental-device-index-v2",
}
LEGACY_FORMATS = {
    "cross-device-project-sync-device-migration-v1",
    "cross-device-project-sync-device-migration-v2",
}
SKILL_PACKAGE_FORMAT = "cross-device-project-sync-skill-package-v1"
SKILL_NAME = "cross-device-project-sync"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^(\d{8})-v(\d+)$")
SAFE_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")


class ReceiverError(ValueError):
    pass


def local_module(filename):
    spec = importlib.util.spec_from_file_location(filename.replace("-", "_"), Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical_public_skill_root() -> Path:
    profile = Path(os.environ.get("USERPROFILE") or Path.home()).resolve()
    return (profile / ".agents" / "skills" / SKILL_NAME).resolve()


def receive_context(args):
    multi = local_module("multi-instance-sync.py")
    actual = multi.actual_caller_home()
    registry, caller = multi.validate_registry(Path(args.instance_registry), actual)
    if args.repository != CANONICAL_REPOSITORY:
        raise ReceiverError(f"whole-device receive must use the canonical private repository: {CANONICAL_REPOSITORY}")
    if Path(args.installed_skill_root).resolve() != canonical_public_skill_root():
        raise ReceiverError(f"installed Skill root must be the canonical shared path: {canonical_public_skill_root()}")
    multi.queue_engine().require_account(Path(args.selection_file), actual)
    # Per-account storage outside project trees. Never infer ownership from a
    # shared restored path or let a caller redirect into another account.
    base = Path(registry["machine_state_root"]).parent / "receivers" / multi.digest(actual.casefold())[:24]
    for text in (args.download_root, args.restore_root):
        if text and (not multi.is_within(Path(text).resolve(), base.resolve()) or Path(text).resolve() == base.resolve()):
            raise ReceiverError(f"receive paths must be below this account's storage root: {base}")
    selection = load_json(Path(args.selection_file))
    if selection.get("release_inventory_complete") is not True:
        raise ReceiverError("snapshot discovery was truncated; list the complete Release inventory before selection")
    unsigned = {key: value for key, value in selection.items() if key != "discovery_sha256"}
    if selection.get("discovery_sha256") != digest(canonical(unsigned)) or selection.get("repository") != args.repository:
        raise ReceiverError("verified snapshot selection catalog is invalid")
    matches = [row for row in selection.get("device_index_releases", []) if row.get("release_tag") == args.release_tag and row.get("verified") is True]
    if len(matches) != 1:
        raise ReceiverError("selected Release is not a verified candidate; discover then explicitly select it")
    return {
        "caller": caller, "registry": registry, "base": str(base.resolve()), "selection": matches[0],
        "selection_file": str(Path(args.selection_file).resolve()), "selection_sha256": selection["discovery_sha256"],
    }


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReceiverError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReceiverError(f"JSON root must be an object: {path}")
    return value


def require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ReceiverError(f"{field} must be a lowercase SHA-256")
    return value


def require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value):
        raise ReceiverError(f"{field} must be a non-empty safe string")
    return value.strip()


def safe_asset_name(value: Any, field: str) -> str:
    name = require_text(value, field)
    pure = PurePosixPath(name)
    if pure.is_absolute() or len(pure.parts) != 1 or name in {".", ".."} or "\\" in name:
        raise ReceiverError(f"{field} is not a safe single-file asset name")
    return name


def parse_notes(body: Any) -> dict[str, str]:
    if not isinstance(body, str):
        raise ReceiverError("Release notes are missing")
    result: dict[str, str] = {}
    for line in body.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key in {"Content-Kind", "Content-Identity-SHA256", "Repository", "Source-Device", "Source-Computer", "Migration-ID"}:
            if key in result:
                raise ReceiverError(f"Release notes repeat {key}")
            result[key] = value.strip()
    return result


def metadata_asset_map(metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    assets = metadata.get("assets")
    if not isinstance(assets, list):
        raise ReceiverError("Release metadata assets must be an array")
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(assets):
        if not isinstance(item, dict):
            raise ReceiverError(f"Release asset {index} is not an object")
        name = safe_asset_name(item.get("name"), f"Release asset {index}.name")
        if name.casefold() in (key.casefold() for key in result):
            raise ReceiverError(f"duplicate Release asset name: {name}")
        result[name] = item
    return result


def verify_remote_record(name: str, data: bytes, record: dict[str, Any]) -> None:
    if record.get("size") != len(data):
        raise ReceiverError(f"Release asset size mismatch: {name}")
    remote_digest = record.get("digest")
    if remote_digest is not None and remote_digest != "sha256:" + digest(data):
        raise ReceiverError(f"Release asset digest mismatch: {name}")


def validate_bound_release(
    metadata_path: Path,
    assets_root: Path,
    repository: str,
    expected_kind: str,
    expected_tag: str | None = None,
) -> dict[str, Any]:
    metadata = load_json(metadata_path)
    tag = require_text(metadata.get("tagName") or metadata.get("tag_name"), "Release tag")
    if expected_tag is not None and tag != expected_tag:
        raise ReceiverError(f"Release tag mismatch: expected {expected_tag}, got {tag}")
    if metadata.get("isDraft") is not False:
        raise ReceiverError(f"Release is missing or still a draft: {tag}")
    notes = parse_notes(metadata.get("body"))
    if notes.get("Content-Kind") != expected_kind:
        raise ReceiverError(f"Release notes Content-Kind is not {expected_kind}: {tag}")
    if notes.get("Repository") != repository:
        raise ReceiverError(f"Release notes repository mismatch: {tag}")
    identity = require_sha256(notes.get("Content-Identity-SHA256"), "Release Content-Identity-SHA256")
    remote_assets = metadata_asset_map(metadata)
    binding_path = assets_root / "CONTENT_RELEASE_BINDING.json"
    binding_data = binding_path.read_bytes()
    if "CONTENT_RELEASE_BINDING.json" not in remote_assets:
        raise ReceiverError(f"Release lacks CONTENT_RELEASE_BINDING.json: {tag}")
    verify_remote_record("CONTENT_RELEASE_BINDING.json", binding_data, remote_assets["CONTENT_RELEASE_BINDING.json"])
    binding = load_json(binding_path)
    if binding.get("format") != BINDING_FORMAT:
        raise ReceiverError("unsupported CONTENT_RELEASE_BINDING.json format")
    if binding.get("repository") != repository or binding.get("release_tag") != tag:
        raise ReceiverError("content binding repository or Release tag mismatch")
    if binding.get("kind") != expected_kind or binding.get("identity_sha256") != identity:
        raise ReceiverError("content binding kind or identity mismatch")
    records = binding.get("assets")
    if not isinstance(records, list) or not records:
        raise ReceiverError("content binding assets must be a non-empty array")
    bound: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(records):
        if not isinstance(item, dict):
            raise ReceiverError(f"content binding asset {index} is not an object")
        name = safe_asset_name(item.get("name"), f"content binding asset {index}.name")
        if name.casefold() in (key.casefold() for key in bound):
            raise ReceiverError(f"duplicate content binding asset: {name}")
        expected_hash = require_sha256(item.get("sha256"), f"content binding asset {name}.sha256")
        data = (assets_root / name).read_bytes()
        if item.get("size") != len(data) or expected_hash != digest(data):
            raise ReceiverError(f"content binding asset hash or size mismatch: {name}")
        if name not in remote_assets:
            raise ReceiverError(f"bound asset is absent from Release metadata: {name}")
        verify_remote_record(name, data, remote_assets[name])
        bound[name] = item
    expected_names = set(bound) | {"CONTENT_RELEASE_BINDING.json"}
    if set(remote_assets) != expected_names:
        raise ReceiverError("Release assets do not exactly match CONTENT_RELEASE_BINDING.json")
    return {
        "tag": tag,
        "identity_sha256": identity,
        "binding": binding,
        "bound_assets": bound,
    }


def parse_version(value: Any, field: str) -> tuple[int, int, str]:
    text = require_text(value, field)
    match = VERSION_RE.fullmatch(text)
    if not match:
        raise ReceiverError(f"{field} must use complete YYYYMMDD-vN form")
    try:
        dt.datetime.strptime(match.group(1), "%Y%m%d")
    except ValueError as exc:
        raise ReceiverError(f"{field} has an invalid date") from exc
    return int(match.group(1)), int(match.group(2)), text


def compare_versions(installed: str, required: str) -> str:
    left = parse_version(installed, "installed Skill version")[:2]
    right = parse_version(required, "required_skill_version")[:2]
    return "older" if left < right else "newer" if left > right else "equal"


def read_skill_version(root: Path) -> str:
    try:
        text = (root / "SKILL.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReceiverError(f"cannot read installed SKILL.md: {exc}") from exc
    match = re.search(r"(?m)^\s*version:\s*['\"]?([^'\"\s]+)", text)
    if not match:
        raise ReceiverError("installed SKILL.md has no metadata.version")
    return parse_version(match.group(1), "installed Skill version")[2]


def validate_incremental(payload: dict[str, Any], raw: bytes, identity: str, repository: str) -> dict[str, Any]:
    claimed = payload.get("index_sha256")
    require_sha256(claimed, "index_sha256")
    without_hash = dict(payload)
    without_hash.pop("index_sha256", None)
    actual = digest(canonical(without_hash))
    if claimed != actual or raw != canonical(payload):
        raise ReceiverError("incremental device index hash or canonical encoding is invalid")
    if identity != claimed:
        raise ReceiverError("device index content binding identity does not match index_sha256")
    if payload.get("repository") != repository:
        raise ReceiverError("device index repository mismatch")
    required = parse_version(payload.get("required_skill_version"), "required_skill_version")[2]
    release = payload.get("skill_release")
    if not isinstance(release, dict):
        raise ReceiverError("device index skill_release is required")
    tag = require_text(release.get("tag"), "skill_release.tag")
    if not SAFE_TAG_RE.fullmatch(tag):
        raise ReceiverError("skill_release.tag is invalid")
    manifest_hash = require_sha256(release.get("manifest_sha256"), "skill_release.manifest_sha256")
    if not isinstance(payload.get("projects"), list):
        raise ReceiverError("device index projects must be an array")
    return {"required_skill_version": required, "skill_release_tag": tag, "skill_manifest_sha256": manifest_hash}


def validate_legacy(payload: dict[str, Any], raw: bytes, identity: str) -> None:
    if identity != digest(raw):
        raise ReceiverError("legacy device index content binding identity does not match the asset SHA-256")
    if payload.get("status") != "complete" or not isinstance(payload.get("projects"), list):
        raise ReceiverError("legacy device index is incomplete or malformed")
    if payload.get("format") == "cross-device-project-sync-device-migration-v1":
        if payload.get("project_count") != len(payload["projects"]):
            raise ReceiverError("legacy device index project_count mismatch")
    else:
        required_counts = (
            "discovered_project_count", "selected_local_project_count", "completed_project_count",
            "failed_project_count", "excluded_project_count", "cloud_project_count",
        )
        if not all(isinstance(payload.get(name), int) and payload[name] >= 0 for name in required_counts):
            raise ReceiverError("v2 device index inventory counts are malformed")


def discover_index(bound_release: dict[str, Any], assets_root: Path, repository: str) -> dict[str, Any]:
    candidates: list[tuple[str, dict[str, Any], bytes]] = []
    unknown_formats: list[str] = []
    for name in bound_release["bound_assets"]:
        path = assets_root / name
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            if path.suffix.casefold() == ".json":
                raise ReceiverError(f"bound JSON asset is damaged: {name}")
            continue
        if not isinstance(payload, dict):
            continue
        value = payload.get("format")
        if value in INCREMENTAL_FORMATS or value in LEGACY_FORMATS:
            candidates.append((name, payload, path.read_bytes()))
        elif isinstance(value, str):
            unknown_formats.append(value)
    if not candidates:
        detail = ", ".join(sorted(set(unknown_formats))) or "none declared"
        raise ReceiverError(f"unsupported device index format; observed: {detail}")
    if len(candidates) != 1:
        raise ReceiverError("device index Release contains multiple supported index assets")
    name, payload, raw = candidates[0]
    if payload["format"] in INCREMENTAL_FORMATS:
        bootstrap = validate_incremental(payload, raw, bound_release["identity_sha256"], repository)
        return {"index_kind": "incremental", "index_asset": name, "index_path": str((assets_root / name).resolve()), **bootstrap}
    validate_legacy(payload, raw, bound_release["identity_sha256"])
    return {
        "index_kind": "legacy",
        "index_asset": name,
        "index_path": str((assets_root / name).resolve()),
        "required_skill_version": None,
        "skill_release_tag": None,
        "skill_manifest_sha256": None,
    }


def is_compiled_cache(name: str) -> bool:
    pure = PurePosixPath(name)
    return "__pycache__" in pure.parts or pure.suffix.casefold() in {".pyc", ".pyo"}


def validate_skill_package(
    bound_release: dict[str, Any],
    assets_root: Path,
    expected_version: str,
    expected_manifest_sha256: str,
) -> dict[str, Any]:
    manifest_candidates = []
    for name in bound_release["bound_assets"]:
        path = assets_root / name
        if path.suffix.casefold() != ".json":
            continue
        try:
            value = load_json(path)
        except ReceiverError:
            continue
        if value.get("format") == SKILL_PACKAGE_FORMAT:
            manifest_candidates.append((name, value, path.read_bytes()))
    if len(manifest_candidates) != 1:
        raise ReceiverError("skill Release must contain exactly one supported package manifest")
    manifest_name, manifest, manifest_raw = manifest_candidates[0]
    if digest(manifest_raw) != expected_manifest_sha256:
        raise ReceiverError("skill package manifest SHA-256 does not match device index binding")
    if manifest.get("skill_name") != SKILL_NAME or manifest.get("version") != expected_version:
        raise ReceiverError("skill package name or complete version does not match the device index")
    parse_version(manifest.get("version"), "skill package version")
    zip_name = safe_asset_name(manifest.get("payload_name"), "skill package payload_name")
    if zip_name not in bound_release["bound_assets"]:
        raise ReceiverError("skill package ZIP is not covered by CONTENT_RELEASE_BINDING.json")
    zip_path = assets_root / zip_name
    zip_data = zip_path.read_bytes()
    payload_hash = require_sha256(manifest.get("payload_sha256"), "skill package payload_sha256")
    if manifest.get("payload_size") != len(zip_data) or payload_hash != digest(zip_data):
        raise ReceiverError("skill package ZIP identity mismatch")
    if bound_release["identity_sha256"] != payload_hash:
        raise ReceiverError("skill Release identity does not match package ZIP SHA-256")
    records = manifest.get("files")
    if not isinstance(records, list) or not records:
        raise ReceiverError("skill package file manifest must be a non-empty array")
    expected: dict[str, dict[str, Any]] = {}
    casefolded: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ReceiverError(f"skill package file record {index} is not an object")
        name = require_text(record.get("path"), f"skill package file {index}.path")
        pure = PurePosixPath(name)
        if pure.is_absolute() or ".." in pure.parts or "\\" in name or len(pure.parts) < 2 or pure.parts[0] != SKILL_NAME:
            raise ReceiverError(f"unsafe skill package member path: {name}")
        if name.casefold() in casefolded:
            raise ReceiverError(f"duplicate or case-colliding skill package member: {name}")
        casefolded.add(name.casefold())
        require_sha256(record.get("sha256"), f"skill package file {name}.sha256")
        if not isinstance(record.get("size"), int) or record["size"] < 0:
            raise ReceiverError(f"skill package file {name}.size is invalid")
        expected[name] = record
    with zipfile.ZipFile(zip_path) as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if len(names) != len(set(names)) or set(names) != set(expected):
            raise ReceiverError("skill package ZIP inventory does not match its manifest")
        for info in infos:
            if info.is_dir():
                raise ReceiverError(f"skill package contains an unexpected directory member: {info.filename}")
            data = archive.read(info)
            record = expected[info.filename]
            if len(data) != record["size"] or digest(data) != record["sha256"]:
                raise ReceiverError(f"skill package member hash mismatch: {info.filename}")
        skill_md_name = f"{SKILL_NAME}/SKILL.md"
        if skill_md_name not in expected:
            raise ReceiverError("skill package does not contain SKILL.md")
        skill_md = archive.read(skill_md_name).decode("utf-8")
        version_match = re.search(r"(?m)^\s*version:\s*['\"]?([^'\"\s]+)", skill_md)
        if not version_match or version_match.group(1) != expected_version:
            raise ReceiverError("packaged SKILL.md version does not match package manifest")
    return {
        "manifest_name": manifest_name,
        "manifest_sha256": expected_manifest_sha256,
        "zip_path": zip_path,
        "records": expected,
        "filtered_compiled_cache_count": sum(is_compiled_cache(name) for name in expected),
    }


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def skill_tree_sha256(root: Path) -> str:
    if not root.is_dir():
        raise ReceiverError(f"installed Skill root is missing: {root}")
    records = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix().casefold()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            records.append({"path": relative, "type": "link", "target": os.readlink(path)})
        elif path.is_file():
            data = path.read_bytes()
            records.append({"path": relative, "type": "file", "size": len(data), "sha256": digest(data)})
        elif path.is_dir():
            records.append({"path": relative, "type": "directory"})
    encoded = (json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    return digest(encoded)


def verify_staged_skill(package: dict[str, Any], target: Path) -> None:
    if not target.is_dir() or target.is_symlink():
        raise ReceiverError(f"bound Skill runtime is missing or unsafe: {target}")
    expected = {
        PurePosixPath(name).relative_to(SKILL_NAME).as_posix(): record
        for name, record in package["records"].items()
        if not is_compiled_cache(name)
    }
    actual: dict[str, Path] = {}
    for path in target.rglob("*"):
        if path.is_symlink():
            raise ReceiverError(f"bound Skill runtime contains a link: {path}")
        if path.is_file():
            relative = path.relative_to(target).as_posix()
            if relative.casefold() in {item.casefold() for item in actual}:
                raise ReceiverError(f"bound Skill runtime contains a case-colliding path: {relative}")
            actual[relative] = path
    if set(actual) != set(expected):
        raise ReceiverError("bound Skill runtime inventory does not match the verified package")
    for relative, record in expected.items():
        data = actual[relative].read_bytes()
        if len(data) != record["size"] or digest(data) != record["sha256"]:
            raise ReceiverError(f"bound Skill runtime file mismatch: {relative}")


def stage_bound_skill(package: dict[str, Any], runtime_root: Path, version: str) -> Path:
    runtime_root = runtime_root.resolve()
    runtime_root.mkdir(parents=True, exist_ok=True)
    identity = package["manifest_sha256"][:16]
    container = runtime_root / f"{version}-{identity}"
    target = container / SKILL_NAME
    if container.exists():
        verify_staged_skill(package, target)
        return target
    stage_root = owned_temp.make_temp(runtime_root, ".bound-skill-stage-")
    extracted_root = stage_root / SKILL_NAME
    try:
        with zipfile.ZipFile(package["zip_path"]) as archive:
            for name in package["records"]:
                if is_compiled_cache(name):
                    continue
                relative = PurePosixPath(name)
                destination = stage_root.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(name))
        if read_skill_version(extracted_root) != version:
            raise ReceiverError("staged Skill version does not match the device index")
        verify_staged_skill(package, extracted_root)
        try:
            os.replace(stage_root, container)
        except OSError:
            if not container.exists():
                raise
            verify_staged_skill(package, target)
        return target
    finally:
        if stage_root.exists():
            owned_temp.remove(stage_root, runtime_root)
        else:
            owned_temp.release(stage_root)


def run_bound_interpreter(installed_root: Path, index_path: Path, kind: str) -> str | None:
    if kind != "incremental":
        return None
    helper = installed_root / "scripts" / "incremental-device-sync.py"
    if not helper.is_file():
        raise ReceiverError("exact bound Skill lacks scripts/incremental-device-sync.py")
    child_env = os.environ.copy()
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", str(helper), "verify-index", "--index", str(index_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=child_env,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise ReceiverError(f"exact bound Skill rejected the device index: {detail}")
    return str(helper.resolve())


def verify_discovered_index_with_bound_skill(
    gh: str,
    repository: str,
    index: dict[str, Any],
    temporary_root: Path,
) -> dict[str, Any] | None:
    """Verify an incremental index with the exact Skill Release it binds."""
    if index["index_kind"] != "incremental":
        return None
    required = index["required_skill_version"]
    release_tag = index["skill_release_tag"]
    manifest_sha256 = index["skill_manifest_sha256"]
    skill_metadata = remote_release_metadata(gh, repository, release_tag)
    metadata_path, assets_root = download_release(
        gh, repository, skill_metadata, temporary_root / "skill-release"
    )
    skill_release = validate_bound_release(
        metadata_path,
        assets_root,
        repository,
        "skill-package",
        expected_tag=release_tag,
    )
    package = validate_skill_package(
        skill_release, assets_root, required, manifest_sha256
    )
    bound_root = stage_bound_skill(
        package, temporary_root / "bound-skill-runtimes", required
    )
    interpreter = run_bound_interpreter(
        bound_root, Path(index["index_path"]), index["index_kind"]
    )
    return {
        "skill_release_tag": release_tag,
        "skill_manifest_sha256": manifest_sha256,
        "bound_skill_interpreter": interpreter,
        "verified": True,
    }


def command_inspect(args: argparse.Namespace) -> dict[str, Any]:
    repository = require_text(args.repository, "repository")
    release = validate_bound_release(
        Path(args.release_metadata), Path(args.assets), repository, "device-index"
    )
    index = discover_index(release, Path(args.assets), repository)
    installed_root = Path(args.installed_skill_root).resolve()
    if installed_root != canonical_public_skill_root():
        raise ReceiverError(f"installed Skill root must be the canonical shared path: {canonical_public_skill_root()}")
    installed_before = read_skill_version(installed_root)
    installed_tree_before = skill_tree_sha256(installed_root)
    required = index["required_skill_version"]
    relation = None
    action = "legacy-no-bound-upgrade"
    bound_runtime = None
    filtered = 0
    interpreter = None
    if required is not None:
        relation = compare_versions(installed_before, required)
        if not args.install_bound_skill or not args.skill_release_metadata or not args.skill_assets:
            raise ReceiverError(
                "the exact hash-bound Skill Release is required even when the installed version string matches"
            )
        skill_release = validate_bound_release(
            Path(args.skill_release_metadata), Path(args.skill_assets), repository, "skill-package",
            expected_tag=index["skill_release_tag"],
        )
        package = validate_skill_package(
            skill_release, Path(args.skill_assets), required, index["skill_manifest_sha256"]
        )
        filtered = package["filtered_compiled_cache_count"]
        runtime_root = Path(args.bound_skill_runtime_root).resolve() if args.bound_skill_runtime_root else Path(args.assets).resolve().parent / "bound-skill-runtimes"
        codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
        discovery_roots = {
            installed_root.parent.resolve(),
            (Path.home() / ".agents" / "skills").resolve(),
            (codex_home / "skills").resolve(),
        }
        if any(is_within(runtime_root, root) for root in discovery_roots):
            raise ReceiverError("bound Skill runtime must stay outside the shared Skill discovery directory")
        bound_root = stage_bound_skill(package, runtime_root, required)
        bound_runtime = str(bound_root)
        action = "staged-exact-bound-skill"
        interpreter = run_bound_interpreter(bound_root, Path(index["index_path"]), index["index_kind"])
    installed_tree_after = skill_tree_sha256(installed_root)
    return {
        "repository": repository,
        "index_release_tag": release["tag"],
        "index_kind": index["index_kind"],
        "index_asset": index["index_asset"],
        "index_identity_sha256": release["identity_sha256"],
        "installed_skill_version_before": installed_before,
        "required_skill_version": required,
        "version_comparison": relation,
        "skill_release_tag": index["skill_release_tag"],
        "skill_action": action,
        "filtered_compiled_cache_count": filtered,
        "installed_skill_tree_sha256_before": installed_tree_before,
        "installed_skill_tree_sha256_after": installed_tree_after,
        "public_skill_unchanged": installed_tree_after == installed_tree_before,
        "bound_skill_runtime_root": bound_runtime,
        "previous_skill_backup": None,
        "bound_skill_interpreter": interpreter,
        "verified": True,
    }


def resolve_gh(requested: str | None) -> str:
    if requested:
        path = Path(requested).resolve()
        if not path.is_file():
            raise ReceiverError(f"GitHub CLI does not exist: {path}")
        return str(path)
    found = shutil.which("gh") or shutil.which("gh.exe")
    if not found:
        raise ReceiverError("GitHub CLI was not found; pass --gh")
    return found


def run_gh(gh: str, *arguments: str, timeout: int = 180) -> str:
    completed = subprocess.run(
        [gh, *arguments], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
    )
    if completed.returncode != 0:
        raise ReceiverError(f"GitHub CLI failed: {' '.join(arguments[:4])}: {completed.stderr.strip()}")
    return completed.stdout


def verify_remote_access(gh: str, repository: str) -> None:
    run_gh(gh, "auth", "status", "--hostname", "github.com")
    repo = json.loads(run_gh(gh, "repo", "view", repository, "--json", "nameWithOwner,isPrivate"))
    if repo.get("nameWithOwner") != repository or repo.get("isPrivate") is not True:
        raise ReceiverError("canonical repository is missing, public, or has unexpected identity")


def remote_release_metadata(gh: str, repository: str, tag: str) -> dict[str, Any]:
    if not SAFE_TAG_RE.fullmatch(tag):
        raise ReceiverError(f"invalid Release tag: {tag}")
    return json.loads(run_gh(
        gh, "release", "view", tag, "--repo", repository,
        "--json", "tagName,isDraft,body,assets,publishedAt",
    ))


def prepare_download_destination(
    metadata: dict[str, Any], destination: Path, allow_existing: bool,
) -> Path:
    metadata_path = destination / "RELEASE_METADATA.json"
    encoded = canonical(metadata)
    if destination.exists():
        if not allow_existing:
            raise ReceiverError(f"download destination already exists: {destination}")
        if not destination.is_dir() or destination.is_symlink():
            raise ReceiverError(f"existing download destination is not a safe directory: {destination}")
        if not metadata_path.is_file() or metadata_path.is_symlink():
            raise ReceiverError(f"existing download metadata is missing or unsafe: {metadata_path}")
        existing_metadata = load_json(metadata_path)
        if canonical(resume_metadata_identity(existing_metadata)) != canonical(resume_metadata_identity(metadata)):
            raise ReceiverError(f"existing download metadata does not match the selected Release: {destination}")
        return metadata_path
    destination.mkdir(parents=True)
    metadata_path.write_bytes(encoded)
    return metadata_path


def resume_metadata_identity(metadata: dict[str, Any]) -> dict[str, Any]:
    """Remove only GitHub's mutable per-asset download counter for resume comparison."""
    normalized = json.loads(json.dumps(metadata, ensure_ascii=False))
    assets = normalized.get("assets")
    if isinstance(assets, list):
        for asset in assets:
            if isinstance(asset, dict):
                asset.pop("downloadCount", None)
    return normalized


def obtain_release_asset(
    gh: str,
    repository: str,
    tag: str,
    destination: Path,
    name: str,
    remote_record: dict[str, Any],
) -> bytes:
    path = destination / name
    if path.exists():
        if not path.is_file() or path.is_symlink():
            raise ReceiverError(f"existing Release asset is not a safe regular file: {name}")
        data = path.read_bytes()
        verify_remote_record(name, data, remote_record)
        return data
    run_gh(
        gh, "release", "download", tag, "--repo", repository,
        "--pattern", name, "--dir", str(destination), timeout=7200,
    )
    data = path.read_bytes()
    verify_remote_record(name, data, remote_record)
    return data


def download_release(
    gh: str,
    repository: str,
    metadata: dict[str, Any],
    destination: Path,
    *,
    allow_existing: bool = False,
) -> tuple[Path, Path]:
    tag = require_text(metadata.get("tagName"), "Release tag")
    metadata_path = prepare_download_destination(metadata, destination, allow_existing)
    remote_assets = metadata_asset_map(metadata)
    if "CONTENT_RELEASE_BINDING.json" not in remote_assets:
        raise ReceiverError(f"Release lacks CONTENT_RELEASE_BINDING.json: {tag}")
    obtain_release_asset(
        gh, repository, tag, destination, "CONTENT_RELEASE_BINDING.json",
        remote_assets["CONTENT_RELEASE_BINDING.json"],
    )
    binding = load_json(destination / "CONTENT_RELEASE_BINDING.json")
    records = binding.get("assets")
    if not isinstance(records, list) or not records:
        raise ReceiverError("downloaded content binding has no assets")
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ReceiverError(f"content binding asset {index} is not an object")
        name = safe_asset_name(record.get("name"), f"content binding asset {index}.name")
        if name not in remote_assets:
            raise ReceiverError(f"bound asset is absent from Release metadata: {name}")
        data = obtain_release_asset(gh, repository, tag, destination, name, remote_assets[name])
        if record.get("size") != len(data) or require_sha256(
            record.get("sha256"), f"content binding asset {name}.sha256"
        ) != digest(data):
            raise ReceiverError(f"content binding asset hash or size mismatch: {name}")
    return metadata_path, destination


def download_all_release_assets(
    gh: str,
    repository: str,
    metadata: dict[str, Any],
    destination: Path,
    *,
    allow_existing: bool = False,
) -> tuple[Path, Path]:
    tag = require_text(metadata.get("tagName"), "Release tag")
    metadata_path = prepare_download_destination(metadata, destination, allow_existing)
    assets = metadata_asset_map(metadata)
    if not assets:
        raise ReceiverError(f"Release has no assets: {tag}")
    for name, record in assets.items():
        obtain_release_asset(gh, repository, tag, destination, name, record)
    return metadata_path, destination


def validate_project_release(metadata_path: Path, assets_root: Path, repository: str, project: dict[str, Any], bound_root: Path, source_device: str, source_computer: str) -> dict[str, Any]:
    metadata = load_json(metadata_path)
    tag = require_text(metadata.get("tagName"), "project Release tag")
    expected_release = project["project_release"]
    if tag != expected_release["tag"] or metadata.get("isDraft") is not False:
        raise ReceiverError(f"project Release is missing, draft, or has the wrong tag: {tag}")
    remote_assets = metadata_asset_map(metadata)
    manifest_path = assets_root / "BACKUP_MANIFEST.json"
    binding_path = assets_root / "RELEASE_BINDING.json"
    manifest_hash = digest(manifest_path.read_bytes())
    if manifest_hash != expected_release["manifest_sha256"]:
        raise ReceiverError(f"project manifest hash does not match device index: {tag}")
    binding = load_json(binding_path)
    if (
        binding.get("repository") != repository
        or binding.get("release_tag") != tag
        or binding.get("manifest_sha256") != manifest_hash
    ):
        raise ReceiverError(f"project Release binding mismatch: {tag}")
    manifest = load_json(manifest_path)
    parts = manifest.get("parts")
    if manifest.get("format") != "split-tar-pax-v2" or not isinstance(parts, list) or not parts:
        raise ReceiverError(f"unsupported project backup manifest: {tag}")
    source_name = require_text(manifest.get("source_name"), "project backup source_name")
    if source_name in {".", ".."} or "/" in source_name or "\\" in source_name:
        raise ReceiverError(f"project backup source_name is not one directory name: {tag}")
    if manifest.get("source_device") != source_device or str(manifest.get("source_computer") or "").casefold() != source_computer.casefold():
        raise ReceiverError(f"project backup source identity does not match device index: {tag}")
    expected_names = {
        "BACKUP_MANIFEST.json", "RELEASE_BINDING.json", "README.md", "RESTORE.txt",
        "restore.ps1", "verify_split_tar.py", "verify_restored_tree.py", "extract_verified_tar.py",
        *(safe_asset_name(item.get("name"), "project backup part name") for item in parts),
    }
    if set(remote_assets) != expected_names:
        raise ReceiverError(f"project Release asset inventory mismatch: {tag}")
    for part in parts:
        name = safe_asset_name(part.get("name"), "project backup part name")
        data = (assets_root / name).read_bytes()
        if part.get("size") != len(data) or require_sha256(part.get("sha256"), f"{name}.sha256") != digest(data):
            raise ReceiverError(f"project backup part mismatch: {tag}/{name}")
    helper = bound_root / "scripts" / "incremental-device-sync.py"
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", str(helper), "manifest-fingerprint", "--manifest", str(manifest_path), "--expected", project["tree_sha256"]],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    if completed.returncode != 0:
        raise ReceiverError(f"project tree fingerprint verification failed: {tag}: {(completed.stderr or completed.stdout).strip()}")
    return {
        "tag": tag,
        "manifest_sha256": manifest_hash,
        "assets_root": str(assets_root),
        "source_name": source_name,
    }


def validate_restore_result(
    result: dict[str, Any], destination: Path, repository: str,
    release_tag: str, manifest_sha256: str, source_name: str,
) -> str:
    if result.get("restored") is not True or result.get("verified") is not True:
        raise ReceiverError("restore helper did not report restored=true and verified=true")
    if result.get("release_binding_verified") is not True:
        raise ReceiverError("restore helper did not verify the Release binding")
    if result.get("release_tag") != release_tag or result.get("manifest_sha256") != manifest_sha256:
        raise ReceiverError("restore helper result does not match the expected Release and manifest")
    if result.get("repository") != repository:
        raise ReceiverError("restore helper result does not match the expected repository")
    reported_destination = Path(require_text(result.get("destination"), "restore destination")).resolve()
    expected_destination = destination.resolve()
    if reported_destination != expected_destination:
        raise ReceiverError("restore helper reported an unexpected destination")
    restored_root = Path(require_text(result.get("restored_project_root"), "restored_project_root")).resolve()
    expected_root = (expected_destination / source_name).resolve()
    if restored_root != expected_root:
        raise ReceiverError("restore helper reported an unexpected restored project root")
    if not restored_root.is_dir():
        raise ReceiverError(f"restored project root was not created: {restored_root}")
    return str(restored_root)


def verify_conversation_release(metadata_path: Path, assets_root: Path, repository: str, project: dict[str, Any], bound_root: Path) -> dict[str, Any]:
    release = validate_bound_release(
        metadata_path, assets_root, repository, "conversation-pack", expected_tag=project["conversation_release"]["tag"]
    )
    if release["identity_sha256"] != project["conversation_set_sha256"]:
        raise ReceiverError(f"conversation Release identity mismatch: {release['tag']}")
    manifest_path = assets_root / "CONVERSATION_PACK_MANIFEST.json"
    if digest(manifest_path.read_bytes()) != project["conversation_release"]["manifest_sha256"]:
        raise ReceiverError(f"conversation manifest hash does not match device index: {release['tag']}")
    helper = bound_root / "scripts" / "bulk-conversation-pack.py"
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", str(helper), "verify", "--pack", str(assets_root)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    if completed.returncode != 0:
        raise ReceiverError(f"conversation pack verification failed: {release['tag']}: {(completed.stderr or completed.stdout).strip()}")
    return {"tag": release["tag"], "manifest_sha256": digest(manifest_path.read_bytes()), "assets_root": str(assets_root)}


def extract_conversation_pack(assets_root: Path, destination: Path) -> None:
    if destination.exists():
        raise ReceiverError(f"conversation extraction destination already exists: {destination}")
    manifest = load_json(assets_root / "CONVERSATION_PACK_MANIFEST.json")
    expected = {str(item.get("path") or ""): item for item in manifest.get("files") or [] if isinstance(item, dict)}
    if "" in expected or len(expected) != len(manifest.get("files") or []) or (not expected and manifest.get("conversation_count") != 0):
        raise ReceiverError("conversation manifest file inventory is invalid")
    stage = owned_temp.make_temp(destination.parent, f".{destination.name}.stage-")
    try:
        with zipfile.ZipFile(assets_root / "CONVERSATION_PACK.zip") as archive:
            if set(archive.namelist()) != set(expected) or len(archive.namelist()) != len(expected):
                raise ReceiverError("conversation ZIP inventory does not match its manifest")
            for name, record in expected.items():
                pure = PurePosixPath(name)
                if pure.is_absolute() or ".." in pure.parts or "\\" in name:
                    raise ReceiverError(f"unsafe conversation ZIP path: {name}")
                data = archive.read(name)
                if record.get("size") != len(data) or record.get("sha256") != digest(data):
                    raise ReceiverError(f"conversation ZIP member mismatch: {name}")
                target = stage.joinpath(*pure.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        os.replace(stage, destination)
    finally:
        if stage.exists():
            owned_temp.remove(stage, destination.parent)
        else:
            owned_temp.release(stage)


def resolve_powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell") or shutil.which("powershell.exe")
    if not executable:
        raise ReceiverError("PowerShell was not found for verified project restoration")
    return executable


def parse_last_json(text: str, label: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    found = []
    for position, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, consumed = decoder.raw_decode(text[position:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            found.append((position + consumed, position, value))
    if found:
        # Nested objects begin later than their containing result, so choosing
        # only the latest opening brace can return an inner object.  The last
        # complete JSON value is the candidate whose parsed value ends latest;
        # prefer the outermost candidate when multiple values share that end.
        return max(found, key=lambda item: (item[0], -item[1]))[2]
    raise ReceiverError(f"{label} did not return a JSON result")


def existing_parent(path: Path) -> Path:
    current = path
    while not current.exists():
        if current.parent == current:
            raise ReceiverError(f"cannot resolve a filesystem volume for capacity check: {path}")
        current = current.parent
    return current


def require_free_space(path: Path, required_bytes: int, label: str) -> None:
    if required_bytes < 0:
        raise ReceiverError(f"invalid required byte count for {label}")
    free = shutil.disk_usage(existing_parent(path)).free
    reserve = max(1024 * 1024 * 1024, required_bytes // 10)
    if free < required_bytes + reserve:
        raise ReceiverError(
            f"insufficient free space for {label}: required payload {required_bytes} bytes plus reserve {reserve}, available {free}"
        )


def command_discover(args: argparse.Namespace) -> dict[str, Any]:
    repository = require_text(args.repository, "repository")
    gh = resolve_gh(args.gh)
    verify_remote_access(gh, repository)
    releases = json.loads(run_gh(
        gh, "release", "list", "--repo", repository, "--limit", str(args.limit),
        "--json", "tagName,isDraft,publishedAt",
    ))
    candidates = []
    for summary in releases:
        if summary.get("isDraft"):
            continue
        metadata = remote_release_metadata(gh, repository, summary["tagName"])
        notes = parse_notes(metadata.get("body"))
        assets = metadata_asset_map(metadata)
        if notes.get("Content-Kind") == "device-index" and notes.get("Repository") == repository:
            with tempfile.TemporaryDirectory(prefix="cross-device-index-discovery-") as raw:
                temporary_root = Path(raw)
                metadata_path, assets_root = download_release(gh, repository, metadata, temporary_root / "index")
                release = validate_bound_release(metadata_path, assets_root, repository, "device-index")
                discovered = discover_index(release, assets_root, repository)
                payload = load_json(Path(discovered["index_path"]))
                bound_verification = verify_discovered_index_with_bound_skill(
                    gh, repository, discovered, temporary_root
                )
                candidates.append({
                    "release_tag": metadata["tagName"], "published_at": metadata.get("publishedAt"),
                    "identity_sha256": release["identity_sha256"], "verified": True,
                    "bound_skill_release_tag": (
                        bound_verification["skill_release_tag"] if bound_verification else None
                    ),
                    "bound_skill_manifest_sha256": (
                        bound_verification["skill_manifest_sha256"] if bound_verification else None
                    ),
                    "source_device": payload.get("source_device"), "source_computer": payload.get("source_computer"),
                    "migration_id": payload.get("migration_id"),
                    "project_count": sum(row.get("has_project_payload", True) and row.get("kind") not in {"projectless-root", "projectless-conversation-group"} for row in payload["projects"]),
                    "conversation_count": sum(int(row.get("conversation_count") or 0) for row in payload["projects"]),
                })
    limit_reached = len(releases) >= args.limit
    result = {
        "repository": repository,
        "device_index_releases": candidates,
        "selection_required": True,
        "release_scan_limit": args.limit,
        "release_inventory_complete": not limit_reached,
    }
    result["discovery_sha256"] = digest(canonical(result))
    if getattr(args, "output", None):
        output = Path(args.output)
        multi = local_module("multi-instance-sync.py")
        multi.queue_engine().require_account(output, multi.actual_caller_home())
        multi.atomic_write_json(output, result)
    return result


def run_inline_readiness(args: argparse.Namespace, context: dict[str, Any]) -> dict[str, Any]:
    readiness = local_module("device-readiness.py")
    report_name = "receive-" + hashlib.sha256(
        (context["caller"]["instance_id"] + "\0" + args.release_tag).encode("utf-8")
    ).hexdigest()[:24] + ".json"
    output = Path(context["registry"]["machine_state_root"]) / "readiness" / report_name
    report = readiness.build_report(argparse.Namespace(
        registry=str(Path(args.instance_registry).resolve()),
        installed_skill_root=str(Path(args.installed_skill_root).resolve()),
        output=str(output), expected_skill_version=CURRENT_SKILL_VERSION,
        expected_skill_package=None, check_cloud=False,
        repository=CANONICAL_REPOSITORY, gh=None,
    ))
    if report.get("status") != "ready":
        raise ReceiverError("device readiness is not ready: " + "; ".join(report.get("errors") or []))
    return {"path": str(output.resolve()), "sha256": report["readiness_sha256"], "report": report}


def _command_receive_locked(
    args: argparse.Namespace, context: dict[str, Any], readiness: dict[str, Any], lock: dict[str, Any],
) -> dict[str, Any]:
    repository = require_text(args.repository, "repository")
    gh = resolve_gh(args.gh)
    verify_remote_access(gh, repository)
    root = Path(args.download_root).resolve()
    resume_downloads = bool(getattr(args, "resume_verified_downloads", False))
    download_options = {"allow_existing": True} if resume_downloads else {}
    if root.exists() and not resume_downloads:
        raise ReceiverError(f"download root must be a new path: {root}")
    if root.exists() and (not root.is_dir() or root.is_symlink()):
        raise ReceiverError(f"existing download root is not a safe directory: {root}")
    index_metadata = remote_release_metadata(gh, repository, args.release_tag)
    notes = parse_notes(index_metadata.get("body"))
    if notes.get("Content-Kind") != "device-index":
        raise ReceiverError("selected Release notes do not declare Content-Kind: device-index")
    index_meta_path, index_assets = download_release(
        gh, repository, index_metadata, root / "device-index",
        **download_options,
    )
    release = validate_bound_release(index_meta_path, index_assets, repository, "device-index")
    if release.get("identity_sha256") != context["selection"]["identity_sha256"]:
        raise ReceiverError("selected Release identity changed since discovery")
    index = discover_index(release, index_assets, repository)
    inspect_args = argparse.Namespace(
        repository=repository,
        release_metadata=str(index_meta_path),
        assets=str(index_assets),
        installed_skill_root=args.installed_skill_root,
        install_bound_skill=False,
        skill_release_metadata=None,
        skill_assets=None,
        bound_skill_runtime_root=str(root / "bound-skill-runtimes"),
    )
    if index["required_skill_version"] is not None:
        skill_metadata = remote_release_metadata(gh, repository, index["skill_release_tag"])
        skill_meta_path, skill_assets = download_release(
            gh, repository, skill_metadata, root / "bound-skill",
            **download_options,
        )
        inspect_args.install_bound_skill = True
        inspect_args.skill_release_metadata = str(skill_meta_path)
        inspect_args.skill_assets = str(skill_assets)
    inspected = command_inspect(inspect_args)
    if inspected["index_kind"] != "incremental":
        return {**inspected, "download_root": str(root), "content_status": "legacy-index-requires-legacy-restore-workflow"}
    bound_root = Path(require_text(inspected.get("bound_skill_runtime_root"), "bound Skill runtime root"))
    index_payload = load_json(Path(index["index_path"]))
    restore_root = Path(args.restore_root).resolve() if args.restore_root else root / "restored"
    if restore_root.exists():
        raise ReceiverError(f"restore root must be a new path: {restore_root}")
    release_metadata_rows = []
    total_download_bytes = 0
    total_project_restore_bytes = 0
    for project in index_payload["projects"]:
        has_project_payload = project.get("has_project_payload", True)
        project_metadata = (
            remote_release_metadata(gh, repository, project["project_release"]["tag"])
            if has_project_payload else None
        )
        conversation_metadata = remote_release_metadata(gh, repository, project["conversation_release"]["tag"])
        release_metadata_rows.append((project_metadata, conversation_metadata))
        for metadata in (item for item in (project_metadata, conversation_metadata) if item is not None):
            for asset in metadata_asset_map(metadata).values():
                size = asset.get("size")
                if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                    raise ReceiverError(f"Release asset has an invalid size: {metadata.get('tagName')}")
                total_download_bytes += size
        project_bytes = project.get("total_bytes")
        if not isinstance(project_bytes, int) or isinstance(project_bytes, bool) or project_bytes < 0:
            raise ReceiverError(f"device index project has invalid total_bytes: {project.get('project_id')}")
        total_project_restore_bytes += project_bytes
    same_volume = os.path.splitdrive(str(root))[0].casefold() == os.path.splitdrive(str(restore_root))[0].casefold()
    if same_volume:
        require_free_space(root, total_download_bytes + total_project_restore_bytes, "combined migration download and restore")
    else:
        require_free_space(root, total_download_bytes, "migration download")
        require_free_space(restore_root, total_project_restore_bytes, "project restoration")
    restore_root.mkdir(parents=True)
    conversation_root = restore_root / "_conversation-packs"
    conversation_root.mkdir()
    powershell = resolve_powershell()
    restored_projects = []
    for position, project in enumerate(index_payload["projects"], 1):
        project_key = hashlib.sha256(project["project_id"].encode("utf-8")).hexdigest()[:16]
        project_metadata, conversation_metadata = release_metadata_rows[position - 1]
        has_project_payload = project.get("has_project_payload", True)
        project_assets = None
        project_release = None
        if has_project_payload:
            assert project_metadata is not None
            project_meta_path, project_assets = download_all_release_assets(
                gh, repository, project_metadata, root / "projects" / project_key,
                **download_options,
            )
            project_release = validate_project_release(
                project_meta_path, project_assets, repository, project, bound_root,
                index_payload["source_device"], index_payload["source_computer"],
            )
        conversation_meta_path, conversation_assets = download_release(
            gh, repository, conversation_metadata, root / "conversations" / project_key,
            **download_options,
        )
        conversation_release = verify_conversation_release(
            conversation_meta_path, conversation_assets, repository, project, bound_root
        )
        conversation_manifest = load_json(conversation_assets / "CONVERSATION_PACK_MANIFEST.json")
        conversation_uncompressed_bytes = sum(
            int(item.get("size") or 0) for item in conversation_manifest.get("files") or [] if isinstance(item, dict)
        )
        require_free_space(
            restore_root,
            (int(project["total_bytes"]) if has_project_payload else 0) + conversation_uncompressed_bytes,
            f"project and conversation restore {project['project_id']}",
        )
        conversation_destination = conversation_root / project_key
        conversation_destination.parent.mkdir(parents=True, exist_ok=True)
        extract_conversation_pack(conversation_assets, conversation_destination)
        restored_project_root = None
        installation = None
        if has_project_payload:
            assert project_assets is not None and project_release is not None
            destination = restore_root / f"{position:03d}-{project_key}"
            restore_helper = bound_root / "scripts" / "restore-full-backup.ps1"
            completed = subprocess.run([
                powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(restore_helper),
                "-BackupPath", str(project_assets), "-Destination", str(destination),
                "-ReleaseTag", project_release["tag"], "-ExpectedRepository", repository,
                "-ExpectedManifestSha256", project_release["manifest_sha256"], "-PythonExe", sys.executable,
            ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=7200)
            if completed.returncode != 0:
                raise ReceiverError(f"project restore failed: {project_release['tag']}: {(completed.stderr or completed.stdout).strip()}")
            restore_result = parse_last_json(completed.stdout, f"project restore {project_release['tag']}")
            restored_project_root = validate_restore_result(
                restore_result, destination, repository, project_release["tag"],
                project_release["manifest_sha256"], project_release["source_name"],
            )
            if index_payload.get("format") == "cross-device-project-sync-incremental-device-index-v2":
                installation = local_module("project-receive-transaction.py").install(
                    restored_project_root, project["tree_sha256"], context["caller"]["codex_home"],
                    index_payload["source_computer"].casefold() + ":" + project["project_id"], context["base"],
                    source_name=project_release["source_name"], source_device=index_payload["source_device"],
                    excluded_relative_paths=project.get("excluded_relative_paths") or [],
                )
                restored_project_root = installation["destination"]
        restored_projects.append({
            "index": position,
            "project_id": project["project_id"],
            "kind": project.get("kind") or "registered-project",
            "has_project_payload": has_project_payload,
            "label": project["label"],
            "restored_project_root": restored_project_root,
            "project_installation": installation,
            "project_release_tag": project_release["tag"] if project_release else None,
            "project_manifest_sha256": project_release["manifest_sha256"] if project_release else None,
            "conversation_release_tag": conversation_release["tag"],
            "conversation_manifest_sha256": conversation_release["manifest_sha256"],
            "conversation_pack_path": str(conversation_destination),
            "conversation_pack_manifest_path": str(conversation_assets / "CONVERSATION_PACK_MANIFEST.json"),
        })
    report = {
        "format": (
            "cross-device-project-sync-incremental-receive-report-v2"
            if index_payload.get("format") == "cross-device-project-sync-incremental-device-index-v2"
            else "cross-device-project-sync-incremental-receive-report-v1"
        ),
        "status": "restored-verified-not-registered",
        "destination_codex_home": context["caller"]["codex_home"],
        "destination_instance_id": context["caller"]["instance_id"],
        "repository": repository,
        "selection_file": context["selection_file"],
        "selection_discovery_sha256": context["selection_sha256"],
        "selected_release_identity_sha256": context["selection"]["identity_sha256"],
        "index_release_metadata_path": str(index_meta_path.resolve()),
        "index_assets_path": str(index_assets.resolve()),
        "installed_skill_root": str(Path(args.installed_skill_root).resolve()),
        "installed_skill_version": inspected["installed_skill_version_before"],
        "installed_skill_tree_sha256": inspected["installed_skill_tree_sha256_after"],
        "required_skill_version": inspected["required_skill_version"],
        "readiness_report_path": readiness["path"],
        "readiness_sha256": readiness["sha256"],
        "machine_lock_operation_id": lock["operation_id"],
        "machine_lock_token": lock["lock_token"],
        "machine_lock_token_sha256": lock["lock_token_sha256"],
        "migration_id": index_payload["migration_id"],
        "source_device": index_payload["source_device"],
        "source_computer": index_payload["source_computer"],
        "index_release_tag": inspected["index_release_tag"],
        "index_sha256": index_payload["index_sha256"],
        "device_index_path": str(Path(index["index_path"]).resolve()),
        "download_root": str(root),
        "restore_root": str(restore_root),
        "project_count": sum(item["has_project_payload"] for item in restored_projects),
        "registered_project_count": sum(item["has_project_payload"] and item["kind"] == "registered-project" for item in restored_projects),
        "projectless_directory_count": sum(item["kind"] == "projectless-root" for item in restored_projects),
        "conversation_only_group_count": sum(not item["has_project_payload"] for item in restored_projects),
        "content_group_count": len(restored_projects),
        "projects": restored_projects,
    }
    report["receive_report_sha256"] = digest(canonical(report))
    report_path = root / "INCREMENTAL_RECEIVE_REPORT.json"
    report_path.write_bytes(canonical(report))
    return {
        **inspected,
        "download_root": str(root),
        "restore_root": str(restore_root),
        "project_count": sum(item["has_project_payload"] for item in restored_projects),
        "conversation_only_group_count": sum(not item["has_project_payload"] for item in restored_projects),
        "receive_report": str(report_path),
        "content_status": "all-bound-projects-and-conversations-restored-and-verified",
        "app_registration_status": "pending-explicit-account-bound-registration",
    }


def command_receive(args: argparse.Namespace) -> dict[str, Any]:
    context = receive_context(args)
    readiness = run_inline_readiness(args, context)
    multi = local_module("multi-instance-sync.py")
    operation_id = "receive-" + hashlib.sha256(
        (context["caller"]["instance_id"] + "\0" + args.release_tag + "\0" + context["selection_sha256"]).encode("utf-8")
    ).hexdigest()[:32]
    lock = multi.acquire_lock(context["registry"], context["caller"], operation_id, "download")
    try:
        result = _command_receive_locked(args, context, readiness, lock)
        if result.get("content_status") == "legacy-index-requires-legacy-restore-workflow":
            released = multi.release_lock(context["registry"], operation_id, lock["lock_token"])
            result["machine_lock_released"] = True
            result["release_evidence_path"] = released["release_evidence_path"]
            result["release_evidence_sha256"] = released["release_evidence_sha256"]
        return result
    except Exception:
        # Controlled failures have no completed receive report to carry the
        # durable token into final acceptance, so release the lock here. A hard
        # process crash still leaves the lock for explicit audited recovery.
        multi.release_lock(context["registry"], operation_id, lock["lock_token"])
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect = subparsers.add_parser("inspect", help="validate already downloaded Release metadata and assets")
    inspect.add_argument("--release-metadata", required=True)
    inspect.add_argument("--assets", required=True)
    inspect.add_argument("--repository", default=CANONICAL_REPOSITORY)
    inspect.add_argument("--installed-skill-root", required=True)
    inspect.add_argument("--skill-release-metadata")
    inspect.add_argument("--skill-assets")
    inspect.add_argument("--stage-bound-skill", "--install-bound-skill", dest="install_bound_skill", action="store_true")
    inspect.add_argument("--bound-skill-runtime-root")
    inspect.set_defaults(handler=command_inspect)

    discover = subparsers.add_parser("discover", help="list verified-note device-index Release candidates")
    discover.add_argument("--repository", default=CANONICAL_REPOSITORY)
    discover.add_argument("--gh")
    discover.add_argument("--limit", type=int, default=1000)
    discover.add_argument("--output", help="verified selection catalog inside current CODEX_HOME")
    discover.set_defaults(handler=command_discover)

    receive = subparsers.add_parser("receive", help="download, verify, and restore every asset bound by one exact device-index Release")
    receive.add_argument("--release-tag", required=True)
    receive.add_argument("--selection-file", required=True)
    receive.add_argument("--instance-registry", default=os.environ.get("CROSS_DEVICE_SYNC_REGISTRY"), required=not os.environ.get("CROSS_DEVICE_SYNC_REGISTRY"))
    receive.add_argument("--download-root", required=True)
    receive.add_argument("--restore-root")
    receive.add_argument("--installed-skill-root", required=True)
    receive.add_argument("--repository", default=CANONICAL_REPOSITORY)
    receive.add_argument("--gh")
    receive.add_argument(
        "--resume-verified-downloads",
        action="store_true",
        help="reuse only existing Release metadata and assets that still match their verified remote size and SHA-256",
    )
    receive.set_defaults(handler=command_receive)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = args.handler(args)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (ValueError, OSError, UnicodeError, json.JSONDecodeError, zipfile.BadZipFile, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
