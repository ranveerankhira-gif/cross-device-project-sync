import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


@unittest.skipUnless(POWERSHELL, "PowerShell is required")
class SafetyGateTests(unittest.TestCase):
    def run_probe(self, body: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory(prefix="cross-device-safety-gate-") as raw:
            probe = Path(raw) / "probe.ps1"
            probe.write_text(body, encoding="utf-8-sig")
            return subprocess.run(
                [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(probe)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )

    def test_draft_asset_recovery_gate_only_deletes_non_uploaded_assets(self):
        helper = str(SCRIPTS / "release-upload-safety.ps1").replace("'", "''")
        completed = self.run_probe(f"""
. '{helper}'
$none = @()
$incomplete = @([pscustomobject]@{{state='new';size=10;digest=$null}})
$exact = @([pscustomobject]@{{state='uploaded';size=10;digest='sha256:abc'}})
$mismatchBlocked = $false
$identityBlocked = $false
try {{ Get-DraftAssetRecoveryAction -ReleaseIsDraft $true -ReleaseIdentityVerified $true -RemoteAssets $exact -ExpectedSize 11 -ExpectedDigest 'sha256:def' -AssetName 'a.bin' | Out-Null }} catch {{ $mismatchBlocked = $true }}
try {{ Get-DraftAssetRecoveryAction -ReleaseIsDraft $true -ReleaseIdentityVerified $false -RemoteAssets $incomplete -ExpectedSize 10 -ExpectedDigest 'sha256:abc' -AssetName 'a.bin' | Out-Null }} catch {{ $identityBlocked = $true }}
[pscustomobject]@{{
  absent=(Get-DraftAssetRecoveryAction -ReleaseIsDraft $true -ReleaseIdentityVerified $true -RemoteAssets $none -ExpectedSize 10 -ExpectedDigest 'sha256:abc' -AssetName 'a.bin')
  incomplete=(Get-DraftAssetRecoveryAction -ReleaseIsDraft $true -ReleaseIdentityVerified $true -RemoteAssets $incomplete -ExpectedSize 10 -ExpectedDigest 'sha256:abc' -AssetName 'a.bin')
  exact=(Get-DraftAssetRecoveryAction -ReleaseIsDraft $true -ReleaseIdentityVerified $true -RemoteAssets $exact -ExpectedSize 10 -ExpectedDigest 'sha256:abc' -AssetName 'a.bin')
  mismatchBlocked=$mismatchBlocked
  identityBlocked=$identityBlocked
}} | ConvertTo-Json -Compress
""")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["absent"], "upload")
        self.assertEqual(result["incomplete"], "delete-incomplete")
        self.assertEqual(result["exact"], "reuse")
        self.assertTrue(result["mismatchBlocked"])
        self.assertTrue(result["identityBlocked"])

    def test_failed_project_state_preserves_prepared_and_release_checkpoints(self):
        helper = str(SCRIPTS / "incremental-upload-state.ps1").replace("'", "''")
        completed = self.run_probe(f"""
. '{helper}'
$old = [pscustomobject]@{{
  project_id='p1';label='Old';source_path='D:\\Project';status='failed';error='network'
  project_release=[pscustomobject]@{{tag='project-tag'}}
  conversation_release=[pscustomobject]@{{tag='conversation-tag'}}
  prepared_backup='D:\\Work\\prepared';conversation_pack='D:\\Work\\pack'
}}
$result = New-ResumedProjectUploadState -ExistingRecord $old -ProjectId 'p1' -Label 'New' -SourcePath 'D:\\Project'
$result | ConvertTo-Json -Depth 5 -Compress
""")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "running")
        self.assertIsNone(result["error"])
        self.assertEqual(result["prepared_backup"], "D:\\Work\\prepared")
        self.assertEqual(result["conversation_pack"], "D:\\Work\\pack")
        self.assertEqual(result["project_release"]["tag"], "project-tag")

    def test_release_metadata_identity_requires_exact_unique_lines(self):
        helper = str(SCRIPTS / "release-upload-safety.ps1").replace("'", "''")
        completed = self.run_probe(f"""
. '{helper}'
$expected = @{{'Content-Kind'='device-index';'Repository'='owner/repo'}}
$exact = $false
$prefixBlocked = $false
$duplicateBlocked = $false
try {{ [void](Assert-ExactReleaseMetadataLines -Body "Content-Kind: device-index`nRepository: owner/repo" -Expected $expected); $exact = $true }} catch {{}}
try {{ [void](Assert-ExactReleaseMetadataLines -Body "Content-Kind: device-index-old`nRepository: owner/repo" -Expected $expected) }} catch {{ $prefixBlocked = $true }}
try {{ [void](Assert-ExactReleaseMetadataLines -Body "Content-Kind: device-index`nContent-Kind: device-index`nRepository: owner/repo" -Expected $expected) }} catch {{ $duplicateBlocked = $true }}
[pscustomobject]@{{exact=$exact;prefixBlocked=$prefixBlocked;duplicateBlocked=$duplicateBlocked}} | ConvertTo-Json -Compress
""")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["exact"])
        self.assertTrue(result["prefixBlocked"])
        self.assertTrue(result["duplicateBlocked"])

    def test_project_release_metadata_requires_one_exact_delimited_field(self):
        helper = str(SCRIPTS / "release-upload-safety.ps1").replace("'", "''")
        completed = self.run_probe(f"""
. '{helper}'
$exact = $false
$wrongPrefixBlocked = $false
$duplicateBlocked = $false
try {{ [void](Assert-ExactDelimitedMetadataField -Body 'Summary; Backup-Manifest-SHA256: abc' -Key 'Backup-Manifest-SHA256' -ExpectedValue 'abc'); $exact = $true }} catch {{}}
try {{ [void](Assert-ExactDelimitedMetadataField -Body 'Summary; X-Backup-Manifest-SHA256: abc' -Key 'Backup-Manifest-SHA256' -ExpectedValue 'abc') }} catch {{ $wrongPrefixBlocked = $true }}
try {{ [void](Assert-ExactDelimitedMetadataField -Body "Summary; Backup-Manifest-SHA256: abc`nBackup-Manifest-SHA256: def" -Key 'Backup-Manifest-SHA256' -ExpectedValue 'abc') }} catch {{ $duplicateBlocked = $true }}
[pscustomobject]@{{exact=$exact;wrongPrefixBlocked=$wrongPrefixBlocked;duplicateBlocked=$duplicateBlocked}} | ConvertTo-Json -Compress
""")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["exact"])
        self.assertTrue(result["wrongPrefixBlocked"])
        self.assertTrue(result["duplicateBlocked"])
        prepared = (SCRIPTS / "upload-prepared-backup.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("Assert-ExactDelimitedMetadataField", prepared)

    def test_project_picker_has_process_owner_and_clipboard_gates(self):
        script = (SCRIPTS / "register-restored-projects-ui.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("FindOwnedProjectPickerWindows", script)
        self.assertIn("GetWindowThreadProcessId", script)
        self.assertIn("GW_OWNER", script)
        self.assertNotIn("FindWindow('#32770'", script)
        self.assertIn("GetDataObject", script)
        self.assertIn("SetDataObject", script)
        self.assertIn("Clipboard format could not be materialized safely", script)
        self.assertIn("ApartmentState -ne [Threading.ApartmentState]::STA", script)
        self.assertIn("status = if ($remainingAfterBatch -eq 0) { 'complete' } else { 'partial' }", script)

    def test_upload_scripts_share_the_strict_partial_asset_and_privacy_gates(self):
        immutable = (SCRIPTS / "upload-immutable-assets.ps1").read_text(encoding="utf-8-sig")
        prepared = (SCRIPTS / "upload-prepared-backup.ps1").read_text(encoding="utf-8-sig")
        for script in (immutable, prepared):
            self.assertIn("release-upload-safety.ps1", script)
            self.assertIn("Get-DraftAssetRecoveryAction", script)
            self.assertIn("release delete-asset", script)
            self.assertIn("delete-incomplete", script)
        self.assertIn("Repository privacy verification failed immediately before publication", immutable)
        self.assertGreaterEqual(immutable.count("Assert-ReleaseIdentity $release"), 3)
        self.assertIn("Assert-ReleaseIdentity $final", immutable)
        self.assertIn("--draft=true", immutable)
        self.assertIn("verified as returned to draft", immutable)
        self.assertIn("Assert-ExactReleaseMetadataLines", immutable)
        self.assertGreaterEqual(prepared.count("Assert-DraftIdentity -Release $draft"), 3)
        self.assertIn("Assert-ReleaseContentIdentity -Release $final", prepared)
        self.assertIn("Assert-DraftIdentity -Release $rolledBack", prepared)

    @unittest.skipUnless(os.name == "nt", "native Windows picker")
    def test_project_picker_native_helper_compiles_without_opening_ui(self):
        with tempfile.TemporaryDirectory(prefix="project-picker-compile-") as raw:
            base = Path(raw)
            (base / ".codex-global-state.json").write_text('{"local-projects":{}}', encoding="utf-8")
            report = base / "report.json"
            report.write_text('{"projects":[]}', encoding="utf-8")
            completed = subprocess.run(
                [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                 str(SCRIPTS / "register-restored-projects-ui.ps1"), "-Report", str(report), "-CodexHome", str(base)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual(json.loads(completed.stdout)["status"], "complete")


if __name__ == "__main__":
    unittest.main()
