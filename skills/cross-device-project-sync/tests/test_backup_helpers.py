"""Offline backup, restore, and path-safety regression tests."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import tarfile
import time
import unittest
from unittest import mock
import zipfile
try:
    import winreg
except ModuleNotFoundError:  # pragma: no cover - exercised by portable CI
    winreg = None


parser = argparse.ArgumentParser()
parser.add_argument("--powershell", default=shutil.which("pwsh") or shutil.which("powershell"))
parser.add_argument("--skill-root", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[1])
options, remaining = parser.parse_known_args()
if not options.powershell:
    parser.error("PowerShell is required")


@unittest.skipUnless(os.name == "nt", "Windows-only backup and restore integration tests")
class BackupHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = pathlib.Path(tempfile.mkdtemp(prefix="backup-helper-tests-"))
        computer = os.environ.get("COMPUTERNAME", "").upper()
        cls.device_label = {
            "WORKSTATION-A": "A设备",
            "WORKSTATION-B": "B设备",
        }.get(computer, "test-device")
        cls.codex_home = cls.base / "codex-home"
        cls.codex_home.mkdir()
        (cls.codex_home / "cross-device-project-sync-device.json").write_text(
            json.dumps(
                {
                    "device_label": cls.device_label,
                    "computer_name": os.environ.get("COMPUTERNAME", ""),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        cls.prepare_env = dict(os.environ, CODEX_HOME=str(cls.codex_home))
        cls.source = cls.base / "示例项目"
        cls.backup = cls.base / "backup"
        (cls.source / "media").mkdir(parents=True)
        (cls.source / "empty").mkdir()
        protected = cls.source / "tools" / "browser-test-profile" / "Default" / "Network"
        protected.mkdir(parents=True)
        (protected / "Cookies").write_text("synthetic-only", encoding="utf-8")
        (cls.source / "media" / "video.bin").write_bytes((b"media-test\x00" * 150000)[:1500000])
        (cls.source / ".hidden").write_text("hidden", encoding="utf-8")
        (cls.source / "media" / "中文 文件.txt").write_text("unicode", encoding="utf-8")
        cls.run_command([
            sys.executable,
            options.skill_root / "scripts" / "create-split-tar.py",
            cls.source,
            cls.backup,
            "--part-size-mib",
            "1",
            "--source-device",
            "test-device",
            "--source-computer",
            "TEST-COMPUTER",
            "--exclude-relative",
            "tools/browser-test-profile",
        ])
        for source_name, target_name in (
            ("verify-split-tar.py", "verify_split_tar.py"),
            ("verify-restored-tree.py", "verify_restored_tree.py"),
            ("extract-verified-tar.py", "extract_verified_tar.py"),
            ("restore-full-backup.ps1", "restore.ps1"),
        ):
            shutil.copy2(options.skill_root / "scripts" / source_name, cls.backup / target_name)
        (cls.backup / "README.md").write_text("synthetic backup", encoding="utf-8")
        manifest_sha256 = hashlib.sha256((cls.backup / "BACKUP_MANIFEST.json").read_bytes()).hexdigest()
        (cls.backup / "RELEASE_BINDING.json").write_text(json.dumps({
            "format": "cross-device-project-sync-release-binding-v1",
            "repository": "owner/private-backup",
            "release_tag": "backup-test-001",
            "manifest_sha256": manifest_sha256,
        }), encoding="utf-8")
        cls.run_command([sys.executable, cls.backup / "verify_split_tar.py", cls.backup])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)  # noqa: SEC-AUDITOR -- class-created temporary test root

    @classmethod
    def run_command(cls, args, *, expect=0, timeout=30):
        result = subprocess.run([str(arg) for arg in args], capture_output=True, timeout=timeout)
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        if result.returncode != expect:
            raise AssertionError((args, result.returncode, output))
        return output

    def copy_backup(self, name: str) -> pathlib.Path:
        target = self.base / name
        shutil.copytree(self.backup, target)
        return target

    def bound_restore_args(self, backup: pathlib.Path) -> list[str]:
        manifest_sha256 = hashlib.sha256((backup / "BACKUP_MANIFEST.json").read_bytes()).hexdigest()
        return [
            "-ExpectedRepository", "owner/private-backup",
            "-ExpectedManifestSha256", manifest_sha256,
        ]

    def downgrade_conversation_bundle_to_v1(self, backup: pathlib.Path) -> None:
        archive_path = backup / "CONVERSATION_HANDOFF.zip"
        unpacked = self.base / f"{backup.name}-legacy-conversation"
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(unpacked)
        (unpacked / "RECEIVER_START.md").unlink()
        index_path = unpacked / "CONVERSATION_INDEX.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["format"] = "cross-device-conversation-handoff-v1"
        index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        metadata_path = unpacked / "BUNDLE_METADATA.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["format"] = "cross-device-conversation-handoff-v1"
        metadata["files"] = [
            {"path": path.relative_to(unpacked).as_posix(), "size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in sorted(unpacked.rglob("*"))
            if path.is_file() and path.name != "BUNDLE_METADATA.json"
        ]
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(unpacked.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(unpacked).as_posix())
        manifest_path = backup / "CONVERSATION_HANDOFF_MANIFEST.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["format"] = "cross-device-conversation-handoff-v1"
        manifest["zip_size"] = archive_path.stat().st_size
        manifest["zip_sha256"] = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        manifest["uncompressed_total_bytes"] = sum(path.stat().st_size for path in unpacked.rglob("*") if path.is_file())
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        binding_path = backup / "RELEASE_BINDING.json"
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        binding["conversation_manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        binding_path.write_text(json.dumps(binding, ensure_ascii=False), encoding="utf-8")

    def synthetic_manifest_backup(self, name: str, files: list[tuple[str, bytes]]) -> pathlib.Path:
        target = self.base / name
        target.mkdir()
        buffer = io.BytesIO()
        records = []
        timestamp = 1_700_000_000
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
            root = tarfile.TarInfo("project")
            root.type = tarfile.DIRTYPE
            root.mtime = timestamp
            archive.addfile(root)
            records.append({"path": "project", "type": "5", "size": 0, "mtime": timestamp})
            for relative, payload in files:
                info = tarfile.TarInfo(f"project/{relative}")
                info.size = len(payload)
                info.mtime = timestamp
                archive.addfile(info, io.BytesIO(payload))
                records.append({
                    "path": info.name,
                    "type": "0",
                    "size": len(payload),
                    "mtime": timestamp,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                })
        archive_bytes = buffer.getvalue()
        part_name = "project-full-backup.tar.part001"
        (target / part_name).write_bytes(archive_bytes)
        manifest = {
            "format": "split-tar-pax-v1",
            "archive_stem": "project-full-backup",
            "source_name": "project",
            "part_size_bytes": len(archive_bytes),
            "archive_total_bytes": len(archive_bytes),
            "entry_count": len(records),
            "source_state_changed_during_backup": False,
            "parts": [{
                "name": part_name,
                "size": len(archive_bytes),
                "sha256": hashlib.sha256(archive_bytes).hexdigest(),
            }],
            "entries": records,
        }
        (target / "BACKUP_MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
        return target

    def test_round_trip_and_protected_exclusion(self):
        manifest = json.loads((self.backup / "BACKUP_MANIFEST.json").read_text(encoding="utf-8"))
        restore_instructions = (self.backup / "RESTORE.txt").read_text(encoding="utf-8")
        self.assertIn("restore-full-backup.ps1", restore_instructions)
        self.assertIn("locally installed", restore_instructions)
        self.assertIn("-ReleaseTag", restore_instructions)
        self.assertIn("-ExpectedRepository", restore_instructions)
        self.assertIn("-ExpectedManifestSha256", restore_instructions)
        self.assertEqual(manifest["excluded_source_paths"], ["tools/browser-test-profile"])
        self.assertFalse(any("browser-test-profile" in item["path"] for item in manifest["entries"]))
        self.assertGreaterEqual(len(manifest["parts"]), 2)

        destination = self.base / "restored"
        output = self.run_command([
            options.powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            self.backup / "restore.ps1",
            "-Destination",
            destination,
            "-PythonExe",
            sys.executable,
            "-ReleaseTag",
            "backup-test-001",
            *self.bound_restore_args(self.backup),
        ], timeout=60)
        self.assertIn("capacity check passed", output.lower())
        self.assertEqual((destination / self.source.name / ".hidden").read_text(encoding="utf-8"), "hidden")
        self.assertFalse((destination / self.source.name / "tools" / "browser-test-profile").exists())
        self.assertFalse((self.backup / "project-full-backup.tar").exists())

    def test_long_unicode_paths_round_trip_without_renaming(self):
        source = self.base / "示例项目-超长路径"
        filename = "中文 文件名（含空格）-最终文件.txt"
        current = source
        index = 0
        while len(str(current / filename)) <= 340:
            index += 1
            current /= f"第{index:02d}层-用于验证长路径的目录"
        current.mkdir(parents=True)
        original = current / filename
        original.write_text("over-315-roundtrip-ok", encoding="utf-8")
        (source / "空目录").mkdir()
        self.assertGreater(len(str(original)), 315)

        backup = self.base / "long-unicode-backup"
        self.run_command([
            sys.executable,
            options.skill_root / "scripts" / "create-split-tar.py",
            source,
            backup,
            "--part-size-mib",
            "1",
            "--source-device",
            "test-device",
            "--source-computer",
            "TEST-COMPUTER",
        ])
        manifest_sha256 = hashlib.sha256((backup / "BACKUP_MANIFEST.json").read_bytes()).hexdigest()
        (backup / "RELEASE_BINDING.json").write_text(json.dumps({
            "format": "cross-device-project-sync-release-binding-v1",
            "repository": "owner/private-backup",
            "release_tag": "backup-long-path-test-001",
            "manifest_sha256": manifest_sha256,
        }), encoding="utf-8")

        destination = self.base / "long-unicode-restored"
        self.run_command([
            options.powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            options.skill_root / "scripts" / "restore-full-backup.ps1",
            "-BackupPath",
            backup,
            "-Destination",
            destination,
            "-PythonExe",
            sys.executable,
            "-ReleaseTag",
            "backup-long-path-test-001",
            "-ExpectedRepository",
            "owner/private-backup",
            "-ExpectedManifestSha256",
            manifest_sha256,
        ], timeout=60)
        restored = destination / original.relative_to(source.parent)
        self.assertEqual(restored.name, filename)
        self.assertEqual(restored.read_text(encoding="utf-8"), "over-315-roundtrip-ok")
        self.assertTrue((destination / source.name / "空目录").is_dir())

    def test_prepare_end_to_end_excludes_before_snapshot(self):
        output = self.base / "prepared-output"
        result = subprocess.run([
            options.powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            options.skill_root / "scripts" / "prepare-full-backup.ps1",
            "-SourceRoot",
            self.source,
            "-OutputRoot",
            output,
            "-PartSizeMiB",
            "1",
            "-MaxConvergencePasses",
            "2",
            "-ExcludeRelativePath",
            "tools/browser-test-profile",
            "-PythonExe",
            sys.executable,
            "-DeviceLabel",
            self.device_label,
        ], env=self.prepare_env, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode("utf-8", errors="replace"))
        runs = list(output.glob("full-backup-*"))
        self.assertEqual(len(runs), 1)
        prepared = runs[0] / "prepared-release-assets"
        prepared_manifest = json.loads((prepared / "BACKUP_MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(prepared_manifest["format"], "split-tar-pax-v2")
        self.assertEqual(prepared_manifest["source_device"], self.device_label)
        self.assertEqual(prepared_manifest["source_computer"].upper(), os.environ["COMPUTERNAME"].upper())
        self.assertTrue((prepared / "verify_restored_tree.py").is_file())
        self.assertTrue((prepared / "extract_verified_tar.py").is_file())
        self.assertFalse((runs[0] / "snapshot" / self.source.name / "tools" / "browser-test-profile").exists())
        self.assertFalse((prepared / ".git").exists())

    def test_prepare_requires_explicit_output_root(self):
        result = subprocess.run([
            options.powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            options.skill_root / "scripts" / "prepare-full-backup.ps1",
            "-SourceRoot",
            self.source,
            "-PythonExe",
            sys.executable,
        ], capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)

    def test_prepare_rejects_broad_profile_root_without_override(self):
        output = self.base / "broad-profile-output"
        result = subprocess.run([
            options.powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            options.skill_root / "scripts" / "prepare-full-backup.ps1",
            "-SourceRoot",
            pathlib.Path.home(),
            "-OutputRoot",
            output,
            "-PythonExe",
            sys.executable,
        ], env=self.prepare_env, capture_output=True, timeout=30)
        combined = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("broad container", combined)
        self.assertFalse(output.exists())

    def test_prepare_rejects_actual_windows_downloads_folder(self):
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                raw, _ = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")
        except OSError:
            self.skipTest("Windows Downloads known-folder mapping is unavailable")
        downloads = pathlib.Path(os.path.expandvars(raw))
        if not downloads.is_dir():
            self.skipTest("Windows Downloads known folder does not exist")
        output = self.base / "downloads-root-output"
        result = subprocess.run([
            options.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
            options.skill_root / "scripts" / "prepare-full-backup.ps1",
            "-SourceRoot", downloads, "-OutputRoot", output, "-PythonExe", sys.executable,
        ], env=self.prepare_env, capture_output=True, timeout=30)
        combined = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("broad container", combined)
        self.assertFalse(output.exists())

    def test_prepare_rejects_device_label_bound_to_another_computer(self):
        actual = os.environ.get("COMPUTERNAME", "")
        wrong_label = "A设备"
        wrong_home = self.base / "wrong-device-home"
        wrong_home.mkdir()
        (wrong_home / "cross-device-project-sync-device.json").write_text(
            json.dumps(
                {
                    "device_label": wrong_label,
                    "computer_name": f"{actual}-OTHER",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        env = dict(os.environ, CODEX_HOME=str(wrong_home))
        output = self.base / "wrong-device-output"
        result = subprocess.run([
            options.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
            options.skill_root / "scripts" / "prepare-full-backup.ps1",
            "-SourceRoot", self.source, "-OutputRoot", output, "-PythonExe", sys.executable,
            "-DeviceLabel", wrong_label,
        ], env=env, capture_output=True, timeout=30)
        combined = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must bind computer_name", combined)
        self.assertFalse(output.exists())

    def test_prepare_rejects_output_inside_source_without_creation(self):
        output = self.source / "recursive-prepare-output"
        result = subprocess.run([
            options.powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            options.skill_root / "scripts" / "prepare-full-backup.ps1",
            "-SourceRoot",
            self.source,
            "-OutputRoot",
            output,
            "-PartSizeMiB",
            "1",
            "-MaxConvergencePasses",
            "2",
            "-PythonExe",
            sys.executable,
        ], env=self.prepare_env, capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())

    def test_prepare_requires_explicit_browser_profile_exclusion(self):
        output = self.base / "unprotected-profile-output"
        result = subprocess.run([
            options.powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            options.skill_root / "scripts" / "prepare-full-backup.ps1",
            "-SourceRoot",
            self.source,
            "-OutputRoot",
            output,
            "-PartSizeMiB",
            "1",
            "-MaxConvergencePasses",
            "2",
            "-PythonExe",
            sys.executable,
        ], env=self.prepare_env, capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"ExcludeRelativePath", result.stdout + result.stderr)
        self.assertFalse(output.exists())

    def test_zero_part_size_is_rejected(self):
        output = self.base / "zero-output"
        result = subprocess.run([
            sys.executable,
            options.skill_root / "scripts" / "create-split-tar.py",
            self.source,
            output,
            "--part-size-mib",
            "0",
            "--source-device",
            "test-device",
            "--source-computer",
            "TEST-COMPUTER",
        ], capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())

    def test_output_inside_source_is_rejected_without_creation(self):
        output = self.source / "recursive-output"
        result = subprocess.run([
            sys.executable,
            options.skill_root / "scripts" / "create-split-tar.py",
            self.source,
            output,
            "--source-device",
            "test-device",
            "--source-computer",
            "TEST-COMPUTER",
        ], capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())

    def test_mirror_hash_check_detects_same_size_same_time_change(self):
        source = self.base / "mirror-source"
        snapshot = self.base / "mirror-snapshot"
        source.mkdir()
        original = source / "same-metadata.bin"
        original.write_bytes(b"AAAA")
        shutil.copytree(source, snapshot, copy_function=shutil.copy2, dirs_exist_ok=True)
        self.run_command([
            sys.executable,
            options.skill_root / "scripts" / "verify-mirror-tree.py",
            source,
            snapshot,
        ])
        altered = snapshot / original.name
        altered.write_bytes(b"BBBB")
        source_stat = original.stat()
        os.utime(altered, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
        result = subprocess.run([
            sys.executable,
            options.skill_root / "scripts" / "verify-mirror-tree.py",
            source,
            snapshot,
        ], capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"mismatch", result.stdout + result.stderr)

    def test_mirror_verifier_rechecks_source_after_snapshot_scan(self):
        source = self.base / "mirror-race-source"
        snapshot = self.base / "mirror-race-snapshot"
        source.mkdir()
        payload = source / "payload.txt"
        payload.write_text("old-state", encoding="utf-8")
        shutil.copytree(source, snapshot, copy_function=shutil.copy2, dirs_exist_ok=True)

        script = options.skill_root / "scripts" / "verify-mirror-tree.py"
        spec = importlib.util.spec_from_file_location("verify_mirror_tree_race_test", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original_inventory = module.inventory
        call_count = 0

        def racing_inventory(path, exclusions=()):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                payload.write_text("new-state", encoding="utf-8")
            return original_inventory(path, exclusions)

        with mock.patch.object(module, "inventory", side_effect=racing_inventory), \
                mock.patch.object(sys, "argv", [str(script), str(source), str(snapshot)]):
            with self.assertRaises(SystemExit) as failure:
                module.main()
        self.assertIn("Source changed", str(failure.exception))

    def test_direct_archive_requires_browser_profile_exclusion(self):
        output = self.base / "direct-unprotected-output"
        result = subprocess.run([
            sys.executable,
            options.skill_root / "scripts" / "create-split-tar.py",
            self.source,
            output,
            "--part-size-mib",
            "1",
            "--source-device",
            "test-device",
            "--source-computer",
            "TEST-COMPUTER",
        ], capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())

    def test_unsafe_manifest_paths_are_rejected(self):
        backup = self.copy_backup("unsafe-manifest")
        manifest_path = backup / "BACKUP_MANIFEST.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["entries"][0]["path"] = "../escape.txt"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        result = subprocess.run([sys.executable, backup / "verify_split_tar.py", backup], capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)

    def test_windows_path_aliases_are_rejected_before_restore(self):
        cases = (
            ("case-collision", [("A.txt", b"first"), ("a.txt", b"second")], b"collision"),
            ("reserved-name", [("CON.txt", b"reserved")], b"reserved"),
        )
        for name, files, expected_message in cases:
            with self.subTest(name=name):
                backup = self.synthetic_manifest_backup(name, files)
                result = subprocess.run([
                    sys.executable,
                    options.skill_root / "scripts" / "verify-split-tar.py",
                    backup,
                ], capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_message, (result.stdout + result.stderr).lower())

    def test_archive_timestamp_mismatch_is_rejected(self):
        backup = self.copy_backup("timestamp-manifest")
        manifest_path = backup / "BACKUP_MANIFEST.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["entries"][0]["mtime"] += 60
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        result = subprocess.run([sys.executable, backup / "verify_split_tar.py", backup], capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)

    def test_archive_rejects_nonzero_data_after_tar_end_marker(self):
        backup = self.copy_backup("trailing-archive-data")
        manifest_path = backup / "BACKUP_MANIFEST.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        final_part = backup / manifest["parts"][-1]["name"]
        tail = b"UNMANIFESTED-TRAILING-DATA" + bytes(10240 - 26)
        with final_part.open("ab") as handle:
            handle.write(tail)
        manifest["parts"][-1]["size"] = final_part.stat().st_size
        manifest["parts"][-1]["sha256"] = hashlib.sha256(final_part.read_bytes()).hexdigest()
        manifest["archive_total_bytes"] = sum(part["size"] for part in manifest["parts"])
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, backup / "verify_split_tar.py", backup],
            capture_output=True,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"after the TAR end marker", result.stdout + result.stderr)

    def test_restored_timestamp_mismatch_is_rejected(self):
        backup = self.copy_backup("restored-timestamp-backup")
        destination = self.base / "restored-timestamp-destination"
        self.run_command([
            options.powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            backup / "restore.ps1",
            "-Destination",
            destination,
            "-PythonExe",
            sys.executable,
            "-ReleaseTag",
            "backup-test-001",
            *self.bound_restore_args(backup),
        ], timeout=60)
        altered = destination / self.source.name / ".hidden"
        current = altered.stat()
        os.utime(altered, ns=(current.st_atime_ns, current.st_mtime_ns + 60_000_000_000))
        result = subprocess.run([
            sys.executable,
            backup / "verify_restored_tree.py",
            backup,
            destination,
        ], capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"timestamp mismatch", result.stdout + result.stderr)

    def test_restore_holds_read_only_file_locks_through_success(self):
        backup = self.copy_backup("restore-write-lock-backup")
        helper = self.base / "restore-write-lock-helper"
        helper.mkdir()
        shutil.copy2(options.skill_root / "scripts" / "restore-full-backup.ps1", helper)
        shutil.copy2(options.skill_root / "scripts" / "verify-split-tar.py", helper)
        shutil.copy2(options.skill_root / "scripts" / "extract-verified-tar.py", helper)
        signal = self.base / "restore-write-lock.signal"
        wrapper = helper / "verify-restored-tree.py"
        wrapper.write_text(
            "import os, pathlib, subprocess, sys, time\n"
            "result=subprocess.run([sys.executable, os.environ['REAL_RESTORE_VERIFIER'], *sys.argv[1:]])\n"
            "if result.returncode: raise SystemExit(result.returncode)\n"
            "pathlib.Path(os.environ['RESTORE_LOCK_SIGNAL']).write_text('verified')\n"
            "time.sleep(2)\n",
            encoding="utf-8",
        )
        destination = self.base / "restore-write-lock-destination"
        env = dict(
            os.environ,
            REAL_RESTORE_VERIFIER=str(options.skill_root / "scripts" / "verify-restored-tree.py"),
            RESTORE_LOCK_SIGNAL=str(signal),
        )
        process = subprocess.Popen([
            options.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", helper / "restore-full-backup.ps1", "-BackupPath", backup,
            "-Destination", destination, "-PythonExe", sys.executable,
            "-ReleaseTag", "backup-test-001", *self.bound_restore_args(backup),
        ], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 20
            while not signal.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if not signal.exists():
                stdout, stderr = process.communicate(timeout=5)
                self.fail((stdout + stderr).decode("utf-8", errors="replace"))
            target = destination / self.source.name / ".hidden"
            with self.assertRaises(OSError):
                target.write_text("changed after verification", encoding="utf-8")
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            self.assertEqual(target.read_text(encoding="utf-8"), "hidden")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    def test_restore_refuses_nonempty_destination(self):
        backup = self.copy_backup("nonempty-backup")
        destination = self.base / "nonempty-destination"
        destination.mkdir()
        sentinel = destination / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        result = subprocess.run([
            options.powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            backup / "restore.ps1",
            "-Destination",
            destination,
            "-PythonExe",
            sys.executable,
            "-ReleaseTag",
            "backup-test-001",
        ], capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        self.assertFalse((backup / "project-full-backup.tar").exists())

    def test_restore_rejects_junction_destination(self):
        backup = self.copy_backup("junction-backup")
        real_destination = self.base / "junction-real-destination"
        junction = self.base / "junction-destination"
        real_destination.mkdir()
        junction_helper = self.base / "make-junction.ps1"
        junction_helper.write_text(
            "param([string]$Path, [string]$Target)\n"
            "New-Item -ItemType Junction -Path $Path -Target $Target | Out-Null\n",
            encoding="utf-8-sig",
        )
        created = subprocess.run([
            options.powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            junction_helper,
            "-Path",
            str(junction),
            "-Target",
            str(real_destination),
        ], capture_output=True, timeout=30)
        if created.returncode != 0:
            detail = (created.stdout + created.stderr).decode("utf-8", errors="replace")
            self.skipTest(f"Junction creation is unavailable in this test environment: {detail}")
        result = subprocess.run([
            options.powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            backup / "restore.ps1",
            "-Destination",
            junction,
            "-PythonExe",
            sys.executable,
            "-ReleaseTag",
            "backup-test-001",
        ], capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(real_destination.iterdir()), [])
        self.assertFalse((backup / "project-full-backup.tar").exists())

    def test_restore_requires_release_tag(self):
        backup = self.copy_backup("missing-release-tag-backup")
        destination = self.base / "missing-release-tag-destination"
        result = subprocess.run([
            options.powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            backup / "restore.ps1",
            "-Destination",
            destination,
            "-PythonExe",
            sys.executable,
        ], capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(destination.exists())

        wrong_destination = self.base / "wrong-release-tag-destination"
        wrong = subprocess.run([
            options.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", backup / "restore.ps1", "-Destination", wrong_destination,
            "-PythonExe", sys.executable, "-ReleaseTag", "backup-test-002",
        ], capture_output=True, timeout=30)
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn(b"RELEASE_BINDING", wrong.stdout + wrong.stderr)
        self.assertFalse(wrong_destination.exists())

        missing_identity_destination = self.base / "missing-bound-identity-destination"
        missing_identity = subprocess.run([
            options.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", backup / "restore.ps1", "-Destination", missing_identity_destination,
            "-PythonExe", sys.executable, "-ReleaseTag", "backup-test-001",
        ], capture_output=True, timeout=30)
        self.assertNotEqual(missing_identity.returncode, 0)
        self.assertIn(b"ExpectedRepository", missing_identity.stdout + missing_identity.stderr)
        self.assertFalse(missing_identity_destination.exists())

        wrong_repository_destination = self.base / "wrong-bound-repository-destination"
        wrong_repository = subprocess.run([
            options.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", backup / "restore.ps1", "-Destination", wrong_repository_destination,
            "-PythonExe", sys.executable, "-ReleaseTag", "backup-test-001",
            "-ExpectedRepository", "owner/wrong-backup",
            "-ExpectedManifestSha256", self.bound_restore_args(backup)[3],
        ], capture_output=True, timeout=30)
        self.assertNotEqual(wrong_repository.returncode, 0)
        self.assertIn(b"expected repository", (wrong_repository.stdout + wrong_repository.stderr).lower())
        self.assertFalse(wrong_repository_destination.exists())

        legacy = self.copy_backup("legacy-unbound-release-backup")
        (legacy / "RELEASE_BINDING.json").unlink()
        legacy_hash = hashlib.sha256((legacy / "BACKUP_MANIFEST.json").read_bytes()).hexdigest()
        legacy_destination = self.base / "legacy-unbound-release-destination"
        legacy_blocked = subprocess.run([
            options.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", legacy / "restore.ps1", "-Destination", legacy_destination,
            "-PythonExe", sys.executable, "-ReleaseTag", "backup-test-legacy",
        ], capture_output=True, timeout=30)
        self.assertNotEqual(legacy_blocked.returncode, 0)
        self.assertFalse(legacy_destination.exists())
        legacy_output = self.run_command([
            options.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", legacy / "restore.ps1", "-Destination", legacy_destination,
            "-PythonExe", sys.executable, "-ReleaseTag", "backup-test-legacy",
            "-ExpectedManifestSha256", legacy_hash, "-AllowLegacyUnboundRelease",
        ], timeout=60)
        self.assertRegex(legacy_output, r'"release_binding_verified"\s*:\s*false')
        self.assertIn("release tag not cryptographically bound", legacy_output)

    def test_system_backup_tools_ignore_path_shadowing(self):
        fake_bin = self.base / "shadowed-system-tools"
        fake_bin.mkdir()
        command_processor = pathlib.Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"
        shutil.copy2(command_processor, fake_bin / "robocopy.exe")
        shutil.copy2(command_processor, fake_bin / "tar.exe")
        env = dict(self.prepare_env, PATH=str(fake_bin) + os.pathsep + os.environ.get("PATH", ""))
        for script_name in ("prepare-full-backup.ps1", "restore-full-backup.ps1"):
            script_text = (options.skill_root / "scripts" / script_name).read_text(encoding="utf-8-sig")
            self.assertIn("[Environment]::SystemDirectory", script_text)
            self.assertNotIn("$env:SystemRoot", script_text)

        output = self.base / "system-tool-output"
        prepared = subprocess.run([
            options.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            options.skill_root / "scripts" / "prepare-full-backup.ps1",
            "-SourceRoot", self.source,
            "-OutputRoot", output,
            "-PartSizeMiB", "1",
            "-MaxConvergencePasses", "2",
            "-ExcludeRelativePath", "tools/browser-test-profile",
            "-PythonExe", sys.executable,
        ], env=env, capture_output=True, timeout=60)
        self.assertEqual(prepared.returncode, 0, (prepared.stdout + prepared.stderr).decode("utf-8", errors="replace"))

        backup = self.copy_backup("system-tool-restore-backup")
        destination = self.base / "system-tool-restore"
        restored = subprocess.run([
            options.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            backup / "restore.ps1",
            "-Destination", destination,
            "-PythonExe", sys.executable,
            "-ReleaseTag", "backup-test-001",
            *self.bound_restore_args(backup),
        ], env=env, capture_output=True, timeout=60)
        self.assertEqual(restored.returncode, 0, (restored.stdout + restored.stderr).decode("utf-8", errors="replace"))
        self.assertTrue((destination / self.source.name / ".hidden").is_file())

    def test_upload_helper_draft_verify_publish_flow(self):
        fake = self.base / "fake-gh.py"
        state = self.base / "fake-gh-state.json"
        launcher = self.base / "fake-gh.cmd"
        fake.write_text(
            "import hashlib, json, os, pathlib, sys\n"
            "args=sys.argv[1:]\n"
            "mode=os.environ.get('FAKE_GH_MODE','')\n"
            "state_path=pathlib.Path(os.environ['FAKE_GH_STATE'])\n"
            "counter_path=state_path.with_suffix('.repo-views')\n"
            "def load(): return json.loads(state_path.read_text()) if state_path.exists() else None\n"
            "def save(value): state_path.write_text(json.dumps(value))\n"
            "if args[:2]==['auth','status']: raise SystemExit(0)\n"
            "expected_repo='github.com/owner/private-backup'\n"
            "if args[:2]==['repo','view']:\n"
            " if args[2].casefold()!=expected_repo: raise SystemExit(12)\n"
            " count=int(counter_path.read_text())+1 if counter_path.exists() else 1; counter_path.write_text(str(count)); private=mode!='public'\n"
            " if mode=='mutate_manifest' and count==1:\n"
            "  path=pathlib.Path(os.environ['FAKE_MUTATE_MANIFEST']); data=json.loads(path.read_text(encoding='utf-8-sig')); data['source_device']='changed-after-verification'; path.write_text(json.dumps(data),encoding='utf-8')\n"
            " if mode=='public_before_publish' and count>=2: private=False\n"
            " if mode in ('public_after_publish','public_after_publish_rollback_noop') and count>=3: private=False\n"
            " print(json.dumps({'isPrivate':private,'defaultBranchRef':{'name':'master' if mode=='default_master' else 'main'},'nameWithOwner':'owner/private-backup','url':'https://github.com/owner/private-backup'})); raise SystemExit(0)\n"
            "if args[:2]==['api','graphql']:\n"
            " if mode=='probe_failure': raise SystemExit(7)\n"
            " value=load(); release=None if value is None else {'tagName':value['tag']}\n"
            " print(json.dumps({'data':{'repository':{'release':release}}})); raise SystemExit(0)\n"
            "if args[:2]==['release','view']:\n"
            " if args[args.index('--repo')+1]!=expected_repo: raise SystemExit(12)\n"
            " value=load()\n"
            " if value is None: raise SystemExit(1)\n"
            " result={'isDraft':value['draft'],'assets':value['assets'],'url':'https://github.com/owner/private-backup/releases/tag/'+value['tag'],'tagName':value['tag'],'body':value.get('body','')}\n"
            " if 'url' in args[-1]:\n"
            "  if mode=='changed_to_draft': result['isDraft']=True\n"
            "  if mode=='missing_status': result.pop('isDraft')\n"
            "  if mode=='string_status': result['isDraft']='false'\n"
            "  if mode=='wrong_tag': result['tagName']='other-backup'\n"
            " print(json.dumps(result)); raise SystemExit(7 if mode=='view_failure' and 'url' in args[-1] else 0)\n"
            "if args[:2]==['release','create']:\n"
            " if args[args.index('--repo')+1]!=expected_repo: raise SystemExit(12)\n"
            " if load() is not None: raise SystemExit(1)\n"
            " target=args[args.index('--target')+1]; body=' '.join(args[args.index('--notes')+1:]); save({'draft':True,'assets':[],'tag':args[2],'target':target,'body':body}); print('https://github.com/owner/private-backup/releases/tag/'+args[2]); raise SystemExit(0)\n"
            "if args[:2]==['release','upload']:\n"
            " if args[args.index('--repo')+1]!=expected_repo: raise SystemExit(12)\n"
            " if '--clobber' in args: raise SystemExit(13)\n"
            " if mode=='interrupted': raise SystemExit(9)\n"
            " value=load(); path=pathlib.Path(args[3]); data=path.read_bytes(); asset={'name':path.name,'size':len(data),'digest':'sha256:'+hashlib.sha256(data).hexdigest(),'state':'uploaded'}\n"
            " if any(item['name']==path.name for item in value['assets']): raise SystemExit(14)\n"
            " value['assets'].append(asset); save(value); raise SystemExit(0)\n"
            "if args[:2]==['release','edit']:\n"
            " if args[args.index('--repo')+1]!=expected_repo: raise SystemExit(12)\n"
            " if mode=='public_after_publish_rollback_noop' and '--draft=true' in args: raise SystemExit(0)\n"
            " value=load(); value['draft']='--draft=true' in args; save(value); raise SystemExit(0)\n"
            "raise SystemExit(2)\n",
            encoding="utf-8",
        )
        launcher.write_text(f'@echo off\r\n"{sys.executable}" "{fake}" %*\r\n', encoding="utf-8")
        env = os.environ.copy()
        env["FAKE_GH_STATE"] = str(state)
        env["FAKE_MUTATE_MANIFEST"] = str(self.backup / "BACKUP_MANIFEST.json")
        env["GH_HOST"] = "wrong-host.invalid"
        (self.backup / "RESTORE.txt").write_text("run untrusted-helper.ps1\n", encoding="utf-8")
        result = subprocess.run([
            options.powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            options.skill_root / "scripts" / "upload-prepared-backup.ps1",
            "-PreparedBackupPath",
            self.backup,
            "-Repository",
            "OWNER/PRIVATE-BACKUP",
            "-ReleaseTag",
            "backup-test-001",
            "-GitHubCli",
            launcher,
            "-PythonExe",
            sys.executable,
        ], env=env, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode("utf-8", errors="replace"))
        final = json.loads(state.read_text())
        manifest = json.loads((self.backup / "BACKUP_MANIFEST.json").read_text(encoding="utf-8"))
        self.assertFalse(final["draft"])
        self.assertEqual(len(final["assets"]), 8 + len(manifest["parts"]))
        trusted_restore = (self.backup / "RESTORE.txt").read_text(encoding="utf-8-sig")
        self.assertNotIn("untrusted-helper", trusted_restore)
        self.assertIn("backup-test-001", trusted_restore)
        binding = json.loads((self.backup / "RELEASE_BINDING.json").read_text(encoding="utf-8-sig"))
        expected_binding_bytes = (
            '{"format":"cross-device-project-sync-release-binding-v1",'
            '"repository":"owner/private-backup","release_tag":"backup-test-001",'
            f'"manifest_sha256":"{hashlib.sha256((self.backup / "BACKUP_MANIFEST.json").read_bytes()).hexdigest()}"}}\n'
        ).encode("utf-8")
        self.assertEqual((self.backup / "RELEASE_BINDING.json").read_bytes(), expected_binding_bytes)
        for generated_name in ("RELEASE_BINDING.json", "README.md", "RESTORE.txt"):
            generated_bytes = (self.backup / generated_name).read_bytes()
            self.assertFalse(generated_bytes.startswith(b"\xef\xbb\xbf"))
            self.assertNotIn(b"\r", generated_bytes)
        self.assertEqual(binding["release_tag"], "backup-test-001")
        self.assertEqual(binding["repository"], "owner/private-backup")
        self.assertEqual(binding["manifest_sha256"], hashlib.sha256((self.backup / "BACKUP_MANIFEST.json").read_bytes()).hexdigest())
        decoded_result = result.stdout.decode("utf-8", errors="replace")
        self.assertRegex(decoded_result, r'"remote_assets_verified"\s*:\s*true')
        self.assertRegex(decoded_result, r'"restore_verified"\s*:\s*false')
        self.assertRegex(decoded_result, r'"uploaded_this_run"\s*:\s*true')
        self.assertRegex(decoded_result, r'"uploaded_asset_count_this_run"\s*:\s*[1-9]\d*')
        self.assertRegex(decoded_result, r'"published_this_run"\s*:\s*true')
        self.assertRegex(decoded_result, r'"operation"\s*:\s*"uploaded_and_published_release"')
        self.assertRegex(decoded_result, r'"repository"\s*:\s*"owner/private-backup"')
        repeated = subprocess.run([
            options.powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            options.skill_root / "scripts" / "upload-prepared-backup.ps1",
            "-PreparedBackupPath",
            self.backup,
            "-Repository",
            "OWNER/PRIVATE-BACKUP",
            "-ReleaseTag",
            "backup-test-001",
            "-GitHubCli",
            launcher,
            "-PythonExe",
            sys.executable,
        ], env=env, capture_output=True, timeout=60)
        self.assertEqual(repeated.returncode, 0, (repeated.stdout + repeated.stderr).decode("utf-8", errors="replace"))
        repeated_output = repeated.stdout.decode("utf-8", errors="replace")
        self.assertRegex(repeated_output, r'"uploaded_this_run"\s*:\s*false')
        self.assertRegex(repeated_output, r'"uploaded_asset_count_this_run"\s*:\s*0')
        self.assertRegex(repeated_output, r'"published_this_run"\s*:\s*false')
        self.assertRegex(repeated_output, r'"operation"\s*:\s*"verified_existing_release"')
        repeated_state = json.loads(state.read_text())
        self.assertFalse(repeated_state["draft"])
        self.assertEqual(repeated_state["assets"], final["assets"])

        state.unlink()
        master = subprocess.run(repeated.args, env=dict(env, FAKE_GH_MODE="default_master"), capture_output=True, timeout=60)
        self.assertEqual(master.returncode, 0, (master.stdout + master.stderr).decode("utf-8", errors="replace"))
        self.assertEqual(json.loads(state.read_text())["target"], "master")
        state.write_text(json.dumps(final))

        state.unlink()
        probe_failure = subprocess.run(
            repeated.args,
            env=dict(env, FAKE_GH_MODE="probe_failure"),
            capture_output=True,
            timeout=60,
        )
        self.assertNotEqual(probe_failure.returncode, 0)
        self.assertFalse(state.exists(), "A failed existence query must not create a Release")
        self.assertIn(b"No Release was created or modified", probe_failure.stdout + probe_failure.stderr)
        state.write_text(json.dumps(final))

        failures = []
        for mode in ('changed_to_draft', 'missing_status', 'string_status', 'wrong_tag', 'view_failure', 'public', 'interrupted', 'digest_mismatch', 'draft_identity_mismatch', 'public_before_publish', 'public_after_publish'):
            seeded = json.loads(json.dumps(final))
            if mode == 'interrupted':
                seeded.update(draft=True, assets=[])
            if mode == 'digest_mismatch':
                seeded['assets'][0]['digest'] = 'sha256:' + '0' * 64
            if mode == 'draft_identity_mismatch':
                seeded.update(draft=True, body='Backup-Manifest-SHA256: ' + '0' * 64)
            if mode in ('public_before_publish', 'public_after_publish'):
                seeded.update(draft=True)
            state.write_text(json.dumps(seeded))
            counter = state.with_suffix('.repo-views')
            if counter.exists():
                counter.unlink()
            fault_env = dict(env, FAKE_GH_MODE=mode)
            fault = subprocess.run(repeated.args, env=fault_env, capture_output=True, timeout=60)
            after = json.loads(state.read_text())
            if fault.returncode == 0 or b'"release_is_published":  true' in fault.stdout:
                failures.append(mode + ': incorrectly reported success')
            if after != seeded:
                failures.append(mode + ': unexpectedly mutated release')
        self.assertEqual(failures, [], '\n'.join(failures))

        manifest_path = self.backup / "BACKUP_MANIFEST.json"
        original_manifest = manifest_path.read_bytes()
        state.write_text(json.dumps(final))
        counter = state.with_suffix('.repo-views')
        if counter.exists():
            counter.unlink()
        try:
            mutation = subprocess.run(
                repeated.args,
                env=dict(env, FAKE_GH_MODE="mutate_manifest"),
                capture_output=True,
                timeout=60,
            )
        finally:
            manifest_path.write_bytes(original_manifest)
        self.assertNotEqual(mutation.returncode, 0)
        self.assertIn(b"changed", mutation.stdout + mutation.stderr)
        self.assertEqual(json.loads(state.read_text()), final)

        seeded = json.loads(json.dumps(final))
        seeded.update(draft=True)
        state.write_text(json.dumps(seeded))
        counter = state.with_suffix('.repo-views')
        if counter.exists():
            counter.unlink()
        rollback_failure = subprocess.run(
            repeated.args,
            env=dict(env, FAKE_GH_MODE="public_after_publish_rollback_noop"),
            capture_output=True,
            timeout=60,
        )
        rollback_output = rollback_failure.stdout + rollback_failure.stderr
        self.assertNotEqual(rollback_failure.returncode, 0)
        self.assertIn(b"potentially exposed", rollback_output)
        self.assertFalse(json.loads(state.read_text())["draft"])

    def test_upload_helper_rejects_hardlinked_support_file_before_writing(self):
        backup = self.copy_backup("upload-hardlink-backup")
        victim = self.base / "upload-hardlink-victim.txt"
        victim.write_text("do not change", encoding="utf-8")
        readme = backup / "README.md"
        readme.unlink()
        os.link(victim, readme)

        fake = self.base / "hardlink-fake-gh.py"
        launcher = self.base / "hardlink-fake-gh.cmd"
        fake.write_text(
            "import json, sys\n"
            "args=sys.argv[1:]\n"
            "if args[:2]==['auth','status']: raise SystemExit(0)\n"
            "if args[:2]==['repo','view']:\n"
            " print(json.dumps({'isPrivate':True,'defaultBranchRef':{'name':'main'},"
            "'nameWithOwner':'owner/private-backup','url':'https://github.com/owner/private-backup'}))\n"
            " raise SystemExit(0)\n"
            "raise SystemExit(2)\n",
            encoding="utf-8",
        )
        launcher.write_text(f'@"{sys.executable}" "{fake}" %*\n', encoding="utf-8")
        result = subprocess.run([
            options.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", options.skill_root / "scripts" / "upload-prepared-backup.ps1",
            "-PreparedBackupPath", backup, "-Repository", "owner/private-backup",
            "-ReleaseTag", "backup-hardlink-test", "-GitHubCli", launcher,
            "-PythonExe", sys.executable,
        ], capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        normalized = b"".join((result.stdout + result.stderr).split())
        self.assertIn(b"single-link", normalized)
        self.assertIn(b"README.md", normalized)
        self.assertEqual(victim.read_text(encoding="utf-8"), "do not change")

    def test_restore_can_use_trusted_external_verifiers(self):
        backup = self.copy_backup("trusted-helper-backup")
        (backup / "verify_split_tar.py").write_text("raise SystemExit('untrusted verifier executed')\n", encoding="utf-8")
        (backup / "verify_restored_tree.py").write_text("raise SystemExit('untrusted verifier executed')\n", encoding="utf-8")
        (backup / "extract_verified_tar.py").write_text("raise SystemExit('untrusted extractor executed')\n", encoding="utf-8")
        destination = self.base / "trusted-helper-restored"
        self.run_command([
            options.powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            options.skill_root / "scripts" / "restore-full-backup.ps1",
            "-BackupPath",
            backup,
            "-Destination",
            destination,
            "-PythonExe",
            sys.executable,
            "-ReleaseTag",
            "backup-test-001",
            *self.bound_restore_args(backup),
        ], timeout=60)
        self.assertEqual((destination / self.source.name / ".hidden").read_text(encoding="utf-8"), "hidden")

    def test_hardlinked_files_round_trip_as_complete_regular_files(self):
        source = self.base / "hardlink-source"
        source.mkdir()
        payload = b"synthetic hardlinked file\x00" * 500
        (source / "first.bin").write_bytes(payload)
        os.link(source / "first.bin", source / "second.bin")
        self.assertTrue(os.path.samefile(source / "first.bin", source / "second.bin"))
        backup = self.base / "hardlink-backup"
        self.run_command([
            sys.executable,
            options.skill_root / "scripts" / "create-split-tar.py",
            source,
            backup,
            "--source-device",
            "test-device",
            "--source-computer",
            "TEST-COMPUTER",
        ])
        for source_name, target_name in (
            ("verify-split-tar.py", "verify_split_tar.py"),
            ("verify-restored-tree.py", "verify_restored_tree.py"),
            ("extract-verified-tar.py", "extract_verified_tar.py"),
            ("restore-full-backup.ps1", "restore.ps1"),
        ):
            shutil.copy2(options.skill_root / "scripts" / source_name, backup / target_name)
        manifest_sha256 = hashlib.sha256((backup / "BACKUP_MANIFEST.json").read_bytes()).hexdigest()
        (backup / "RELEASE_BINDING.json").write_text(json.dumps({
            "format": "cross-device-project-sync-release-binding-v1",
            "repository": "owner/private-backup",
            "release_tag": "backup-test-001",
            "manifest_sha256": manifest_sha256,
        }), encoding="utf-8")
        self.run_command([sys.executable, backup / "verify_split_tar.py", backup])
        destination = self.base / "hardlink-restored"
        self.run_command([
            options.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            backup / "restore.ps1", "-Destination", destination, "-PythonExe", sys.executable,
            "-ReleaseTag", "backup-test-001",
            *self.bound_restore_args(backup),
        ])
        first = destination / source.name / "first.bin"
        second = destination / source.name / "second.bin"
        self.assertEqual(first.read_bytes(), payload)
        self.assertEqual(second.read_bytes(), payload)
        self.assertFalse(os.path.samefile(first, second))

    def test_bound_conversation_handoff_restores_beside_project(self):
        backup = self.copy_backup("conversation-bound-backup")
        thread_id = "01a00000-1111-7222-8333-555555555555"
        codex_home = self.base / "conversation-codex-home"
        session_dir = codex_home / "sessions" / "2026" / "09" / "10"
        session_dir.mkdir(parents=True)
        image = base64.b64encode(b"\x89PNG\r\n\x1a\nconversation-image").decode("ascii")
        records = [
            {"timestamp": "2026-09-10T00:00:00Z", "ordinal": 1, "type": "session_meta", "payload": {"id": thread_id, "session_id": thread_id}},
            {"timestamp": "2026-09-10T00:00:01Z", "ordinal": 2, "type": "response_item", "payload": {"type": "message", "id": "u1", "role": "user", "content": [{"type": "input_text", "text": "Preserve the exact discussion."}, {"type": "input_image", "image_url": "data:image/png;base64," + image}]}},
            {"timestamp": "2026-09-10T00:00:02Z", "ordinal": 3, "type": "response_item", "payload": {"type": "message", "id": "a1", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "The exact discussion is preserved."}]}},
        ]
        session = session_dir / f"rollout-2026-09-10T00-00-00-{thread_id}.jsonl"
        session.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        work = self.base / "conversation-work-integration"
        self.run_command([
            sys.executable, options.skill_root / "scripts" / "export-conversation-handoff.py", "prepare",
            "--prepared-backup", backup, "--work-dir", work, "--thread-id", thread_id,
            "--project-root", self.source,
            "--thread-title", "Integration task", "--codex-home", codex_home,
            "--device-label", "B device",
        ])
        headings = (
            "Purpose And Requested Outcome", "Chronological Narrative", "Decisions And Rationale",
            "Work Performed", "Files And Artifacts", "Failures, Corrections, And Rejected Approaches",
            "Current State", "Unresolved Questions And Next Actions", "User Preferences And Constraints",
            "Verification And Evidence",
        )
        handoff = ["# Detailed Conversation Handoff", ""]
        for heading in headings:
            handoff.extend((f"## {heading}", "", f"Detailed integration evidence for {heading}, retained for the receiver.", ""))
        (work / "DETAILED_HANDOFF.md").write_text("\n".join(handoff), encoding="utf-8")
        self.run_command([
            sys.executable, options.skill_root / "scripts" / "export-conversation-handoff.py", "finalize",
            "--prepared-backup", backup, "--work-dir", work,
        ])
        manifest_sha256 = hashlib.sha256((backup / "BACKUP_MANIFEST.json").read_bytes()).hexdigest()
        conversation_sha256 = hashlib.sha256((backup / "CONVERSATION_HANDOFF_MANIFEST.json").read_bytes()).hexdigest()
        (backup / "RELEASE_BINDING.json").write_text(json.dumps({
            "format": "cross-device-project-sync-release-binding-v2",
            "repository": "owner/private-backup",
            "release_tag": "backup-test-001",
            "manifest_sha256": manifest_sha256,
            "conversation_manifest_sha256": conversation_sha256,
        }), encoding="utf-8")
        destination = self.base / "conversation-restored"
        output = self.run_command([
            options.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            options.skill_root / "scripts" / "restore-full-backup.ps1",
            "-BackupPath", backup, "-Destination", destination, "-PythonExe", sys.executable,
            "-ReleaseTag", "backup-test-001", "-ExpectedRepository", "owner/private-backup",
            "-ExpectedManifestSha256", manifest_sha256,
        ], timeout=60)
        self.assertRegex(output, r'"conversation_handoff_restored"\s*:\s*true')
        self.assertRegex(output, r'"restored_project_root"\s*:')
        self.assertRegex(output, r'"receiver_bootstrap_path"\s*:')
        self.assertRegex(output, r'"receiver_bootstrap_kind"\s*:\s*"receiver-start-v3"')
        self.assertRegex(output, r'"conversation_lineage_task_count"\s*:\s*1')
        self.assertRegex(output, r'"conversation_lineage_message_count"\s*:\s*2')
        self.assertRegex(output, r'"conversation_cutoff"\s*:\s*"2026-09-10T00:00:02Z"')
        self.assertRegex(output, r'"receiver_task_plan_status"\s*:\s*"pending-app-task-creation"')
        self.assertRegex(output, r'"receiver_operation_key"\s*:\s*"[0-9a-f]{64}"')
        self.assertRegex(output, r'"receiver_task_receipt_path"\s*:')
        self.assertRegex(output, r'"codex_receiving_task_created"\s*:\s*false')
        self.assertRegex(output, r'"conversation_continuation_complete"\s*:\s*false')
        self.assertTrue((destination / "_codex-conversation-handoff" / "DETAILED_HANDOFF.md").is_file())
        self.assertTrue((destination / "_codex-conversation-handoff" / "RECEIVER_START.md").is_file())
        plan_path = destination / "_codex-receiving-task-plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(plan["format"], "cross-device-project-sync-receiver-task-plan-v2")
        self.assertEqual(plan["status"], "pending-app-task-creation")
        self.assertEqual(pathlib.Path(plan["restored_project_root"]), destination / self.source.name)
        self.assertEqual(plan["receiver_bootstrap_kind"], "receiver-start-v3")
        self.assertEqual(plan["lineage_message_count"], 2)
        self.assertEqual(plan["lineage_attachment_count"], 1)
        self.assertEqual(plan["conversation_cutoff"], "2026-09-10T00:00:02Z")
        self.assertEqual(len(plan["operation_key"]), 64)
        self.assertIn(plan["operation_key"], plan["initial_prompt"])
        self.assertEqual(pathlib.Path(plan["receipt_path"]), destination / "_codex-receiving-task-receipt.json")
        self.assertIn("Do not modify project files in your first response", plan["initial_prompt"])
        self.assertTrue((destination / self.source.name / ".hidden").is_file())

        legacy_backup = self.base / "conversation-bound-backup-v1"
        shutil.copytree(backup, legacy_backup)
        self.downgrade_conversation_bundle_to_v1(legacy_backup)
        legacy_destination = self.base / "conversation-restored-v1"
        legacy_output = self.run_command([
            options.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            options.skill_root / "scripts" / "restore-full-backup.ps1",
            "-BackupPath", legacy_backup, "-Destination", legacy_destination, "-PythonExe", sys.executable,
            "-ReleaseTag", "backup-test-001", "-ExpectedRepository", "owner/private-backup",
            "-ExpectedManifestSha256", manifest_sha256,
        ], timeout=60)
        self.assertRegex(legacy_output, r'"receiver_bootstrap_kind"\s*:\s*"legacy-detailed-handoff-v1"')
        self.assertRegex(legacy_output, r'"receiver_bootstrap_path"\s*:')
        self.assertTrue((legacy_destination / "_codex-receiving-task-plan.json").is_file())
        self.assertFalse((legacy_destination / "_codex-conversation-handoff" / "RECEIVER_START.md").exists())
        self.assertTrue((legacy_destination / "_codex-conversation-handoff" / "DETAILED_HANDOFF.md").is_file())

    def test_python_helpers_reject_protected_roots_and_descendants(self):
        protected = self.base / "root-check" / "chrome-test-profile"
        (protected / "nested").mkdir(parents=True)
        (protected / "nested" / "sample.txt").write_text("synthetic-only", encoding="utf-8")
        for index, source in enumerate((protected, protected / "nested")):
            prepare_output = self.base / f"protected-prepare-{index}"
            prepared = subprocess.run([
                options.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                options.skill_root / "scripts" / "prepare-full-backup.ps1",
                "-SourceRoot", source, "-OutputRoot", prepare_output, "-PythonExe", sys.executable,
            ], env=self.prepare_env, capture_output=True, timeout=30)
            self.assertNotEqual(prepared.returncode, 0)
            self.assertFalse(prepare_output.exists())
            scanned = subprocess.run([
                options.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                options.skill_root / "scripts" / "secret-scan.ps1", "-Path", source,
            ], capture_output=True, timeout=30)
            self.assertNotEqual(scanned.returncode, 0)
            self.assertIn(b"protected", (scanned.stdout + scanned.stderr).lower())
            snapshot = self.base / f"root-check-snapshot-{index}"
            shutil.copytree(source, snapshot)
            for script in ("create-split-tar.py", "verify-mirror-tree.py"):
                with self.subTest(script=script, source=source.name):
                    target = snapshot if script.startswith("verify") else self.base / f"root-check-output-{index}"
                    extra = ["--source-device", "test-device", "--source-computer", "TEST-COMPUTER"] if script.startswith("create") else []
                    result = subprocess.run([
                        sys.executable, options.skill_root / "scripts" / script, source, target, *extra,
                    ], capture_output=True, timeout=30)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(b"protected", (result.stdout + result.stderr).lower())

    def test_python_helpers_reject_junction_roots_and_ancestors(self):
        real = self.base / "alias-real"
        (real / "nested").mkdir(parents=True)
        (real / "nested" / "sample.txt").write_text("synthetic-only", encoding="utf-8")
        junction = self.base / "alias-link"
        helper = self.base / "make-alias.ps1"
        helper.write_text(
            "param([string]$Path,[string]$Target)\n$ErrorActionPreference='Stop'\n"
            "New-Item -ItemType Junction -Path $Path -Target $Target | Out-Null\n",
            encoding="utf-8-sig",
        )
        self.run_command([options.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                          "-File", helper, "-Path", junction, "-Target", real])
        for index, source in enumerate((junction, junction / "nested")):
            snapshot = self.base / f"alias-snapshot-{index}"
            shutil.copytree(real if index == 0 else real / "nested", snapshot)
            for script in ("create-split-tar.py", "verify-mirror-tree.py"):
                with self.subTest(script=script, nested=index):
                    target = snapshot if script.startswith("verify") else self.base / f"alias-output-{index}"
                    extra = ["--source-device", "test-device", "--source-computer", "TEST-COMPUTER"] if script.startswith("create") else []
                    result = subprocess.run([
                        sys.executable, options.skill_root / "scripts" / script, source, target, *extra,
                    ], capture_output=True, timeout=30)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(b"reparse", (result.stdout + result.stderr).lower())


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
