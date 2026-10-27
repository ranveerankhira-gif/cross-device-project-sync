import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
RECEIVER = ROOT / "scripts" / "device-index-receiver.py"
PACKAGER = ROOT / "scripts" / "package-installed-skill.py"
REPOSITORY = "example-owner/example-private-backups"
INCREMENTAL_FORMAT = "cross-device-project-sync-incremental-device-index-v1"
LEGACY_FORMAT = "cross-device-project-sync-device-migration-v1"


def load_receiver_module():
    spec = importlib.util.spec_from_file_location("cross_device_project_sync_receiver", RECEIVER)
    module = importlib.util.module_from_spec(spec)
    scripts_dir = str(RECEIVER.parent)
    sys.path.insert(0, scripts_dir)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def write_installed_skill(path, version):
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        f'---\nname: cross-device-project-sync\nmetadata:\n  version: "{version}"\n---\n',
        encoding="utf-8",
    )
    scripts = path / "scripts"
    scripts.mkdir()
    incremental_helper = (ROOT / "scripts" / "incremental-device-sync.py").read_text(encoding="utf-8")
    incremental_helper = re.sub(
        r'(?m)^REQUIRED_SKILL_VERSION = "[^"]+"$',
        f'REQUIRED_SKILL_VERSION = "{version}"',
        incremental_helper,
        count=1,
    )
    (scripts / "incremental-device-sync.py").write_text(incremental_helper, encoding="utf-8")


