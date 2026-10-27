#!/usr/bin/env python3
"""Freeze live Codex session logs before a long multi-project sync."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any
import owned_temp


FORMAT = "cross-device-project-sync-conversation-source-snapshots-v1"


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
    parser.add_argument("--snapshot-root", required=True)
    parser.add_argument("--output-discovery", required=True)
    args = parser.parse_args()

    discovery_path = Path(args.discovery).resolve(strict=True)
    snapshot_root = Path(args.snapshot_root).absolute()
    output_discovery = Path(args.output_discovery).absolute()
    if snapshot_root.exists() or output_discovery.exists():
        raise ValueError("snapshot root and output discovery must not already exist")
    discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
    projects = discovery.get("projects")
    if not isinstance(projects, list) or not projects:
        raise ValueError("discovery projects are missing")

    exporter = load_exporter(Path(__file__).resolve().parent)
    snapshot_root.parent.mkdir(parents=True, exist_ok=True)
    output_discovery.parent.mkdir(parents=True, exist_ok=True)
    staging_root = owned_temp.make_temp(snapshot_root.parent, f".{snapshot_root.name}.staging-")
    output_fd, output_name = tempfile.mkstemp(prefix=f".{output_discovery.name}.staging-", dir=output_discovery.parent)
    os.close(output_fd)
    output_staging = Path(output_name)
    committed_root = False
    try:
        records: list[dict[str, Any]] = []
        seen_threads: set[str] = set()
        for project in projects:
            project_id = str(project.get("project_id") or "")
            for conversation in project.get("conversations") or []:
                thread_id = str(conversation.get("thread_id") or "").strip()
                if not thread_id or thread_id.casefold() in seen_threads:
                    raise ValueError(f"missing or duplicate conversation thread id: {thread_id}")
                seen_threads.add(thread_id.casefold())
                source = Path(str(conversation["session_log"])).resolve(strict=True)
                data = exporter.read_stable(source)
                for line_number, raw_line in enumerate(data.split(b"\n"), 1):
                    if raw_line.strip():
                        try:
                            json.loads(raw_line)
                        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                            raise ValueError(f"session JSONL is invalid at line {line_number}: {source}") from exc
                name = hashlib.sha256(thread_id.encode("utf-8")).hexdigest() + ".jsonl"
                staged_destination = staging_root / name
                final_destination = snapshot_root / name
                staged_destination.write_bytes(data)
                conversation["original_session_log"] = str(source)
                conversation["snapshot_session_log"] = str(final_destination)
                records.append({
                    "project_id": project_id,
                    "thread_id": thread_id,
                    "original_session_log": str(source),
                    "snapshot_session_log": str(final_destination),
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                })

        manifest = {"format": FORMAT, "conversation_count": len(records), "records": records}
        (staging_root / "SNAPSHOT_MANIFEST.json").write_bytes(canonical_bytes(manifest))
        output_staging.write_bytes(canonical_bytes(discovery))
        os.replace(staging_root, snapshot_root)
        owned_temp.release(staging_root)
        owned_temp.claim_existing(snapshot_root, snapshot_root.parent)
        committed_root = True
        os.replace(output_staging, output_discovery)
        owned_temp.release(snapshot_root)
        committed_root = False
    except Exception:
        if committed_root:
            owned_temp.remove(snapshot_root, snapshot_root.parent)
        raise
    finally:
        if staging_root.exists():
            owned_temp.remove(staging_root, snapshot_root.parent)
        else:
            owned_temp.release(staging_root)
        output_staging.unlink(missing_ok=True)
    print(json.dumps({
        "snapshot_root": str(snapshot_root),
        "output_discovery": str(output_discovery),
        "conversation_count": len(records),
        "manifest_sha256": hashlib.sha256(canonical_bytes(manifest)).hexdigest(),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
