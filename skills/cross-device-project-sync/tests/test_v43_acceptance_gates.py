import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    scripts_dir = str(ROOT / "scripts")
    sys.path.insert(0, scripts_dir)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


readiness = load("v43_readiness_test", "device-readiness.py")
acceptance = load("v43_acceptance_test", "receive-final-acceptance.py")
multi = load("v43_multi_test", "multi-instance-sync.py")
queue_module = load("v43_queue_test", "receive-queue-engine.py")
ledger_module = load("v43_ledger_test", "conversation-sync-ledger.py")
project_transaction = load("v44_project_transaction_test", "project-receive-transaction.py")
app_proof_module = load("v45_app_proof_test", "receiver-app-proof.py")


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def seal(value, field):
    value.pop(field, None)
    value[field] = hashlib.sha256(canonical(value)).hexdigest()
    return value


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical(value))


def make_home(path, label, computer="TESTBOX"):
    path.mkdir(parents=True)
    (path / "sessions").mkdir()
    write_json(path / ".codex-global-state.json", {"projects": []})
    write_json(
        path / "cross-device-project-sync-device.json",
        {"device_label": label, "computer_name": computer},
    )


def make_registry(path, homes, computer="TESTBOX", label="测试设备"):
    value = {
        "format": multi.REGISTRY_FORMAT,
        "device_label": label,
        "computer_name": computer,
        "machine_state_root": str(path.parent.resolve()),
        "instances": [
            {"instance_id": key, "display_name": key + "号", "codex_home": str(home.resolve())}
            for key, home in homes
        ],
    }
    write_json(path, value)
    return value


class DeviceReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="v43-readiness-")
        self.root = Path(self.temp.name)
        self.state = self.root / "machine-state"
        self.state.mkdir()
        self.a = self.root / "home-a"
        self.b = self.root / "home-b"
        make_home(self.a, "测试设备")
        make_home(self.b, "测试设备")
        self.registry = self.state / "instances.json"
        make_registry(self.registry, [("a", self.a), ("b", self.b)])
        self.profile = self.root / "user-profile"
        self.skill = self.profile / ".agents" / "skills" / "cross-device-project-sync"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text(
            "---\nname: cross-device-project-sync\ndescription: test\nmetadata:\n  version: \"20261016-v45\"\n---\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def args(self, name):
        return argparse.Namespace(
            registry=str(self.registry), installed_skill_root=str(self.skill),
            output=str(self.state / name), expected_skill_version="20261016-v45",
            expected_skill_package=None, check_cloud=False,
            repository=readiness.CANONICAL_REPOSITORY, gh=None,
        )

    def test_ready_report_binds_allow_list_identity_and_skill(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.b), "COMPUTERNAME": "TESTBOX", "USERPROFILE": str(self.profile)}):
            report = readiness.build_report(self.args("ready.json"))
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["caller_instance_id"], "b")
        self.assertEqual(report["readiness_sha256"], readiness.digest({key: value for key, value in report.items() if key != "readiness_sha256"}))

    def test_readiness_report_is_reverified_against_freshness_skill_registry_and_lock(self):
        args = self.args("verify-ready.json")
        environment = {"CODEX_HOME": str(self.b), "COMPUTERNAME": "TESTBOX", "USERPROFILE": str(self.profile)}
        with mock.patch.dict(os.environ, environment):
            report = readiness.build_report(args)
            registry, caller = multi.validate_registry(self.registry, str(self.b), require_paths=True)
            lock = multi.acquire_lock(registry, caller, "upload-v45-test", "upload")
            verified = readiness.verify_report(argparse.Namespace(
                verify_report=args.output, registry=str(self.registry), installed_skill_root=str(self.skill),
                expected_skill_version="20261016-v45", operation_id=lock["operation_id"],
                lock_token=lock["lock_token"], kind="upload",
            ))
            self.assertEqual(verified["status"], "verified-current-readiness")
            stale = dict(report)
            stale["generated_utc"] = "2020-01-01T00:00:00+00:00"
            seal(stale, "readiness_sha256")
            write_json(Path(args.output), stale)
            with self.assertRaisesRegex(ValueError, "stale"):
                readiness.verify_report(argparse.Namespace(
                    verify_report=args.output, registry=str(self.registry), installed_skill_root=str(self.skill),
                    expected_skill_version="20261016-v45", operation_id=lock["operation_id"],
                    lock_token=lock["lock_token"], kind="upload",
                ))
            multi.release_lock(registry, lock["operation_id"], lock["lock_token"])

    def test_device_identity_mismatch_is_not_ready(self):
        make_registry(self.registry, [("a", self.a), ("b", self.b)], computer="OTHERBOX")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.a), "COMPUTERNAME": "TESTBOX", "USERPROFILE": str(self.profile)}):
            report = readiness.build_report(self.args("identity-mismatch.json"))
        self.assertEqual(report["status"], "not-ready")
        self.assertTrue(any(row["name"] == "device-identity" and row["status"] == "failed" for row in report["checks"]))

    def test_non_allow_listed_caller_is_not_ready(self):
        other = self.root / "unlisted"
        make_home(other, "测试设备")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(other), "COMPUTERNAME": "TESTBOX", "USERPROFILE": str(self.profile)}):
            report = readiness.build_report(self.args("unlisted.json"))
        self.assertEqual(report["status"], "not-ready")
        self.assertIn("not allow-listed", " ".join(report["errors"]))

    def test_private_duplicate_skill_is_not_ready(self):
        (self.a / "skills" / "cross-device-project-sync").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.a), "COMPUTERNAME": "TESTBOX", "USERPROFILE": str(self.profile)}):
            report = readiness.build_report(self.args("duplicate.json"))
        self.assertEqual(report["status"], "not-ready")
        duplicate_check = next(row for row in report["checks"] if row["name"] == "no-private-skill-duplicates")
        self.assertEqual(duplicate_check["status"], "failed")

    def test_alternate_same_name_skill_root_is_not_ready(self):
        alternate = self.root / "alternate" / "cross-device-project-sync"
        alternate.mkdir(parents=True)
        (alternate / "SKILL.md").write_text((self.skill / "SKILL.md").read_text(encoding="utf-8"), encoding="utf-8")
        args = self.args("alternate.json")
        args.installed_skill_root = str(alternate)
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.a), "COMPUTERNAME": "TESTBOX", "USERPROFILE": str(self.profile)}):
            report = readiness.build_report(args)
        self.assertEqual(report["status"], "not-ready")
        self.assertIn("canonical shared path", " ".join(report["errors"]))


