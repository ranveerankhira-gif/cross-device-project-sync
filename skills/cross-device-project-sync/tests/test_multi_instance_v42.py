from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SKILL = Path(__file__).resolve().parents[1]


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SKILL / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    scripts_dir = str(SKILL / "scripts")
    sys.path.insert(0, scripts_dir)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


multi = load("multi_instance_sync_v42", "multi-instance-sync.py")
incremental = load("incremental_device_sync_v42", "incremental-device-sync.py")
packer = load("bulk_conversation_pack_v42", "bulk-conversation-pack.py")
ledger_module = load("ledger_v42", "conversation-sync-ledger.py")


def seal_queue_inputs(path: Path) -> Path:
    catalog = multi.read_json(path)
    tasks = catalog["receiving_tasks"]
    catalog.update(format=ledger_module.RECEIVING_CATALOG_FORMAT_V2,
                   source_computer="SOURCE", source_device=catalog.get("source_device") or "A设备",
                   project_count=1, raw_session_count=len(tasks), user_visible_conversation_count=len(tasks),
                   excluded_internal_session_count=0, bootstrap_task_count=0, target_receiving_task_count=len(tasks),
                   projects=[{"raw_session_count": len(tasks)}], excluded_sessions=[])
    for row in tasks:
        row.setdefault("original_thread_id", row.get("thread_id"))
        row.setdefault("kind", "conversation")
        row.setdefault("lineage_id", multi.lineage_id("SOURCE", row["original_thread_id"]))
        row.setdefault("branch_id", "main")
        row.setdefault("conversation_sha256", multi.digest(row["original_thread_id"]))
        row.setdefault("initial_prompt", "Register lazily; wait for user continuation.")
    catalog.pop("catalog_sha256", None)
    catalog["catalog_sha256"] = multi.digest(catalog)
    write_json(path, catalog)
    plan = ledger_module.build_plan(catalog, {"format": multi.LEDGER_V2, "entries": {}}, {})
    plan_path = path.with_suffix(".plan.json")
    write_json(plan_path, plan)
    return plan_path


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def write_session(path: Path, thread_id: str, cwd: Path, user: str, assistant: str = "done") -> None:
    rows = [
        {"type": "session_meta", "payload": {"id": thread_id, "cwd": str(cwd)}},
        {"type": "response_item", "timestamp": "2026-10-14T00:00:00Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": user}]}},
        {"type": "response_item", "timestamp": "2026-10-14T00:00:01Z", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": assistant}]}},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    for parent in path.parents:
        database = parent / "state_5.sqlite"
        if database.is_file():
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("INSERT OR REPLACE INTO threads VALUES (?, ?, ?, ?, ?)",
                                   (thread_id, str(path), "vscode", int("archived_sessions" in path.parts), thread_id))
                connection.commit()
            break


class MultiInstanceV42Tests(unittest.TestCase):
    def registry(self, root: Path, homes: list[tuple[str, Path]]) -> tuple[Path, dict]:
        path = root / "machine-state" / "instances.json"
        value = {
            "format": multi.REGISTRY_FORMAT,
            "device_label": "测试设备",
            "computer_name": "TESTBOX",
            "machine_state_root": str(path.parent),
            "instances": [
                {"instance_id": instance_id, "display_name": instance_id, "codex_home": str(home)}
                for instance_id, home in homes
            ],
        }
        write_json(path, value)
        return path, value

    def make_home(self, root: Path, name: str, projects: dict | None = None, assignments: dict | None = None) -> Path:
        home = root / name
        (home / "sessions").mkdir(parents=True)
        (home / "archived_sessions").mkdir()
        with closing(sqlite3.connect(home / "state_5.sqlite")) as connection:
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT, archived INTEGER, title TEXT)")
        write_json(home / ".codex-global-state.json", {
            "local-projects": projects or {},
            "thread-project-assignments": assignments or {},
        })
        return home

    def test_lock_is_explicitly_held_after_creator_process_lifetime(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, {"COMPUTERNAME": "TESTBOX"}):
            root = Path(raw)
            home = self.make_home(root, "home")
            registry_path, _ = self.registry(root, [("a", home)])
            registry, caller = multi.validate_registry(registry_path, str(home))
            lock = multi.acquire_lock(registry, caller, "op-1", "upload")
            with self.assertRaises(multi.MultiInstanceError):
                multi.acquire_lock(registry, caller, "op-2", "upload")
            recovered = multi.recover_lock(registry, caller, "op-1", "test abandoned operation")
            self.assertTrue(Path(recovered["archived_lock"]).is_file())
            self.assertFalse(multi.lock_path(registry).exists())
            second = multi.acquire_lock(registry, caller, "op-2", "upload")
            multi.release_lock(registry, "op-2", second["lock_token"])

    def test_registry_rejects_unlisted_caller(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, {"COMPUTERNAME": "TESTBOX"}):
            root = Path(raw)
            home = self.make_home(root, "home")
            other = self.make_home(root, "test-home")
            registry_path, _ = self.registry(root, [("a", home)])
            with self.assertRaises(multi.MultiInstanceError):
                multi.validate_registry(registry_path, str(other))

    def test_snapshot_deduplicates_project_root_and_keeps_all_instances(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, {"COMPUTERNAME": "TESTBOX"}):
            root = Path(raw)
            project = root / "shared-project"
            project.mkdir()
            homes = []
            for index, instance in enumerate(("a", "b", "c"), 1):
                project_id = f"project-{instance}"
                home = self.make_home(
                    root, f"home-{instance}",
                    {project_id: {"name": "shared", "rootPaths": [str(project)]}},
                    {f"thread-{instance}-000000": {"projectKind": "local", "projectId": project_id}},
                )
                write_session(home / "sessions" / f"thread-{instance}.jsonl", f"thread-{instance}-000000", project, f"from {instance}")
                homes.append((instance, home))
            registry_path, _ = self.registry(root, homes)
            registry, caller = multi.validate_registry(registry_path, str(homes[0][1]))
            lock = multi.acquire_lock(registry, caller, "snapshot-1", "upload")
            output = root / "work" / "discovery.json"
            result = multi.snapshot_discovery(registry, "snapshot-1", lock["lock_token"], root / "work" / "snap", output)
            value = multi.read_json(output)
            multi.release_lock(registry, "snapshot-1", lock["lock_token"])
            self.assertEqual(result["instance_count"], 3)
            self.assertEqual(len(value["projects"]), 1)
            self.assertEqual(len(value["projects"][0]["registrations"]), 3)
            self.assertEqual(len(value["projects"][0]["conversations"]), 3)

    def test_migrate_139_legacy_entries_without_duplicate_creation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ledger = root / "ledger.json"
            entries = {}
            for index in range(139):
                thread = f"thread-{index:03d}"
                key = hashlib.sha256(thread.encode()).hexdigest()
                entries[key] = {
                    "source_computer": "SOURCE-A",
                    "original_thread_id": thread,
                    "receiving_thread_id": f"receiver-{index:03d}",
                    "receiver_disposition": "archived" if index % 2 else "active",
                    "conversation_sha256": hashlib.sha256((thread + "-content").encode()).hexdigest(),
                }
            write_json(ledger, {"format": multi.LEDGER_V1, "entries": entries})
            result = multi.migrate_ledger(ledger, "a", root / "backups")
            migrated = multi.read_json(ledger)
            self.assertEqual(result["entry_count"], 139)
            self.assertEqual(len(migrated["entries"]), 139)
            self.assertEqual({row["receiving_thread_id"] for row in migrated["entries"].values()}, {f"receiver-{i:03d}" for i in range(139)})
            again = multi.migrate_ledger(ledger, "a", root / "backups")
            self.assertFalse(again["migrated"])

    def test_metadata_controls_archive_and_orphan_internal_logs_are_excluded(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, {"COMPUTERNAME": "TESTBOX"}):
            root = Path(raw)
            project = root / "project"
            project.mkdir()
            home = self.make_home(root, "a", {"p": {"rootPaths": [str(project)]}})
            for name in ("visible", "orphan", "internal"):
                write_session(home / "sessions" / (name + ".jsonl"), name, project, "Question")
            with closing(sqlite3.connect(home / "state_5.sqlite")) as connection:
                connection.execute("UPDATE threads SET archived=1 WHERE id='visible'")
                connection.execute("DELETE FROM threads WHERE id='orphan'")
                connection.execute("UPDATE threads SET source=? WHERE id='internal'", ('{"subagent":{"other":"guardian"}}',))
                connection.commit()
            (home / "auth.json").write_text("DO NOT COPY", encoding="utf-8")
            unlisted = self.make_home(root, "test-not-allowlisted")
            write_session(unlisted / "sessions" / "test.jsonl", "unlisted", project, "test")
            registry_path, _ = self.registry(root, [("a", home)])
            registry, caller = multi.validate_registry(registry_path, str(home))
            lock = multi.acquire_lock(registry, caller, "audit", "upload")
            snapshot = root / "snapshot"
            output = root / "discovery.json"
            multi.snapshot_discovery(registry, "audit", lock["lock_token"], snapshot, output)
            value = multi.read_json(output)
            rows = [row for project in value["projects"] for row in project["conversations"]]
            self.assertEqual([row["thread_id"] for row in rows], ["visible"])
            self.assertEqual(rows[0]["source_disposition"], "archived")
            self.assertEqual({row["kind"] for row in value["exclusions"]}, {"internal-runtime-session", "unverifiable-or-deleted-task"})
            self.assertFalse(list(snapshot.rglob("*.sqlite")))
            self.assertFalse(list(snapshot.rglob("auth.json")))
            self.assertEqual(value["instances"][0]["instance_id"], "a")

    @unittest.skipUnless(os.name == "nt", "Windows extended path syntax")
    def test_snapshot_matches_extended_length_database_rollout_path(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, {"COMPUTERNAME": "TESTBOX"}):
            root = Path(raw)
            project = root / "project"
            project.mkdir()
            home = self.make_home(root, "home", {"p": {"rootPaths": [str(project)]}})
            session = home / "sessions" / "visible.jsonl"
            write_session(session, "visible", project, "Question")
            with closing(sqlite3.connect(home / "state_5.sqlite")) as connection:
                connection.execute("UPDATE threads SET rollout_path=? WHERE id='visible'", ("\\\\?\\" + str(session.resolve()),))
                connection.commit()
            registry_path, _ = self.registry(root, [("a", home)])
            registry, caller = multi.validate_registry(registry_path, str(home))
            lock = multi.acquire_lock(registry, caller, "long-path", "upload")
            output = root / "discovery.json"
            multi.snapshot_discovery(registry, "long-path", lock["lock_token"], root / "snapshot", output)
            value = multi.read_json(output)
            rows = [row for project_row in value["projects"] for row in project_row["conversations"]]
            self.assertEqual([row["thread_id"] for row in rows], ["visible"])
            self.assertTrue(value["conversation_inventory_complete"])

    def test_missing_empty_task_without_user_event_is_nonblocking_exclusion(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, {"COMPUTERNAME": "TESTBOX"}):
            root = Path(raw)
            home = self.make_home(root, "home")
            with closing(sqlite3.connect(home / "state_5.sqlite")) as connection:
                connection.execute("ALTER TABLE threads ADD COLUMN has_user_event INTEGER")
                connection.execute(
                    "INSERT INTO threads (id, rollout_path, source, archived, title, has_user_event) VALUES (?, ?, ?, ?, ?, ?)",
                    ("empty", str(home / "sessions" / "missing.jsonl"), "vscode", 0, "", 0),
                )
                connection.commit()
            registry_path, _ = self.registry(root, [("a", home)])
            registry, caller = multi.validate_registry(registry_path, str(home))
            lock = multi.acquire_lock(registry, caller, "empty-task", "upload")
            output = root / "discovery.json"
            result = multi.snapshot_discovery(registry, "empty-task", lock["lock_token"], root / "snapshot", output)
            value = multi.read_json(output)
            self.assertTrue(result["complete"])
            self.assertEqual(value["expected_conversation_count"], 0)
            self.assertEqual(value["exclusions"][0]["kind"], "empty-task-no-user-event")

    def test_missing_task_with_user_event_blocks_complete_inventory(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, {"COMPUTERNAME": "TESTBOX"}):
            root = Path(raw)
            home = self.make_home(root, "home")
            with closing(sqlite3.connect(home / "state_5.sqlite")) as connection:
                connection.execute("ALTER TABLE threads ADD COLUMN has_user_event INTEGER")
                connection.execute(
                    "INSERT INTO threads (id, rollout_path, source, archived, title, has_user_event) VALUES (?, ?, ?, ?, ?, ?)",
                    ("missing", str(home / "sessions" / "missing.jsonl"), "vscode", 0, "Needs transfer", 1),
                )
                connection.commit()
            registry_path, _ = self.registry(root, [("a", home)])
            registry, caller = multi.validate_registry(registry_path, str(home))
            lock = multi.acquire_lock(registry, caller, "missing-task", "upload")
            output = root / "discovery.json"
            result = multi.snapshot_discovery(registry, "missing-task", lock["lock_token"], root / "snapshot", output)
            value = multi.read_json(output)
            self.assertFalse(result["complete"])
            self.assertEqual(value["exclusions"][0]["kind"], "unreadable-session")

    def test_visible_task_with_mismatched_rollout_path_blocks_inventory(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, {"COMPUTERNAME": "TESTBOX"}):
            root = Path(raw)
            project = root / "project"
            project.mkdir()
            home = self.make_home(root, "home", {"p": {"rootPaths": [str(project)]}})
            session = home / "sessions" / "visible.jsonl"
            write_session(session, "visible", project, "Question")
            with closing(sqlite3.connect(home / "state_5.sqlite")) as connection:
                connection.execute("UPDATE threads SET rollout_path=? WHERE id='visible'", (str(home / "sessions" / "other.jsonl"),))
                connection.commit()
            registry_path, _ = self.registry(root, [("a", home)])
            registry, caller = multi.validate_registry(registry_path, str(home))
            lock = multi.acquire_lock(registry, caller, "path-mismatch", "upload")
            output = root / "discovery.json"
            result = multi.snapshot_discovery(registry, "path-mismatch", lock["lock_token"], root / "snapshot", output)
            value = multi.read_json(output)
            self.assertFalse(result["complete"])
            self.assertEqual(value["expected_conversation_count"], 0)
            self.assertEqual(value["exclusions"][0]["kind"], "unreadable-session")
            self.assertIn("does not match", value["exclusions"][0]["reason"])

    def test_explicit_projectless_inside_registered_project_stays_projectless(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, {"COMPUTERNAME": "TESTBOX"}):
            root = Path(raw)
            project = root / "project"
            project.mkdir()
            home = self.make_home(root, "home", {"p": {"rootPaths": [str(project)]}})
            write_session(home / "sessions" / "loose.jsonl", "loose", project, "Question")
            state_path = home / ".codex-global-state.json"
            state = multi.read_json(state_path)
            state["projectless-thread-ids"] = ["loose"]
            write_json(state_path, state)
            registry_path, _ = self.registry(root, [("a", home)])
            registry, caller = multi.validate_registry(registry_path, str(home))
            lock = multi.acquire_lock(registry, caller, "projectless", "upload")
            output = root / "discovery.json"
            multi.snapshot_discovery(registry, "projectless", lock["lock_token"], root / "snapshot", output)
            value = multi.read_json(output)
            registered = next(p for p in value["projects"] if p["kind"] == "registered-project")
            loose = next(p for p in value["projects"] if p["kind"] == "projectless-conversation-group")
            self.assertEqual(registered["conversations"], [])
            self.assertEqual(loose["conversations"][0]["thread_id"], "loose")
            self.assertIsNone(loose["source_path"])

    def test_active_file_mutation_retries_until_a_consistent_copy(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source, dest = root / "source", root / "dest"
            source.write_bytes(b"first")
            original = Path.read_bytes
            changed = False
            def mutate(path):
                nonlocal changed
                data = original(path)
                if path == source and not changed:
                    changed = True
                    source.write_bytes(b"second complete snapshot")
                return data
            with mock.patch.object(Path, "read_bytes", mutate):
                data = multi.stable_copy(source, dest)
            self.assertEqual(data, b"second complete snapshot")
            self.assertEqual(source.read_bytes(), dest.read_bytes())

    def test_same_original_thread_in_two_accounts_diverges_without_pack_path_collision(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, {"COMPUTERNAME": "TESTBOX"}):
            root = Path(raw)
            project = root / "project"
            project.mkdir()
            thread = "01a00000-0000-7000-8000-000000000001"
            homes = []
            for account in ("a", "b"):
                home = self.make_home(root, "home-" + account, {"p": {"rootPaths": [str(project)]}})
                write_session(home / "sessions" / "same.jsonl", thread, project, "branch " + account)
                homes.append((account, home))
            registry_path, _ = self.registry(root, homes)
            registry, caller = multi.validate_registry(registry_path, str(homes[0][1]))
            lock = multi.acquire_lock(registry, caller, "branches", "upload")
            discovery = root / "discovery.json"
            multi.snapshot_discovery(registry, "branches", lock["lock_token"], root / "snapshots", discovery)
            catalog_path = root / "catalog.json"
            incremental.command_scan(argparse.Namespace(discovery=str(discovery), output=str(catalog_path), checkpoint_dir=None, status_file=None))
            catalog = multi.read_json(catalog_path)
            content = catalog["projects"][0]
            self.assertEqual(content["conversation_count"], 2)
            self.assertEqual(len({row["thread_id"] for row in content["conversations"]}), 2)
            self.assertEqual({row["origin_thread_id"] for row in content["conversations"]}, {thread})
            packer.build(argparse.Namespace(catalog=str(catalog_path), project_id=content["project_id"], output=str(root / "pack")))
            self.assertTrue(packer.verify(argparse.Namespace(pack=str(root / "pack")))["verified"])

    def test_returned_lineage_reuses_native_origin_task(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, {"COMPUTERNAME": "TESTBOX"}):
            root = Path(raw)
            home = self.make_home(root, "native-home")
            thread = "native-origin"
            write_session(home / "sessions" / "native.jsonl", thread, root, "original native task")
            registry_path, _ = self.registry(root, [("a", home)])
            registry, caller = multi.validate_registry(registry_path, str(home))
            catalog = home / "catalog.json"
            write_json(catalog, {"receiving_tasks": [{"thread_id": "returned-storage-id", "lineage_id": multi.lineage_id("TESTBOX", thread)}]})
            seal_queue_inputs(catalog)
            state_path = home / "receiver-state.json"
            multi.build_receiver_state(catalog, registry, caller, state_path)
            states, info = ledger_module.receiver_states(state_path)
            plan = ledger_module.build_plan(multi.read_json(catalog), {"format": multi.LEDGER_V2, "entries": {}}, states, info)
            self.assertEqual(plan["decisions"][0]["action"], "update-existing")
            self.assertEqual(plan["decisions"][0]["receiving_thread_id"], thread)

    def test_receive_queue_is_bound_to_caller_and_eight_item_batches(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, {"COMPUTERNAME": "TESTBOX"}):
            root = Path(raw)
            home_a = self.make_home(root, "home-a")
            home_b = self.make_home(root, "home-b")
            registry_path, _ = self.registry(root, [("a", home_a), ("b", home_b)])
            registry, caller = multi.validate_registry(registry_path, str(home_b))
            tasks = [{
                "receiver_operation_key": hashlib.sha256(str(i).encode()).hexdigest(),
                "thread_id": f"thread-{i}", "display_title": f"task {i}",
                "source_device": "A设备", "source_instance_id": "a", "source_disposition": "active",
            } for i in range(21)]
            catalog = root / "catalog.json"
            write_json(catalog, {"receiving_tasks": tasks, "catalog_sha256": hashlib.sha256(b"catalog").hexdigest()})
            queue_path = home_b / "queue.json"
            result = multi.build_queue(catalog, registry, caller, queue_path, seal_queue_inputs(catalog))
            queue = multi.read_json(queue_path)
            self.assertEqual(result["batch_size"], 8)
            self.assertEqual(queue["destination_instance_id"], "b")
            self.assertEqual(queue["destination_codex_home"], str(home_b.absolute()))
            self.assertTrue(all(item["status"] == "pending" for item in queue["tasks"]))

    def test_project_update_decision_never_overlays_local_changes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = hashlib.sha256(b"target").hexdigest()
            old = hashlib.sha256(b"old").hexdigest()
            local = hashlib.sha256(b"local-change").hexdigest()
            ledger = root / "projects.json"
            write_json(ledger, {"format": multi.PROJECT_LEDGER_FORMAT, "projects": {"p": {"last_applied_tree_sha256": old, "restored_project_root": str(root / "p")}}})
            safe = multi.plan_project_destination(ledger, "p", target, old, root / "restore", "商品选购2", "B设备")
            diverged = multi.plan_project_destination(ledger, "p", target, local, root / "restore", "商品选购2", "B设备")
            self.assertEqual(safe["action"], "transactional-safe-replace")
            self.assertEqual(diverged["action"], "restore-side-by-side")
            self.assertTrue(diverged["must_not_overlay"])
            self.assertEqual(Path(diverged["destination"]).name, "商品选购2")

    def test_v2_projectless_group_scans_and_packs_without_fake_project(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            session = root / "session.jsonl"
            thread = "01a13a31-8958-7981-aec6-da8bd12077c8"
            write_session(session, thread, root, "projectless request")
            lineage = incremental.hashlib.sha256(incremental.canonical_bytes({"origin_computer": "source", "origin_thread_id": thread})).hexdigest()
            discovery = {
                "format": "cross-device-project-sync-multi-instance-discovery-v2",
                "source_device": "A设备", "source_computer": "SOURCE", "instances": [{"instance_id": "a"}],
                "instance_registry_sha256": hashlib.sha256(b"registry").hexdigest(),
                "projects": [{
                    "project_id": "projectless-a", "kind": "projectless-conversation-group", "label": "a projectless",
                    "source_path": None, "registrations": [], "exclude_relative_paths": [], "exclusion_records": [],
                    "conversation_inventory": {"complete": True, "expected_thread_ids": [thread]},
                    "conversations": [{
                        "thread_id": thread, "thread_title": "P", "source_instance_id": "a", "source_instance_name": "a号",
                        "source_disposition": "archived", "origin_computer": "SOURCE", "origin_instance_id": "a",
                        "origin_thread_id": thread, "lineage_id": lineage, "branch_id": "main", "cwd": str(root),
                        "session_log": str(session), "snapshot_session_log": str(session), "original_session_log": str(session),
                        "inherited_handoff_paths": [],
                    }],
                }],
                "cloud_managed_projects": [], "exclusions": [], "conversation_exclusions": [],
                "conversation_inventory_complete": True, "expected_conversation_count": 1,
            }
            unsigned = dict(discovery)
            discovery["discovery_sha256"] = hashlib.sha256(incremental.canonical_bytes(unsigned)).hexdigest()
            discovery_path = root / "discovery.json"
            catalog_path = root / "catalog.json"
            write_json(discovery_path, discovery)
            incremental.command_scan(argparse.Namespace(discovery=str(discovery_path), output=str(catalog_path), checkpoint_dir=None, status_file=None))
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            project = catalog["projects"][0]
            self.assertEqual(catalog["format"], incremental.CATALOG_FORMAT_V2)
            self.assertFalse(project["has_project_payload"])
            self.assertIsNone(project["tree_sha256"])
            self.assertEqual(project["conversation_pack_generation"], incremental.CONVERSATION_PACK_GENERATION_V2)
            pack = root / "pack"
            packer.build(argparse.Namespace(catalog=str(catalog_path), project_id="projectless-a", output=str(pack)))
            verified = packer.verify(argparse.Namespace(pack=str(pack)))
            self.assertTrue(verified["verified"])
            bindings = root / "bindings.json"
            write_json(bindings, {
                "migration_id": "device-v42-test",
                "skill_release": {"tag": "skill-v42-test", "manifest_sha256": hashlib.sha256(b"skill").hexdigest(), "reused": False},
                "projects": [{
                    "project_id": "projectless-a", "project_release": None,
                    "conversation_release": {"tag": "conversation-v42-test", "manifest_sha256": hashlib.sha256(b"conversation").hexdigest(), "reused": False},
                }],
            })
            index_path = root / "index.json"
            handoff_path = root / "read-first.md"
            incremental.command_build_index(argparse.Namespace(
                catalog=str(catalog_path), bindings=str(bindings), output=str(index_path), handoff=str(handoff_path),
            ))
            index = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(index["format"], incremental.INDEX_FORMAT_V2)
            self.assertIsNone(index["projects"][0]["project_release"])
            self.assertTrue(incremental.command_verify_index(argparse.Namespace(index=str(index_path)))["verified"])

    def test_lineage_reconciliation_deduplicates_ancestor_and_keeps_branches(self):
        lineage = hashlib.sha256(b"lineage").hexdigest()
        def conversation(thread, sequence):
            return {"thread_id": thread, "lineage_id": lineage, "branch_id": "main", "source_instance_id": thread, "message_fingerprints": sequence, "messages_sha256": hashlib.sha256(thread.encode()).hexdigest(), "conversation_sha256": hashlib.sha256((thread + "c").encode()).hexdigest(), "session_log": thread}
        projects = [
            {"project_id": "p1", "conversations": [conversation("a", ["1", "2"])]},
            {"project_id": "p2", "conversations": [conversation("b", ["1", "2", "3"])]},
            {"project_id": "p3", "conversations": [conversation("c", ["1", "2", "4"])]},
        ]
        exclusions = []
        incremental.reconcile_lineage_branches(projects, exclusions)
        self.assertEqual(projects[0]["conversations"], [])
        branches = {projects[1]["conversations"][0]["branch_id"], projects[2]["conversations"][0]["branch_id"]}
        self.assertEqual(len(branches), 2)
        self.assertTrue(all(branch.startswith("branch-") for branch in branches))

    def test_offline_a_to_b_to_a_other_instance_round_trip(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, {"COMPUTERNAME": "TESTBOX"}):
            root = Path(raw)
            profile = root / "profile"
            profile.mkdir()
            instance_a = self.make_home(root, "a-home")
            instance_c = self.make_home(root, "c-home")
            instance_b = self.make_home(root, "b-home")
            lineage = hashlib.sha256(multi.canonical_bytes({"origin_computer": "A-PC", "origin_thread_id": "origin-a"})).hexdigest()
            operation_key = multi.source_key_v2(lineage, "main")

            registry_b_path, _ = self.registry(root / "registry-b", [("b", instance_b)])
            registry_b, caller_b = multi.validate_registry(registry_b_path, str(instance_b))
            incoming = root / "incoming-a.json"
            write_json(incoming, {"source_device": "A设备", "receiving_tasks": [{
                "receiver_operation_key": operation_key, "thread_id": "origin-a", "display_title": "round trip",
                "source_instance_id": "a", "source_instance_name": "a号", "source_disposition": "active",
                "lineage_id": lineage, "branch_id": "main", "target_kind": "projectless",
            }]})
            queue_b = instance_b / "queue-b.json"
            multi.build_queue(incoming, registry_b, caller_b, queue_b, seal_queue_inputs(incoming))
            self.assertEqual(multi.read_json(queue_b)["destination_instance_id"], "b")

            receiving_thread = "01b00000-0000-7000-8000-000000000001"
            write_session(instance_b / "sessions" / "received.jsonl", receiving_thread, profile, "continued on B")
            write_json(instance_b / "cross-device-conversation-sync-ledger.json", {
                "format": multi.LEDGER_V2,
                "entries": {operation_key: {
                    "origin_computer": "A-PC", "origin_instance_id": "a", "origin_thread_id": "origin-a",
                    "lineage_id": lineage, "branch_id": "main", "receiving_thread_id": receiving_thread,
                    "receiver_disposition": "active",
                }},
            })
            with mock.patch.dict(os.environ, {"USERPROFILE": str(profile)}):
                lock = multi.acquire_lock(registry_b, caller_b, "b-upload", "upload")
                discovery_path = root / "b-discovery.json"
                multi.snapshot_discovery(registry_b, "b-upload", lock["lock_token"], root / "b-snapshot", discovery_path)
                multi.release_lock(registry_b, "b-upload", lock["lock_token"])
            discovery = multi.read_json(discovery_path)
            uploaded = next(item for project in discovery["projects"] for item in project["conversations"])
            self.assertEqual(uploaded["lineage_id"], lineage)
            self.assertEqual(uploaded["origin_thread_id"], "origin-a")

            registry_a_path, _ = self.registry(root / "registry-a", [("a", instance_a), ("c", instance_c)])
            registry_a, caller_c = multi.validate_registry(registry_a_path, str(instance_c))
            returned_catalog = root / "returned-b.json"
            write_json(returned_catalog, {"source_device": "B设备", "receiving_tasks": [{
                "receiver_operation_key": operation_key, "thread_id": receiving_thread, "display_title": "round trip",
                "source_instance_id": "b", "source_instance_name": "b号", "source_disposition": "active",
                "lineage_id": lineage, "branch_id": "main", "target_kind": "projectless",
            }]})
            queue_c = instance_c / "queue-c.json"
            multi.build_queue(returned_catalog, registry_a, caller_c, queue_c, seal_queue_inputs(returned_catalog))
            returned = multi.read_json(queue_c)
            self.assertEqual(returned["destination_instance_id"], "c")
            self.assertEqual(returned["tasks"][0]["lineage_id"], lineage)
            self.assertEqual(returned["tasks"][0]["operation_key"], operation_key)


if __name__ == "__main__":
    unittest.main()
