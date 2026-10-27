#!/usr/bin/env python3
"""Verified project installation with account ownership and recoverable renames.

Never overlays a tree and never deletes a rollback copy. All replacement moves
are within one account-owned base. A journal is durable before the first rename.
"""
from __future__ import annotations
import importlib.util
import os
from pathlib import Path
import re
import secrets
import shutil
import owned_temp


def sibling_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


io = sibling_module("project_receive_io", "receive-queue-engine.py")
inventory = sibling_module("project_receive_inventory", "incremental-device-sync.py")
FORMAT = "cross-device-project-receiver-ledger-v2"
MAX_PROJECT_COMPONENT_UTF16 = 96
WINDOWS_RESERVED_COMPONENTS = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
WINDOWS_INVALID_COMPONENT = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def fingerprint(path):
    # Compare every local path, including locally added excluded/auth files.
    return inventory.project_inventory(Path(path), [])["tree_sha256"]


def within(path, parent):
    path, parent = Path(path).resolve(), Path(parent).resolve()
    return path != parent and parent in path.parents


def owned(path, base):
    if not within(path, base):
        raise ValueError("project transaction path is outside the account-owned restore root")
    current = Path(path).absolute()
    while current != current.parent:
        if current.exists() and inventory.is_reparse(current):
            raise ValueError("project transaction path traverses a reparse point")
        current = current.parent


def ledger_path(home):
    return Path(home) / "cross-device-project-receiver-ledger.json"


def _truncate_utf16(value, limit=MAX_PROJECT_COMPONENT_UTF16):
    result = []
    units = 0
    for character in value:
        width = len(character.encode("utf-16-le")) // 2
        if units + width > limit:
            break
        result.append(character)
        units += width
    return "".join(result)


def safe_project_component(value, fallback):
    """Return one readable Windows-safe directory component."""
    text = WINDOWS_INVALID_COMPONENT.sub("_", str(value or "").strip()).rstrip(" .")
    if not text or text in {".", ".."}:
        text = WINDOWS_INVALID_COMPONENT.sub("_", str(fallback or "project").strip()).rstrip(" .") or "project"
    text = _truncate_utf16(text).rstrip(" .") or "project"
    if text.split(".", 1)[0].upper() in WINDOWS_RESERVED_COMPONENTS:
        text = _truncate_utf16("_" + text).rstrip(" .")
    return text


def _component_with_suffix(name, suffix):
    suffix = safe_project_component(suffix, "")
    suffix_units = len(suffix.encode("utf-16-le")) // 2
    if suffix_units >= MAX_PROJECT_COMPONENT_UTF16:
        return _truncate_utf16(suffix)
    prefix = _truncate_utf16(name, MAX_PROJECT_COMPONENT_UTF16 - suffix_units).rstrip(" .") or "project"
    return safe_project_component(prefix + suffix, "project")


def _component_exists(parent, name):
    if not parent.exists():
        return False
    folded = name.casefold()
    return any(child.name.casefold() == folded for child in parent.iterdir())


def choose_new_destination(base, source_name, source_device, project_key, candidate_sha256):
    """Choose a readable, non-overwriting destination for one distinct source project."""
    projects = Path(base).resolve() / "projects"
    readable = safe_project_component(source_name, f"project-{io.digest(project_key)[:8]}")
    candidates = [readable]
    device = safe_project_component(source_device, "source-device")
    from_device = _component_with_suffix(readable, f"（来自{device}）")
    candidates.append(from_device)
    stable = io.digest(project_key)[:8]
    candidates.append(_component_with_suffix(from_device, f"-{stable}"))
    candidates.append(_component_with_suffix(from_device, f"-{stable}-{candidate_sha256[:8]}"))
    for component in candidates:
        if not _component_exists(projects, component):
            return projects / component
    while True:
        component = _component_with_suffix(from_device, f"-{stable}-{secrets.token_hex(4)}")
        if not _component_exists(projects, component):
            return projects / component


def load_ledger(home, base):
    path = ledger_path(home)
    value = io.read(path) if path.exists() else {"format": FORMAT, "destination_codex_home": str(Path(home).resolve()),
                                               "restore_base": str(Path(base).resolve()), "projects": {}}
    if value.get("format") != FORMAT or not io.same_path(value["destination_codex_home"], home) or not io.same_path(value["restore_base"], base):
        raise ValueError("project receiver ledger belongs to another account or restore base")
    return value


