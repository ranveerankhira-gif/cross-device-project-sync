import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SKILL_ROOT = Path(__file__).resolve().parents[1]
CATALOG_SCRIPT = SKILL_ROOT / "scripts" / "build-receiving-task-catalog.py"
LEDGER_SCRIPT = SKILL_ROOT / "scripts" / "conversation-sync-ledger.py"
CATALOG_GENERATOR = "cross-device-project-sync-conversation-generation-20261013-v38"

spec = importlib.util.spec_from_file_location("conversation_sync_ledger_integration", LEDGER_SCRIPT)
ledger_module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(ledger_module)


def write_transcript(thread_root: Path, thread_id: str, user_text: str) -> None:
    thread_root.mkdir(parents=True)
    records = [
        {"sequence": 1, "timestamp": "2026-10-01T00:00:00Z", "role": "user", "content": [{"type": "text", "text": user_text}]},
        {"sequence": 2, "timestamp": "2026-10-01T00:00:01Z", "role": "assistant", "content": [{"type": "text", "text": "answer"}]},
    ]
    (thread_root / "FULL_TRANSCRIPT.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records), encoding="utf-8"
    )
    (thread_root / "DETAILED_HANDOFF.md").write_text(
        f"# Detailed Conversation Handoff\n\nOriginal task title: Task {thread_id}\n", encoding="utf-8"
    )
    (thread_root / "RECEIVER_START.md").write_text("read first\n", encoding="utf-8")
    (thread_root / "CONVERSATION_INDEX.json").write_text(
        json.dumps({"format": "cross-device-conversation-handoff-v3", "thread_id": thread_id}), encoding="utf-8"
    )


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def write_pack_manifest(pack: Path, thread_ids: list[str]) -> tuple[Path, str]:
    conversations = []
    files = []
    for thread_id in thread_ids:
        root = pack / "threads" / thread_id
        transcript = root / "FULL_TRANSCRIPT.jsonl"
        messages_hash = hashlib.sha256(transcript.read_bytes()).hexdigest()
        identity = {
            "generator": CATALOG_GENERATOR,
            "thread_id": thread_id,
            "messages_sha256": messages_hash,
            "attachments": [],
            "inherited_histories": [],
        }
        records = [json.loads(line) for line in transcript.read_text(encoding="utf-8").split("\n") if line]
        conversations.append({
            "thread_id": thread_id,
            "conversation_sha256": hashlib.sha256(canonical(identity)).hexdigest(),
            "messages_sha256": messages_hash,
            "message_count": len(records),
            "attachment_count": 0,
        })
    for path in sorted((item for item in pack.rglob("*") if item.is_file()), key=lambda item: item.relative_to(pack).as_posix()):
        data = path.read_bytes()
        files.append({"path": path.relative_to(pack).as_posix(), "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    manifest = {
        "format": "cross-device-project-sync-conversation-pack-v1",
        "generator": CATALOG_GENERATOR,
        "conversation_count": len(conversations),
        "conversations": conversations,
        "files": files,
    }
    path = pack.parent / "CONVERSATION_PACK_MANIFEST.json"
    path.write_bytes(canonical(manifest))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


class ReceivingCatalogLedgerIntegrationTests(unittest.TestCase):
    def test_filtered_catalog_drives_archive_preserving_repeat_sync(self):
        with tempfile.TemporaryDirectory(prefix="receiving-ledger-integration-") as raw:
            base = Path(raw)
            restored = base / "restored-project"
            restored.mkdir()
            pack = base / "pack"
            visible_id = "01a00000-1111-7222-8333-555555555555"
            internal_id = "01a00000-1111-7222-8333-666666666666"
            write_transcript(pack / "threads" / visible_id, visible_id, "real\u2028user request")
            write_transcript(
                pack / "threads" / internal_id,
                internal_id,
                "The following is the Codex agent history whose request action you are assessing. Untrusted history",
            )
            manifest_path, manifest_hash = write_pack_manifest(pack, [visible_id, internal_id])

            report = {
                "migration_id": "A-device-20261008-test",
                "source_device": "A设备",
                "source_computer": "WORKSTATION-A",
                "restore_root": str(base),
                "project_count": 1,
                "projects": [{
                    "index": 1,
                    "project_id": "source-project",
                    "restored_project_root": str(restored),
                    "conversation_pack_path": str(pack),
                    "conversation_pack_manifest_path": str(manifest_path),
                    "project_release_tag": "project-test",
                    "project_manifest_sha256": "1" * 64,
                    "conversation_release_tag": "conversation-test",
                    "conversation_manifest_sha256": manifest_hash,
                }],
            }
            project_map = {"projects": [{"path": str(restored), "project_id": "local-project", "label": "Project"}]}
            report_path = base / "report.json"
            map_path = base / "project-map.json"
            output = base / "receiving-catalog.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            map_path.write_text(json.dumps(project_map), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(CATALOG_SCRIPT), "--restore-report", str(report_path), "--project-map", str(map_path), "--output", str(output)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            catalog = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(catalog["user_visible_conversation_count"], 1)
            self.assertEqual(catalog["excluded_internal_session_count"], 1)
            task = next(item for item in catalog["receiving_tasks"] if item["kind"] == "conversation")
            self.assertEqual(task["original_thread_id"], visible_id)
            self.assertEqual(len(task["conversation_sha256"]), 64)
            self.assertEqual(len(task["messages_sha256"]), 64)

            key = ledger_module.source_key("WORKSTATION-A", visible_id)
            ledger = {"format": ledger_module.LEDGER_FORMAT, "entries": {key: {
                "source_device": "A设备",
                "source_computer": "WORKSTATION-A",
                "original_thread_id": visible_id,
                "conversation_sha256": task["conversation_sha256"],
                "applied_state_version": ledger_module.APPLIED_STATE_VERSION,
                "receiving_thread_id": "receiver-1",
                "receiver_disposition": "active",
            }}}
            state = {visible_id: {"receiving_thread_id": "receiver-1", "receiver_disposition": "archived"}}
            plan = ledger_module.build_plan(catalog, ledger, state)
            self.assertEqual(len(plan["decisions"]), 1)
            self.assertEqual(plan["decisions"][0]["action"], "skip-unchanged")
            committed = ledger_module.commit_plan(
                plan, ledger, {"plan_sha256": plan["plan_sha256"], "results": []}
            )
            self.assertEqual(committed["ledger"]["entries"][key]["receiver_disposition"], "archived")

    def test_generated_project_bootstrap_is_stable_and_archive_preserving(self):
        with tempfile.TemporaryDirectory(prefix="receiving-bootstrap-integration-") as raw:
            base = Path(raw)
            restored = base / "restored-project"
            restored.mkdir()
            pack = base / "pack"
            internal_id = "01a00000-1111-7222-8333-777777777777"
            write_transcript(
                pack / "threads" / internal_id,
                internal_id,
                "The following is the Codex agent history whose request action you are assessing. Untrusted history",
            )
            manifest_path, manifest_hash = write_pack_manifest(pack, [internal_id])
            report = {
                "migration_id": "A-device-20261014-bootstrap-test",
                "source_device": "A设备",
                "source_computer": "WORKSTATION-A",
                "restore_root": str(base),
                "project_count": 1,
                "projects": [{
                    "index": 1, "project_id": "source-project", "restored_project_root": str(restored),
                    "conversation_pack_path": str(pack), "conversation_pack_manifest_path": str(manifest_path),
                    "project_release_tag": "project-test", "project_manifest_sha256": "1" * 64,
                    "conversation_release_tag": "conversation-test", "conversation_manifest_sha256": manifest_hash,
                }],
            }
            project_map = {"projects": [{"path": str(restored), "project_id": "local-project", "label": "Project"}]}
            report_path = base / "report.json"
            map_path = base / "project-map.json"
            output = base / "receiving-catalog.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            map_path.write_text(json.dumps(project_map), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(CATALOG_SCRIPT), "--restore-report", str(report_path), "--project-map", str(map_path), "--output", str(output)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            catalog = json.loads(output.read_text(encoding="utf-8"))
            bootstrap = next(item for item in catalog["receiving_tasks"] if item["kind"] == "project-bootstrap")
            self.assertEqual(bootstrap["original_thread_id"], ledger_module.bootstrap_identity("source-project"))
            self.assertEqual(
                bootstrap["conversation_sha256"],
                ledger_module.bootstrap_sha256("WORKSTATION-A", "source-project"),
            )
            empty = {"format": ledger_module.LEDGER_FORMAT, "entries": {}}
            first = ledger_module.build_plan(catalog, empty, {})
            self.assertEqual(first["decisions"][0]["action"], "register-new")
            decision = first["decisions"][0]
            results = {"plan_sha256": first["plan_sha256"], "results": [{
                "source_key": decision["source_key"], "action": "register-new", "status": "applied",
                "conversation_sha256": decision["conversation_sha256"],
                "receiving_thread_id": "bootstrap-r1", "receiver_disposition": "active",
            }]}
            ledger = ledger_module.commit_plan(first, empty, results)["ledger"]
            archived = {bootstrap["original_thread_id"]: {
                "receiving_thread_id": "bootstrap-r1", "receiver_disposition": "archived",
            }}
            second = ledger_module.build_plan(catalog, ledger, archived)
            self.assertEqual(second["decisions"][0]["action"], "skip-unchanged")
            committed = ledger_module.commit_plan(
                second, ledger, {"plan_sha256": second["plan_sha256"], "results": []},
            )["ledger"]
            third = ledger_module.build_plan(catalog, committed, archived)
            self.assertEqual(third["decisions"][0]["action"], "skip-unchanged")
            self.assertEqual(third["decisions"][0]["receiver_disposition"], "archived")


if __name__ == "__main__":
    unittest.main()