class AcceptanceFixture:
    def __init__(self, root):
        self.root = root
        self.profile = root / "user-profile"
        self.skill = self.profile / ".agents" / "skills" / "cross-device-project-sync"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text(
            "---\nname: cross-device-project-sync\ndescription: test\nmetadata:\n  version: \"20261016-v45\"\n---\n",
            encoding="utf-8",
        )
        self.home = root / "receiver-home"
        make_home(self.home, "B设备", "WORKSTATION-B")
        self.state = root / "machine-state"
        self.state.mkdir()
        self.registry = self.state / "instances.json"
        make_registry(self.registry, [("c", self.home)], computer="WORKSTATION-B", label="B设备")
        self.restored = root / "restored-project"
        self.restored.mkdir()
        self.paths = {name: self.home / (name + ".json") for name in (
            "project_map", "catalog", "plan", "queue", "results", "app_proof", "ledger", "commit_receipt", "acceptance",
        )}
        self.paths["selection"] = self.home / "versions.json"
        self.index_assets = root / "index-assets"
        self.index_assets.mkdir()
        self.paths["index"] = self.index_assets / "INCREMENTAL_DEVICE_INDEX.json"
        self.paths["index_metadata"] = root / "index-release-metadata.json"
        self.paths["receive"] = root / "INCREMENTAL_RECEIVE_REPORT.json"
        self.h1, self.h2 = "1" * 64, "2" * 64
        self.l1, self.l2 = "a" * 64, "b" * 64
        self.build()

    def build(self):
        self.paths["commit_receipt"].unlink(missing_ok=True)
        lock_path = self.state / "operation.lock.json"
        if lock_path.exists():
            lock_path.unlink()
        readiness_path = self.state / "readiness.json"
        readiness_args = argparse.Namespace(
            registry=str(self.registry), installed_skill_root=str(self.skill),
            output=str(readiness_path), expected_skill_version="20261016-v45",
            expected_skill_package=None, check_cloud=False,
            repository=readiness.CANONICAL_REPOSITORY, gh=None,
        )
        with mock.patch.dict(os.environ, {
            "CODEX_HOME": str(self.home), "COMPUTERNAME": "WORKSTATION-B", "USERPROFILE": str(self.profile),
        }):
            readiness_report = readiness.build_report(readiness_args)
        if readiness_report["status"] != "ready":
            raise AssertionError(readiness_report)
        with mock.patch.dict(os.environ, {"COMPUTERNAME": "WORKSTATION-B"}):
            registry_value, caller = multi.validate_registry(self.registry, str(self.home), require_paths=True)
            lock = multi.acquire_lock(registry_value, caller, "download-v45-test", "download")
        for number in (1, 2):
            pack_root = self.root / f"conversation-pack-{number}"
            pack_root.mkdir(exist_ok=True)
            manifest_path = self.root / f"conversation-manifest-{number}.json"
            manifest = {
                "format": "cross-device-project-sync-conversation-pack-v1",
                "conversation_count": 1, "conversations": [{"thread_id": f"source-thread-{number}"}], "files": [],
            }
            write_json(manifest_path, manifest)
            self.paths[f"pack_{number}"] = pack_root
            self.paths[f"pack_manifest_{number}"] = manifest_path
        conversation_manifest_hashes = {
            number: hashlib.sha256(self.paths[f"pack_manifest_{number}"].read_bytes()).hexdigest()
            for number in (1, 2)
        }
        projects = [
            {
                "project_id": "source-project", "kind": "registered-project", "has_project_payload": True,
                "label": "Project", "total_bytes": 1, "tree_sha256": "3" * 64,
                "project_release": {"tag": "project-release", "manifest_sha256": "4" * 64, "reused": False},
                "conversation_release": {"tag": "conversation-release", "manifest_sha256": conversation_manifest_hashes[1], "reused": False},
                "conversation_count": 1, "conversations": [{
                    "thread_id": "source-thread-1", "conversation_sha256": self.h1, "lineage_id": self.l1,
                    "branch_id": "main", "source_disposition": "active",
                }],
            },
            {
                "project_id": "projectless-group", "kind": "projectless-conversation-group", "has_project_payload": False,
                "label": "Projectless", "total_bytes": 0, "tree_sha256": "6" * 64,
                "project_release": None,
                "conversation_release": {"tag": "projectless-conversation", "manifest_sha256": conversation_manifest_hashes[2], "reused": False},
                "conversation_count": 1, "conversations": [{
                    "thread_id": "source-thread-2", "conversation_sha256": self.h2, "lineage_id": self.l2,
                    "branch_id": "main", "source_disposition": "archived",
                }],
            },
        ]
        index = {
            "format": "cross-device-project-sync-incremental-device-index-v2",
            "migration_id": "A-device-v45-test", "repository": "example-owner/example-private-backups",
            "required_skill_version": "20261016-v45",
            "skill_release": {"tag": "skill-cross-device-project-sync-20261016-v45-test", "manifest_sha256": "f" * 64},
            "source_device": "A设备", "source_computer": "WORKSTATION-A",
            "projects": projects, "cloud_managed_projects": [{"project_id": "cloud-one"}],
            "conversation_exclusions": [{"thread_id": "internal-one"}], "exclusions": [{"kind": "broad-root"}],
        }
        seal(index, "index_sha256")
        write_json(self.paths["index"], index)
        release_tag = "device-index-test-" + index["index_sha256"][:16]
        binding = {
            "format": "cross-device-project-sync-content-release-binding-v1",
            "repository": "example-owner/example-private-backups", "release_tag": release_tag,
            "kind": "device-index", "identity_sha256": index["index_sha256"],
            "assets": [{
                "name": "INCREMENTAL_DEVICE_INDEX.json", "size": self.paths["index"].stat().st_size,
                "sha256": hashlib.sha256(self.paths["index"].read_bytes()).hexdigest(),
            }],
        }
        binding_path = self.index_assets / "CONTENT_RELEASE_BINDING.json"
        write_json(binding_path, binding)
        metadata = {
            "tagName": release_tag, "isDraft": False,
            "body": "\n".join([
                "Content-Kind: device-index", f"Content-Identity-SHA256: {index['index_sha256']}",
                "Repository: example-owner/example-private-backups",
            ]),
            "assets": [
                {"name": path.name, "size": path.stat().st_size, "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}
                for path in (self.paths["index"], binding_path)
            ],
        }
        write_json(self.paths["index_metadata"], metadata)
        selection = {
            "repository": "example-owner/example-private-backups", "release_inventory_complete": True,
            "device_index_releases": [{"release_tag": release_tag, "identity_sha256": index["index_sha256"], "verified": True}],
            "selection_required": True, "release_scan_limit": 1000,
        }
        selection["discovery_sha256"] = hashlib.sha256(canonical(selection)).hexdigest()
        write_json(self.paths["selection"], selection)
        installed_tree_sha256 = project_transaction.fingerprint(self.restored)
        receive_projects = [
            {
                "index": 1, "project_id": "source-project", "kind": "registered-project", "has_project_payload": True,
                "label": "Project", "restored_project_root": str(self.restored),
                "project_installation": {"verified": True, "destination": str(self.restored), "installed_tree_sha256": installed_tree_sha256, "project_key": "workstation-a:source-project"},
                "project_release_tag": "project-release", "project_manifest_sha256": "4" * 64,
                "conversation_release_tag": "conversation-release", "conversation_manifest_sha256": conversation_manifest_hashes[1],
                "conversation_pack_path": str(self.paths["pack_1"]), "conversation_pack_manifest_path": str(self.paths["pack_manifest_1"]),
            },
            {
                "index": 2, "project_id": "projectless-group", "kind": "projectless-conversation-group", "has_project_payload": False,
                "label": "Projectless", "restored_project_root": None, "project_installation": None,
                "project_release_tag": None, "project_manifest_sha256": None,
                "conversation_release_tag": "projectless-conversation", "conversation_manifest_sha256": conversation_manifest_hashes[2],
                "conversation_pack_path": str(self.paths["pack_2"]), "conversation_pack_manifest_path": str(self.paths["pack_manifest_2"]),
            },
        ]
        receive = {
            "format": "cross-device-project-sync-incremental-receive-report-v2",
            "status": "restored-verified-not-registered", "destination_codex_home": str(self.home),
            "destination_instance_id": "c", "migration_id": index["migration_id"],
            "source_device": index["source_device"], "source_computer": index["source_computer"],
            "repository": "example-owner/example-private-backups",
            "selection_file": str(self.paths["selection"]),
            "selection_discovery_sha256": selection["discovery_sha256"],
            "selected_release_identity_sha256": index["index_sha256"],
            "index_release_metadata_path": str(self.paths["index_metadata"]),
            "index_assets_path": str(self.index_assets),
            "installed_skill_root": str(self.skill),
            "installed_skill_version": "20261016-v45",
            "installed_skill_tree_sha256": next(
                row["detail"]["tree_sha256"] for row in readiness_report["checks"]
                if row["name"] == "public-skill-integrity"
            ),
            "required_skill_version": "20261016-v45",
            "readiness_report_path": str(readiness_path),
            "readiness_sha256": readiness_report["readiness_sha256"],
            "machine_lock_operation_id": lock["operation_id"],
            "machine_lock_token": lock["lock_token"],
            "machine_lock_token_sha256": lock["lock_token_sha256"],
            "index_release_tag": release_tag, "index_sha256": index["index_sha256"],
            "content_group_count": 2, "projects": receive_projects,
        }
        seal(receive, "receive_report_sha256")
        write_json(self.paths["receive"], receive)
        project_map = {"projects": [{"path": str(self.restored), "project_id": "local-project", "label": "Project"}]}
        write_json(self.paths["project_map"], project_map)
        tasks = [
            {
                "kind": "conversation", "project_index": 1, "project_id": "local-project", "project_label": "Project",
                "target_kind": "project", "original_thread_id": "source-thread-1", "conversation_sha256": self.h1,
                "messages_sha256": "8" * 64, "lineage_id": self.l1, "branch_id": "main", "source_disposition": "active",
                "message_count": 2, "attachment_count": 0,
            },
            {
                "kind": "conversation", "project_index": 2, "project_id": None, "project_label": "Projectless",
                "target_kind": "projectless", "original_thread_id": "source-thread-2", "conversation_sha256": self.h2,
                "messages_sha256": "9" * 64, "lineage_id": self.l2, "branch_id": "main", "source_disposition": "archived",
                "message_count": 3, "attachment_count": 0,
            },
        ]
        catalog = {
            "format": "cross-device-receiving-task-catalog-v2", "migration_id": index["migration_id"],
            "source_device": index["source_device"], "source_computer": index["source_computer"],
            "project_count": 2, "raw_session_count": 2, "user_visible_conversation_count": 2,
            "excluded_internal_session_count": 0, "bootstrap_task_count": 0, "target_receiving_task_count": 2,
            "projects": [
                {"index": 1, "source_project_id": "source-project", "kind": "registered-project", "has_project_payload": True,
                 "registered_project_id": "local-project", "restored_project_root": str(self.restored), "raw_session_count": 1},
                {"index": 2, "source_project_id": "projectless-group", "kind": "projectless-conversation-group", "has_project_payload": False,
                 "registered_project_id": None, "restored_project_root": None, "raw_session_count": 1},
            ],
            "receiving_tasks": tasks, "excluded_sessions": [],
        }
        seal(catalog, "catalog_sha256")
        write_json(self.paths["catalog"], catalog)
        empty_ledger = {"format": ledger_module.LEDGER_FORMAT_V2, "entries": {}}
        decisions = []
        for task in tasks:
            key = ledger_module.lineage_source_key(task["lineage_id"], "main")
            decisions.append({
                **task, "source_key": key, "thread_title": task["original_thread_id"],
                "origin_computer": index["source_computer"], "origin_instance_id": "a",
                "origin_thread_id": task["original_thread_id"], "source_instance_id": "a",
                "receiver_disposition": task["source_disposition"], "receiving_thread_id": None,
                "action": "register-new", "reason": "new",
            })
        plan = {
            "format": ledger_module.PLAN_FORMAT, "source_device": index["source_device"], "source_computer": index["source_computer"],
            "catalog_sha256": catalog["catalog_sha256"], "receiver_state_sha256": "c" * 64,
            "receiver_state_captured_at_utc": "2026-10-15T00:00:00+00:00",
            "ledger_sha256": ledger_module.ledger_sha256(empty_ledger), "decisions": decisions, "source_removed": [],
        }
        seal(plan, "plan_sha256")
        write_json(self.paths["plan"], plan)
        queue_tasks = []
        for number, (task, decision) in enumerate(zip(tasks, decisions), 1):
            receipt_marker = "[cross-device-receipt:" + queue_module.digest({
                "source_key": decision["source_key"], "conversation_sha256": task["conversation_sha256"],
            }) + "]"
            receiving_thread_id = f"receiver-{number}"
            queue_tasks.append({
                **task, "operation_key": decision["source_key"], "action": "register-new",
                "title": "test", "receipt_marker": receipt_marker, "initial_prompt": receipt_marker + "\ntest",
                "target_project_id": task["project_id"], "status": "registered-lazy",
                "restored_project_root": str(self.restored) if task["target_kind"] == "project" else None,
                "receiving_thread_id": receiving_thread_id, "expected_disposition": task["source_disposition"],
                "claim_token": None, "attempts": 1,
                "last_observation": {
                    "status": "registered-lazy", "destination_codex_home": str(self.home),
                    "receiving_thread_id": receiving_thread_id, "receipt_marker": receipt_marker,
                    "receiver_disposition": task["source_disposition"], "target_kind": task["target_kind"],
                    "target_project_id": task["project_id"],
                },
            })
        queue = {
            "format": queue_module.FORMAT, "catalog_sha256": catalog["catalog_sha256"], "plan_sha256": plan["plan_sha256"],
            "destination_codex_home": str(self.home), "destination_instance_id": "c",
            "created_utc": "2026-10-15T00:00:00+00:00", "max_batch_size": 8, "tasks": queue_tasks,
        }
        seal(queue, "queue_sha256")
        write_json(self.paths["queue"], queue)
        write_json(self.home / ".codex-global-state.json", {
            "local-projects": {"local-project": {"rootPaths": [str(self.restored)]}},
            "thread-project-assignments": {
                "receiver-1": {"projectKind": "local", "projectId": "local-project"},
            },
            "projectless-thread-ids": ["receiver-2"],
        })
        database_path = self.home / "state_5.sqlite"
        database_path.unlink(missing_ok=True)
        database = sqlite3.connect(database_path)
        try:
            database.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, source TEXT, archived INTEGER, project_id TEXT, "
                "first_user_message TEXT, has_user_event INTEGER)"
            )
            for task in queue_tasks:
                database.execute(
                    "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        task["receiving_thread_id"], "app-server",
                        1 if task["expected_disposition"] == "archived" else 0,
                        task["target_project_id"], task["initial_prompt"], 1,
                    ),
                )
            database.commit()
        finally:
            database.close()
        proof = app_proof_module.build_proof(self.paths["queue"], self.home)
        write_json(self.paths["app_proof"], proof)
        results = queue_module.ledger_results(self.paths["queue"], self.home)
        write_json(self.paths["results"], results)
        write_json(self.paths["ledger"], empty_ledger)
        env = dict(os.environ)
        env["CODEX_HOME"] = str(self.home)
        completed = subprocess.run([
            sys.executable, str(ROOT / "scripts" / "conversation-sync-ledger.py"), "commit",
            "--plan", str(self.paths["plan"]), "--ledger", str(self.paths["ledger"]),
            "--result", str(self.paths["results"]), "--queue", str(self.paths["queue"]),
            "--app-proof", str(self.paths["app_proof"]), "--receipt", str(self.paths["commit_receipt"]),
            "--destination-instance-id", "c",
        ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, env=env)
        if completed.returncode != 0:
            raise AssertionError(completed.stdout + completed.stderr)

    def args(self, output=None):
        return argparse.Namespace(
            registry=str(self.registry), device_index=str(self.paths["index"]), receive_report=str(self.paths["receive"]),
            project_map=str(self.paths["project_map"]), catalog=str(self.paths["catalog"]), plan=str(self.paths["plan"]),
            queue=str(self.paths["queue"]), results=str(self.paths["results"]), ledger=str(self.paths["ledger"]),
            app_proof=str(self.paths["app_proof"]), commit_receipt=str(self.paths["commit_receipt"]),
            output=str(output or self.paths["acceptance"]),
        )


class ReceiveAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="v43-acceptance-")
        self.fixture = AcceptanceFixture(Path(self.temp.name))
        self.environment = mock.patch.dict(os.environ, {
            "CODEX_HOME": str(self.fixture.home), "COMPUTERNAME": "WORKSTATION-B", "USERPROFILE": str(self.fixture.profile),
        })
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def test_complete_receive_generates_hash_bound_acceptance(self):
        first = acceptance.build_acceptance(self.fixture.args())
        self.assertEqual(first["status"], "receive-complete-verified")
        self.assertEqual(first["acceptance_sha256"], acceptance.digest(acceptance.unsigned(first, "acceptance_sha256")))
        self.assertEqual(first["counts"]["registered_projects"], 1)
        self.assertEqual(first["counts"]["source_archived"], 1)
        self.assertEqual(first["counts"]["failed"], 0)

    def test_exact_committed_receipt_retry_is_idempotent(self):
        environment = dict(os.environ)
        environment["CODEX_HOME"] = str(self.fixture.home)
        ledger_before = self.fixture.paths["ledger"].read_bytes()
        receipt_before = self.fixture.paths["commit_receipt"].read_bytes()
        completed = subprocess.run([
            sys.executable, str(ROOT / "scripts" / "conversation-sync-ledger.py"), "commit",
            "--plan", str(self.fixture.paths["plan"]), "--ledger", str(self.fixture.paths["ledger"]),
            "--result", str(self.fixture.paths["results"]), "--queue", str(self.fixture.paths["queue"]),
            "--app-proof", str(self.fixture.paths["app_proof"]),
            "--receipt", str(self.fixture.paths["commit_receipt"]), "--destination-instance-id", "c",
        ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, env=environment)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["idempotent"])
        self.assertEqual(self.fixture.paths["ledger"].read_bytes(), ledger_before)
        self.assertEqual(self.fixture.paths["commit_receipt"].read_bytes(), receipt_before)

    def test_committed_receipt_fast_path_rejects_tampered_plan_content(self):
        plan = json.loads(self.fixture.paths["plan"].read_text(encoding="utf-8"))
        plan["source_device"] = "tampered-without-resealing"
        write_json(self.fixture.paths["plan"], plan)
        environment = dict(os.environ)
        environment["CODEX_HOME"] = str(self.fixture.home)
        completed = subprocess.run([
            sys.executable, str(ROOT / "scripts" / "conversation-sync-ledger.py"), "commit",
            "--plan", str(self.fixture.paths["plan"]), "--ledger", str(self.fixture.paths["ledger"]),
            "--result", str(self.fixture.paths["results"]), "--queue", str(self.fixture.paths["queue"]),
            "--app-proof", str(self.fixture.paths["app_proof"]),
            "--receipt", str(self.fixture.paths["commit_receipt"]), "--destination-instance-id", "c",
        ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, env=environment)
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("plan_sha256", completed.stderr)

    def test_committed_receipt_fast_path_rejects_resealed_forged_summary(self):
        receipt = json.loads(self.fixture.paths["commit_receipt"].read_text(encoding="utf-8"))
        receipt["updated"] = receipt["updated"] + 100
        seal(receipt, "commit_receipt_sha256")
        write_json(self.fixture.paths["commit_receipt"], receipt)
        environment = dict(os.environ)
        environment["CODEX_HOME"] = str(self.fixture.home)
        completed = subprocess.run([
            sys.executable, str(ROOT / "scripts" / "conversation-sync-ledger.py"), "commit",
            "--plan", str(self.fixture.paths["plan"]), "--ledger", str(self.fixture.paths["ledger"]),
            "--result", str(self.fixture.paths["results"]), "--queue", str(self.fixture.paths["queue"]),
            "--app-proof", str(self.fixture.paths["app_proof"]),
            "--receipt", str(self.fixture.paths["commit_receipt"]), "--destination-instance-id", "c",
        ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, env=environment)
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("summary", completed.stderr)

    def test_final_acceptance_rejects_resealed_forged_commit_receipt_summary(self):
        receipt = json.loads(self.fixture.paths["commit_receipt"].read_text(encoding="utf-8"))
        receipt["updated"] = receipt["updated"] + 100
        seal(receipt, "commit_receipt_sha256")
        write_json(self.fixture.paths["commit_receipt"], receipt)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "summary"):
            acceptance.build_acceptance(self.fixture.args())

    def test_final_acceptance_rejects_resealed_commit_timestamp_drift(self):
        receipt = json.loads(self.fixture.paths["commit_receipt"].read_text(encoding="utf-8"))
        receipt["committed_utc"] = "2099-01-01T00:00:00+00:00"
        seal(receipt, "commit_receipt_sha256")
        write_json(self.fixture.paths["commit_receipt"], receipt)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "timestamp"):
            acceptance.build_acceptance(self.fixture.args())

    def test_final_acceptance_rejects_resealed_ledger_snapshot_metadata_drift(self):
        ledger = json.loads(self.fixture.paths["ledger"].read_text(encoding="utf-8"))
        source_key = next(iter(ledger["entries"]))
        ledger["entries"][source_key]["message_count"] += 100
        write_json(self.fixture.paths["ledger"], ledger)
        receipt = json.loads(self.fixture.paths["commit_receipt"].read_text(encoding="utf-8"))
        receipt["ledger_sha256_after"] = ledger_module.ledger_sha256(ledger)
        seal(receipt, "commit_receipt_sha256")
        write_json(self.fixture.paths["commit_receipt"], receipt)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "message_count"):
            acceptance.build_acceptance(self.fixture.args())

    def test_final_acceptance_rejects_resealed_applied_entry_with_fake_pending_state(self):
        ledger = json.loads(self.fixture.paths["ledger"].read_text(encoding="utf-8"))
        source_key = next(iter(ledger["entries"]))
        ledger["entries"][source_key]["pending_conversation_sha256"] = "f" * 64
        write_json(self.fixture.paths["ledger"], ledger)
        receipt = json.loads(self.fixture.paths["commit_receipt"].read_text(encoding="utf-8"))
        receipt["ledger_sha256_after"] = ledger_module.ledger_sha256(ledger)
        seal(receipt, "commit_receipt_sha256")
        write_json(self.fixture.paths["commit_receipt"], receipt)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "pending"):
            acceptance.build_acceptance(self.fixture.args())

    def test_idempotent_fast_path_rejects_resealed_commit_timestamp_drift(self):
        receipt = json.loads(self.fixture.paths["commit_receipt"].read_text(encoding="utf-8"))
        receipt["committed_utc"] = "2099-01-01T00:00:00+00:00"
        seal(receipt, "commit_receipt_sha256")
        write_json(self.fixture.paths["commit_receipt"], receipt)
        environment = dict(os.environ)
        environment["CODEX_HOME"] = str(self.fixture.home)
        completed = subprocess.run([
            sys.executable, str(ROOT / "scripts" / "conversation-sync-ledger.py"), "commit",
            "--plan", str(self.fixture.paths["plan"]), "--ledger", str(self.fixture.paths["ledger"]),
            "--result", str(self.fixture.paths["results"]), "--queue", str(self.fixture.paths["queue"]),
            "--app-proof", str(self.fixture.paths["app_proof"]),
            "--receipt", str(self.fixture.paths["commit_receipt"]), "--destination-instance-id", "c",
        ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, env=environment)
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("timestamp", completed.stderr)

    def test_idempotent_fast_path_rejects_resealed_results_not_derived_from_queue(self):
        results = json.loads(self.fixture.paths["results"].read_text(encoding="utf-8"))
        results["results"][0]["conversation_sha256"] = "e" * 64
        seal(results, "results_sha256")
        write_json(self.fixture.paths["results"], results)
        receipt = json.loads(self.fixture.paths["commit_receipt"].read_text(encoding="utf-8"))
        receipt["results_sha256"] = results["results_sha256"]
        seal(receipt, "commit_receipt_sha256")
        write_json(self.fixture.paths["commit_receipt"], receipt)
        environment = dict(os.environ)
        environment["CODEX_HOME"] = str(self.fixture.home)
        completed = subprocess.run([
            sys.executable, str(ROOT / "scripts" / "conversation-sync-ledger.py"), "commit",
            "--plan", str(self.fixture.paths["plan"]), "--ledger", str(self.fixture.paths["ledger"]),
            "--result", str(self.fixture.paths["results"]), "--queue", str(self.fixture.paths["queue"]),
            "--app-proof", str(self.fixture.paths["app_proof"]),
            "--receipt", str(self.fixture.paths["commit_receipt"]), "--destination-instance-id", "c",
        ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, env=environment)
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("completed queue", completed.stderr)

    def test_receipt_first_ledger_write_failure_recovers_exactly_once(self):
        empty = {"format": ledger_module.LEDGER_FORMAT_V2, "entries": {}}
        write_json(self.fixture.paths["ledger"], empty)
        self.fixture.paths["commit_receipt"].unlink()
        argv = [
            "conversation-sync-ledger.py", "commit",
            "--plan", str(self.fixture.paths["plan"]), "--ledger", str(self.fixture.paths["ledger"]),
            "--result", str(self.fixture.paths["results"]), "--queue", str(self.fixture.paths["queue"]),
            "--app-proof", str(self.fixture.paths["app_proof"]),
            "--receipt", str(self.fixture.paths["commit_receipt"]), "--destination-instance-id", "c",
        ]
        original_write = ledger_module.write_json
        failed = {"value": False}

        def fail_ledger_once(path, value):
            if Path(path).resolve() == self.fixture.paths["ledger"].resolve() and not failed["value"]:
                failed["value"] = True
                raise OSError("synthetic ledger atomic replacement failure")
            return original_write(path, value)

        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.fixture.home)}), mock.patch.object(
            sys, "argv", argv,
        ), mock.patch.object(ledger_module, "write_json", side_effect=fail_ledger_once):
            self.assertEqual(ledger_module.main(), 1)
        receipt_before_retry = self.fixture.paths["commit_receipt"].read_bytes()
        self.assertEqual(json.loads(self.fixture.paths["ledger"].read_text(encoding="utf-8")), empty)

        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.fixture.home)}), mock.patch.object(
            sys, "argv", argv,
        ):
            self.assertEqual(ledger_module.main(), 0)
        self.assertEqual(self.fixture.paths["commit_receipt"].read_bytes(), receipt_before_retry)
        receipt = json.loads(receipt_before_retry)
        ledger = json.loads(self.fixture.paths["ledger"].read_text(encoding="utf-8"))
        self.assertEqual(receipt["ledger_sha256_after"], ledger_module.ledger_sha256(ledger))

    def test_fake_or_missing_codex_task_blocks_commit_without_polluting_ledger(self):
        empty = {"format": ledger_module.LEDGER_FORMAT_V2, "entries": {}}
        write_json(self.fixture.paths["ledger"], empty)
        database = sqlite3.connect(self.fixture.home / "state_5.sqlite")
        database.execute("DELETE FROM threads WHERE id = ?", ("receiver-1",))
        database.commit()
        database.close()
        before = self.fixture.paths["ledger"].read_bytes()
        environment = dict(os.environ)
        environment["CODEX_HOME"] = str(self.fixture.home)
        completed = subprocess.run([
            sys.executable, str(ROOT / "scripts" / "conversation-sync-ledger.py"), "commit",
            "--plan", str(self.fixture.paths["plan"]), "--ledger", str(self.fixture.paths["ledger"]),
            "--result", str(self.fixture.paths["results"]), "--queue", str(self.fixture.paths["queue"]),
            "--app-proof", str(self.fixture.paths["app_proof"]), "--receipt", str(self.fixture.paths["commit_receipt"]),
            "--destination-instance-id", "c",
        ], capture_output=True, text=True, encoding="utf-8", errors="replace", env=environment)
        self.assertNotEqual(completed.returncode, 0)
        self.assertRegex(completed.stderr, "does not exist|live Codex task state changed")
        self.assertEqual(self.fixture.paths["ledger"].read_bytes(), before)

    def test_queue_bound_results_cannot_be_resealed_for_another_queue(self):
        empty = {"format": ledger_module.LEDGER_FORMAT_V2, "entries": {}}
        write_json(self.fixture.paths["ledger"], empty)
        results = json.loads(self.fixture.paths["results"].read_text(encoding="utf-8"))
        results["queue_sha256"] = "0" * 64
        seal(results, "results_sha256")
        write_json(self.fixture.paths["results"], results)
        before = self.fixture.paths["ledger"].read_bytes()
        environment = dict(os.environ)
        environment["CODEX_HOME"] = str(self.fixture.home)
        completed = subprocess.run([
            sys.executable, str(ROOT / "scripts" / "conversation-sync-ledger.py"), "commit",
            "--plan", str(self.fixture.paths["plan"]), "--ledger", str(self.fixture.paths["ledger"]),
            "--result", str(self.fixture.paths["results"]), "--queue", str(self.fixture.paths["queue"]),
            "--app-proof", str(self.fixture.paths["app_proof"]),
        ], capture_output=True, text=True, encoding="utf-8", errors="replace", env=environment)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("exact plan and receive queue", completed.stderr)
        self.assertEqual(self.fixture.paths["ledger"].read_bytes(), before)

    def test_released_lock_evidence_allows_idempotent_final_report_recovery(self):
        first = acceptance.build_acceptance(self.fixture.args())
        self.fixture.paths["acceptance"].unlink()
        second = acceptance.build_acceptance(self.fixture.args())
        self.assertEqual(second["status"], "receive-complete-verified")
        self.assertTrue(second["machine_lock_release"]["already_released"])
        self.assertEqual(first["prepared_acceptance_sha256"], second["prepared_acceptance_sha256"])

    def test_final_report_atomic_write_failure_does_not_poison_retry(self):
        args = self.fixture.args()
        argv = [
            "receive-final-acceptance.py", "--registry", args.registry,
            "--device-index", args.device_index, "--receive-report", args.receive_report,
            "--project-map", args.project_map, "--catalog", args.catalog, "--plan", args.plan,
            "--queue", args.queue, "--results", args.results, "--app-proof", args.app_proof,
            "--ledger", args.ledger, "--commit-receipt", args.commit_receipt, "--output", args.output,
        ]
        original_replace = os.replace
        failed = {"value": False}

        def fail_final_once(source, destination):
            if Path(destination).resolve() == Path(args.output).resolve() and not failed["value"]:
                failed["value"] = True
                raise OSError("synthetic final report atomic write failure")
            return original_replace(source, destination)

        with mock.patch.object(sys, "argv", argv), mock.patch("os.replace", side_effect=fail_final_once):
            self.assertEqual(acceptance.main(), 2)
        prepared = Path(args.output + ".prepared")
        self.assertTrue(prepared.is_file())
        self.assertFalse(Path(args.output).exists())
        acceptance.write_blocked(Path(args.output), RuntimeError("legacy poisoned output"))
        with mock.patch.object(sys, "argv", argv):
            self.assertEqual(acceptance.main(), 0)
        final = json.loads(Path(args.output).read_text(encoding="utf-8"))
        self.assertEqual(final["status"], "receive-complete-verified")
        self.assertTrue(final["machine_lock_release"]["already_released"])

    def test_pending_uncertain_and_failed_queue_states_block_completion(self):
        for state in ("pending", "in-flight", "uncertain", "failed", "blocked-divergence"):
            with self.subTest(state=state):
                queue = json.loads(self.fixture.paths["queue"].read_text(encoding="utf-8"))
                queue["tasks"][0]["status"] = state
                seal(queue, "queue_sha256")
                write_json(self.fixture.paths["queue"], queue)
                with self.assertRaisesRegex(acceptance.AcceptanceError, "unfinished"):
                    acceptance.build_acceptance(self.fixture.args())
                self.fixture.build()

    def test_projectless_directory_cannot_be_registered_as_project(self):
        fake_root = self.fixture.root / "projectless-directory"
        fake_root.mkdir()
        receive = json.loads(self.fixture.paths["receive"].read_text(encoding="utf-8"))
        receive["projects"][1]["restored_project_root"] = str(fake_root)
        seal(receive, "receive_report_sha256")
        write_json(self.fixture.paths["receive"], receive)
        project_map = json.loads(self.fixture.paths["project_map"].read_text(encoding="utf-8"))
        project_map["projects"].append({"path": str(fake_root), "project_id": "fake-project", "label": "Wrong"})
        write_json(self.fixture.paths["project_map"], project_map)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "Projectless|restored root"):
            acceptance.build_acceptance(self.fixture.args())

    def test_queue_target_cannot_be_changed_to_another_project(self):
        queue = json.loads(self.fixture.paths["queue"].read_text(encoding="utf-8"))
        queue["tasks"][0]["target_project_id"] = "wrong-project"
        seal(queue, "queue_sha256")
        write_json(self.fixture.paths["queue"], queue)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "target binding"):
            acceptance.build_acceptance(self.fixture.args())

    def test_app_write_cannot_be_faked_as_skipped_or_without_observation(self):
        queue = json.loads(self.fixture.paths["queue"].read_text(encoding="utf-8"))
        queue["tasks"][0]["status"] = "skipped"
        queue["tasks"][0]["last_observation"] = None
        seal(queue, "queue_sha256")
        write_json(self.fixture.paths["queue"], queue)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "app-write"):
            acceptance.build_acceptance(self.fixture.args())

        self.fixture.build()
        queue = json.loads(self.fixture.paths["queue"].read_text(encoding="utf-8"))
        queue["tasks"][0].pop("last_observation")
        seal(queue, "queue_sha256")
        write_json(self.fixture.paths["queue"], queue)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "app-write"):
            acceptance.build_acceptance(self.fixture.args())

    def test_conversations_cannot_be_swapped_between_project_and_projectless_groups(self):
        catalog = json.loads(self.fixture.paths["catalog"].read_text(encoding="utf-8"))
        first, second = catalog["receiving_tasks"]
        first.update(project_index=2, project_id=None, project_label="Projectless", target_kind="projectless")
        second.update(project_index=1, project_id="local-project", project_label="Project", target_kind="project")
        seal(catalog, "catalog_sha256")
        write_json(self.fixture.paths["catalog"], catalog)

        plan = json.loads(self.fixture.paths["plan"].read_text(encoding="utf-8"))
        plan["catalog_sha256"] = catalog["catalog_sha256"]
        seal(plan, "plan_sha256")
        write_json(self.fixture.paths["plan"], plan)
        queue = json.loads(self.fixture.paths["queue"].read_text(encoding="utf-8"))
        queue["catalog_sha256"] = catalog["catalog_sha256"]
        queue["plan_sha256"] = plan["plan_sha256"]
        seal(queue, "queue_sha256")
        write_json(self.fixture.paths["queue"], queue)
        results = json.loads(self.fixture.paths["results"].read_text(encoding="utf-8"))
        results["plan_sha256"] = plan["plan_sha256"]
        write_json(self.fixture.paths["results"], results)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "conversation inventory"):
            acceptance.build_acceptance(self.fixture.args())

    def test_plan_and_queue_cannot_omit_a_catalog_task(self):
        plan = json.loads(self.fixture.paths["plan"].read_text(encoding="utf-8"))
        plan["decisions"] = plan["decisions"][:1]
        seal(plan, "plan_sha256")
        write_json(self.fixture.paths["plan"], plan)
        queue = json.loads(self.fixture.paths["queue"].read_text(encoding="utf-8"))
        queue["tasks"] = queue["tasks"][:1]
        queue["plan_sha256"] = plan["plan_sha256"]
        seal(queue, "queue_sha256")
        write_json(self.fixture.paths["queue"], queue)
        results = json.loads(self.fixture.paths["results"].read_text(encoding="utf-8"))
        results["plan_sha256"] = plan["plan_sha256"]
        write_json(self.fixture.paths["results"], results)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "task inventories differ"):
            acceptance.build_acceptance(self.fixture.args())

    def test_restored_project_tree_cannot_change_after_installation(self):
        (self.fixture.restored / "local-edit.txt").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(acceptance.AcceptanceError, "changed after verified installation"):
            acceptance.build_acceptance(self.fixture.args())

    def test_restored_project_tree_cannot_disappear_after_installation(self):
        self.fixture.restored.rmdir()
        with self.assertRaisesRegex(acceptance.AcceptanceError, "path is missing|root is missing"):
            acceptance.build_acceptance(self.fixture.args())

    def test_release_repository_and_required_skill_bindings_cannot_drift(self):
        receive = json.loads(self.fixture.paths["receive"].read_text(encoding="utf-8"))
        receive["index_release_tag"] = "different-release"
        seal(receive, "receive_report_sha256")
        write_json(self.fixture.paths["receive"], receive)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "selected Release"):
            acceptance.build_acceptance(self.fixture.args())

        self.fixture.build()
        receive = json.loads(self.fixture.paths["receive"].read_text(encoding="utf-8"))
        receive["repository"] = "attacker/other-repository"
        seal(receive, "receive_report_sha256")
        write_json(self.fixture.paths["receive"], receive)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "canonical private repository"):
            acceptance.build_acceptance(self.fixture.args())

        self.fixture.build()
        receive = json.loads(self.fixture.paths["receive"].read_text(encoding="utf-8"))
        receive["required_skill_version"] = "20261014-v43"
        seal(receive, "receive_report_sha256")
        write_json(self.fixture.paths["receive"], receive)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "required Skill version"):
            acceptance.build_acceptance(self.fixture.args())

    def test_changed_device_label_or_missing_download_lock_blocks_acceptance(self):
        write_json(self.fixture.home / "cross-device-project-sync-device.json", {"device_label": "A设备"})
        with self.assertRaisesRegex(acceptance.AcceptanceError, "device label changed"):
            acceptance.build_acceptance(self.fixture.args())

        write_json(
            self.fixture.home / "cross-device-project-sync-device.json",
            {"device_label": "B设备", "computer_name": "WORKSTATION-B"},
        )
        self.fixture.build()
        (self.fixture.state / "operation.lock.json").unlink()
        with self.assertRaisesRegex(Exception, "lock.*missing|evidence.*missing"):
            acceptance.build_acceptance(self.fixture.args())

    def test_acceptance_output_cannot_overwrite_the_receiver_ledger(self):
        before = self.fixture.paths["ledger"].read_bytes()
        args = self.fixture.args(self.fixture.paths["ledger"])
        command = [
            sys.executable, str(ROOT / "scripts" / "receive-final-acceptance.py"),
            "--registry", args.registry, "--device-index", args.device_index,
            "--receive-report", args.receive_report, "--project-map", args.project_map,
            "--catalog", args.catalog, "--plan", args.plan, "--queue", args.queue,
            "--results", args.results, "--app-proof", args.app_proof, "--ledger", args.ledger,
            "--commit-receipt", args.commit_receipt, "--output", args.output,
        ]
        environment = dict(os.environ)
        environment.update({
            "CODEX_HOME": str(self.fixture.home), "COMPUTERNAME": "WORKSTATION-B",
            "USERPROFILE": str(self.fixture.profile),
        })
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace", env=environment,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("new path distinct", completed.stderr)
        self.assertEqual(self.fixture.paths["ledger"].read_bytes(), before)

    def test_commit_receipt_cannot_alias_any_commit_input_or_ledger_lock(self):
        aliases = {
            "plan": self.fixture.paths["plan"], "ledger": self.fixture.paths["ledger"],
            "result": self.fixture.paths["results"], "queue": self.fixture.paths["queue"],
            "app-proof": self.fixture.paths["app_proof"],
            "ledger-lock": Path(str(self.fixture.paths["ledger"]) + ".lock"),
        }
        environment = dict(os.environ)
        environment["CODEX_HOME"] = str(self.fixture.home)
        for label, alias in aliases.items():
            with self.subTest(alias=label):
                before = {name: path.read_bytes() for name, path in aliases.items() if path.exists()}
                completed = subprocess.run([
                    sys.executable, str(ROOT / "scripts" / "conversation-sync-ledger.py"), "commit",
                    "--plan", str(self.fixture.paths["plan"]), "--ledger", str(self.fixture.paths["ledger"]),
                    "--result", str(self.fixture.paths["results"]), "--queue", str(self.fixture.paths["queue"]),
                    "--app-proof", str(self.fixture.paths["app_proof"]), "--receipt", str(alias),
                    "--destination-instance-id", "c",
                ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, env=environment)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("pairwise distinct", completed.stderr)
                self.assertEqual(
                    {name: path.read_bytes() for name, path in aliases.items() if path.exists()}, before,
                )

    def test_project_map_rejects_empty_and_relative_paths(self):
        for value in ("", "relative-project"):
            with self.subTest(value=value), self.assertRaisesRegex(acceptance.AcceptanceError, "empty path|absolute"):
                acceptance.project_map_by_path({"projects": [{"path": value, "project_id": "p"}]})

    def test_catalog_plan_and_commit_receipt_hash_mismatches_block(self):
        catalog = json.loads(self.fixture.paths["catalog"].read_text(encoding="utf-8"))
        catalog["source_device"] = "tampered"
        write_json(self.fixture.paths["catalog"], catalog)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "catalog_sha256"):
            acceptance.build_acceptance(self.fixture.args())
        self.fixture.build()
        receipt = json.loads(self.fixture.paths["commit_receipt"].read_text(encoding="utf-8"))
        receipt["plan_sha256"] = "0" * 64
        seal(receipt, "commit_receipt_sha256")
        write_json(self.fixture.paths["commit_receipt"], receipt)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "plan_sha256"):
            acceptance.build_acceptance(self.fixture.args())

    def test_another_calling_account_is_rejected(self):
        other = self.fixture.root / "other-home"
        make_home(other, "B设备", "WORKSTATION-B")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(other), "COMPUTERNAME": "WORKSTATION-B"}):
            with self.assertRaisesRegex(Exception, "allow-listed|calling CODEX_HOME|inside"):
                acceptance.build_acceptance(self.fixture.args(other / "acceptance.json"))


if __name__ == "__main__":
    unittest.main()