def write_release(root, tag, kind, identity, payloads, *, notes_kind=None):
    assets = root / (tag + "-assets")
    assets.mkdir()
    asset_records = []
    for name, data in payloads.items():
        (assets / name).write_bytes(data)
        asset_records.append({"name": name, "size": len(data), "sha256": sha256(data)})
    binding = {
        "format": "cross-device-project-sync-content-release-binding-v1",
        "repository": REPOSITORY,
        "release_tag": tag,
        "kind": kind,
        "identity_sha256": identity,
        "assets": asset_records,
    }
    binding_bytes = json.dumps(binding, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    (assets / "CONTENT_RELEASE_BINDING.json").write_bytes(binding_bytes)
    release_assets = [
        {"name": item["name"], "size": item["size"], "digest": "sha256:" + item["sha256"]}
        for item in asset_records
    ]
    release_assets.append({
        "name": "CONTENT_RELEASE_BINDING.json",
        "size": len(binding_bytes),
        "digest": "sha256:" + sha256(binding_bytes),
    })
    metadata = {
        "tagName": tag,
        "isDraft": False,
        "body": (
            f"Content-Kind: {notes_kind or kind}\n"
            f"Content-Identity-SHA256: {identity}\n"
            f"Repository: {REPOSITORY}"
        ),
        "assets": release_assets,
    }
    metadata_path = root / (tag + "-release.json")
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    return metadata_path, assets


def make_incremental_index(skill_tag="skill-cross-device-project-sync-v26-test", manifest_sha="3" * 64):
    index = {
        "format": INCREMENTAL_FORMAT,
        "migration_id": "A-device-test",
        "repository": REPOSITORY,
        "required_skill_version": "20260929-v26",
        "skill_release": {"tag": skill_tag, "manifest_sha256": manifest_sha, "reused": False},
        "source_device": "A设备",
        "source_computer": "WORKSTATION-A",
        "content_catalog_sha256": "1" * 64,
        "projects": [],
        "cloud_managed_projects": [],
        "conversation_exclusions": [],
    }
    index["index_sha256"] = sha256(canonical(index))
    return index, canonical(index)


def make_legacy_index():
    index = {
        "format": LEGACY_FORMAT,
        "status": "complete",
        "migration_id": "A-device-legacy",
        "source_device": "A设备",
        "source_computer": "WORKSTATION-A",
        "created_utc": "2026-09-28T00:00:00Z",
        "project_count": 1,
        "projects": [{
            "project_id": "legacy-project",
            "label": "Legacy",
            "source_path": "D:\\Projects\\Legacy",
            "release_tag": "backup-a-legacy",
            "project_manifest_sha256": "a" * 64,
            "snapshot_created_utc": "2026-09-28T00:00:00Z",
            "conversation": None,
        }],
    }
    return index, json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"


def make_skill_package(required_version, *, include_compiled_cache):
    skill_md = (
        f'---\nname: cross-device-project-sync\nmetadata:\n  version: "{required_version}"\n---\n'
    ).encode("utf-8")
    incremental_helper = (ROOT / "scripts" / "incremental-device-sync.py").read_text(encoding="utf-8")
    incremental_helper = re.sub(
        r'(?m)^REQUIRED_SKILL_VERSION = "[^"]+"$',
        f'REQUIRED_SKILL_VERSION = "{required_version}"',
        incremental_helper,
        count=1,
    )
    files = {
        "cross-device-project-sync/SKILL.md": skill_md,
        "cross-device-project-sync/scripts/device-index-receiver.py": RECEIVER.read_bytes(),
        "cross-device-project-sync/scripts/incremental-device-sync.py": incremental_helper.encode("utf-8"),
    }
    if include_compiled_cache:
        files["cross-device-project-sync/scripts/__pycache__/bootstrap.cpython-311.pyc"] = b"verified-but-never-installed"
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    zip_bytes = zip_buffer.getvalue()
    zip_name = f"cross-device-project-sync-{required_version}.zip"
    manifest = {
        "format": "cross-device-project-sync-skill-package-v1",
        "skill_name": "cross-device-project-sync",
        "version": required_version,
        "payload_name": zip_name,
        "payload_size": len(zip_bytes),
        "payload_sha256": sha256(zip_bytes),
        "files": [
            {"path": name, "size": len(data), "sha256": sha256(data)}
            for name, data in files.items()
        ],
    }
    manifest_bytes = canonical(manifest)
    return manifest_bytes, {"SKILL_PACKAGE_MANIFEST.json": manifest_bytes, zip_name: zip_bytes}


class DeviceIndexReceiverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.receiver = load_receiver_module()

    def run_receiver(self, *args, env=None):
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(RECEIVER), *map(str, args)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )

    def inspect(self, metadata, assets, installed, *extra):
        installed_path = Path(installed).resolve()
        if installed_path.parent.name != "skills" or installed_path.parent.parent.name != ".agents":
            raise AssertionError("receiver tests must place the installed Skill at the canonical shared path")
        environment = dict(os.environ)
        environment["USERPROFILE"] = str(installed_path.parent.parent.parent)
        return self.run_receiver(
            "inspect",
            "--release-metadata", metadata,
            "--assets", assets,
            "--repository", REPOSITORY,
            "--installed-skill-root", installed,
            *extra,
            env=environment,
        )

    def test_legacy_receive_return_releases_machine_lock_with_evidence(self):
        with tempfile.TemporaryDirectory(prefix="legacy-lock-release-") as raw:
            root = Path(raw)
            state = root / "machine-state"
            state.mkdir()
            home = root / "home"
            home.mkdir()
            caller = {"instance_id": "a", "display_name": "a号", "codex_home": str(home.resolve())}
            registry = {
                "format": "cross-device-codex-instance-registry-v1", "device_label": "测试设备",
                "computer_name": "TESTBOX", "machine_state_root": str(state.resolve()), "instances": [caller],
            }
            context = {"registry": registry, "caller": caller, "selection_sha256": "a" * 64}
            args = SimpleNamespace(release_tag="legacy-release")
            with mock.patch.object(self.receiver, "receive_context", return_value=context), \
                 mock.patch.object(self.receiver, "run_inline_readiness", return_value={"status": "ready"}), \
                 mock.patch.object(self.receiver, "_command_receive_locked", return_value={
                     "content_status": "legacy-index-requires-legacy-restore-workflow",
                 }):
                result = self.receiver.command_receive(args)
            self.assertTrue(result["machine_lock_released"])
            self.assertFalse((state / "operation.lock.json").exists())
            evidence = Path(result["release_evidence_path"])
            self.assertTrue(evidence.is_file())
            self.assertEqual(sha256(evidence.read_bytes()), result["release_evidence_sha256"])

    def test_same_version_still_uses_exact_hash_bound_isolated_skill(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            installed = root / "profile" / ".agents" / "skills" / "cross-device-project-sync"
            write_installed_skill(installed, "20260929-v26")
            (installed / "scripts" / "incremental-device-sync.py").write_text("raise RuntimeError('tampered local helper')\n", encoding="utf-8")
            skill_tag = "skill-cross-device-project-sync-v26-same-version"
            manifest_bytes, skill_payloads = make_skill_package("20260929-v26", include_compiled_cache=False)
            zip_name = next(name for name in skill_payloads if name.endswith(".zip"))
            skill_metadata, skill_assets = write_release(
                root, skill_tag, "skill-package", sha256(skill_payloads[zip_name]), skill_payloads,
            )
            index, index_bytes = make_incremental_index(skill_tag, sha256(manifest_bytes))
            metadata, assets = write_release(
                root, "device-index-a-test", "device-index", index["index_sha256"],
                {"INCREMENTAL_DEVICE_INDEX.json": index_bytes, "RECEIVER_READ_FIRST.md": b"read first\n"},
            )

            completed = self.inspect(
                metadata, assets, installed,
                "--skill-release-metadata", skill_metadata,
                "--skill-assets", skill_assets,
                "--stage-bound-skill",
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["index_kind"], "incremental")
            self.assertEqual(result["index_asset"], "INCREMENTAL_DEVICE_INDEX.json")
            self.assertEqual(result["required_skill_version"], "20260929-v26")
            self.assertEqual(result["version_comparison"], "equal")
            self.assertEqual(result["skill_action"], "staged-exact-bound-skill")
            self.assertTrue(result["public_skill_unchanged"])
            self.assertIn("bound-skill-runtimes", result["bound_skill_interpreter"])

    def test_same_version_without_hash_bound_package_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            installed = root / "profile" / ".agents" / "skills" / "cross-device-project-sync"
            write_installed_skill(installed, "20260929-v26")
            index, index_bytes = make_incremental_index()
            metadata, assets = write_release(
                root, "device-index-a-no-package", "device-index", index["index_sha256"],
                {"INCREMENTAL_DEVICE_INDEX.json": index_bytes},
            )

            completed = self.inspect(metadata, assets, installed)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("required even when the installed version string matches", completed.stderr)

    def test_accepts_legacy_device_migration_index(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            installed = root / "profile" / ".agents" / "skills" / "cross-device-project-sync"
            write_installed_skill(installed, "20260928-v26")
            _, index_bytes = make_legacy_index()
            metadata, assets = write_release(
                root, "legacy-device-index", "device-index", sha256(index_bytes),
                {"DEVICE_MIGRATION_MANIFEST.json": index_bytes},
            )

            completed = self.inspect(metadata, assets, installed)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["index_kind"], "legacy")
            self.assertEqual(result["index_asset"], "DEVICE_MIGRATION_MANIFEST.json")
            self.assertIsNone(result["required_skill_version"])

    def test_unknown_or_corrupt_index_stops_safely(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            installed = root / "profile" / ".agents" / "skills" / "cross-device-project-sync"
            write_installed_skill(installed, "20260929-v26")
            unknown = canonical({"format": "cross-device-project-sync-device-index-v999", "projects": []})
            metadata, assets = write_release(
                root, "unknown-device-index", "device-index", sha256(unknown),
                {"SOMETHING_NEW.json": unknown},
            )
            completed = self.inspect(metadata, assets, installed)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("unsupported device index format", completed.stderr)

            index, index_bytes = make_incremental_index()
            metadata, assets = write_release(
                root, "corrupt-device-index", "device-index", index["index_sha256"],
                {"INCREMENTAL_DEVICE_INDEX.json": index_bytes},
            )
            (assets / "INCREMENTAL_DEVICE_INDEX.json").write_bytes(index_bytes + b"corruption")
            completed = self.inspect(metadata, assets, installed)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("asset", completed.stderr.lower())

    def test_old_receiver_stages_exact_bound_skill_without_replacing_public_install(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            installed = root / "profile" / ".agents" / "skills" / "cross-device-project-sync"
            write_installed_skill(installed, "20260928-v26")
            skill_tag = "skill-cross-device-project-sync-v26-828d9a501753ba2f"
            manifest_bytes, skill_payloads = make_skill_package("20260929-v26", include_compiled_cache=True)
            zip_name = next(name for name in skill_payloads if name.endswith(".zip"))
            skill_metadata, skill_assets = write_release(
                root, skill_tag, "skill-package", sha256(skill_payloads[zip_name]), skill_payloads,
            )
            index, index_bytes = make_incremental_index(skill_tag, sha256(manifest_bytes))
            index_metadata, index_assets = write_release(
                root, "device-index-a-e1ec4e1fcbb66562", "device-index", index["index_sha256"],
                {"INCREMENTAL_DEVICE_INDEX.json": index_bytes},
            )

            completed = self.inspect(
                index_metadata, index_assets, installed,
                "--skill-release-metadata", skill_metadata,
                "--skill-assets", skill_assets,
                "--install-bound-skill",
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["installed_skill_version_before"], "20260928-v26")
            self.assertEqual(result["required_skill_version"], "20260929-v26")
            self.assertEqual(result["version_comparison"], "older")
            self.assertEqual(result["skill_action"], "staged-exact-bound-skill")
            self.assertEqual(result["skill_release_tag"], skill_tag)
            self.assertEqual(result["filtered_compiled_cache_count"], 1)
            self.assertTrue(result["public_skill_unchanged"])
            self.assertIn('version: "20260928-v26"', (installed / "SKILL.md").read_text(encoding="utf-8"))
            runtime = Path(result["bound_skill_runtime_root"])
            self.assertIn('version: "20260929-v26"', (runtime / "SKILL.md").read_text(encoding="utf-8"))
            self.assertFalse(any(path.suffix == ".pyc" or "__pycache__" in path.parts for path in runtime.rglob("*")))
            self.assertIsNone(result["previous_skill_backup"])
            self.assertFalse(any(path.name.startswith("cross-device-project-sync.pre-bound-backup-") for path in installed.parent.iterdir()))

    def test_bound_runtime_cannot_be_staged_inside_shared_skill_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            installed = root / "profile" / ".agents" / "skills" / "cross-device-project-sync"
            write_installed_skill(installed, "20260928-v26")
            skill_tag = "skill-cross-device-project-sync-v26-828d9a501753ba2f"
            manifest_bytes, skill_payloads = make_skill_package("20260929-v26", include_compiled_cache=False)
            zip_name = next(name for name in skill_payloads if name.endswith(".zip"))
            skill_metadata, skill_assets = write_release(
                root, skill_tag, "skill-package", sha256(skill_payloads[zip_name]), skill_payloads,
            )
            index, index_bytes = make_incremental_index(skill_tag, sha256(manifest_bytes))
            index_metadata, index_assets = write_release(
                root, "device-index-a-e1ec4e1fcbb66562", "device-index", index["index_sha256"],
                {"INCREMENTAL_DEVICE_INDEX.json": index_bytes},
            )

            completed = self.inspect(
                index_metadata, index_assets, installed,
                "--skill-release-metadata", skill_metadata,
                "--skill-assets", skill_assets,
                "--install-bound-skill",
                "--bound-skill-runtime-root", installed.parent / "historical-runtime",
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("outside the shared Skill discovery directory", completed.stderr)
            self.assertIn('version: "20260928-v26"', (installed / "SKILL.md").read_text(encoding="utf-8"))

    def test_packager_excludes_python_cache_and_compiled_files(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            skill = root / "cross-device-project-sync"
            write_installed_skill(skill, "20261001-v99")
            (skill / "scripts" / "__pycache__").mkdir(parents=True)
            (skill / "scripts" / "worker.py").write_text("print('ok')\n", encoding="utf-8")
            (skill / "scripts" / "__pycache__" / "worker.cpython-311.pyc").write_bytes(b"compiled")
            (skill / "orphan.pyc").write_bytes(b"compiled")
            output = root / "package"
            completed = subprocess.run(
                [sys.executable, "-X", "utf8", str(PACKAGER), "build", "--skill-root", str(skill), "--output", str(output)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            manifest = json.loads((output / "SKILL_PACKAGE_MANIFEST.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["version"], "20261001-v99")
            self.assertEqual(manifest["payload_name"], "cross-device-project-sync-20261001-v99.zip")
            checksum = (output / (manifest["payload_name"] + ".sha256")).read_text(encoding="ascii")
            self.assertEqual(checksum, f"{manifest['payload_sha256']} *{manifest['payload_name']}\n")
            with zipfile.ZipFile(output / manifest["payload_name"]) as archive:
                names = archive.namelist()
            self.assertFalse(any("__pycache__" in name or name.lower().endswith((".pyc", ".pyo")) for name in names))
            self.assertFalse(any("__pycache__" in item["path"] or item["path"].lower().endswith((".pyc", ".pyo")) for item in manifest["files"]))
            verified = subprocess.run(
                [sys.executable, "-X", "utf8", str(PACKAGER), "verify", "--package", str(output)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)

    def test_parse_last_json_returns_outer_multiline_result(self):
        expected = {
            "status": "restored-verified",
            "restored_project_root": "D:\\Restored\\Project",
            "verification": {"files": 12, "timestamps": {"matched": True}},
        }
        output = "restore progress 100%\n" + json.dumps(expected, ensure_ascii=False, indent=2) + "\n"
        self.assertEqual(self.receiver.parse_last_json(output, "restore"), expected)

    def test_require_free_space_stops_before_write_when_capacity_is_insufficient(self):
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "future" / "download"
            with mock.patch.object(
                self.receiver.shutil,
                "disk_usage",
                return_value=SimpleNamespace(total=10_000, used=9_000, free=1_000),
            ):
                with self.assertRaisesRegex(self.receiver.ReceiverError, "insufficient free space"):
                    self.receiver.require_free_space(target, 2_000, "migration download")
            self.assertFalse(target.exists())

    def test_download_release_uses_long_timeout_for_every_asset(self):
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "download"
            payload = b"payload"
            binding = {
                "assets": [{"name": "payload.bin", "size": len(payload), "sha256": sha256(payload)}]
            }
            binding_bytes = canonical(binding)
            metadata = {
                "tagName": "conversation-test",
                "assets": [
                    {
                        "name": "CONTENT_RELEASE_BINDING.json",
                        "size": len(binding_bytes),
                        "digest": "sha256:" + sha256(binding_bytes),
                    },
                    {
                        "name": "payload.bin",
                        "size": len(payload),
                        "digest": "sha256:" + sha256(payload),
                    },
                ],
            }
            calls = []

            def fake_run_gh(_gh, *arguments, timeout=180):
                calls.append((arguments, timeout))
                pattern = arguments[arguments.index("--pattern") + 1]
                output = Path(arguments[arguments.index("--dir") + 1])
                output.mkdir(parents=True, exist_ok=True)
                if pattern == "CONTENT_RELEASE_BINDING.json":
                    (output / pattern).write_bytes(binding_bytes)
                else:
                    (output / pattern).write_bytes(payload)
                return ""

            with mock.patch.object(self.receiver, "run_gh", side_effect=fake_run_gh):
                self.receiver.download_release(
                    "gh", REPOSITORY, metadata, destination
                )
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(timeout == 7200 for _, timeout in calls))

    def test_resume_reuses_only_hash_verified_existing_release_assets(self):
        with tempfile.TemporaryDirectory(prefix="resume-verified-assets-") as raw:
            root = Path(raw)
            payload = b"already downloaded and verified"
            metadata_path, assets = write_release(
                root,
                "device-index-resume-test",
                "device-index",
                sha256(payload),
                {"payload.bin": payload},
            )
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            (assets / "RELEASE_METADATA.json").write_bytes(canonical(metadata))
            current_metadata = json.loads(json.dumps(metadata))
            for position, asset in enumerate(current_metadata["assets"], 1):
                asset["downloadCount"] = position + 10

            with mock.patch.object(
                self.receiver,
                "run_gh",
                side_effect=AssertionError("verified existing assets must not be downloaded again"),
            ):
                reused_metadata, reused_assets = self.receiver.download_release(
                    "gh.exe", REPOSITORY, current_metadata, assets, allow_existing=True
                )
            self.assertEqual(reused_metadata, assets / "RELEASE_METADATA.json")
            self.assertEqual(reused_assets, assets)

            (assets / "payload.bin").write_bytes(b"damaged partial file")
            with self.assertRaisesRegex(self.receiver.ReceiverError, "size mismatch"):
                self.receiver.download_release(
                    "gh.exe", REPOSITORY, current_metadata, assets, allow_existing=True
                )

    def test_resume_rejects_release_metadata_drift(self):
        with tempfile.TemporaryDirectory(prefix="resume-metadata-drift-") as raw:
            destination = Path(raw) / "download"
            destination.mkdir()
            (destination / "RELEASE_METADATA.json").write_bytes(canonical({"tagName": "wrong-release"}))
            metadata = {"tagName": "expected-release", "assets": []}
            with self.assertRaisesRegex(self.receiver.ReceiverError, "metadata does not match"):
                self.receiver.download_all_release_assets(
                    "gh.exe", REPOSITORY, metadata, destination, allow_existing=True
                )

    def test_discovery_uses_exact_bound_skill_without_touching_public_skill(self):
        with tempfile.TemporaryDirectory(prefix="discover-bound-skill-") as raw:
            root = Path(raw)
            public_skill = root / "public" / "cross-device-project-sync"
            write_installed_skill(public_skill, "20261019-v48")
            public_before = self.receiver.skill_tree_sha256(public_skill)
            index_path = root / "INCREMENTAL_DEVICE_INDEX.json"
            index_path.write_bytes(b"historical index")
            historical_root = root / "isolated" / "cross-device-project-sync"
            (historical_root / "scripts").mkdir(parents=True)
            historical_helper = historical_root / "scripts" / "incremental-device-sync.py"
            historical_helper.write_text("# historical verifier\n", encoding="utf-8")
            index = {
                "index_kind": "incremental",
                "index_path": str(index_path),
                "required_skill_version": "20260929-v26",
                "skill_release_tag": "skill-cross-device-project-sync-v26-bound",
                "skill_manifest_sha256": "3" * 64,
            }
            calls = {}

            def fake_validate_release(_metadata, _assets, repository, kind, expected_tag=None):
                calls["release"] = (repository, kind, expected_tag)
                return {"tag": expected_tag}

            def fake_validate_package(_release, _assets, version, manifest_sha256):
                calls["package"] = (version, manifest_sha256)
                return {"manifest_sha256": manifest_sha256}

            def fake_stage(_package, runtime_root, version):
                calls["stage"] = (Path(runtime_root), version)
                return historical_root

            def fake_run(bound_root, received_index, kind):
                calls["run"] = (Path(bound_root), Path(received_index), kind)
                return str(historical_helper)

            with (
                mock.patch.object(self.receiver, "remote_release_metadata", return_value={"tagName": index["skill_release_tag"]}),
                mock.patch.object(self.receiver, "download_release", return_value=(root / "metadata.json", root / "assets")),
                mock.patch.object(self.receiver, "validate_bound_release", side_effect=fake_validate_release),
                mock.patch.object(self.receiver, "validate_skill_package", side_effect=fake_validate_package),
                mock.patch.object(self.receiver, "stage_bound_skill", side_effect=fake_stage),
                mock.patch.object(self.receiver, "run_bound_interpreter", side_effect=fake_run),
                mock.patch.object(self.receiver, "local_module", side_effect=AssertionError("current local verifier must not be used")),
            ):
                result = self.receiver.verify_discovered_index_with_bound_skill(
                    "gh.exe", REPOSITORY, index, root / "temporary"
                )

            self.assertTrue(result["verified"])
            self.assertEqual(calls["release"], (REPOSITORY, "skill-package", index["skill_release_tag"]))
            self.assertEqual(calls["package"], (index["required_skill_version"], index["skill_manifest_sha256"]))
            self.assertEqual(calls["run"], (historical_root, index_path, "incremental"))
            self.assertEqual(self.receiver.skill_tree_sha256(public_skill), public_before)

    def test_discovery_preserves_strict_bound_release_and_manifest_failures(self):
        with tempfile.TemporaryDirectory(prefix="discover-bound-failures-") as raw:
            root = Path(raw)
            index = {
                "index_kind": "incremental",
                "index_path": str(root / "index.json"),
                "required_skill_version": "20260929-v26",
                "skill_release_tag": "skill-cross-device-project-sync-v26-bound",
                "skill_manifest_sha256": "3" * 64,
            }
            common = (
                mock.patch.object(self.receiver, "remote_release_metadata", return_value={"tagName": index["skill_release_tag"]}),
                mock.patch.object(self.receiver, "download_release", return_value=(root / "metadata.json", root / "assets")),
            )
            for failing_function, message in (
                ("validate_bound_release", "Release tag mismatch"),
                ("validate_skill_package", "skill package manifest SHA-256 does not match"),
            ):
                with self.subTest(failing_function=failing_function), common[0], common[1], \
                     mock.patch.object(self.receiver, "validate_bound_release", return_value={"tag": index["skill_release_tag"]}), \
                     mock.patch.object(self.receiver, "validate_skill_package", return_value={"manifest_sha256": index["skill_manifest_sha256"]}), \
                     mock.patch.object(self.receiver, failing_function, side_effect=self.receiver.ReceiverError(message)):
                    with self.assertRaisesRegex(self.receiver.ReceiverError, message):
                        self.receiver.verify_discovered_index_with_bound_skill(
                            "gh.exe", REPOSITORY, index, root / ("temporary-" + failing_function)
                        )

    def test_conversation_extraction_rejects_tampering_transactionally(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            assets = root / "assets"
            assets.mkdir()
            expected = b"verified transcript"
            manifest = {
                "files": [{
                    "path": "threads/thread-1/FULL_TRANSCRIPT.jsonl",
                    "size": len(expected),
                    "sha256": sha256(expected),
                }]
            }
            (assets / "CONVERSATION_PACK_MANIFEST.json").write_bytes(canonical(manifest))
            with zipfile.ZipFile(assets / "CONVERSATION_PACK.zip", "w") as archive:
                archive.writestr("threads/thread-1/FULL_TRANSCRIPT.jsonl", b"tampered transcript")
            destination = root / "restored-conversations"

            with self.assertRaisesRegex(self.receiver.ReceiverError, "member mismatch"):
                self.receiver.extract_conversation_pack(assets, destination)
            self.assertFalse(destination.exists())
            self.assertFalse(any(root.glob(".restored-conversations.stage-*")))

    def test_receive_restores_every_bound_project_and_conversation_pack(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            download_root = base / "download"
            restore_root = base / "restore"
            bound_root = base / "bound-skill"
            (bound_root / "scripts").mkdir(parents=True)
            (bound_root / "scripts" / "restore-full-backup.ps1").write_text("# mocked\n", encoding="utf-8")
            index_path = base / "INCREMENTAL_DEVICE_INDEX.json"
            projects = []
            for number in (1, 2):
                projects.append({
                    "project_id": f"project-{number}",
                    "label": f"Project {number}",
                    "total_bytes": 100 * number,
                    "tree_sha256": str(number) * 64,
                    "project_release": {
                        "tag": f"project-release-{number}",
                        "manifest_sha256": "a" * 64,
                    },
                    "conversation_set_sha256": "b" * 64,
                    "conversation_release": {
                        "tag": f"conversation-release-{number}",
                        "manifest_sha256": "c" * 64,
                    },
                })
            index_payload = {
                "format": "cross-device-project-sync-incremental-device-index-v2",
                "migration_id": "A-device-complete-test",
                "source_device": "A设备",
                "source_computer": "WORKSTATION-A",
                "index_sha256": "d" * 64,
                "projects": projects,
            }

            def metadata(tag):
                return {
                    "tagName": tag,
                    "assets": [{"name": f"{tag}.bin", "size": 10, "digest": "sha256:" + "e" * 64}],
                }

            def fake_download_release(_gh, _repository, release_metadata, destination):
                destination.mkdir(parents=True)
                metadata_path = destination / "RELEASE_METADATA.json"
                metadata_path.write_bytes(canonical(release_metadata))
                return metadata_path, destination

            def fake_download_all(_gh, _repository, release_metadata, destination):
                return fake_download_release(_gh, _repository, release_metadata, destination)

            def fake_load_json(path):
                if Path(path).name == "INCREMENTAL_DEVICE_INDEX.json":
                    return index_payload
                if Path(path).name == "CONVERSATION_PACK_MANIFEST.json":
                    return {"files": [{"path": "thread.jsonl", "size": 25, "sha256": "f" * 64}]}
                raise AssertionError(f"unexpected JSON read: {path}")

            restored_destinations = []
            install_calls = []

            def fake_subprocess_run(arguments, **_kwargs):
                destination = Path(arguments[arguments.index("-Destination") + 1])
                restored_root = destination / "source-project"
                restored_root.mkdir(parents=True)
                restored_destinations.append(restored_root)
                result = {
                    "restored": True,
                    "verified": True,
                    "release_binding_verified": True,
                    "release_tag": arguments[arguments.index("-ReleaseTag") + 1],
                    "manifest_sha256": arguments[arguments.index("-ExpectedManifestSha256") + 1],
                    "repository": arguments[arguments.index("-ExpectedRepository") + 1],
                    "destination": str(destination),
                    "restored_project_root": str(restored_root),
                }
                return SimpleNamespace(returncode=0, stdout="progress\n" + json.dumps(result, indent=2), stderr="")

            def fake_extract(_assets, destination):
                destination.mkdir(parents=True)
                (destination / "thread.jsonl").write_text("verified", encoding="utf-8")

            def fake_install(stage, tree_sha256, caller_home, project_key, base_path, **kwargs):
                install_calls.append({
                    "stage": str(stage), "tree_sha256": tree_sha256,
                    "caller_home": caller_home, "project_key": project_key,
                    "base": base_path, **kwargs,
                })
                return {"destination": str(stage), "verified": True, "action": "restore-side-by-side"}

            args = SimpleNamespace(
                repository=REPOSITORY,
                gh="gh.exe",
                download_root=str(download_root),
                restore_root=str(restore_root),
                installed_skill_root=str(ROOT),
                release_tag="device-index-complete-test",
            )
            release_metadata = {"tagName": args.release_tag, "body": "Content-Kind: device-index"}
            remote_metadata = {args.release_tag: release_metadata}
            for project in projects:
                remote_metadata[project["project_release"]["tag"]] = metadata(project["project_release"]["tag"])
                remote_metadata[project["conversation_release"]["tag"]] = metadata(project["conversation_release"]["tag"])

            with (
                mock.patch.object(self.receiver, "receive_context", return_value={"caller": {"codex_home": str(base / "home"), "instance_id": "a"}, "base": str(base), "selection": {"identity_sha256": None}}),
                mock.patch.object(self.receiver, "resolve_gh", return_value="gh.exe"),
                mock.patch.object(self.receiver, "verify_remote_access"),
                mock.patch.object(self.receiver, "remote_release_metadata", side_effect=lambda _g, _r, tag: remote_metadata[tag]),
                mock.patch.object(self.receiver, "download_release", side_effect=fake_download_release),
                mock.patch.object(self.receiver, "validate_bound_release", return_value={"tag": args.release_tag}),
                mock.patch.object(self.receiver, "discover_index", return_value={
                    "required_skill_version": None,
                    "index_path": str(index_path),
                }),
                mock.patch.object(self.receiver, "command_inspect", return_value={
                    "index_kind": "incremental",
                    "bound_skill_runtime_root": str(bound_root),
                    "index_release_tag": args.release_tag,
                    "installed_skill_version_before": "20261015-v44",
                    "installed_skill_tree_sha256_after": "9" * 64,
                    "required_skill_version": "20261015-v44",
                }),
                mock.patch.object(self.receiver, "load_json", side_effect=fake_load_json),
                mock.patch.object(self.receiver, "require_free_space"),
                mock.patch.object(self.receiver, "resolve_powershell", return_value="pwsh.exe"),
                mock.patch.object(self.receiver, "download_all_release_assets", side_effect=fake_download_all),
                mock.patch.object(self.receiver, "validate_project_release", side_effect=lambda _m, a, _r, p, *_: {
                    "tag": p["project_release"]["tag"],
                    "manifest_sha256": p["project_release"]["manifest_sha256"],
                    "assets_root": str(a),
                    "source_name": "source-project",
                }),
                mock.patch.object(self.receiver, "verify_conversation_release", side_effect=lambda _m, a, _r, p, _b: {
                    "tag": p["conversation_release"]["tag"],
                    "manifest_sha256": p["conversation_release"]["manifest_sha256"],
                    "assets_root": str(a),
                }),
                mock.patch.object(self.receiver, "extract_conversation_pack", side_effect=fake_extract),
                mock.patch.object(self.receiver.subprocess, "run", side_effect=fake_subprocess_run),
                mock.patch.object(self.receiver, "local_module", return_value=SimpleNamespace(install=fake_install)),
            ):
                result = self.receiver._command_receive_locked(
                    args,
                    {
                        "caller": {"codex_home": str(base / "home"), "instance_id": "a"},
                        "base": str(base), "selection": {"identity_sha256": None},
                        "selection_file": str(base / "home" / "versions.json"),
                        "selection_sha256": "7" * 64,
                    },
                    {"path": str(base / "readiness.json"), "sha256": "8" * 64},
                    {
                        "operation_id": "receive-test", "lock_token": "token",
                        "lock_token_sha256": sha256(b"token"),
                    },
                )

            self.assertEqual(result["project_count"], 2)
            self.assertEqual(len(restored_destinations), 2)
            report = json.loads(Path(result["receive_report"]).read_text(encoding="utf-8"))
            self.assertEqual(report["project_count"], 2)
            self.assertEqual([item["project_id"] for item in report["projects"]], ["project-1", "project-2"])
            self.assertTrue(all(Path(item["conversation_pack_path"]).is_dir() for item in report["projects"]))
            self.assertEqual(len(install_calls), 2)
            self.assertTrue(all(item["source_name"] == "source-project" for item in install_calls))
            self.assertTrue(all(item["source_device"] == "A设备" for item in install_calls))

    def test_restore_result_requires_verified_bound_identity_and_expected_path(self):
        with tempfile.TemporaryDirectory(prefix="device-restore-result-") as raw:
            destination = Path(raw) / "destination"
            restored = destination / "project"
            restored.mkdir(parents=True)
            valid = {
                "restored": True,
                "verified": True,
                "release_binding_verified": True,
                "release_tag": "project-tag",
                "manifest_sha256": "a" * 64,
                "repository": REPOSITORY,
                "destination": str(destination),
                "restored_project_root": str(restored),
            }
            self.assertEqual(
                self.receiver.validate_restore_result(
                    valid, destination, REPOSITORY, "project-tag", "a" * 64, "project"
                ),
                str(restored.resolve()),
            )
            for field, wrong in (
                ("verified", False),
                ("release_binding_verified", False),
                ("release_tag", "wrong-tag"),
                ("manifest_sha256", "b" * 64),
                ("repository", "wrong/repo"),
                ("restored_project_root", str(destination / "other")),
            ):
                damaged = dict(valid)
                damaged[field] = wrong
                with self.subTest(field=field), self.assertRaises(self.receiver.ReceiverError):
                    self.receiver.validate_restore_result(
                        damaged, destination, REPOSITORY, "project-tag", "a" * 64, "project"
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