def save_journal(path, value, phase):
    value["phase"] = phase
    value["updated_utc"] = io.now()
    value["journal_sha256"] = io.digest(io.unsigned(value, "journal_sha256"))
    io.write(path, value)


def _commit(journal, home, base):
    ledger = load_ledger(home, base)
    ledger["projects"][journal["project_key"]] = {
        "restored_project_root": journal["destination"], "last_applied_tree_sha256": journal["candidate_sha256"],
        "source_tree_sha256": journal["source_tree_sha256"], "last_transaction_id": journal["transaction_id"],
        "source_name": journal.get("source_name"), "source_device": journal.get("source_device"),
        "rollback_path": journal.get("rollback"), "updated_utc": io.now(),
    }
    io.write(ledger_path(home), ledger)


def cleanup_redundant_stage(stage, base):
    """Remove only a verified redundant staging tree inside the receiver base."""
    stage, base = Path(stage).resolve(), Path(base).resolve()
    owned(stage, base)
    if not stage.is_dir():
        return {"status": "already-absent", "path": str(stage)}
    quarantine = stage.with_name(stage.name + ".cleanup-pending-" + secrets.token_hex(6))
    owned(quarantine, base)
    if quarantine.exists():
        raise ValueError("generated staging cleanup path already exists")
    try:
        os.rename(stage, quarantine)
        owned_temp.claim_existing(quarantine, base)
    except OSError as exc:
        return {"status": "retained-cleanup-failed", "path": str(stage), "error": str(exc)}
    try:
        # quarantine was resolved and proven to be a strict descendant of the
        # caller-owned restore base before this recursive delete.
        owned_temp.remove(quarantine, base)
        return {"status": "deleted-redundant-stage", "path": str(stage)}
    except OSError as exc:
        return {"status": "retained-cleanup-failed", "path": str(quarantine), "error": str(exc)}


