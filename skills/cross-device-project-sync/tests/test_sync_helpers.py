"""Offline regression tests; synthetic files/credentials and local bare remotes only.

python test_sync_helpers.py --git <git.exe> --powershell <pwsh.exe or powershell.exe>
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

parser = argparse.ArgumentParser()
parser.add_argument("--git", default=shutil.which("git"))
parser.add_argument("--powershell", default=shutil.which("pwsh") or shutil.which("powershell"))
parser.add_argument("--skill-root", type=Path, default=Path(__file__).resolve().parents[1])
options, remaining = parser.parse_known_args()
if not options.git or not options.powershell:
    parser.error("Git and PowerShell executables are required")


class SyncHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = Path(tempfile.mkdtemp(prefix="sync-helper-tests-"))
        cls.env = os.environ.copy()
        cls.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)  # noqa: SEC-AUDITOR -- class-created temporary test root

    def setUp(self):
        self.root = self.base / self._testMethodName
        self.root.mkdir()

    def run_command(self, args):
        result = subprocess.run([str(x) for x in args], env=self.env, capture_output=True, timeout=60)
        def decode(data):
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError:
                return data.decode("mbcs", errors="replace")
        return result.returncode, decode(result.stdout), decode(result.stderr)

    def git(self, repo, *args):
        code, out, err = self.run_command([options.git, "-C", repo, *args])
        self.assertEqual(code, 0, (args, out, err))
        return out.strip()

    def repo(self, name="repo"):
        repo = self.root / name
        repo.mkdir()
        self.git(repo, "init", "-b", "main")
        self.git(repo, "config", "user.name", "Offline Test")
        self.git(repo, "config", "user.email", "test@example.invalid")
        self.git(repo, "config", "core.quotepath", "true")
        (repo / "readme.txt").write_text("baseline\n", encoding="utf-8")
        (repo / ".gitignore").write_text("*.mp4\nassets\n", encoding="utf-8")
        self.git(repo, "add", ".gitignore", "readme.txt")
        self.git(repo, "commit", "-m", "baseline")
        return repo

    def script(self, name, repo, *args):
        return self.run_command([options.powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", options.skill_root / "scripts" / name, "-Path", repo, "-GitExe", options.git, *args])

    def pair(self):
        seed = self.repo("seed")
        remote = self.root / "remote.git"
        remote.mkdir()
        self.git(remote, "init", "--bare", "-b", "main")
        self.git(seed, "remote", "add", "origin", str(remote))
        self.git(seed, "push", "-u", "origin", "main")
        clone = self.root / "clone"
        self.git(self.root, "clone", str(remote), str(clone))
        return seed, clone

    def push_file(self, seed, relative, content=b"incoming"):
        path = seed / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self.git(seed, "add", "-f", relative)
        self.git(seed, "commit", "-m", "incoming change")
        self.git(seed, "push")

    def test_secret_unicode_and_special_paths(self):
        repo = self.repo("中文 空格;'项目")
        relative = "中文 子目录/配置 [1]'s.txt"
        path = repo / relative
        path.parent.mkdir()
        path.write_text("fake=ghp_" + "X" * 36, encoding="utf-8")
        for tracked in (False, True):
            with self.subTest(tracked=tracked):
                if tracked:
                    self.git(repo, "add", relative)
                code, out, err = self.script("secret-scan.ps1", repo, "-Json")
                self.assertEqual(code, 2, (out, err))
                findings = json.loads(out)
                self.assertIsInstance(findings, list)
                self.assertTrue(any(x["Path"].replace("\\", "/") == relative for x in findings), findings)

    def test_secret_ascii_clean_and_trailing_separator(self):
        repo = self.repo()
        code, out, err = self.script("secret-scan.ps1", str(repo) + os.sep)
        self.assertEqual(code, 0, (out, err))
        (repo / "sample.txt").write_text("fake=ghp_" + "X" * 36, encoding="utf-8")
        code, out, err = self.script("secret-scan.ps1", repo)
        self.assertEqual(code, 2, (out, err))

    def test_secret_clean_json_is_an_array(self):
        repo = self.repo()
        code, out, err = self.script("secret-scan.ps1", repo, "-Json")
        self.assertEqual(code, 0, (out, err))
        self.assertEqual(json.loads(out), [])

    def test_secret_scan_reads_staged_blob_instead_of_clean_worktree(self):
        repo = self.repo()
        path = repo / "ordinary-config.txt"
        path.write_text("value=ghp_" + "S" * 36, encoding="utf-8")
        self.git(repo, "add", path.name)
        path.write_text("value=clean", encoding="utf-8")
        code, out, err = self.script("secret-scan.ps1", repo, "-Json")
        self.assertEqual(code, 2, (out, err))
        findings = json.loads(out)
        self.assertTrue(any(item["Path"] == path.name and "index" in item["Detail"] for item in findings))

    def test_secret_scan_reads_utf16_text(self):
        repo = self.repo()
        path = repo / "ordinary-windows.txt"
        path.write_text("value=ghp_" + "U" * 36, encoding="utf-16")
        code, out, err = self.script("secret-scan.ps1", repo, "-Json")
        self.assertEqual(code, 2, (out, err))
        self.assertTrue(any(item["Path"] == path.name for item in json.loads(out)))

    def test_secret_scan_rejects_protected_browser_profile(self):
        repo = self.repo()
        protected = repo / "browser-test-profile" / "Default" / "Network"
        protected.mkdir(parents=True)
        (protected / "Cookies").write_text("synthetic-only", encoding="utf-8")
        code, out, err = self.script("secret-scan.ps1", repo)
        self.assertNotEqual(code, 0)
        self.assertIn("protected browser test profile", (out + err).lower())

    def test_secret_scan_rejects_zero_content_limit(self):
        repo = self.repo()
        code, out, err = self.script("secret-scan.ps1", repo, "-MaxFileBytes", "0")
        self.assertNotEqual(code, 0)

    def test_status_redacts_userinfo_queries_and_fragments(self):
        repo = self.repo()
        markers = ["FAKE_PASSWORD", "FAKE_QUERY_VALUE", "FAKE_FRAGMENT"]
        remote = f"https://user:{markers[0]}@example.invalid/repo.git?access_token={markers[1]}#{markers[2]}"
        self.git(repo, "remote", "add", "origin", remote)
        for args in ((), ("-Json",)):
            code, out, err = self.script("sync-status.ps1", repo, *args)
            self.assertEqual(code, 0, (out, err))
            for marker in markers:
                self.assertNotIn(marker, out + err)
            self.assertIn("example.invalid", out)
        self.assertEqual(self.git(repo, "remote", "get-url", "origin"), remote)

    def test_status_discloses_cached_remote_basis(self):
        seed, clone = self.pair()
        code, out, err = self.script("sync-status.ps1", clone, "-Json")
        self.assertEqual(code, 0, (out, err))
        status = json.loads(out)
        self.assertIn("no fetch", status["RemoteStateBasis"])

    def test_status_does_not_report_head_as_an_unborn_commit(self):
        repo = self.root / "unborn"
        repo.mkdir()
        self.git(repo, "init", "-b", "main")
        code, out, err = self.script("sync-status.ps1", repo, "-Json")
        self.assertEqual(code, 0, (out, err))
        self.assertEqual(json.loads(out)["Commit"], "")

    def test_configurable_git_timeout_is_accepted(self):
        repo = self.repo()
        code, out, err = self.script("sync-status.ps1", repo, "-GitTimeoutMilliseconds", "1000", "-DeviceLabel", "test-device", "-Json")
        self.assertEqual(code, 0, (out, err))
        status = json.loads(out)
        self.assertTrue(status["IsGitRepository"])
        self.assertEqual(status["Device"], "test-device")
        self.assertRegex(status["Commit"], r"^[0-9a-f]{40}$")

    def test_git_error_output_is_redacted(self):
        repo = self.repo()
        common = str(options.skill_root / "scripts/git-common.ps1").replace("'", "''")
        repo_literal = str(repo).replace("'", "''")
        probe = self.root / "error-probe.ps1"
        probe.write_text(". '" + common + "'\ntry { Invoke-Git -Repo '" + repo_literal + "' -Arguments @('log', '--badflag=https://user:FAKE_ERROR_PASSWORD@example.invalid/repo?token=FAKE_ERROR_QUERY') } catch { Write-Output $_.Exception.Message }\n", encoding="utf-8-sig")
        code, out, err = self.run_command([options.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", probe])
        self.assertEqual(code, 0, (out, err))
        self.assertIn("Git failed", out)
        self.assertNotIn("FAKE_ERROR_PASSWORD", out + err)
        self.assertNotIn("FAKE_ERROR_QUERY", out + err)

    def collision(self, local_path, remote_path, local_directory=False):
        seed, clone = self.pair()
        path = clone / local_path
        if local_directory:
            path.mkdir()
            path = path / "local.txt"
        path.write_bytes(b"local irreplaceable")
        before = self.git(clone, "rev-parse", "HEAD")
        self.push_file(seed, remote_path)
        self.assertEqual(self.git(clone, "status", "--porcelain"), "")
        code, out, err = self.script("sync-before-work.ps1", clone)
        self.assertEqual(code, 26, (out, err))
        self.assertEqual(path.read_bytes(), b"local irreplaceable")
        self.assertEqual(self.git(clone, "rev-parse", "HEAD"), before)

    def test_ignored_file_collision(self):
        self.collision("asset.mp4", "asset.mp4")

    def test_ignored_directory_replaced_by_file(self):
        self.collision("assets", "assets", local_directory=True)

    def test_ignored_file_replaced_by_directory(self):
        self.collision("assets", "assets/remote.txt")

    def test_normal_update_preserves_unrelated_ignored_media(self):
        seed, clone = self.pair()
        (clone / "keep.mp4").write_bytes(b"keep")
        self.push_file(seed, "new.txt")
        code, out, err = self.script("sync-before-work.ps1", clone, "-DeviceLabel", "test-device")
        self.assertEqual(code, 0, (out, err))
        self.assertIn("Device: test-device", out)
        self.assertRegex(out, r"Result version: main@[0-9a-f]{40}")
        self.assertEqual((clone / "keep.mp4").read_bytes(), b"keep")
        self.assertEqual(self.git(clone, "rev-parse", "HEAD"), self.git(seed, "rev-parse", "HEAD"))

    def test_update_helper_does_not_lock_head_file(self):
        script = (options.skill_root / "scripts" / "sync-before-work.ps1").read_text(encoding="utf-8-sig")
        self.assertNotIn("Open-GitHeadReadLock", script)
        self.assertNotIn('"--git-path", "HEAD"', script)
        self.assertIn("cross-device-project-sync.lock", script)

    def test_git_timeout_terminates_remote_helper_process_tree(self):
        repo = self.repo()
        marker = self.root / "remote-helper-survived.txt"
        helper = self.root / "slow-remote.cmd"
        helper.write_text(
            "@echo off\n"
            "ping 127.0.0.1 -n 4 >nul\n"
            f'echo survived>"{marker}"\n'
            "exit /b 1\n",
            encoding="utf-8",
        )
        self.git(repo, "remote", "add", "origin", "ext::" + helper.as_posix())
        self.git(repo, "config", "branch.main.remote", "origin")
        self.git(repo, "config", "branch.main.merge", "refs/heads/main")
        self.git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        env = dict(self.env, GIT_ALLOW_PROTOCOL="ext")
        result = subprocess.run([
            options.powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File",
            options.skill_root / "scripts" / "sync-before-work.ps1",
            "-Path", repo, "-GitExe", options.git, "-GitTimeoutMilliseconds", "1000",
        ], env=env, capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"process tree was terminated", result.stdout + result.stderr)
        time.sleep(4)
        self.assertFalse(marker.exists())

    def test_no_pull_does_not_mutate_head_or_media(self):
        seed, clone = self.pair()
        (clone / "asset.mp4").write_bytes(b"keep")
        before = self.git(clone, "rev-parse", "HEAD")
        self.push_file(seed, "asset.mp4")
        code, out, err = self.script("sync-before-work.ps1", clone, "-NoPull")
        self.assertEqual(code, 0, (out, err))
        self.assertIn("pending", out)
        self.assertEqual(self.git(clone, "rev-parse", "HEAD"), before)
        self.assertEqual((clone / "asset.mp4").read_bytes(), b"keep")

    def test_modified_tracked_file_stops_update(self):
        seed, clone = self.pair()
        (clone / "readme.txt").write_bytes(b"local modification")
        before = self.git(clone, "rev-parse", "HEAD")
        self.push_file(seed, "new.txt")
        code, out, err = self.script("sync-before-work.ps1", clone)
        self.assertEqual(code, 20, (out, err))
        self.assertEqual((clone / "readme.txt").read_bytes(), b"local modification")
        self.assertEqual(self.git(clone, "rev-parse", "HEAD"), before)

    def test_missing_upstream_reports_setup_needed(self):
        repo = self.repo()
        code, out, err = self.script("sync-before-work.ps1", repo)
        self.assertEqual(code, 23, (out, err))
        self.assertIn("no upstream", out)


if __name__ == "__main__":
    unittest.main(argv=[__file__, *remaining], verbosity=2)
