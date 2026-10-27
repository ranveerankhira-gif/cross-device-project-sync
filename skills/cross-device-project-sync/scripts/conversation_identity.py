"""Portable v42 conversation identity, independent of receiving wrapper tasks."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def message_fingerprint(message):
    content = []
    for part in message.get("content") or []:
        if part.get("type") == "text":
            content.append({"type": "text", "text": str(part.get("text") or "")})
        elif part.get("type") == "image":
            content.append({"type": "image", "mime_type": str(part.get("mime_type") or ""),
                            "size": int(part.get("size") or 0), "sha256": str(part.get("sha256") or "").lower()})
        else:
            raise ValueError("unsupported user-visible content type in conversation identity")
    return digest({"role": str(message.get("role") or ""), "phase": message.get("phase"), "content": content})


def visible_fingerprints(messages, received=False):
    result, registration_turn = [], False
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
            raise ValueError("invalid portable transcript message")
        if received and message["role"] == "user":
            text = "\n".join(part.get("text", "") for part in message.get("content", []) if part.get("type") == "text")
            registration_turn = bool(re.match(r"^\[cross-device-receipt:[0-9a-f]{64}\]\n", text))
        if not registration_turn:
            result.append(message_fingerprint(message))
    return result


def descendant(sequences):
    """Retained parents must belong to one history chain. Never concatenate forks."""
    winner = []
    for sequence in sequences:
        if len(sequence) >= len(winner) and sequence[:len(winner)] == winner:
            winner = sequence
        elif winner[:len(sequence)] != sequence:
            raise ValueError("inherited histories diverge; cannot construct a single conversation")
    return winner


def flatten(root, depth=0):
    if depth > 64:
        raise ValueError("conversation history exceeds 64 generations")
    root = Path(root)
    history = root / "history"
    parents = sorted((p for p in history.iterdir() if p.is_dir()), key=lambda p: p.name) if history.is_dir() else []
    before = descendant(flatten(parent, depth + 1) for parent in parents)
    messages = [json.loads(line) for line in (root / "FULL_TRANSCRIPT.jsonl").read_bytes().splitlines() if line.strip()]
    current = visible_fingerprints(messages, received=bool(parents))
    # A flattened export can already contain the complete inherited prefix.
    if before and current[:len(before)] == before:
        return current
    return before + current


def combined(paths, messages):
    parents = descendant(flatten(Path(path)) for path in paths)
    current = visible_fingerprints(messages, received=bool(paths))
    if parents and current[:len(parents)] == parents:
        return current
    return parents + current


def content_hash(lineage, branch, fingerprints, generation):
    return digest({"generator": generation, "lineage_id": lineage, "branch_id": branch or "main",
                   "message_fingerprints": fingerprints})


def evidence_hash(messages_sha256, attachments, histories):
    return digest({"messages_sha256": messages_sha256, "attachments": attachments,
                   "inherited_histories": [{"tree_sha256": item["tree_sha256"], "file_count": item["file_count"], "total_bytes": item["total_bytes"]} for item in histories]})


def set_row(item):
    # A new archive state/provenance needs a new pack, not a new conversation.
    return {"lineage_id": item["lineage_id"], "branch_id": item.get("branch_id") or "main",
            "conversation_sha256": item["conversation_sha256"], "source_disposition": item.get("source_disposition") or "active",
            "source_instance_id": item.get("source_instance_id") or "legacy-unknown",
            "evidence_sha256": item.get("evidence_sha256"),
            "projectless_directory_ids": sorted(item.get("projectless_directory_ids") or [])}
