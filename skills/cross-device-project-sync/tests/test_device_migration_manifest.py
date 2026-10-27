import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


class DeviceMigrationManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill_root = pathlib.Path(__file__).resolve().parents[1]
        cls.script = cls.skill_root / "scripts" / "device-migration-manifest.py"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def discovery(self):
        return {
            "migration_id": "A-device-20260924-210000",
            "source_device": "A设备",
            "source_computer": "WORKSTATION-A",
            "created_utc": "2026-09-24T13:00:00Z",
            "inventory": [
                {
                    "project_id": "project-b",
                    "label": "Beta",
                    "project_kind": "local",
                    "source_path": "D:\\Projects\\Beta",
                    "disposition": "pending",
                    "reason": None,
                    "canonical_project_id": None,
                },
                {
                    "project_id": "desktop-root",
                    "label": "Desktop",
                    "project_kind": "local",
                    "source_path": "C:\\Users\\Example\\Desktop",
                    "disposition": "excluded-broad-root",
                    "reason": "Desktop is a broad container",
                    "canonical_project_id": None,
                },
                {
                    "project_id": "cloud-project",
                    "label": "Cloud project",
                    "project_kind": "chatgpt",
                    "source_path": None,
                    "disposition": "cloud-managed",
                    "reason": None,
                    "canonical_project_id": None,
                },
                {
                    "project_id": "project-a",
                    "label": "Alpha",
                    "project_kind": "local",
                    "source_path": "C:\\Projects\\Alpha",
                    "disposition": "pending",
                    "reason": None,
                    "canonical_project_id": None,
                },
            ],
        }

    def project_result(self, project_id):
        if project_id == "project-a":
            label, path, tag, digest = "Alpha", "C:\\Projects\\Alpha", "backup-A-alpha", "a" * 64
        else:
            label, path, tag, digest = "Beta", "D:\\Projects\\Beta", "backup-A-beta", "b" * 64
        return {
            "project_id": project_id,
            "label": label,
            "source_path": path,
            "source_device": "A设备",
            "source_computer": "WORKSTATION-A",
            "project_manifest_format": "split-tar-pax-v2",
            "release_tag": tag,
            "project_manifest_sha256": digest,
            "snapshot_created_utc": "2026-09-24T12:58:00Z",
            "conversation": None,
        }

    def run_script(self, *args):
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(self.script), *map(str, args)],
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

    def write_json(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def initialize(self, payload=None):
        state = self.root / "DEVICE_MIGRATION_STATE.json"
        result = self.run_script(
            "init",
            "--input",
            self.write_json("discovery.json", payload or self.discovery()),
            "--output",
            state,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return state

    def record_success(self, state, project_id):
        result = self.run_script(
            "record-success",
            "--state",
            state,
            "--result",
            self.write_json(f"{project_id}.json", self.project_result(project_id)),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_failure_resume_build_and_verify_complete_inventory(self):
        state = self.initialize()
        self.record_success(state, "project-a")
        failed = self.run_script(
            "record-failure", "--state", state, "--project-id", "project-b", "--error", "network stopped"
        )
        self.assertEqual(failed.returncode, 0, failed.stderr)
        inspected = self.run_script("inspect", "--state", state)
        progress = json.loads(inspected.stdout)
        self.assertEqual(progress["status"], "failed")
        self.assertEqual(progress["failed_project_ids"], ["project-b"])
        self.assertEqual(progress["completed_release_tags"], ["backup-A-alpha"])

        blocked = self.run_script(
            "build", "--state", state, "--output", self.root / "DEVICE_MIGRATION_MANIFEST.json"
        )
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("pending or failed", blocked.stderr)

        self.record_success(state, "project-b")
        ready = json.loads(self.run_script("inspect", "--state", state).stdout)
        self.assertEqual(ready["status"], "ready-to-finalize")
        output = self.root / "DEVICE_MIGRATION_MANIFEST.json"
        built = self.run_script("build", "--state", state, "--output", output)
        self.assertEqual(built.returncode, 0, built.stderr)
        manifest = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(manifest["format"], "cross-device-project-sync-device-migration-v2")
        self.assertEqual(manifest["discovered_project_count"], 4)
        self.assertEqual(manifest["selected_local_project_count"], 2)
        self.assertEqual(manifest["completed_project_count"], 2)
        self.assertEqual(manifest["excluded_project_count"], 1)
        self.assertEqual(manifest["cloud_project_count"], 1)
        self.assertEqual([item["label"] for item in manifest["projects"]], ["Alpha", "Beta"])

        verified = self.run_script(
            "verify",
            output,
            "--expected-device",
            "A设备",
            "--expected-computer",
            "workstation-a",
        )
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertEqual(json.loads(verified.stdout)["state"], "verified-complete")

    def test_completed_result_is_idempotent_but_cannot_be_replaced(self):
        state = self.initialize()
        self.record_success(state, "project-a")
        repeated = self.record_success(state, "project-a")
        self.assertEqual(repeated["state"], "already-recorded")
        changed = self.project_result("project-a")
        changed["release_tag"] = "different-release"
        result = self.run_script(
            "record-success",
            "--state",
            state,
            "--result",
            self.write_json("changed.json", changed),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot be replaced", result.stderr)

    def test_build_rejects_omitted_pending_project(self):
        state = self.initialize()
        self.record_success(state, "project-a")
        result = self.run_script("build", "--state", state, "--output", self.root / "out.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pending or failed", result.stderr)

    def test_project_source_identity_must_match_migration(self):
        state = self.initialize()
        result_payload = self.project_result("project-a")
        result_payload["source_computer"] = "WORKSTATION-B"
        result = self.run_script(
            "record-success",
            "--state",
            state,
            "--result",
            self.write_json("wrong-identity.json", result_payload),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source identity", result.stderr)

    def test_public_release_does_not_require_a_private_device_mapping(self):
        payload = self.discovery()
        payload["source_computer"] = "WORKSTATION-B"
        result = self.run_script(
            "init",
            "--input",
            self.write_json("wrong-mapping.json", payload),
            "--output",
            self.root / "wrong-mapping-state.json",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = json.loads((self.root / "wrong-mapping-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["source_computer"], "WORKSTATION-B")

    def test_duplicate_alias_requires_one_matching_canonical_project(self):
        payload = self.discovery()
        payload["inventory"].append(
            {
                "project_id": "project-a-alias",
                "label": "Alpha alias",
                "project_kind": "local",
                "source_path": "c:/projects/alpha/",
                "disposition": "excluded-duplicate-alias",
                "reason": "same normalized local root",
                "canonical_project_id": "project-a",
            }
        )
        state = self.initialize(payload)
        data = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(data["excluded_project_count"], 2)

        payload = self.discovery()
        payload["inventory"].append(
            {
                "project_id": "bad-alias",
                "label": "Bad alias",
                "project_kind": "local",
                "source_path": "c:/projects/alpha/",
                "disposition": "excluded-duplicate-alias",
                "reason": "bad canonical identity",
                "canonical_project_id": "project-b",
            }
        )
        result = self.run_script(
            "init",
            "--input",
            self.write_json("bad-discovery.json", payload),
            "--output",
            self.root / "bad-state.json",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("canonical_project_id", result.stderr)

    def test_existing_state_is_not_overwritten(self):
        output = self.root / "DEVICE_MIGRATION_STATE.json"
        output.write_text("keep", encoding="utf-8")
        result = self.run_script(
            "init",
            "--input",
            self.write_json("discovery.json", self.discovery()),
            "--output",
            output,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output.read_text(encoding="utf-8"), "keep")

    def test_legacy_v1_index_remains_verifiable_but_inventory_is_unproven(self):
        legacy = {
            "format": "cross-device-project-sync-device-migration-v1",
            "status": "complete",
            "migration_id": "A-device-legacy",
            "source_device": "A设备",
            "source_computer": "WORKSTATION-A",
            "created_utc": "2026-09-24T13:00:00Z",
            "project_count": 1,
            "projects": [
                {
                    "project_id": "project-a",
                    "label": "Alpha",
                    "source_path": "C:\\Projects\\Alpha",
                    "release_tag": "backup-A-alpha",
                    "project_manifest_sha256": "a" * 64,
                    "snapshot_created_utc": "2026-09-24T12:58:00Z",
                    "conversation": None,
                }
            ],
        }
        path = self.root / "legacy.json"
        path.write_text(json.dumps(legacy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result = self.run_script("verify", path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], "verified-complete-legacy-inventory-unproven")


if __name__ == "__main__":
    unittest.main()