def install(stage, source_tree_sha256, caller_home, project_key, base, *, source_name=None,
            source_device=None, excluded_relative_paths=None, fault=None):
    """Stage is already restored using the bound runtime. Verify it again here.

    source_tree_sha256 may exclude protected directories; the receiving baseline
    always includes ALL restored paths so local additions prevent replacement.
    fault is an offline test hook, never a CLI argument.
    """
    stage, base, caller_home = Path(stage).resolve(), Path(base).resolve(), Path(caller_home).resolve()
    owned(stage, base)
    if not stage.is_dir():
        raise ValueError("verified staging directory is missing")
    actual = inventory.project_inventory(stage, excluded_relative_paths or [])["tree_sha256"]
    if actual != source_tree_sha256:
        raise ValueError("staged project does not match the selected source tree")
    candidate = fingerprint(stage)
    key = io.digest(project_key)
    journal_path = caller_home / "cross-device-project-transactions" / (key + ".json")
    with io.locked(ledger_path(caller_home)):
        if journal_path.exists() and io.read(journal_path).get("phase") not in {"complete", "rolled-back"}:
            raise ValueError("unfinished project transaction must be recovered before another install")
        ledger = load_ledger(caller_home, base)
        old = ledger["projects"].get(project_key)
        previous = Path(old["restored_project_root"]) if old else None
        current = None
        if previous and previous.is_dir():
            owned(previous, base)
            try:
                current = fingerprint(previous)
            except (OSError, ValueError):
                current = None  # uncertain/unsupported local tree: retain it untouched
        if previous and current == candidate:
            cleanup = cleanup_redundant_stage(stage, base)
            return {
                "action": "reuse-unchanged", "destination": str(previous), "staging_cleanup": cleanup,
                "installed_tree_sha256": candidate, "project_key": project_key, "verified": True,
            }
        replace = bool(old and current and current == old["last_applied_tree_sha256"])
        token = secrets.token_hex(12)
        destination = previous if replace else choose_new_destination(
            base, source_name, source_device, project_key, candidate
        )
        owned(destination, base)
        destination.parent.mkdir(parents=True, exist_ok=True)
        rollback = destination.with_name(destination.name + ".rollback-" + token) if replace else None
        if not replace and destination.exists():
            raise ValueError("new project destination already exists")
        if rollback and rollback.exists():
            raise ValueError("rollback destination already exists")
        if within(stage, destination) or within(destination, stage) or stage == destination:
            raise ValueError("staging and destination paths overlap")
        journal = {"format": "cross-device-project-transaction-v1", "transaction_id": token,
                   "destination_codex_home": str(caller_home), "restore_base": str(base), "project_key": project_key,
                   "stage": str(stage), "destination": str(destination), "rollback": str(rollback) if rollback else None,
                   "candidate_sha256": candidate, "source_tree_sha256": source_tree_sha256,
                   "source_name": safe_project_component(source_name, f"project-{key[:8]}"),
                   "source_device": str(source_device or ""),
                   "previous_sha256": current if replace else None,
                   "action": "transactional-safe-replace" if replace else "restore-side-by-side"}
        save_journal(journal_path, journal, "prepared")
        if fault:
            fault("prepared", journal)
        if replace:
            # Check immediately before rename AND on the preserved rollback tree.
            if fingerprint(destination) != current:
                save_journal(journal_path, journal, "rolled-back")
                raise ValueError("local project changed before replacement; kept original and stage")
            os.rename(destination, rollback)
            save_journal(journal_path, journal, "old-renamed")
            if fault:
                fault("old-renamed", journal)
            if fingerprint(rollback) != current:
                os.rename(rollback, destination)
                save_journal(journal_path, journal, "rolled-back")
                raise ValueError("local project changed during replacement; original restored")
        # os.rename refuses an existing directory on Windows; no os.replace/overlay.
        os.rename(stage, destination)
        save_journal(journal_path, journal, "new-installed")
        if fault:
            fault("new-installed", journal)
        if fingerprint(destination) != candidate:
            raise ValueError("installed tree changed; journal requires recovery, no ledger advancement")
        _commit(journal, caller_home, base)
        save_journal(journal_path, journal, "complete")
        return {
            "action": journal["action"], "destination": str(destination), "rollback_path": journal["rollback"],
            "transaction_journal": str(journal_path), "installed_tree_sha256": candidate,
            "project_key": project_key, "source_name": journal["source_name"],
            "source_device": journal["source_device"], "verified": True,
        }


def recover(journal_path, caller_home, base):
    """Recover interrupted renames. Ambiguous or locally edited trees stop safely."""
    io.require_account(journal_path, caller_home)
    journal = io.read(journal_path)
    if journal.get("journal_sha256") != io.digest(io.unsigned(journal, "journal_sha256")):
        raise ValueError("project transaction journal hash mismatch")
    if not io.same_path(journal["destination_codex_home"], caller_home) or not io.same_path(journal["restore_base"], base):
        raise ValueError("transaction belongs to another account")
    stage, destination = Path(journal["stage"]), Path(journal["destination"])
    rollback = Path(journal["rollback"]) if journal.get("rollback") else None
    for path in (stage, destination, rollback):
        if path:
            owned(path, base)
    with io.locked(ledger_path(caller_home)):
        if journal["phase"] in {"complete", "rolled-back"}:
            return {"phase": journal["phase"], "unchanged": True}
        if destination.is_dir() and not stage.exists() and fingerprint(destination) == journal["candidate_sha256"]:
            _commit(journal, caller_home, base)
            save_journal(journal_path, journal, "complete")
        elif rollback and rollback.is_dir() and not destination.exists() and stage.is_dir():
            # Preserve even edits to the rollback: no bytes are discarded.
            os.rename(rollback, destination)
            save_journal(journal_path, journal, "rolled-back")
        elif stage.is_dir() and ((not rollback and not destination.exists()) or
                                 (rollback and not rollback.exists() and destination.is_dir())):
            save_journal(journal_path, journal, "rolled-back")
        else:
            raise ValueError("ambiguous transaction or edited installed tree; preserve all paths for explicit recovery")
        return {"phase": journal["phase"], "destination": str(destination), "stage": str(stage), "rollback": str(rollback) if rollback else None}
