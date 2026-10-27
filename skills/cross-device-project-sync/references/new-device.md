# First Use On Another Windows Device

Use this when a second Windows computer needs to start using the synced project.

## Restore A Complete Backup

If the project uses complete cloud snapshots, retrieve the confirmed snapshot and manifest from the explicitly configured private repository, verify the downloaded hashes, and restore into a new local directory. Run the locally installed `scripts/restore-full-backup.ps1` with `-BackupPath <downloaded-assets-directory>`, the mandatory known `-ReleaseTag`, `-ExpectedRepository <owner/repository>`, and the independently confirmed `-ExpectedManifestSha256 <sha256>` so downloaded helper scripts and identity files are never the sole trust source. Use a different historical repository only for a user-requested named Release. New backups include `RELEASE_BINDING.json`, which the helper checks against all three external identity values. A known legacy backup without this file additionally requires `-AllowLegacyUnboundRelease` and its independently confirmed manifest hash. The helper checks free space on both drives before creating output and resolves TAR from the Windows system directory rather than mutable `PATH` input. Follow `full-cloud-backup.md`, including every restored file and empty-directory or link inventory. A Git clone alone cannot restore ignored files or uncommitted work.

Keep the restored working tree outside live cloud-sync folders. Read project instructions before modifying it. Check runtime paths and machine-specific setup separately: preserved bytes do not guarantee that installed dependencies run on another machine. Recreate credentials through supported login/setup when they were not included in an explicitly authorized encrypted backup.

## Clone

Use this section for a Git-only project, or a separately configured Git checkout; do not overwrite an existing restored directory.

Prefer a normal local folder outside real-time file-sync directories:

```powershell
$ProjectRoot = Read-Host 'Project folder on the selected data drive (for example D:\Projects)'
New-Item -ItemType Directory -Path $ProjectRoot -Force
Set-Location -LiteralPath $ProjectRoot
git clone <private-repo-url>
cd <project-name>
```

Avoid placing the repository under OneDrive, Dropbox, Baidu Netdisk, Nutstore, or similar tools.

## Local Setup

After cloning:

1. Read `README.md` if present.
2. Read `AGENTS.md` if present.
3. Read `docs/project-state.md` if present.
4. Identify the package manager and runtimes from files such as `package.json`, `pnpm-lock.yaml`, `requirements.txt`, `pyproject.toml`, `Cargo.toml`, or similar.
5. Check `.env.example` or setup docs. Do not expect real `.env` files to come from GitHub.
6. Tell the user which local environment variables or credentials must be recreated.
7. Ask before installing dependencies when the command will download from the network or mutate a broad environment.
8. Run the smallest useful verification command after setup.

Finally, the user can add the cloned folder as a Codex Local Project on that Windows device.

After download or restore, report the device label, operation date/time and timezone, Release tag, manifest SHA-256, original snapshot creation time, and verification result. An older snapshot retains its original version date even when restored today.
