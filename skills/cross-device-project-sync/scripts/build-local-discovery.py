#!/usr/bin/env python3
"""Build complete registered-project or explicitly expanded Codex task discovery."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any


FORMAT = "cross-device-project-sync-expanded-discovery-v1"


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def windows_key(path: Path) -> str:
    return os.path.normpath(str(path.absolute())).casefold()


def broad_root_map() -> dict[str, str]:
    home = Path.home().absolute()
    roots = {
        windows_key(home): "user profile is a broad container",
        windows_key(home / "Desktop"): "Desktop is a broad container",
        windows_key(home / "Documents"): "Documents is a broad container",
        windows_key(home / "Downloads"): "Downloads is a broad container",
    }
    if os.name != "nt":
        return roots
    try:
        import winreg

        registry_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        values = {
            "Desktop": "Desktop is a broad container",
            "Personal": "Documents is a broad container",
            "{374DE290-123F-4565-9164-39C4925E467B}": "Downloads is a broad container",
        }
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry_path) as key:
            for name, reason in values.items():
                try:
                    raw, _ = winreg.QueryValueEx(key, name)
                except OSError:
                    continue
                expanded = os.path.expandvars(str(raw).strip())
                if expanded:
                    roots[windows_key(Path(expanded))] = reason
    except (ImportError, OSError):
        pass
    return roots


def broad_root_reason(path: Path) -> str | None:
    absolute = path.absolute()
    if absolute.parent == absolute:
        return "drive root is a broad container"
    return broad_root_map().get(windows_key(absolute))


def select_task_roots(paths: list[Path]) -> tuple[list[Path], list[dict[str, str]]]:
    unique = sorted({windows_key(path): path.absolute() for path in paths}.values(), key=lambda p: (len(windows_key(p)), windows_key(p)))
    eligible: list[Path] = []
    exclusions: list[dict[str, str]] = []
    for candidate in unique:
        reason = broad_root_reason(candidate)
        if reason:
            exclusions.append({"path": str(candidate), "reason": reason})
        else:
            eligible.append(candidate)
    top_roots: list[Path] = []
    for candidate in eligible:
        key = windows_key(candidate)
        if any(key.startswith(windows_key(parent).rstrip("\\/") + os.sep.casefold()) for parent in top_roots):
            continue
        top_roots.append(candidate)
    return top_roots, sorted(exclusions, key=lambda item: item["path"].casefold())


def is_reparse(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def first_record(path: Path) -> dict[str, Any] | None:
    with path.open("r", encoding="utf-8") as handle:
        line = handle.readline()
    if not line:
        return None
    record = json.loads(line)
    if record.get("type") != "session_meta":
        return None
    return record.get("payload") or {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-home", required=True)
    parser.add_argument("--device-label", required=True)
    parser.add_argument("--source-computer", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--registered-projects")
    parser.add_argument("--cloud-projects")
    parser.add_argument("--mode", choices=("expanded-task-roots", "registered-projects"), default="expanded-task-roots")
    parser.add_argument("--allow-inaccessible-exclusion", action="store_true")
    parser.add_argument("--allow-session-scan-exclusion", action="store_true")
    args = parser.parse_args()

    codex_home = Path(args.codex_home).absolute()
    session_roots = (codex_home / "sessions", codex_home / "archived_sessions")
    rows = []
    session_scan_errors = []
    seen_thread_ids: dict[str, Path] = {}
    session_paths = sorted(
        (session for root in session_roots if root.is_dir() for session in root.rglob("*.jsonl")),
        key=lambda path: str(path).casefold(),
    )
    for session in session_paths:
        try:
            payload = first_record(session)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            session_scan_errors.append({"session_log": str(session.absolute()), "reason": str(exc)})
            continue
        if not isinstance(payload, dict):
            session_scan_errors.append({
                "session_log": str(session.absolute()),
                "reason": "missing first-line session_meta payload",
            })
            continue
        thread_id = str(payload.get("id") or "").strip()
        cwd_text = str(payload.get("cwd") or "").strip()
        if not thread_id or not cwd_text:
            session_scan_errors.append({
                "session_log": str(session.absolute()),
                "reason": "session_meta is missing id or cwd",
            })
            continue
        cwd = Path(cwd_text).absolute()
        thread_key = thread_id.casefold()
        if thread_key in seen_thread_ids:
            raise ValueError(f"duplicate thread ID in session metadata: {thread_id}")
        seen_thread_ids[thread_key] = session.absolute()
        rows.append({"thread_id": thread_id, "cwd": cwd, "session_log": session.absolute()})
    if session_scan_errors and not args.allow_session_scan_exclusion:
        raise ValueError(f"session metadata scan was incomplete: {len(session_scan_errors)} unreadable or malformed file(s)")

    registered = {}
    if args.registered_projects:
        for item in json.loads(Path(args.registered_projects).read_text(encoding="utf-8")):
            key = windows_key(Path(item["source_path"]))
            if key in registered:
                raise ValueError(f"duplicate registered project path: {item['source_path']}")
            registered[key] = item
    cloud = json.loads(Path(args.cloud_projects).read_text(encoding="utf-8")) if args.cloud_projects else []
    ledger_path = codex_home / "cross-device-conversation-sync-ledger.json"
    inherited_by_receiving_thread: dict[str, list[str]] = {}
    if ledger_path.is_file():
        ledger = json.loads(ledger_path.read_text(encoding="utf-8-sig"))
        if ledger.get("format") != "cross-device-conversation-sync-ledger-v1" or not isinstance(ledger.get("entries"), dict):
            raise ValueError(f"unsupported local conversation ledger: {ledger_path}")
        for entry in ledger["entries"].values():
            receiving_id = str(entry.get("receiving_thread_id") or "").strip().casefold()
            retained = str(entry.get("retained_handoff_path") or entry.get("conversation_path") or "").strip()
            if receiving_id and retained:
                inherited_by_receiving_thread.setdefault(receiving_id, []).append(retained)

    assigned_rows: dict[str, list[dict[str, Any]]] = {}
    conversation_exclusions = []
    if args.mode == "registered-projects":
        if not registered:
            raise ValueError("registered-projects mode requires --registered-projects")
        top_roots = []
        seen_project_ids = set()
        for key, item in registered.items():
            project_id = str(item.get("project_id") or "").strip()
            if not project_id or project_id.casefold() in seen_project_ids:
                raise ValueError(f"missing or duplicate registered project ID: {project_id!r}")
            seen_project_ids.add(project_id.casefold())
            root = Path(item["source_path"]).absolute()
            if not root.is_dir():
                raise ValueError(f"registered project root is missing or inaccessible: {root}")
            if is_reparse(root):
                raise ValueError(f"registered project root is a reparse point and cannot be archived: {root}")
            reason = broad_root_reason(root)
            if reason:
                raise ValueError(f"registered project root must be excluded before discovery: {root}: {reason}")
            top_roots.append(root)
            assigned_rows[key] = []
        top_roots.sort(key=lambda item: windows_key(item))
        roots_by_depth = sorted(top_roots, key=lambda item: (len(windows_key(item)), windows_key(item)))
        for index, parent in enumerate(roots_by_depth):
            parent_prefix = windows_key(parent).rstrip("\\/") + os.sep.casefold()
            for child in roots_by_depth[index + 1:]:
                if windows_key(child).startswith(parent_prefix):
                    raise ValueError(
                        f"overlapping registered project roots would duplicate backup content: {parent} contains {child}"
                    )
        for row in rows:
            cwd_key = windows_key(row["cwd"])
            matches = [
                root for root in top_roots
                if cwd_key == windows_key(root) or cwd_key.startswith(windows_key(root).rstrip("\\/") + os.sep.casefold())
            ]
            if not matches:
                conversation_exclusions.append({
                    "thread_id": row["thread_id"],
                    "session_log": str(row["session_log"]),
                    "original_session_log": str(row["session_log"]),
                    "reason": "session working directory is not inside an eligible registered project",
                })
                continue
            selected = max(matches, key=lambda item: len(windows_key(item)))
            assigned_rows[windows_key(selected)].append(row)
        broad_exclusions = []
    else:
        top_roots, broad_exclusions = select_task_roots([item["cwd"] for item in rows])
        for root in top_roots:
            root_key = windows_key(root)
            assigned_rows[root_key] = [
                item for item in rows
                if windows_key(item["cwd"]) == root_key
                or windows_key(item["cwd"]).startswith(root_key.rstrip("\\/") + os.sep.casefold())
            ]

    projects = []
    for root in top_roots:
        root_key = windows_key(root)
        reg = registered.get(root_key)
        project_id = str(reg["project_id"]) if reg else "taskroot-" + hashlib.sha256(root_key.encode("utf-8")).hexdigest()[:24]
        label = str(reg.get("label") or root.name) if reg else root.name
        conversation_rows = assigned_rows.get(root_key, [])
        conversations = []
        for item in conversation_rows:
            conversation = {"thread_id": item["thread_id"], "thread_title": None, "session_log": str(item["session_log"])}
            inherited = sorted(set(inherited_by_receiving_thread.get(item["thread_id"].casefold(), [])), key=str.casefold)
            if inherited:
                conversation["inherited_handoff_paths"] = inherited
            conversations.append(conversation)
        conversations.sort(key=lambda item: item["thread_id"].casefold())
        exclusions = []
        exclusion_records = []
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                children = list(current.iterdir())
            except PermissionError:
                if not args.allow_inaccessible_exclusion or current == root:
                    raise PermissionError(f"inaccessible during discovery: {current}")
                relative = current.relative_to(root).as_posix()
                exclusions.append(relative)
                exclusion_records.append({"path": relative, "reason": "inaccessible directory; not archived"})
                continue
            for child in children:
                relative = child.relative_to(root).as_posix()
                if is_reparse(child):
                    exclusions.append(relative)
                    exclusion_records.append({"path": relative, "reason": "reparse point; external target not followed"})
                elif child.is_dir():
                    stack.append(child)
        projects.append({
            "project_id": project_id,
            "label": label,
            "source_path": str(root),
            "exclude_relative_paths": sorted(exclusions, key=str.casefold),
            "exclusion_records": sorted(exclusion_records, key=lambda item: item["path"].casefold()),
            "conversations": conversations,
            "conversation_inventory": {
                "complete": not session_scan_errors,
                "source": "complete-local-session-metadata-scan",
                "expected_thread_ids": [item["thread_id"] for item in conversations],
            },
        })
    projects.sort(key=lambda item: item["source_path"].casefold())
    discovery = {
        "format": "cross-device-project-sync-registered-project-discovery-v1" if args.mode == "registered-projects" else FORMAT,
        "source_device": args.device_label,
        "source_computer": args.source_computer,
        "projects": projects,
        "cloud_managed_projects": cloud,
        "task_root_exclusions": broad_exclusions,
        "conversation_exclusions": conversation_exclusions,
        "session_scan_errors": session_scan_errors,
        "conversation_inventory_complete": not session_scan_errors,
    }
    output = Path(args.output).absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(canonical_bytes(discovery))
    print(json.dumps({
        "output": str(output),
        "project_count": len(projects),
        "conversation_count": sum(len(item["conversations"]) for item in projects),
        "excluded_reparse_count": sum(len(item["exclude_relative_paths"]) for item in projects),
        "excluded_broad_root_count": len(broad_exclusions),
        "discovery_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
