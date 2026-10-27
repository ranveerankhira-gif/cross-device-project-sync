#!/usr/bin/env python3
"""Export one Codex task into a portable, verified conversation handoff bundle."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile
import time
from typing import Any
import zipfile

from conversation_history import inherit_histories
import owned_temp


BUNDLE_FORMAT = "cross-device-conversation-handoff-v3"
MANIFEST_NAME = "CONVERSATION_HANDOFF_MANIFEST.json"
ZIP_NAME = "CONVERSATION_HANDOFF.zip"
REQUIRED_HANDOFF_HEADINGS = (
    "Purpose And Requested Outcome",
    "Chronological Narrative",
    "Decisions And Rationale",
    "Work Performed",
    "Files And Artifacts",
    "Failures, Corrections, And Rejected Approaches",
    "Current State",
    "Unresolved Questions And Next Actions",
    "User Preferences And Constraints",
    "Verification And Evidence",
)
ALWAYS_INJECTED_PREFIXES = (
    "The following is the Codex agent history whose request action you are assessing.",
    "The following is the Codex agent history added since your last approval assessment.",
)
INJECTED_PREFIXES = (
    "<environment_context>",
    "<recommended_plugins>",
    "<app-context>",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<apps_instructions>",
    "<plugins_instructions>",
    "<turn_aborted>",
    "# AGENTS.md instructions",
)
SECRET_PATTERNS = (
    ("OpenAI-style API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("private key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("bearer credential", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE)),
)
ALLOWED_IMAGE_MIME = {"image/gif", "image/jpeg", "image/png", "image/webp"}
MAX_EMBEDDED_IMAGE_BYTES = 20 * 1024 * 1024


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def write_json(path: Path, value: Any) -> None:
    path.write_bytes(canonical_json(value))


def resolve_codex_home(requested: str | None) -> Path:
    raw = requested or os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    return Path(raw).expanduser().resolve()


def resolve_thread_id(requested: str | None) -> str:
    thread_id = requested or os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
    if not thread_id or not re.fullmatch(r"[0-9a-fA-F-]{16,80}", thread_id):
        raise ValueError("A valid --thread-id or CODEX_THREAD_ID/CODEX_SESSION_ID is required.")
    return thread_id


def resolve_device_label(codex_home: Path, requested: str | None) -> str:
    label = requested
    if not label:
        config = codex_home / "cross-device-project-sync-device.json"
        if not config.is_file():
            raise ValueError("Conversation export requires --device-label or the local cross-device-project-sync-device.json configuration.")
        try:
            label = str(json.loads(config.read_text(encoding="utf-8")).get("device_label") or "")
        except json.JSONDecodeError as exc:
            raise ValueError("The local device-label configuration is invalid JSON.") from exc
    label = label.strip()
    if not label or len(label) > 80 or any(ord(char) < 32 or ord(char) == 127 for char in label):
        raise ValueError("Conversation source device label is invalid.")
    return label


def resolve_session(codex_home: Path, thread_id: str, requested: str | None) -> Path:
    session_roots = tuple((codex_home / name).resolve() for name in ("sessions", "archived_sessions"))
    if requested:
        candidate = Path(requested).expanduser().resolve()
        if not any(candidate == root or candidate.is_relative_to(root) for root in session_roots):
            raise ValueError("The session log must be inside <CODEX_HOME>/sessions or <CODEX_HOME>/archived_sessions.")
        matches = [candidate]
    else:
        matches = sorted(
            (path for root in session_roots if root.is_dir() for path in root.rglob(f"rollout-*-{thread_id}.jsonl")),
            key=lambda path: str(path).casefold(),
        )
    matches = [path for path in matches if path.is_file() and path.name.endswith(f"-{thread_id}.jsonl")]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one session log for thread {thread_id}; found {len(matches)}.")
    return matches[0].resolve()


def read_stable(path: Path, attempts: int = 4) -> bytes:
    for attempt in range(attempts):
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
        if before.st_size == after.st_size == len(data) and before.st_mtime_ns == after.st_mtime_ns:
            return data
        if attempt + 1 < attempts:
            time.sleep(0.25)
    raise RuntimeError("The Codex session log kept changing during export; retry after the current turn is idle.")


def secret_findings(texts: list[str]) -> list[str]:
    findings: set[str] = set()
    for text in texts:
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                findings.add(name)
    return sorted(findings)


def is_injected_user_record(texts: list[str]) -> bool:
    joined = "\n".join(texts).lstrip()
    if joined.startswith(ALWAYS_INJECTED_PREFIXES):
        return True
    if "## My request:" in joined:
        return False
    return joined.startswith(INJECTED_PREFIXES)


def extension_for_mime(mime: str) -> str:
    known = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
    return known.get(mime.lower()) or mimetypes.guess_extension(mime.lower()) or ".bin"


def image_signature_matches(mime: str, payload: bytes) -> bool:
    if mime == "image/png":
        return payload.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/jpeg":
        return payload.startswith(b"\xff\xd8\xff")
    if mime == "image/gif":
        return payload.startswith((b"GIF87a", b"GIF89a"))
    if mime == "image/webp":
        return len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP"
    return False


def decode_data_url(url: str) -> tuple[str, bytes]:
    match = re.fullmatch(r"data:([^;,]+);base64,([A-Za-z0-9+/=\r\n]+)", url)
    if not match:
        raise ValueError("A conversation image is not embedded as a base64 data URL; exact export is unavailable.")
    mime = match.group(1).lower()
    if mime not in ALLOWED_IMAGE_MIME:
        raise ValueError(f"Unsupported conversation-image MIME type: {mime}")
    encoded = re.sub(r"\s+", "", match.group(2))
    if len(encoded) > ((MAX_EMBEDDED_IMAGE_BYTES + 2) // 3) * 4:
        raise ValueError("A conversation image exceeds the 20 MiB decoded-size limit.")
    try:
        payload = base64.b64decode(encoded, validate=True)  # noqa: SEC-AUDITOR -- approved image MIME and 20 MiB bound
    except ValueError as exc:
        raise ValueError("A conversation image contains invalid base64 data.") from exc
    if len(payload) > MAX_EMBEDDED_IMAGE_BYTES:
        raise ValueError("A conversation image exceeds the 20 MiB decoded-size limit.")
    if not image_signature_matches(mime, payload):
        raise ValueError("A conversation image MIME type does not match its file signature.")
    return mime, payload


def handoff_template() -> str:
    sections = ["# Detailed Conversation Handoff", "", "<!-- Replace every guidance comment before finalizing. -->", ""]
    guidance = {
        "Purpose And Requested Outcome": "Describe the user's actual outcome and scope in detail.",
        "Chronological Narrative": "Reconstruct important events in order, including requirement changes.",
        "Decisions And Rationale": "Record decisions, alternatives, and why each choice was made.",
        "Work Performed": "List concrete operations and their results.",
        "Files And Artifacts": "Name important files, paths, versions, hashes, and generated artifacts.",
        "Failures, Corrections, And Rejected Approaches": "Preserve mistakes, failed attempts, corrections, and user rejections.",
        "Current State": "State exactly what exists now and what has been verified.",
        "Unresolved Questions And Next Actions": "List remaining work and any decisions still needed.",
        "User Preferences And Constraints": "Preserve relevant preferences and operational boundaries.",
        "Verification And Evidence": "Record tests, checks, dates, device identity, and evidence.",
    }
    for heading in REQUIRED_HANDOFF_HEADINGS:
        sections.extend((f"## {heading}", "", f"<!-- {guidance[heading]} -->", ""))
    return "\n".join(sections)


def receiver_start(metadata: dict[str, Any]) -> str:
    return "\n".join((
        "# Receiving Codex Task Startup",
        "",
        "This verified handoff belongs to a newly restored project. It does not recreate the original task ID or old chat bubbles.",
        "",
        "Before changing any project file:",
        "",
        "1. Read `DETAILED_HANDOFF.md` completely.",
        "2. Read `BUNDLE_METADATA.json` and `CONVERSATION_INDEX.json`.",
        "3. Inspect the restored project and its Git state read-only.",
        "4. Use `FULL_TRANSCRIPT.jsonl` to confirm exact prior wording, corrections, rejected approaches, and details whenever needed.",
        "5. Inspect every retained attachment, including attachments in inherited history, and report what each one materially shows. Do not infer image or media content from its filename.",
        "6. If this directory is outside your allowed filesystem roots, request read-only access to this exact directory. Stop and report the access failure if permission is unavailable or denied.",
        "7. Produce the Inherited Context Report below before starting new work.",
        "8. Recursively read every inherited_handoffs entry in BUNDLE_METADATA.json. Each path is relative to its own metadata directory. Preserve these directories on the next export; history is not contained in this task's new messages alone.",
        "",
        "Do not treat a short summary as a substitute for the transcript. This is an immutable snapshot through the recorded cutoff: no active task can archive messages that have not been written yet. Establish transfer completion after that cutoff from the verified Release, restore result, and receiving-task receipt; never invent future dialogue.",
        "",
        "## Required Inherited Context Report",
        "",
        "- Identity: original task ID/title, both source devices, inherited snapshot version supplied in your prompt, and conversation cutoff.",
        "- Lineage: report the verified total retained task generations, messages, and attachments, not only this newest task.",
        "- File access evidence: independently calculate and report the byte size and SHA-256 of every `DETAILED_HANDOFF.md`, `FULL_TRANSCRIPT.jsonl`, and retained attachment.",
        "- Attachment evidence: for every retained attachment, report its relative path, source transcript and sequence, `inspection_status: inspected`, and a substantive observation of what it shows.",
        "- Detailed understanding: substantive chronology, decisions and rationale, corrections or rejected approaches, completed work, current state, unresolved actions, and relevant user constraints.",
        "- Transcript evidence: cite the exact `sequence` value for each material correction, rejection, or decision. If at least three messages exist, cite at least three distinct sequences and include both user and assistant records when both roles are present.",
        "- Read-only confirmation: state that no project files were changed during this context-loading turn.",
        "- Citation evidence: identify each source by its relative transcript path, sequence, a meaningful exact quote, and the claim it supports. Include inherited history as well as current messages. Report later corrections that supersede earlier decisions. Hashes prove bytes, not understanding.",
        "",
        f"- Original task ID: `{metadata['thread_id']}`",
        f"- Original task title: {metadata.get('thread_title') or '(not recorded)'}",
        f"- Project source device: {metadata.get('project_source_device') or '(not recorded)'}",
        f"- Conversation source device: {metadata.get('conversation_source_device') or '(not recorded)'}",
        f"- Conversation cutoff: {metadata.get('cutoff_timestamp') or '(not recorded)'}",
        "",
    ))


def is_link_like(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def inventory_files(root: Path, excluded: set[str] | None = None) -> list[dict[str, Any]]:
    excluded = excluded or set()
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if is_link_like(path):
            raise ValueError(f"Conversation work directory may not contain links: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        files.append({"path": relative, "size": path.stat().st_size, "sha256": sha256_file(path)})
    return files


def export_messages(session_data: bytes, work_dir: Path, thread_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    index: list[dict[str, Any]] = []
    texts_for_scan: list[str] = []
    excluded = {"developer_or_system_messages": 0, "reasoning_and_tool_records": 0, "injected_runtime_context": 0}
    session_ids: set[str] = set()
    attachment_count = 0

    for line_number, raw_line in enumerate(session_data.splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Session JSONL is invalid at line {line_number}.") from exc
        if record.get("type") == "session_meta":
            payload = record.get("payload") or {}
            for key in ("id", "session_id"):
                if payload.get(key):
                    session_ids.add(str(payload[key]))
            continue
        if record.get("type") != "response_item" or (record.get("payload") or {}).get("type") != "message":
            excluded["reasoning_and_tool_records"] += 1
            continue

        payload = record["payload"]
        role = str(payload.get("role") or "")
        if role not in {"user", "assistant"}:
            excluded["developer_or_system_messages"] += 1
            continue
        phase = str(payload.get("phase") or "") if role == "assistant" else ""
        # Older and some imported Codex logs omit ``phase`` on user-visible
        # assistant messages. Only reject an explicitly non-visible phase.
        if role == "assistant" and phase and phase not in {"commentary", "final_answer"}:
            excluded["reasoning_and_tool_records"] += 1
            continue

        text_parts = [str(item.get("text") or "") for item in payload.get("content") or [] if item.get("type") in {"input_text", "output_text"}]
        if role == "user" and is_injected_user_record(text_parts):
            excluded["injected_runtime_context"] += 1
            continue

        sequence = len(messages) + 1
        output_content: list[dict[str, Any]] = []
        attachments: list[str] = []
        for part_index, item in enumerate(payload.get("content") or [], 1):
            item_type = item.get("type")
            if item_type in {"input_text", "output_text"}:
                text = str(item.get("text") or "")
                output_content.append({"type": "text", "text": text})
                texts_for_scan.append(text)
            elif item_type == "input_image":
                mime, data = decode_data_url(str(item.get("image_url") or ""))
                relative = PurePosixPath("attachments") / f"message-{sequence:06d}-part-{part_index:02d}{extension_for_mime(mime)}"
                target = work_dir / Path(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                output_content.append({
                    "type": "image",
                    "path": relative.as_posix(),
                    "mime_type": mime,
                    "size": len(data),
                    "sha256": sha256_bytes(data),
                })
                attachments.append(relative.as_posix())
                attachment_count += 1

        if not output_content:
            continue
        metadata = payload.get("internal_chat_message_metadata_passthrough") or {}
        message = {
            "sequence": sequence,
            "timestamp": record.get("timestamp"),
            "ordinal": record.get("ordinal"),
            "role": role,
            "phase": phase or None,
            "turn_id": metadata.get("turn_id"),
            "source_record_id": payload.get("id"),
            "content": output_content,
        }
        messages.append(message)
        joined_text = "\n".join(part["text"] for part in output_content if part["type"] == "text")
        preview = re.sub(r"\s+", " ", joined_text).strip()[:240]
        index.append({
            "sequence": sequence,
            "timestamp": message["timestamp"],
            "role": role,
            "phase": message["phase"],
            "turn_id": message["turn_id"],
            "text_sha256": sha256_bytes(joined_text.encode("utf-8")),
            "preview": preview,
            "attachments": attachments,
        })

    if session_ids and thread_id not in session_ids:
        raise ValueError("The selected session metadata does not match the requested thread ID.")
    findings = secret_findings(texts_for_scan)
    if findings:
        raise ValueError("Potential credentials were detected in the conversation: " + ", ".join(findings) + ". Remove or rotate them before export; no bundle was finalized.")
    if not messages or not any(message["role"] == "user" for message in messages):
        raise ValueError("No user-visible conversation messages were found.")

    transcript_path = work_dir / "FULL_TRANSCRIPT.jsonl"
    with transcript_path.open("wb") as handle:
        for message in messages:
            handle.write(canonical_json(message))
    write_json(work_dir / "CONVERSATION_INDEX.json", {
        "format": BUNDLE_FORMAT,
        "thread_id": thread_id,
        "message_count": len(messages),
        "messages": index,
    })
    return messages, {"excluded": excluded, "attachment_count": attachment_count}


def load_project_manifest(prepared: Path) -> tuple[dict[str, Any], str]:
    path = prepared / "BACKUP_MANIFEST.json"
    if not path.is_file():
        raise ValueError("Prepared backup is missing BACKUP_MANIFEST.json.")
    data = path.read_bytes()
    try:
        manifest = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ValueError("BACKUP_MANIFEST.json is invalid JSON.") from exc
    return manifest, sha256_bytes(data)


def prepare(args: argparse.Namespace) -> None:
    prepared = Path(args.prepared_backup).resolve()
    work_dir = Path(args.work_dir).resolve()
    if work_dir.exists():
        raise ValueError("Conversation work directory must not already exist.")
    if prepared == work_dir or prepared in work_dir.parents or work_dir in prepared.parents:
        raise ValueError("Conversation work directory must be separate from prepared release assets.")
    project_manifest, project_manifest_sha = load_project_manifest(prepared)
    project_root = Path(args.project_root).absolute()
    if not project_root.is_dir() or project_root.name != project_manifest.get("source_name"):
        raise ValueError("--project-root must identify the actual project named in the backup.")
    sources = list(args.inherit_handoff or [])
    automatic_history = project_root.parent / "_codex-conversation-handoff"
    if automatic_history.exists():
        sources.append(str(automatic_history))
    thread_id = resolve_thread_id(args.thread_id)
    codex_home = resolve_codex_home(args.codex_home)
    conversation_device = resolve_device_label(codex_home, args.device_label)
    session = resolve_session(codex_home, thread_id, args.session)
    session_data = read_stable(session)

    temp_dir = owned_temp.make_temp(work_dir.parent, "conversation-handoff-")
    try:
        messages, details = export_messages(session_data, temp_dir, thread_id)
        inherited = inherit_histories(temp_dir, sources, secret_findings)
        inherited_task_count = sum(item["lineage_task_count"] for item in inherited)
        inherited_message_count = sum(item["lineage_message_count"] for item in inherited)
        inherited_attachment_count = sum(item["lineage_attachment_count"] for item in inherited)
        project_source_device = str(project_manifest.get("source_device") or "")
        metadata = {
            "format": BUNDLE_FORMAT,
            "status": "draft-needs-detailed-handoff",
            "thread_id": thread_id,
            "thread_title": args.thread_title or "",
            "project_source_device": project_source_device,
            "conversation_source_device": conversation_device,
            "source_computer": os.environ.get("COMPUTERNAME", ""),
            "project_source_name": str(project_manifest.get("source_name") or ""),
            "project_manifest_sha256": project_manifest_sha,
            "session_log_name": session.name,
            "session_log_sha256": sha256_bytes(session_data),
            "session_log_bytes": len(session_data),
            "message_count": len(messages),
            "user_message_count": sum(message["role"] == "user" for message in messages),
            "assistant_message_count": sum(message["role"] == "assistant" for message in messages),
            "attachment_count": details["attachment_count"],
            "lineage_task_count": 1 + inherited_task_count,
            "lineage_message_count": len(messages) + inherited_message_count,
            "lineage_attachment_count": details["attachment_count"] + inherited_attachment_count,
            "excluded_record_counts": details["excluded"],
            "dialogue_completeness": "all user and assistant message records present in the stable task log through the recorded cutoff; runtime instructions, reasoning, and tool payloads excluded",
            "snapshot_boundary": "immutable through cutoff_timestamp; later transfer completion is established by verified release, restore result, and receiving-task receipt",
            "cutoff_ordinal": max((message.get("ordinal") or 0) for message in messages),
            "cutoff_timestamp": messages[-1].get("timestamp"),
            "prepared_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "inherited_handoffs": inherited,
        }
        (temp_dir / "RECEIVER_START.md").write_text(receiver_start(metadata), encoding="utf-8", newline="\n")
        metadata["prepared_files"] = inventory_files(temp_dir)
        write_json(temp_dir / "BUNDLE_METADATA.json", metadata)
        (temp_dir / "DETAILED_HANDOFF.md").write_text(handoff_template(), encoding="utf-8", newline="\n")
        temp_dir.replace(work_dir)
        owned_temp.release(temp_dir)
    except Exception:
        if temp_dir.exists():
            owned_temp.remove(temp_dir, work_dir.parent)
        else:
            owned_temp.release(temp_dir)
        raise
    print(json.dumps({"status": "draft_ready", "work_dir": str(work_dir), "messages": len(messages), "attachments": details["attachment_count"]}, ensure_ascii=False))


def validate_detailed_handoff(path: Path) -> str:
    if not path.is_file():
        raise ValueError("DETAILED_HANDOFF.md is missing.")
    text = path.read_text(encoding="utf-8")
    if "<!--" in text or "-->" in text:
        raise ValueError("DETAILED_HANDOFF.md still contains guidance comments and is not complete.")
    missing = [heading for heading in REQUIRED_HANDOFF_HEADINGS if not re.search(rf"^##\s+{re.escape(heading)}\s*$", text, re.MULTILINE)]
    if missing:
        raise ValueError("DETAILED_HANDOFF.md is missing required sections: " + ", ".join(missing))
    empty = []
    for heading in REQUIRED_HANDOFF_HEADINGS:
        match = re.search(rf"^##\s+{re.escape(heading)}\s*$\n(.*?)(?=^##\s+|\Z)", text, re.MULTILINE | re.DOTALL)
        if not match or len(match.group(1).strip()) < 20:
            empty.append(heading)
    if empty:
        raise ValueError("DETAILED_HANDOFF.md has empty or superficial sections: " + ", ".join(empty))
    findings = secret_findings([text])
    if findings:
        raise ValueError("Potential credentials were detected in DETAILED_HANDOFF.md: " + ", ".join(findings))
    return text


def finalize(args: argparse.Namespace) -> None:
    prepared = Path(args.prepared_backup).resolve()
    work_dir = Path(args.work_dir).resolve()
    if not work_dir.is_dir():
        raise ValueError("Conversation work directory does not exist.")
    if is_link_like(work_dir):
        raise ValueError("Conversation work directory may not be a link or junction.")
    project_manifest, project_manifest_sha = load_project_manifest(prepared)
    metadata_path = work_dir / "BUNDLE_METADATA.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("format") != BUNDLE_FORMAT or metadata.get("project_manifest_sha256") != project_manifest_sha:
        raise ValueError("Conversation draft is not bound to this prepared project backup.")
    validate_detailed_handoff(work_dir / "DETAILED_HANDOFF.md")
    prepared_files = metadata.get("prepared_files")
    if not isinstance(prepared_files, list) or prepared_files != inventory_files(work_dir, {"BUNDLE_METADATA.json", "DETAILED_HANDOFF.md"}):
        raise ValueError("Verbatim transcript, index, or attachments changed after preparation; export a new draft.")

    transcript_texts: list[str] = []
    transcript_count = 0
    with (work_dir / "FULL_TRANSCRIPT.jsonl").open("r", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            transcript_count += 1
            for item in record.get("content") or []:
                if item.get("type") == "text":
                    transcript_texts.append(str(item.get("text") or ""))
    findings = secret_findings(transcript_texts)
    if findings:
        raise ValueError("Potential credentials were detected before finalization: " + ", ".join(findings))
    if transcript_count != int(metadata.get("message_count") or -1):
        raise ValueError("Transcript message count changed after preparation.")

    files = inventory_files(work_dir, {"BUNDLE_METADATA.json"})
    metadata.update({
        "status": "complete",
        "finalized_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": files,
    })
    write_json(metadata_path, metadata)

    zip_path = prepared / ZIP_NAME
    manifest_path = prepared / MANIFEST_NAME
    if zip_path.exists() or manifest_path.exists():
        raise ValueError("Prepared backup already contains conversation handoff assets; use a new backup run.")
    temp_zip = zip_path.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for path in sorted(work_dir.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(work_dir).as_posix())
        zip_size = temp_zip.stat().st_size
        zip_sha = sha256_file(temp_zip)
        outer_manifest = {
            "format": BUNDLE_FORMAT,
            "project_manifest_sha256": project_manifest_sha,
            "project_source_name": str(project_manifest.get("source_name") or ""),
            "project_source_device": metadata.get("project_source_device"),
            "conversation_source_device": metadata.get("conversation_source_device"),
            "thread_id": metadata.get("thread_id"),
            "thread_title": metadata.get("thread_title"),
            "message_count": metadata.get("message_count"),
            "attachment_count": metadata.get("attachment_count"),
            "lineage_task_count": metadata.get("lineage_task_count"),
            "lineage_message_count": metadata.get("lineage_message_count"),
            "lineage_attachment_count": metadata.get("lineage_attachment_count"),
            "cutoff_ordinal": metadata.get("cutoff_ordinal"),
            "cutoff_timestamp": metadata.get("cutoff_timestamp"),
            "zip_name": ZIP_NAME,
            "zip_size": zip_size,
            "zip_sha256": zip_sha,
            "uncompressed_total_bytes": sum(path.stat().st_size for path in work_dir.rglob("*") if path.is_file()),
            "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        temp_zip.replace(zip_path)
        write_json(manifest_path, outer_manifest)
    finally:
        if temp_zip.exists():
            temp_zip.unlink()
    print(json.dumps({"status": "complete", "zip": str(zip_path), "manifest": str(manifest_path), "messages": transcript_count}, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare", help="Export the exact task dialogue into a draft work directory.")
    prepare_parser.add_argument("--prepared-backup", required=True)
    prepare_parser.add_argument("--project-root", required=True, help="Actual working project; discovers its sibling inherited handoff automatically.")
    prepare_parser.add_argument("--inherit-handoff", action="append", help="Additional explicitly selected finalized history directory, including all its ancestors.")
    prepare_parser.add_argument("--work-dir", required=True)
    prepare_parser.add_argument("--thread-id")
    prepare_parser.add_argument("--thread-title")
    prepare_parser.add_argument("--session")
    prepare_parser.add_argument("--codex-home")
    prepare_parser.add_argument("--device-label")
    prepare_parser.set_defaults(func=prepare)
    finalize_parser = subparsers.add_parser("finalize", help="Validate the detailed handoff and create bound release assets.")
    finalize_parser.add_argument("--prepared-backup", required=True)
    finalize_parser.add_argument("--work-dir", required=True)
    finalize_parser.set_defaults(func=finalize)
    args = parser.parse_args()
    try:
        args.func(args)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
