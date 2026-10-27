#!/usr/bin/env python3
"""Remove only zero-visible-message shell logs from snapshotted discovery.

Every removed shell is recorded with its thread id and original path. Any
other validation failure, including suspected credentials, remains fatal.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from typing import Any


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def load_exporter(script_dir: Path):
    path = script_dir / "export-conversation-handoff.py"
    spec = importlib.util.spec_from_file_location("cross_device_conversation_exporter", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load conversation exporter")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(script_dir))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--discovery", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    source = Path(args.discovery).resolve(strict=True)
    output = Path(args.output).absolute()
    if output.exists():
        raise ValueError("output discovery already exists")
    discovery = json.loads(source.read_text(encoding="utf-8"))
    exporter = load_exporter(Path(__file__).resolve().parent)
    exclusions = list(discovery.get("conversation_exclusions") or [])
    for exclusion in exclusions:
        if "original_session_log" not in exclusion and exclusion.get("session_log"):
            exclusion["original_session_log"] = exclusion["session_log"]
        if "session_log" not in exclusion and exclusion.get("original_session_log"):
            exclusion["session_log"] = exclusion["original_session_log"]
    retained = 0
    newly_excluded = 0
    for project in discovery.get("projects") or []:
        kept = []
        for conversation in project.get("conversations") or []:
            thread_id = str(conversation["thread_id"])
            snapshot = Path(str(conversation.get("snapshot_session_log") or conversation["session_log"])).resolve(strict=True)
            data = exporter.read_stable(snapshot)
            try:
                with tempfile.TemporaryDirectory(prefix="codex-conversation-preflight-") as raw:
                    exporter.export_messages(data, Path(raw), thread_id)
            except ValueError as exc:
                if str(exc) != "No user-visible conversation messages were found.":
                    raise ValueError(f"conversation preflight failed for {thread_id} ({snapshot}): {exc}") from exc
                exclusions.append({
                    "project_id": str(project.get("project_id") or ""),
                    "thread_id": thread_id,
                    "original_session_log": str(conversation.get("original_session_log") or conversation.get("session_log") or ""),
                    "session_log": str(conversation.get("original_session_log") or conversation.get("session_log") or ""),
                    "reason": "zero user-visible messages after required system, reasoning, tool, and injected-context filtering",
                })
                newly_excluded += 1
                continue
            kept.append(conversation)
            retained += 1
        project["conversations"] = kept
    discovery["conversation_exclusions"] = sorted(
        exclusions,
        key=lambda item: (str(item.get("project_id") or "").casefold(), str(item.get("thread_id") or "").casefold(), str(item.get("original_session_log") or item.get("session_log") or "").casefold()),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(canonical_bytes(discovery))
    print(json.dumps({"output": str(output), "retained_conversation_count": retained, "newly_excluded_empty_shell_count": newly_excluded, "total_conversation_exclusion_count": len(exclusions)}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
