import importlib.util
import json
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock


SKILL_ROOT = pathlib.Path(__file__).resolve().parents[1]
INCREMENTAL = SKILL_ROOT / "scripts" / "incremental-device-sync.py"
PACK = SKILL_ROOT / "scripts" / "bulk-conversation-pack.py"
SNAPSHOT = SKILL_ROOT / "scripts" / "snapshot-conversation-sources.py"
AUDIT = SKILL_ROOT / "scripts" / "audit-catalog-secrets.py"
BROWSER_EXCLUSIONS = SKILL_ROOT / "scripts" / "add-protected-browser-exclusions.py"
BUILD_DISCOVERY = SKILL_ROOT / "scripts" / "build-local-discovery.py"
PRUNE = SKILL_ROOT / "scripts" / "prune-empty-conversation-snapshots.py"
RUN_UPLOAD = SKILL_ROOT / "scripts" / "run-incremental-upload.ps1"


class IncrementalDeviceSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="incremental-sync-test-")
        self.base = pathlib.Path(self.temp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        (self.project / "file.txt").write_text("alpha", encoding="utf-8")
        self.thread_id = "01a00000-1111-7222-8333-555555555555"
        self.session = self.base / f"rollout-test-{self.thread_id}.jsonl"
        self.write_session("first request", "first answer")

    def tearDown(self):
        self.temp.cleanup()

    def write_session(self, user, assistant):
        records = [
            {"timestamp":"2026-09-01T00:00:00Z","type":"session_meta","payload":{"id":self.thread_id,"cwd":str(self.project)}},
            {"timestamp":"2026-09-01T00:00:01Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":user}]}},
            {"timestamp":"2026-09-01T00:00:02Z","type":"response_item","payload":{"type":"message","role":"assistant","phase":"final_answer","content":[{"type":"output_text","text":assistant}]}},
        ]
        self.session.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")

    def discovery(self, path):
        payload = {
            "source_device":"A设备","source_computer":"WORKSTATION-A",
            "projects":[{"project_id":"project-001","label":"Test","source_path":str(self.project),"exclude_relative_paths":[],"conversations":[{"thread_id":self.thread_id,"thread_title":"Test task","session_log":str(self.session)}],"conversation_inventory":{"complete":True,"source":"test","expected_thread_ids":[self.thread_id]}}],
            "cloud_managed_projects":[],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def run_cmd(self, *args):
        completed = subprocess.run(
            [sys.executable, *map(str, args)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
            timeout=60,
        )
        if completed.returncode:
            self.fail(completed.stdout + completed.stderr)
        return json.loads(completed.stdout)

    def run_secret_audit(self, catalog, report):
        completed = subprocess.run(
            [sys.executable, str(AUDIT), "--catalog", str(catalog), "--output", str(report), "--git", "git"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        )
        return completed, json.loads(report.read_text(encoding="utf-8"))

    def test_project_and_conversation_change_independently(self):
        discovery = self.base / "discovery.json"
        self.discovery(discovery)
        first = self.base / "first.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", first)
        first_payload = json.loads(first.read_text(encoding="utf-8"))
        first_payload["projects"][0]["project_release"] = {"tag":"project-old","manifest_sha256":"1"*64}
        first_payload["projects"][0]["conversation_release"] = {"tag":"conversation-old","manifest_sha256":"2"*64}
        unsigned = dict(first_payload)
        unsigned.pop("catalog_sha256", None)
        module_spec = importlib.util.spec_from_file_location("incremental", INCREMENTAL)
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        first_payload["catalog_sha256"] = module.hashlib.sha256(module.canonical_bytes(unsigned)).hexdigest()
        first.write_bytes(module.canonical_bytes(first_payload))

        self.write_session("first request", "changed answer")
        second = self.base / "second.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", second)
        plan = self.base / "plan.json"
        result = self.run_cmd(INCREMENTAL, "plan", "--current", second, "--previous", first, "--output", plan)
        self.assertEqual(result["project_reuse_count"], 1)
        self.assertEqual(result["conversation_upload_count"], 1)

        (self.project / "file.txt").write_text("beta", encoding="utf-8")
        third = self.base / "third.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", third)
        third_plan = self.base / "third-plan.json"
        result = self.run_cmd(INCREMENTAL, "plan", "--current", third, "--previous", first, "--output", third_plan)
        self.assertEqual(result["project_upload_count"], 1)

    def test_bulk_pack_round_trip_verification(self):
        discovery = self.base / "discovery.json"
        self.discovery(discovery)
        catalog = self.base / "catalog.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", catalog)
        pack = self.base / "pack"
        built = self.run_cmd(PACK, "build", "--catalog", catalog, "--project-id", "project-001", "--output", pack)
        verified = self.run_cmd(PACK, "verify", "--pack", pack)
        self.assertTrue(verified["verified"])
        self.assertEqual(built["conversation_count"], 1)
        self.assertEqual(verified["conversation_count"], 1)

        bindings = self.base / "bindings.json"
        bindings.write_text(json.dumps({
            "migration_id":"A-device-20260929-test",
            "skill_release":{"tag":"skill-v26-test","manifest_sha256":"3"*64,"reused":False},
            "projects":[{
                "project_id":"project-001",
                "project_release":{"tag":"project-test","manifest_sha256":"1"*64,"reused":False},
                "conversation_release":{"tag":"conversation-test","manifest_sha256":"2"*64,"reused":False},
            }],
        }), encoding="utf-8")
        index = self.base / "index.json"
        handoff = self.base / "RECEIVER_READ_FIRST.md"
        made = self.run_cmd(INCREMENTAL, "build-index", "--catalog", catalog, "--bindings", bindings, "--output", index, "--handoff", handoff)
        checked = self.run_cmd(INCREMENTAL, "verify-index", "--index", index)
        self.assertEqual(made["index_sha256"], checked["index_sha256"])
        index_payload = json.loads(index.read_text(encoding="utf-8"))
        self.assertEqual(
            index_payload["projects"][0]["conversations"][0]["messages_sha256"],
            json.loads(catalog.read_text(encoding="utf-8"))["projects"][0]["conversations"][0]["messages_sha256"],
        )
        pack_manifest = json.loads((pack / "CONVERSATION_PACK_MANIFEST.json").read_text(encoding="utf-8"))
        self.assertIn("messages_sha256", pack_manifest["conversations"][0])
        self.assertIn("复用", handoff.read_text(encoding="utf-8"))
        self.assertIn("没有作为本地归档上传的内容", handoff.read_text(encoding="utf-8"))

    def test_bulk_pack_failure_leaves_no_final_or_staging_directory(self):
        discovery = self.base / "transaction-discovery.json"
        self.discovery(discovery)
        catalog = self.base / "transaction-catalog.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", catalog)
        self.write_session("changed after scan", "new answer")
        pack = self.base / "transaction-pack"
        completed = subprocess.run(
            [sys.executable, str(PACK), "build", "--catalog", str(catalog), "--project-id", "project-001", "--output", str(pack)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(pack.exists())
        self.assertFalse(any(self.base.glob(".transaction-pack.building-*")))

    def test_live_conversation_is_frozen_before_catalog_and_pack(self):
        discovery = self.base / "discovery.json"
        self.discovery(discovery)
        snapshot_root = self.base / "snapshots"
        frozen_discovery = self.base / "frozen-discovery.json"
        result = self.run_cmd(SNAPSHOT, "--discovery", discovery, "--snapshot-root", snapshot_root, "--output-discovery", frozen_discovery)
        self.assertEqual(result["conversation_count"], 1)
        self.write_session("later request", "later answer")
        catalog = self.base / "frozen-catalog.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", frozen_discovery, "--output", catalog)
        pack = self.base / "frozen-pack"
        self.run_cmd(PACK, "build", "--catalog", catalog, "--project-id", "project-001", "--output", pack)
        with zipfile.ZipFile(pack / "CONVERSATION_PACK.zip") as archive:
            transcript_name = next(name for name in archive.namelist() if name.endswith("FULL_TRANSCRIPT.jsonl"))
            transcript = archive.read(transcript_name).decode("utf-8")
        self.assertIn("first request", transcript)
        self.assertNotIn("later request", transcript)

    def test_failed_snapshot_leaves_no_partial_outputs(self):
        discovery = self.base / "two-conversations.json"
        second_id = "01a00000-1111-7222-8333-777777777777"
        broken = self.base / f"rollout-broken-{second_id}.jsonl"
        broken.write_bytes(b'{"type":"session_meta"}\nnot-json\n')
        payload = {
            "source_device": "A设备", "source_computer": "WORKSTATION-A",
            "projects": [{
                "project_id": "project-001", "label": "Test", "source_path": str(self.project),
                "exclude_relative_paths": [],
                "conversations": [
                    {"thread_id": self.thread_id, "session_log": str(self.session)},
                    {"thread_id": second_id, "session_log": str(broken)},
                ],
            }],
        }
        discovery.write_text(json.dumps(payload), encoding="utf-8")
        snapshot_root = self.base / "failed-snapshots"
        output = self.base / "failed-frozen.json"
        completed = subprocess.run(
            [sys.executable, str(SNAPSHOT), "--discovery", str(discovery), "--snapshot-root", str(snapshot_root), "--output-discovery", str(output)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(snapshot_root.exists())
        self.assertFalse(output.exists())
        self.assertFalse(any(self.base.glob(".failed-snapshots.staging-*")))

    def test_inherited_history_changes_conversation_identity_and_is_packed(self):
        inherited = self.base / "retained-history"
        inherited.mkdir()
        (inherited / "FULL_TRANSCRIPT.jsonl").write_text('{"sequence":1,"role":"user","content":[]}\n', encoding="utf-8")
        discovery = self.base / "inherited-discovery.json"
        self.discovery(discovery)
        payload = json.loads(discovery.read_text(encoding="utf-8"))
        payload["projects"][0]["conversations"][0]["inherited_handoff_paths"] = [str(inherited)]
        discovery.write_text(json.dumps(payload), encoding="utf-8")
        first = self.base / "inherited-first.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", first)
        pack = self.base / "inherited-pack"
        self.run_cmd(PACK, "build", "--catalog", first, "--project-id", "project-001", "--output", pack)
        with zipfile.ZipFile(pack / "CONVERSATION_PACK.zip") as archive:
            self.assertTrue(any("/history/" in name and name.endswith("FULL_TRANSCRIPT.jsonl") for name in archive.namelist()))
        first_hash = json.loads(first.read_text(encoding="utf-8"))["projects"][0]["conversation_set_sha256"]
        (inherited / "FULL_TRANSCRIPT.jsonl").write_text('{"sequence":1,"role":"user","content":[{"type":"text","text":"changed"}]}\n', encoding="utf-8")
        second = self.base / "inherited-second.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", second)
        self.assertNotEqual(first_hash, json.loads(second.read_text(encoding="utf-8"))["projects"][0]["conversation_set_sha256"])

    def test_build_index_rejects_incomplete_conversation_inventory(self):
        discovery = self.base / "incomplete-index-discovery.json"
        discovery.write_text(json.dumps({
            "source_device": "A设备", "source_computer": "WORKSTATION-A",
            "projects": [{"project_id": "project-001", "label": "Test", "source_path": str(self.project), "exclude_relative_paths": [], "conversations": []}],
        }), encoding="utf-8")
        catalog_path = self.base / "incomplete-index-catalog.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", catalog_path)
        bindings = self.base / "incomplete-bindings.json"
        bindings.write_text(json.dumps({
            "migration_id": "A-device-incomplete-test",
            "skill_release": {"tag": "skill-test", "manifest_sha256": "3" * 64, "reused": False},
            "projects": [{"project_id": "project-001", "project_release": {"tag": "project-test", "manifest_sha256": "1" * 64, "reused": False}, "conversation_release": {"tag": "conversation-test", "manifest_sha256": "2" * 64, "reused": False}}],
        }), encoding="utf-8")
        output = self.base / "should-not-exist-index.json"
        completed = subprocess.run(
            [sys.executable, str(INCREMENTAL), "build-index", "--catalog", str(catalog_path), "--bindings", str(bindings), "--output", str(output), "--handoff", str(self.base / "handoff.md")],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("complete conversation inventory", completed.stderr)
        self.assertFalse(output.exists())

    def test_secret_audit_blocks_high_confidence_token_without_echoing_it(self):
        token = "ghp_" + "A" * 40
        (self.project / "config.txt").write_text("token=" + token, encoding="utf-8")
        discovery = self.base / "discovery.json"
        self.discovery(discovery)
        catalog = self.base / "catalog.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", catalog)
        report = self.base / "audit.json"
        completed = subprocess.run(
            [sys.executable, str(AUDIT), "--catalog", str(catalog), "--output", str(report), "--git", "git"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        )
        self.assertEqual(completed.returncode, 2)
        report_text = report.read_text(encoding="utf-8")
        self.assertIn("high-confidence-secret-pattern", report_text)
        self.assertNotIn(token, report_text)

    def test_secret_audit_ignores_task_names_and_token_lexicons(self):
        (self.project / "token-lexicon.json").write_text('{"term":"task-property-picker"}', encoding="utf-8")
        (self.project / "styles.css").write_text(".task-property-picker { color: red; }", encoding="utf-8")
        discovery = self.base / "discovery.json"
        self.discovery(discovery)
        catalog = self.base / "catalog.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", catalog)
        report = self.base / "audit.json"
        completed = subprocess.run(
            [sys.executable, str(AUDIT), "--catalog", str(catalog), "--output", str(report), "--git", "git"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["status"], "passed")

    def test_secret_audit_does_not_regex_scan_recognized_binary_payloads(self):
        fake_block = b"-----BEGIN PRIVATE KEY-----\n" + b"A" * 128 + b"\n-----END PRIVATE KEY-----\n"  # PUBLICATION-SECRET-FIXTURE
        (self.project / "tool.exe").write_bytes(b"MZ\x00" + fake_block + b"ghp_" + b"A" * 40)
        discovery = self.base / "binary-discovery.json"
        self.discovery(discovery)
        catalog = self.base / "binary-catalog.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", catalog)
        completed, result = self.run_secret_audit(catalog, self.base / "binary-audit.json")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["blocking_findings"], [])
        self.assertGreaterEqual(result["binary_skipped_file_count"], 1)

    def test_secret_audit_requires_complete_private_key_block(self):
        (self.project / "parser.py").write_text(
            'PRIVATE_KEY_HEADER = "-----BEGIN PRIVATE KEY-----"\n', encoding="utf-8"  # PUBLICATION-SECRET-FIXTURE
        )
        discovery = self.base / "header-discovery.json"
        self.discovery(discovery)
        catalog = self.base / "header-catalog.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", catalog)
        completed, result = self.run_secret_audit(catalog, self.base / "header-audit.json")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(result["blocking_findings"], [])

    def test_secret_audit_reports_vendor_test_private_key_without_blocking(self):
        fixture = self.project / "node_modules" / "example-package" / "tests" / "fixtures" / "server.key"
        fixture.parent.mkdir(parents=True)
        fixture.write_bytes(
            b"-----BEGIN PRIVATE KEY-----\n" + b"A" * 128 + b"\n-----END PRIVATE KEY-----\n"  # PUBLICATION-SECRET-FIXTURE
        )
        discovery = self.base / "fixture-discovery.json"
        self.discovery(discovery)
        catalog = self.base / "fixture-catalog.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", catalog)
        completed, result = self.run_secret_audit(catalog, self.base / "fixture-audit.json")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["blocking_findings"], [])
        self.assertTrue(result["nonblocking_test_findings"])
        self.assertTrue(all(item["reason"] == "third-party-package-test-fixture" for item in result["nonblocking_test_findings"]))

    def test_secret_audit_still_blocks_complete_private_key_outside_tests(self):
        (self.project / "production.key").write_bytes(
            b"-----BEGIN PRIVATE KEY-----\n" + b"A" * 128 + b"\n-----END PRIVATE KEY-----\n"  # PUBLICATION-SECRET-FIXTURE
        )
        discovery = self.base / "real-key-discovery.json"
        self.discovery(discovery)
        catalog = self.base / "real-key-catalog.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", catalog)
        completed, result = self.run_secret_audit(catalog, self.base / "real-key-audit.json")
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(len(result["blocking_findings"]), 2)
        self.assertEqual(result["nonblocking_test_findings"], [])

    def test_secret_audit_keeps_browser_auth_database_blocking(self):
        cookies = self.project / "tests" / "browser-profile" / "Default" / "Network" / "Cookies"
        cookies.parent.mkdir(parents=True)
        cookies.write_bytes(b"SQLite format 3\x00synthetic")
        discovery = self.base / "cookies-discovery.json"
        self.discovery(discovery)
        catalog = self.base / "cookies-catalog.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", catalog)
        completed, result = self.run_secret_audit(catalog, self.base / "cookies-audit.json")
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(result["blocking_findings"][0]["category"], "sensitive-auth-file")
        self.assertEqual(result["nonblocking_test_findings"], [])

    def test_secret_audit_blocks_openai_style_key_with_boundaries(self):
        token = "sk-" + "Ab3" * 16
        (self.project / "config.txt").write_text('api_key="' + token + '"', encoding="utf-8")
        discovery = self.base / "discovery.json"
        self.discovery(discovery)
        catalog = self.base / "catalog.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", catalog)
        report = self.base / "audit.json"
        completed = subprocess.run(
            [sys.executable, str(AUDIT), "--catalog", str(catalog), "--output", str(report), "--git", "git"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        )
        self.assertEqual(completed.returncode, 2)
        report_text = report.read_text(encoding="utf-8")
        self.assertIn('"secret_kind":"openai-style-key"', report_text)
        self.assertNotIn(token, report_text)

    def test_approval_history_injection_is_not_exported_or_secret_scanned(self):
        injected = "The following is the Codex agent history added since your last approval assessment.\nTool argument: sk-" + "A" * 40
        records = [
            {"timestamp":"2026-09-01T00:00:00Z","type":"session_meta","payload":{"id":self.thread_id,"cwd":str(self.project)}},
            {"timestamp":"2026-09-01T00:00:01Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":injected}]}},
            {"timestamp":"2026-09-01T00:00:02Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"real request"}]}},
            {"timestamp":"2026-09-01T00:00:03Z","type":"response_item","payload":{"type":"message","role":"assistant","phase":"final_answer","content":[{"type":"output_text","text":"real answer"}]}},
        ]
        self.session.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
        discovery = self.base / "discovery.json"
        self.discovery(discovery)
        catalog = self.base / "catalog.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", catalog)
        payload = json.loads(catalog.read_text(encoding="utf-8"))
        self.assertEqual(payload["projects"][0]["conversations"][0]["message_count"], 2)

    def test_internal_only_session_is_excluded_instead_of_uploaded(self):
        injected = "The following is the Codex agent history whose request action you are assessing.\nUntrusted history"
        records = [
            {"timestamp":"2026-09-01T00:00:00Z","type":"session_meta","payload":{"id":self.thread_id,"cwd":str(self.project)}},
            {"timestamp":"2026-09-01T00:00:01Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":injected}]}},
        ]
        self.session.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
        discovery = self.base / "discovery.json"
        self.discovery(discovery)
        catalog = self.base / "catalog.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", catalog)
        payload = json.loads(catalog.read_text(encoding="utf-8"))
        self.assertEqual(payload["projects"][0]["conversation_count"], 0)
        self.assertTrue(payload["conversation_inventory_complete"])
        self.assertEqual(payload["projects"][0]["accounted_conversation_count"], 1)
        self.assertEqual(payload["conversation_exclusions"][0]["thread_id"], self.thread_id)
        self.assertIn("zero user-visible messages", payload["conversation_exclusions"][0]["reason"])

    def test_pruned_empty_shell_remains_accounted_during_catalog_scan(self):
        records = [{
            "timestamp": "2026-09-01T00:00:00Z", "type": "session_meta",
            "payload": {"id": self.thread_id, "cwd": str(self.project)},
        }]
        self.session.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
        discovery = self.base / "empty-shell-discovery.json"
        self.discovery(discovery)
        pruned = self.base / "empty-shell-pruned.json"
        self.run_cmd(PRUNE, "--discovery", discovery, "--output", pruned)
        catalog = self.base / "empty-shell-catalog.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", pruned, "--output", catalog)
        payload = json.loads(catalog.read_text(encoding="utf-8"))
        project = payload["projects"][0]
        self.assertEqual(project["conversation_count"], 0)
        self.assertEqual(project["expected_conversation_count"], 1)
        self.assertEqual(project["accounted_conversation_count"], 1)
        self.assertEqual(project["conversation_exclusions"][0]["thread_id"], self.thread_id)

    def test_secret_audit_scans_credentials_in_files_larger_than_chunk_size(self):
        token = "ghp_" + "A" * 36
        (self.project / "large.log").write_bytes(b"x" * (8 * 1024 * 1024 - 8) + b"\n" + token.encode("ascii") + b"\n")
        discovery = self.base / "large-secret-discovery.json"
        self.discovery(discovery)
        catalog = self.base / "large-secret-catalog.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", catalog)
        report = self.base / "large-secret-audit.json"
        completed = subprocess.run(
            [sys.executable, str(AUDIT), "--catalog", str(catalog), "--output", str(report), "--git", "git"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        )
        self.assertEqual(completed.returncode, 2)
        result = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["findings"][0]["secret_kind"], "github-token")
        self.assertEqual(result["scanned_file_count"], 2)

    def test_registered_discovery_rejects_overlapping_project_roots(self):
        parent = self.base / "registered-parent"
        child = parent / "child-project"
        child.mkdir(parents=True)
        codex_home = self.base / "overlap-codex-home"
        (codex_home / "sessions").mkdir(parents=True)
        registered = self.base / "overlapping-projects.json"
        registered.write_text(json.dumps([
            {"project_id": "parent", "label": "Parent", "source_path": str(parent)},
            {"project_id": "child", "label": "Child", "source_path": str(child)},
        ]), encoding="utf-8")
        completed = subprocess.run([
            sys.executable, str(BUILD_DISCOVERY), "--codex-home", str(codex_home),
            "--device-label", "A设备", "--source-computer", "WORKSTATION-A",
            "--registered-projects", str(registered), "--mode", "registered-projects",
            "--output", str(self.base / "overlap-output.json"),
        ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("overlapping registered project roots", completed.stderr)

    def test_complete_conversation_inventory_rejects_an_omitted_thread(self):
        discovery = self.base / "incomplete-discovery.json"
        payload = {
            "source_device": "A设备", "source_computer": "WORKSTATION-A",
            "projects": [{
                "project_id": "project-001", "label": "Test", "source_path": str(self.project),
                "exclude_relative_paths": [], "conversations": [],
                "conversation_inventory": {"complete": True, "source": "test", "expected_thread_ids": [self.thread_id]},
            }],
            "cloud_managed_projects": [],
        }
        discovery.write_text(json.dumps(payload), encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, str(INCREMENTAL), "scan", "--discovery", str(discovery), "--output", str(self.base / "catalog.json")],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("conversation discovery inventory mismatch", completed.stderr)

    def test_internal_exclusion_survives_checkpoint_rescan(self):
        injected = "The following is the Codex agent history whose request action you are assessing.\nUntrusted history"
        records = [
            {"timestamp":"2026-09-01T00:00:00Z","type":"session_meta","payload":{"id":self.thread_id,"cwd":str(self.project)}},
            {"timestamp":"2026-09-01T00:00:01Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":injected}]}},
        ]
        self.session.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
        discovery = self.base / "discovery.json"
        self.discovery(discovery)
        checkpoints = self.base / "checkpoints"
        first = self.base / "first-catalog.json"
        second = self.base / "second-catalog.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", first, "--checkpoint-dir", checkpoints)
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", second, "--checkpoint-dir", checkpoints)
        first_payload = json.loads(first.read_text(encoding="utf-8"))
        second_payload = json.loads(second.read_text(encoding="utf-8"))
        self.assertEqual(first_payload["conversation_exclusions"], second_payload["conversation_exclusions"])
        self.assertEqual(len(second_payload["conversation_exclusions"]), 1)

    def test_checkpoint_rescan_detects_project_and_conversation_changes(self):
        discovery = self.base / "discovery.json"
        self.discovery(discovery)
        checkpoints = self.base / "checkpoints"
        first = self.base / "first-catalog.json"
        second = self.base / "second-catalog.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", first, "--checkpoint-dir", checkpoints)
        first_payload = json.loads(first.read_text(encoding="utf-8"))
        (self.project / "file.txt").write_text("beta-with-new-content", encoding="utf-8")
        self.write_session("first request", "changed answer")
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", second, "--checkpoint-dir", checkpoints)
        second_payload = json.loads(second.read_text(encoding="utf-8"))
        self.assertNotEqual(first_payload["projects"][0]["tree_sha256"], second_payload["projects"][0]["tree_sha256"])
        self.assertNotEqual(first_payload["projects"][0]["conversation_set_sha256"], second_payload["projects"][0]["conversation_set_sha256"])

    def test_legacy_checkpoint_is_invalidated_after_scanner_upgrade(self):
        injected = "The following is the Codex agent history whose request action you are assessing.\nUntrusted history"
        records = [
            {"timestamp":"2026-09-01T00:00:00Z","type":"session_meta","payload":{"id":self.thread_id,"cwd":str(self.project)}},
            {"timestamp":"2026-09-01T00:00:01Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":injected}]}},
        ]
        self.session.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
        discovery = self.base / "discovery.json"
        self.discovery(discovery)
        raw_project = json.loads(discovery.read_text(encoding="utf-8"))["projects"][0]
        module_spec = importlib.util.spec_from_file_location("incremental_checkpoint_test", INCREMENTAL)
        incremental = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(incremental)
        legacy_binding = incremental.hashlib.sha256(incremental.canonical_bytes(raw_project)).hexdigest()
        legacy_project = {
            "project_id": "project-001", "label": "Test", "source_path": str(self.project),
            "tree_sha256": "1" * 64, "entry_count": 1, "file_count": 1, "total_bytes": 1,
            "excluded_relative_paths": [], "entries": [], "exclusion_records": [],
            "conversation_set_sha256": "2" * 64, "conversation_count": 1,
            "conversations": [{
                "thread_id": self.thread_id, "thread_title": "internal", "conversation_sha256": "3" * 64,
                "messages_sha256": "4" * 64, "message_count": 1, "attachment_count": 0,
                "cutoff_timestamp": None,
            }],
        }
        checkpoints = self.base / "legacy-checkpoints"
        checkpoints.mkdir()
        checkpoint_name = incremental.hashlib.sha256(b"project-001").hexdigest() + ".json"
        (checkpoints / checkpoint_name).write_text(json.dumps({
            "discovery_binding_sha256": legacy_binding,
            "project": legacy_project,
        }), encoding="utf-8")
        catalog = self.base / "catalog-after-upgrade.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", catalog, "--checkpoint-dir", checkpoints)
        payload = json.loads(catalog.read_text(encoding="utf-8"))
        self.assertEqual(payload["projects"][0]["conversation_count"], 0)
        self.assertEqual(len(payload["conversation_exclusions"]), 1)
        rewritten = json.loads((checkpoints / checkpoint_name).read_text(encoding="utf-8"))
        self.assertEqual(rewritten["format"], incremental.SCAN_CHECKPOINT_FORMAT)

    def test_tampered_catalog_is_rejected_before_planning(self):
        discovery = self.base / "discovery.json"
        self.discovery(discovery)
        catalog = self.base / "catalog.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", catalog)
        payload = json.loads(catalog.read_text(encoding="utf-8"))
        payload["projects"][0]["label"] = "tampered"
        catalog.write_text(json.dumps(payload), encoding="utf-8")
        output = self.base / "plan.json"
        completed = subprocess.run(
            [sys.executable, str(INCREMENTAL), "plan", "--current", str(catalog), "--output", str(output)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("catalog_sha256", completed.stderr)
        self.assertFalse(output.exists())

    def test_unhashed_previous_index_is_rejected(self):
        discovery = self.base / "discovery.json"
        self.discovery(discovery)
        catalog = self.base / "catalog.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", catalog)
        current = json.loads(catalog.read_text(encoding="utf-8"))
        previous = self.base / "unhashed-index.json"
        previous.write_text(json.dumps({
            "format": "cross-device-project-sync-incremental-device-index-v1",
            "repository": "example-owner/example-private-backups",
            "source_device": current["source_device"],
            "source_computer": current["source_computer"],
            "projects": current["projects"],
        }), encoding="utf-8")
        output = self.base / "plan.json"
        completed = subprocess.run(
            [sys.executable, str(INCREMENTAL), "plan", "--current", str(catalog), "--previous", str(previous), "--output", str(output)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("index_sha256", completed.stderr)

    def test_tampered_plan_is_rejected_by_verifier(self):
        discovery = self.base / "discovery.json"
        self.discovery(discovery)
        catalog = self.base / "catalog.json"
        plan = self.base / "plan.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", catalog)
        self.run_cmd(INCREMENTAL, "plan", "--current", catalog, "--output", plan)
        payload = json.loads(plan.read_text(encoding="utf-8"))
        payload["decisions"][0]["project_action"] = "reuse"
        plan.write_text(json.dumps(payload), encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, str(INCREMENTAL), "verify-plan", "--plan", str(plan), "--catalog", str(catalog)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("plan_sha256", completed.stderr)

    def test_upload_preflight_uses_actual_local_device_identity(self):
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is unavailable")
        computer = os.environ.get("COMPUTERNAME") or platform.node()
        known = {"WORKSTATION-A": "A设备", "WORKSTATION-B": "B设备"}
        label = known.get(computer.upper(), "test-device")
        discovery = self.base / "device-discovery.json"
        payload = {
            "source_device": label, "source_computer": computer,
            "projects": [{
                "project_id": "project-001", "label": "Test", "source_path": str(self.project),
                "exclude_relative_paths": [], "conversations": [],
                "conversation_inventory": {"complete": True, "source": "test", "expected_thread_ids": []},
            }],
            "cloud_managed_projects": [],
        }
        discovery.write_text(json.dumps(payload), encoding="utf-8")
        catalog = self.base / "device-catalog.json"
        plan = self.base / "device-plan.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", catalog)
        self.run_cmd(INCREMENTAL, "plan", "--current", catalog, "--output", plan)
        codex_home = self.base / "codex-home"
        codex_home.mkdir()
        (codex_home / "cross-device-project-sync-device.json").write_text(
            json.dumps({"device_label": label, "computer_name": computer}), encoding="utf-8"
        )
        env = dict(os.environ, CODEX_HOME=str(codex_home), PYTHONIOENCODING="utf-8")
        completed = subprocess.run([
            powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(RUN_UPLOAD),
            "-Catalog", str(catalog), "-Plan", str(plan), "-WorkRoot", str(self.base / "upload-work"),
            "-MigrationId", "device-preflight-test", "-Repository", "test-owner/test-private-backups",
            "-PythonExe", sys.executable, "-PreflightOnly",
        ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, env=env)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["source_device"], label)
        self.assertEqual(result["source_computer"].casefold(), computer.casefold())

    def test_upload_preflight_rejects_incomplete_conversation_inventory(self):
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is unavailable")
        computer = os.environ.get("COMPUTERNAME") or platform.node()
        known = {"WORKSTATION-A": "A设备", "WORKSTATION-B": "B设备"}
        label = known.get(computer.upper(), "test-device")
        discovery = self.base / "incomplete-discovery.json"
        discovery.write_text(json.dumps({
            "source_device": label,
            "source_computer": computer,
            "projects": [{
                "project_id": "project-001",
                "label": "Test",
                "source_path": str(self.project),
                "exclude_relative_paths": [],
                "conversations": [],
            }],
            "cloud_managed_projects": [],
        }), encoding="utf-8")
        catalog = self.base / "incomplete-catalog.json"
        plan = self.base / "incomplete-plan.json"
        self.run_cmd(INCREMENTAL, "scan", "--discovery", discovery, "--output", catalog)
        self.run_cmd(INCREMENTAL, "plan", "--current", catalog, "--output", plan)
        codex_home = self.base / "incomplete-codex-home"
        codex_home.mkdir()
        (codex_home / "cross-device-project-sync-device.json").write_text(
            json.dumps({"device_label": label, "computer_name": computer}), encoding="utf-8"
        )
        env = dict(os.environ, CODEX_HOME=str(codex_home), PYTHONIOENCODING="utf-8")
        completed = subprocess.run([
            powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(RUN_UPLOAD),
            "-Catalog", str(catalog), "-Plan", str(plan), "-WorkRoot", str(self.base / "blocked-upload-work"),
            "-MigrationId", "device-incomplete-test", "-Repository", "test-owner/test-private-backups",
            "-PythonExe", sys.executable, "-PreflightOnly",
        ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, env=env)
        self.assertNotEqual(completed.returncode, 0)
        combined = completed.stdout + completed.stderr
        self.assertIn("does not prove a complete per-project conversation", combined)
        self.assertIn("inventory; upload is blocked", combined)

    def test_registered_project_discovery_scans_all_local_session_metadata(self):
        codex_home = self.base / "codex-home-discovery"
        sessions = codex_home / "sessions" / "2026" / "10"
        archived_sessions = codex_home / "archived_sessions"
        sessions.mkdir(parents=True)
        archived_sessions.mkdir(parents=True)
        second_thread = "01a00000-1111-7222-8333-666666666666"
        for thread_id, suffix in ((self.thread_id, "active"), (second_thread, "archived")):
            records = [
                {"type": "session_meta", "payload": {"id": thread_id, "cwd": str(self.project)}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": suffix}]}},
            ]
            root = archived_sessions if suffix == "archived" else sessions
            (root / f"rollout-{suffix}-{thread_id}.jsonl").write_text(
                "".join(json.dumps(item) + "\n" for item in records), encoding="utf-8"
            )
        registered = self.base / "registered-projects.json"
        registered.write_text(json.dumps([{
            "project_id": "project-001", "label": "Test", "source_path": str(self.project),
        }]), encoding="utf-8")
        output = self.base / "registered-discovery.json"
        result = self.run_cmd(
            BUILD_DISCOVERY, "--codex-home", codex_home, "--device-label", "A设备",
            "--source-computer", "WORKSTATION-A", "--registered-projects", registered,
            "--mode", "registered-projects", "--output", output,
        )
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(result["conversation_count"], 2)
        self.assertTrue(payload["conversation_inventory_complete"])
        self.assertEqual(
            set(payload["projects"][0]["conversation_inventory"]["expected_thread_ids"]),
            {self.thread_id, second_thread},
        )

    def test_registered_discovery_keeps_session_from_missing_nested_workdir(self):
        codex_home = self.base / "codex-home-missing-cwd"
        sessions = codex_home / "sessions"
        sessions.mkdir(parents=True)
        missing_nested = self.project / "removed-subdirectory"
        records = [
            {"type": "session_meta", "payload": {"id": self.thread_id, "cwd": str(missing_nested)}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "keep me"}]}},
        ]
        (sessions / f"rollout-{self.thread_id}.jsonl").write_text(
            "".join(json.dumps(item) + "\n" for item in records), encoding="utf-8"
        )
        registered = self.base / "registered-missing-cwd.json"
        registered.write_text(json.dumps([{
            "project_id": "project-001", "label": "Test", "source_path": str(self.project),
        }]), encoding="utf-8")
        output = self.base / "missing-cwd-discovery.json"
        result = self.run_cmd(
            BUILD_DISCOVERY, "--codex-home", codex_home, "--device-label", "A设备",
            "--source-computer", "WORKSTATION-A", "--registered-projects", registered,
            "--mode", "registered-projects", "--output", output,
        )
        self.assertEqual(result["conversation_count"], 1)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["projects"][0]["conversations"][0]["thread_id"], self.thread_id)

    def test_registered_discovery_rejects_malformed_session_metadata(self):
        codex_home = self.base / "codex-home-malformed"
        sessions = codex_home / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "rollout-malformed.jsonl").write_text(
            json.dumps({"type": "response_item", "payload": {"type": "message"}}) + "\n",
            encoding="utf-8",
        )
        registered = self.base / "registered-malformed.json"
        registered.write_text(json.dumps([{
            "project_id": "project-001", "label": "Test", "source_path": str(self.project),
        }]), encoding="utf-8")
        output = self.base / "malformed-discovery.json"
        completed = subprocess.run([
            sys.executable, str(BUILD_DISCOVERY), "--codex-home", str(codex_home),
            "--device-label", "A设备", "--source-computer", "WORKSTATION-A",
            "--registered-projects", str(registered), "--mode", "registered-projects",
            "--output", str(output),
        ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("session metadata scan was incomplete", completed.stderr)

    def test_expanded_discovery_excludes_broad_parent_but_keeps_nested_project(self):
        spec = importlib.util.spec_from_file_location("build_discovery_test", BUILD_DISCOVERY)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        broad = pathlib.Path.home() / "Documents"
        nested = broad / "IndependentProject"
        roots, exclusions = module.select_task_roots([broad, nested])
        self.assertEqual([module.windows_key(item) for item in roots], [module.windows_key(nested)])
        self.assertEqual(len(exclusions), 1)
        self.assertIn("broad container", exclusions[0]["reason"])

    def test_expanded_discovery_excludes_relocated_windows_known_folder(self):
        spec = importlib.util.spec_from_file_location("build_discovery_relocated_test", BUILD_DISCOVERY)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        relocated = self.base / "RelocatedDownloads"
        nested = relocated / "IndependentProject"
        known = {module.windows_key(relocated): "Downloads is a broad container"}
        with mock.patch.object(module, "broad_root_map", return_value=known):
            roots, exclusions = module.select_task_roots([relocated, nested])
        self.assertEqual([module.windows_key(item) for item in roots], [module.windows_key(nested)])
        self.assertEqual(exclusions, [{"path": str(relocated.absolute()), "reason": "Downloads is a broad container"}])

    def test_browser_test_profiles_are_explicitly_excluded(self):
        protected = self.project / "tools" / "chrome-test-profile-3"
        protected.mkdir(parents=True)
        (protected / "Login Data").write_bytes(b"must-not-be-scanned")
        discovery = self.base / "discovery.json"
        self.discovery(discovery)
        safe_discovery = self.base / "safe-discovery.json"
        result = self.run_cmd(BROWSER_EXCLUSIONS, "--discovery", discovery, "--output", safe_discovery)
        self.assertEqual(result["protected_browser_profile_count"], 1)
        payload = json.loads(safe_discovery.read_text(encoding="utf-8"))
        project = payload["projects"][0]
        self.assertIn("tools/chrome-test-profile-3", project["exclude_relative_paths"])
        self.assertIn("authentication state", project["exclusion_records"][0]["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
