param(
    [Parameter(Mandatory = $true)]
    [string]$PreparedBackupPath,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')]
    [string]$Repository,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')]
    [string]$ReleaseTag = (Get-Date -Format 'backup-yyyyMMdd-HHmmss'),
    [string]$Title,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$')]
    [string]$TargetBranch,
    [string]$GitHubCli,
    [string]$PythonExe,
    [string]$ProxyUrl,
    [string]$DeviceLabel
)

$ErrorActionPreference = 'Stop'
$safetyHelper = Join-Path $PSScriptRoot 'release-upload-safety.ps1'
if (-not (Test-Path -LiteralPath $safetyHelper -PathType Leaf)) { throw 'Release upload safety helper is missing.' }
. $safetyHelper

function ConvertFrom-JsonPreservingStrings {
    param([Parameter(Mandatory = $true)][string]$Json)
    $command = Get-Command ConvertFrom-Json
    if ($command.Parameters.ContainsKey('DateKind')) {
        return $Json | ConvertFrom-Json -DateKind String
    }
    return $Json | ConvertFrom-Json
}

Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

public static class CrossDeviceProjectSyncSafeAssetFile
{
    private const uint GENERIC_READ = 0x80000000;
    private const uint GENERIC_WRITE = 0x40000000;
    private const uint FILE_SHARE_READ = 0x00000001;
    private const uint OPEN_EXISTING = 3;
    private const uint OPEN_ALWAYS = 4;
    private const uint FILE_ATTRIBUTE_DIRECTORY = 0x00000010;
    private const uint FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400;
    private const uint FILE_ATTRIBUTE_NORMAL = 0x00000080;
    private const uint FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000;

    [StructLayout(LayoutKind.Sequential)]
    private struct ByHandleFileInformation
    {
        public uint FileAttributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
        public uint VolumeSerialNumber;
        public uint FileSizeHigh;
        public uint FileSizeLow;
        public uint NumberOfLinks;
        public uint FileIndexHigh;
        public uint FileIndexLow;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFileW(
        string fileName,
        uint desiredAccess,
        uint shareMode,
        IntPtr securityAttributes,
        uint creationDisposition,
        uint flagsAndAttributes,
        IntPtr templateFile);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetFileInformationByHandle(
        SafeFileHandle handle,
        out ByHandleFileInformation information);

    private static SafeFileHandle OpenChecked(string path, uint access, uint share, uint disposition)
    {
        SafeFileHandle handle = CreateFileW(
            path,
            access,
            share,
            IntPtr.Zero,
            disposition,
            FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
            IntPtr.Zero);
        if (handle.IsInvalid)
        {
            int error = Marshal.GetLastWin32Error();
            handle.Dispose();
            throw new Win32Exception(error, "Unable to open backup asset safely: " + path);
        }
        ByHandleFileInformation information;
        if (!GetFileInformationByHandle(handle, out information))
        {
            int error = Marshal.GetLastWin32Error();
            handle.Dispose();
            throw new Win32Exception(error, "Unable to inspect backup asset safely: " + path);
        }
        if ((information.FileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)) != 0 ||
            information.NumberOfLinks != 1)
        {
            handle.Dispose();
            throw new IOException("Backup assets must be single-link regular files: " + path);
        }
        return handle;
    }

    public static SafeFileHandle OpenSingleLinkForRead(string path)
    {
        return OpenChecked(path, GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING);
    }

    public static void RewriteSingleLinkFile(string path, byte[] content)
    {
        using (SafeFileHandle handle = OpenChecked(path, GENERIC_READ | GENERIC_WRITE, 0, OPEN_ALWAYS))
        using (FileStream stream = new FileStream(handle, FileAccess.ReadWrite))
        {
            stream.SetLength(0);
            stream.Write(content, 0, content.Length);
            stream.Flush(true);
        }
    }
}
'@

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$Path)
    $handle = [CrossDeviceProjectSyncSafeAssetFile]::OpenSingleLinkForRead($Path)
    try {
        $stream = [System.IO.FileStream]::new($handle, [IO.FileAccess]::Read)
    } catch {
        $handle.Dispose()
        throw
    }
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
        $stream.Dispose()
    }
}

function Write-DeterministicUtf8Text {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text
    )
    $normalized = $Text.Replace("`r`n", "`n").Replace("`r", "`n").TrimEnd("`n") + "`n"
    $encoding = New-Object System.Text.UTF8Encoding($false)
    $bytes = $encoding.GetBytes($normalized)
    [CrossDeviceProjectSyncSafeAssetFile]::RewriteSingleLinkFile($Path, $bytes)
}

function Assert-NoReparsePointInExistingPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $cursor = Get-Item -LiteralPath ([IO.Path]::GetFullPath($Path)) -Force
    while ($cursor) {
        if (($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "PreparedBackupPath may not pass through a reparse point: $($cursor.FullName)"
        }
        $cursor = $cursor.Parent
    }
}

function Resolve-GitHubCli {
    param([string]$Requested)
    if ($Requested) {
        $resolved = (Resolve-Path -LiteralPath $Requested).Path
        if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) { throw 'GitHubCli must point to an executable file.' }
        return $resolved
    }
    $command = Get-Command gh.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
    $codexHome = if ($env:CODEX_HOME) { [IO.Path]::GetFullPath($env:CODEX_HOME) } else { Join-Path $env:USERPROFILE '.codex' }
    $localConfig = Join-Path $codexHome 'cross-device-project-sync-github.json'
    if (Test-Path -LiteralPath $localConfig -PathType Leaf) {
        $configuration = Get-Content -LiteralPath $localConfig -Raw -Encoding UTF8 | ConvertFrom-Json
        if ([string]$configuration.hostname -cne 'github.com') { throw 'Local GitHub configuration must use github.com.' }
        $configured = [IO.Path]::GetFullPath([string]$configuration.github_cli)
        if (-not (Test-Path -LiteralPath $configured -PathType Leaf)) { throw 'Configured GitHub CLI path does not exist.' }
        if ($configuration.http_timeout_seconds) {
            $timeout = [int]$configuration.http_timeout_seconds
            if ($timeout -lt 30 -or $timeout -gt 600) { throw 'Configured GitHub HTTP timeout must be between 30 and 600 seconds.' }
            if (-not $env:GH_HTTP_TIMEOUT) { $env:GH_HTTP_TIMEOUT = [string]$timeout }
        }
        return $configured
    }
    $toolRoot = Join-Path $env:USERPROFILE '.codex\tools\gh'
    if (Test-Path -LiteralPath $toolRoot -PathType Container) {
        $bundled = Get-ChildItem -LiteralPath $toolRoot -Filter 'gh.exe' -File -Recurse -ErrorAction SilentlyContinue |
            Sort-Object { try { [version]$_.Directory.Name } catch { [version]'0.0' } } -Descending |
            Select-Object -First 1
        if ($bundled) { return $bundled.FullName }
    }
    throw 'GitHub CLI is not installed. Pass -GitHubCli with an absolute path.'
}

function Resolve-Python {
    param([string]$Requested)
    if ($Requested) {
        $resolved = (Resolve-Path -LiteralPath $Requested).Path
        if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) { throw 'PythonExe must point to an executable file.' }
        return $resolved
    }
    $known = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $known -PathType Leaf) { return $known }
    foreach ($name in @('python.exe', 'python3.exe')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command) { return $command.Source }
    }
    throw 'Python 3 is required to verify the prepared backup.'
}

function Get-PrivateRepositoryInfo {
    $json = & $gh repo view $repositorySpec --json isPrivate,defaultBranchRef,nameWithOwner,url
    if ($LASTEXITCODE -ne 0) { throw 'The target GitHub repository does not exist or is inaccessible.' }
    $info = ConvertFrom-JsonPreservingStrings -Json ($json -join "`n")
    if ($info.isPrivate -isnot [bool]) { throw 'GitHub repository response is missing a Boolean privacy status.' }
    if (-not $info.isPrivate) { throw 'Complete backups may only be uploaded to a private repository.' }
    $repositoryUri = $null
    if ([string]$info.nameWithOwner -ine $Repository -or
        -not [uri]::TryCreate([string]$info.url, [UriKind]::Absolute, [ref]$repositoryUri) -or
        $repositoryUri.Scheme -ne 'https' -or
        $repositoryUri.Host -ine 'github.com' -or
        $repositoryUri.AbsolutePath.TrimEnd('/') -ine "/$Repository") {
        throw 'GitHub repository response does not match the requested github.com repository.'
    }
    return $info
}

function Resolve-DeviceLabel {
    param([string]$Requested)
    $label = $Requested
    if ([string]::IsNullOrWhiteSpace($label)) {
        $codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
        $configPath = Join-Path $codexHome 'cross-device-project-sync-device.json'
        $label = if (Test-Path -LiteralPath $configPath -PathType Leaf) {
            [string](ConvertFrom-JsonPreservingStrings -Json (Get-Content -LiteralPath $configPath -Raw -Encoding UTF8)).device_label
        } else { 'unconfigured device' }
    }
    if ([string]::IsNullOrWhiteSpace($label) -or $label.Length -gt 80 -or $label -match '[\x00-\x1f\x7f]') { throw 'Device label is invalid.' }
    return $label.Trim()
}

if ($ProxyUrl) {
    $env:HTTP_PROXY = $ProxyUrl
    $env:HTTPS_PROXY = $ProxyUrl
}

$preparedInput = [IO.Path]::GetFullPath($PreparedBackupPath)
Assert-NoReparsePointInExistingPath -Path $preparedInput
$prepared = (Resolve-Path -LiteralPath $preparedInput).Path
$manifestPath = Join-Path $prepared 'BACKUP_MANIFEST.json'
$verifyPath = Join-Path $PSScriptRoot 'verify-split-tar.py'
foreach ($required in @($manifestPath, $verifyPath)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Prepared backup helper is missing: $required"
    }
}

$python = Resolve-Python -Requested $PythonExe
$manifestShaBeforeRead = (Get-Sha256Hex -Path $manifestPath)
$manifest = ConvertFrom-JsonPreservingStrings -Json (Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8)
$manifestSha256 = (Get-Sha256Hex -Path $manifestPath)
if ($manifestShaBeforeRead -ne $manifestSha256) {
    throw 'BACKUP_MANIFEST.json changed while upload preparation began.'
}
$conversationManifestPath = Join-Path $prepared 'CONVERSATION_HANDOFF_MANIFEST.json'
$conversationZipPath = Join-Path $prepared 'CONVERSATION_HANDOFF.zip'
$hasConversationManifest = Test-Path -LiteralPath $conversationManifestPath -PathType Leaf
$hasConversationZip = Test-Path -LiteralPath $conversationZipPath -PathType Leaf
if ($hasConversationManifest -ne $hasConversationZip) {
    throw 'Conversation handoff is incomplete. Both CONVERSATION_HANDOFF_MANIFEST.json and CONVERSATION_HANDOFF.zip are required.'
}
$hasConversationHandoff = $hasConversationManifest -and $hasConversationZip
$conversationManifest = $null
$conversationManifestSha256 = $null
if ($hasConversationHandoff) {
    $conversationVerifier = Join-Path $PSScriptRoot 'verify-conversation-handoff.py'
    if (-not (Test-Path -LiteralPath $conversationVerifier -PathType Leaf)) {
        throw "Installed conversation verifier is missing: $conversationVerifier"
    }
    & $python $conversationVerifier $prepared --expected-project-manifest-sha256 $manifestSha256
    if ($LASTEXITCODE -ne 0) { throw 'Conversation handoff verification failed before upload.' }
    $conversationManifestSha256 = Get-Sha256Hex -Path $conversationManifestPath
    $conversationManifest = ConvertFrom-JsonPreservingStrings -Json (Get-Content -LiteralPath $conversationManifestPath -Raw -Encoding UTF8)
}
$device = Resolve-DeviceLabel -Requested $DeviceLabel
$repositorySpec = "github.com/$Repository"
if ($manifest.source_state_changed_during_backup) {
    throw 'Prepared backup records source changes and cannot be uploaded as complete.'
}

$gh = Resolve-GitHubCli -Requested $GitHubCli
& $gh auth status --hostname github.com | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'GitHub CLI is not logged in.' }

$repoInfo = Get-PrivateRepositoryInfo
$Repository = [string]$repoInfo.nameWithOwner
$repositorySpec = "github.com/$Repository"
if (-not $repoInfo.defaultBranchRef) { throw 'Initialize the private repository before creating a release.' }
$selectedTargetBranch = if ($TargetBranch) { $TargetBranch } else { [string]$repoInfo.defaultBranchRef.name }
if ($selectedTargetBranch -notmatch '^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$') {
    throw 'The selected target branch name is invalid.'
}

$supportNames = @('BACKUP_MANIFEST.json', 'RELEASE_BINDING.json', 'README.md', 'RESTORE.txt', 'restore.ps1', 'verify_split_tar.py', 'verify_restored_tree.py')
if ($hasConversationHandoff) {
    $supportNames += @('CONVERSATION_HANDOFF_MANIFEST.json', 'CONVERSATION_HANDOFF.zip')
}
$partNames = @($manifest.parts | ForEach-Object { $_.name })
$archiveStem = if ($manifest.archive_stem) { [string]$manifest.archive_stem } else { 'sample-project-full-backup' }
if ($archiveStem -notmatch '^[A-Za-z0-9._-]{1,80}$') { throw 'Manifest contains an unsafe archive stem.' }
$expectedPartNames = @(1..$manifest.parts.Count | ForEach-Object { "$archiveStem.tar.part{0:d3}" -f $_ })
if ($partNames.Count -ne $expectedPartNames.Count -or
    @($partNames | Select-Object -Unique).Count -ne $partNames.Count -or
    @($partNames | Where-Object { $_ -notin $expectedPartNames }).Count) {
    throw 'Manifest contains unexpected or duplicate release part names.'
}
$assetNames = @($supportNames + $partNames)
if ($assetNames.Count -gt 1000) {
    throw "The backup requires $($assetNames.Count) Release assets; GitHub currently allows at most 1000 per release."
}
$expected = @{}
foreach ($part in $manifest.parts) {
    $expected[$part.name] = [ordered]@{
        size = [int64]$part.size
        digest = 'sha256:' + $part.sha256.ToLowerInvariant()
    }
}

$bindingFormat = if ($hasConversationHandoff) { 'cross-device-project-sync-release-binding-v2' } else { 'cross-device-project-sync-release-binding-v1' }
$binding = [ordered]@{
    format = $bindingFormat
    repository = $Repository
    release_tag = $ReleaseTag
    manifest_sha256 = $manifestSha256
}
if ($hasConversationHandoff) {
    $binding['conversation_manifest_sha256'] = $conversationManifestSha256
}
$bindingJson = $binding | ConvertTo-Json -Compress
Write-DeterministicUtf8Text -Path (Join-Path $prepared 'RELEASE_BINDING.json') -Text $bindingJson

$restoreInstructions = @"
Keep every file in this directory together.

Repository: $Repository
Release tag: $ReleaseTag
Manifest SHA-256: $manifestSha256

On Windows, use the locally installed cross-device-project-sync helper. Do not execute downloaded helper scripts:

    & "`$env:USERPROFILE\.agents\skills\cross-device-project-sync\scripts\restore-full-backup.ps1" -BackupPath (Get-Location).Path -Destination '<new-empty-directory-on-selected-drive>' -ReleaseTag '$ReleaseTag' -ExpectedRepository '$Repository' -ExpectedManifestSha256 '$manifestSha256'

The helper validates RELEASE_BINDING.json, verifies all split parts, rejects unsafe archive paths, restores into the new directory, and checks every restored file against BACKUP_MANIFEST.json. Do not extract unverified parts manually.

Conversation handoff included: $hasConversationHandoff
Conversation handoff manifest SHA-256: $conversationManifestSha256
"@
Write-DeterministicUtf8Text -Path (Join-Path $prepared 'RESTORE.txt') -Text $restoreInstructions

$readme = @"
# Complete project backup

This private backup contains the permitted project scope as a lossless split TAR snapshot. Ignore rules were not applied.

- Repository: $Repository
- Release tag: $ReleaseTag
- Manifest SHA-256: $manifestSha256
- Source folder: $($manifest.source_name)
- Source device: $($manifest.source_device)
- Source computer: $(if ($manifest.source_computer) { [string]$manifest.source_computer } else { 'legacy manifest: not recorded' })
- Snapshot UTC: $(if ($manifest.created_utc) { [string]$manifest.created_utc } else { 'legacy manifest: not recorded' })
- Entries: $($manifest.entry_count)
- Archive bytes: $($manifest.archive_total_bytes)
- Parts: $($manifest.parts.Count)
- Source changes during archive: $($manifest.source_state_changed_during_backup)
- Explicitly excluded source paths: $($manifest.excluded_source_paths.Count)
- Conversation handoff included: $hasConversationHandoff
- Conversation handoff manifest SHA-256: $conversationManifestSha256
- Conversation source device: $(if ($hasConversationHandoff) { [string]$conversationManifest.conversation_source_device } else { 'not included' })
- Conversation messages through cutoff: $(if ($hasConversationHandoff) { [string]$conversationManifest.message_count } else { 'not included' })
- Conversation cutoff: $(if ($hasConversationHandoff) { [string]$conversationManifest.cutoff_timestamp } else { 'not included' })

Restore only with the locally installed trusted helper and the release tag shown above. Do not execute helper scripts downloaded with the backup.
"@
Write-DeterministicUtf8Text -Path (Join-Path $prepared 'README.md') -Text $readme

function Get-ReleaseAssetErrors {
    param(
        [object]$Release,
        [hashtable]$ExpectedAssets,
        [string[]]$ExpectedNames
    )
    $verificationErrors = @()
    foreach ($name in $ExpectedNames) {
        $remote = @($Release.assets | Where-Object name -eq $name)
        if ($remote.Count -ne 1) { $verificationErrors += "$name count=$($remote.Count)"; continue }
        if ([int64]$remote[0].size -ne [int64]$ExpectedAssets[$name].size) { $verificationErrors += "$name size" }
        if ([string]$remote[0].digest -ne [string]$ExpectedAssets[$name].digest) { $verificationErrors += "$name digest" }
        if ([string]$remote[0].state -ne 'uploaded') { $verificationErrors += "$name state" }
    }
    $unexpectedAssets = @($Release.assets | Where-Object { $_.name -notin $ExpectedNames })
    if ($unexpectedAssets.Count) { $verificationErrors += "unexpected assets=$($unexpectedAssets.Count)" }
    return @($verificationErrors)
}

function Get-CheckedRelease {
    $fields = 'isDraft,url,tagName,body,assets'
    $json = & $gh release view $ReleaseTag --repo $repositorySpec --json $fields
    if ($LASTEXITCODE -ne 0) { throw 'Unable to read the GitHub Release; upload completion is unconfirmed.' }
    $release = ConvertFrom-JsonPreservingStrings -Json ($json -join "`n")
    if ($null -eq $release -or $release -is [array] -or
        $release.isDraft -isnot [bool] -or $release.assets -isnot [array]) {
        throw 'GitHub Release response is missing valid draft or asset information.'
    }
    $releaseUri = $null
    if ($release.tagName -cne $ReleaseTag -or
        -not [uri]::TryCreate([string]$release.url, [UriKind]::Absolute, [ref]$releaseUri) -or
        $releaseUri.Scheme -ne 'https' -or
        $releaseUri.Host -ine 'github.com' -or
        -not $releaseUri.AbsolutePath.StartsWith("/$Repository/releases/", [StringComparison]::OrdinalIgnoreCase)) {
        throw 'GitHub Release response has an invalid identity.'
    }
    return $release
}

function Test-ExactReleaseExists {
    $repositoryParts = @($Repository -split '/', 2)
    if ($repositoryParts.Count -ne 2) { throw 'Canonical GitHub repository identity is invalid.' }
    $query = 'query($owner:String!,$name:String!,$tag:String!){repository(owner:$owner,name:$name){release(tagName:$tag){tagName}}}'
    $json = & $gh api graphql --hostname github.com -f "query=$query" -F "owner=$($repositoryParts[0])" -F "name=$($repositoryParts[1])" -F "tag=$ReleaseTag"
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to determine whether the GitHub Release exists. No Release was created or modified.'
    }
    $response = ConvertFrom-JsonPreservingStrings -Json ($json -join "`n")
    if ($null -eq $response -or $response -is [array] -or
        $null -eq $response.data -or $null -eq $response.data.repository) {
        throw 'GitHub returned an invalid response while checking the Release tag.'
    }
    $release = $response.data.repository.release
    if ($null -eq $release) { return $false }
    if ([string]$release.tagName -cne $ReleaseTag) {
        throw 'GitHub returned a Release identity that does not match the requested tag.'
    }
    return $true
}

function Assert-ReleaseContentIdentity {
    param([Parameter(Mandatory = $true)][object]$Release)
    [void](Assert-ExactDelimitedMetadataField -Body ([string]$Release.body) -Key 'Backup-Manifest-SHA256' -ExpectedValue $manifestSha256 -Context 'Existing project backup Release')
}

function Assert-DraftIdentity {
    param([Parameter(Mandatory = $true)][object]$Release)
    if (-not $Release.isDraft) {
        throw 'Release is no longer a draft. Refusing to upload or replace any asset.'
    }
    Assert-ReleaseContentIdentity -Release $Release
}

function Write-SuccessResult {
    param(
        [object]$Release,
        [int]$UploadedAssetCount,
        [bool]$PublishedThisRun,
        [string]$Operation
    )
    if ($Release.isDraft -isnot [bool] -or $Release.isDraft) {
        throw 'Release is not confirmed as published.'
    }
    [ordered]@{
        remote_backup_present = $true
        release_is_published = $true
        uploaded_this_run = ($UploadedAssetCount -gt 0)
        uploaded_asset_count_this_run = $UploadedAssetCount
        published_this_run = $PublishedThisRun
        operation = $Operation
        archive_deep_verified = $true
        remote_assets_verified = $true
        restore_verified = $false
        restore_verification = 'pending on a restored download'
        repository = $Repository
        tag = $Release.tagName
        url = $Release.url
        assets = $Release.assets.Count
        device = $device
        source_device = [string]$manifest.source_device
        source_computer = if ($manifest.source_computer) { [string]$manifest.source_computer } else { $null }
        operation_time = (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')
        manifest_sha256 = $manifestSha256
        snapshot_created = [string]$manifest.created_local
        snapshot_created_utc = if ($manifest.created_utc) { [string]$manifest.created_utc } else { $null }
        conversation_handoff_included = $hasConversationHandoff
        conversation_manifest_sha256 = $conversationManifestSha256
        conversation_thread_id = if ($hasConversationHandoff) { [string]$conversationManifest.thread_id } else { $null }
        conversation_source_device = if ($hasConversationHandoff) { [string]$conversationManifest.conversation_source_device } else { $null }
        conversation_message_count = if ($hasConversationHandoff) { [int]$conversationManifest.message_count } else { 0 }
        conversation_attachment_count = if ($hasConversationHandoff) { [int]$conversationManifest.attachment_count } else { 0 }
        conversation_lineage_task_count = if ($hasConversationHandoff -and $conversationManifest.PSObject.Properties.Name -contains 'lineage_task_count') { [int]$conversationManifest.lineage_task_count } elseif ($hasConversationHandoff) { 1 } else { 0 }
        conversation_lineage_message_count = if ($hasConversationHandoff -and $conversationManifest.PSObject.Properties.Name -contains 'lineage_message_count') { [int]$conversationManifest.lineage_message_count } elseif ($hasConversationHandoff) { [int]$conversationManifest.message_count } else { 0 }
        conversation_lineage_attachment_count = if ($hasConversationHandoff -and $conversationManifest.PSObject.Properties.Name -contains 'lineage_attachment_count') { [int]$conversationManifest.lineage_attachment_count } elseif ($hasConversationHandoff) { [int]$conversationManifest.attachment_count } else { 0 }
        conversation_cutoff = if ($hasConversationHandoff) { [string]$conversationManifest.cutoff_timestamp } else { $null }
    } | ConvertTo-Json
}
foreach ($name in $supportNames) {
    $path = Join-Path $prepared $name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Release asset is missing: $name" }
    $file = Get-Item -LiteralPath $path
    $expected[$name] = [ordered]@{
        size = [int64]$file.Length
        digest = 'sha256:' + (Get-Sha256Hex -Path $path)
    }
}
$trustedHelpers = @{
    'restore.ps1' = Join-Path $PSScriptRoot 'restore-full-backup.ps1'
    'verify_split_tar.py' = Join-Path $PSScriptRoot 'verify-split-tar.py'
    'verify_restored_tree.py' = Join-Path $PSScriptRoot 'verify-restored-tree.py'
}
foreach ($name in $trustedHelpers.Keys) {
    $trustedPath = $trustedHelpers[$name]
    if (-not (Test-Path -LiteralPath $trustedPath -PathType Leaf) -or
        (Get-Sha256Hex -Path (Join-Path $prepared $name)) -ne (Get-Sha256Hex -Path $trustedPath)) {
        throw "Prepared helper does not match the installed Skill: $name"
    }
}

# All upload inputs and their expected hashes are now fixed. Perform the deep
# archive check after that point so a changed manifest cannot inherit an older
# verification result. Later local changes cannot publish because GitHub's
# final asset digests must still match these expected hashes.
& $python $verifyPath $prepared
if ($LASTEXITCODE -ne 0) { throw 'Prepared backup verification failed.' }
if ((Get-Sha256Hex -Path $manifestPath) -ne $manifestSha256) {
    throw 'BACKUP_MANIFEST.json changed after upload preparation began.'
}
$checkedBinding = ConvertFrom-JsonPreservingStrings -Json (Get-Content -LiteralPath (Join-Path $prepared 'RELEASE_BINDING.json') -Raw -Encoding UTF8)
if ($checkedBinding.format -cne $bindingFormat -or
    [string]$checkedBinding.repository -cne $Repository -or
    [string]$checkedBinding.release_tag -cne $ReleaseTag -or
    [string]$checkedBinding.manifest_sha256 -cne $manifestSha256) {
    throw 'RELEASE_BINDING.json changed or does not identify this upload.'
}
if ($hasConversationHandoff -and [string]$checkedBinding.conversation_manifest_sha256 -cne $conversationManifestSha256) {
    throw 'RELEASE_BINDING.json changed or does not bind the conversation handoff manifest.'
}

if (-not $Title) { $Title = "Complete project backup $ReleaseTag" }
$notes = "Verified split-TAR project backup. SourceDevice=$([string]$manifest.source_device); SourceComputer=$(if ($manifest.source_computer) { [string]$manifest.source_computer } else { 'legacy-not-recorded' }); UploadDevice=$device; Entries=$($manifest.entry_count); ArchiveBytes=$($manifest.archive_total_bytes); Parts=$($manifest.parts.Count); ExcludedProtectedPaths=$($manifest.excluded_source_paths.Count); LocalDeepVerification=passed; ReleaseBinding=$bindingFormat; ConversationHandoff=$hasConversationHandoff; ConversationSourceDevice=$(if ($hasConversationHandoff) { [string]$conversationManifest.conversation_source_device } else { 'none' }); Conversation-Manifest-SHA256: $conversationManifestSha256; Backup-Manifest-SHA256: $manifestSha256"

if (-not (Test-ExactReleaseExists)) {
    & $gh release create $ReleaseTag --repo $repositorySpec --target $selectedTargetBranch --draft --title $Title --notes $notes | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Unable to create the draft GitHub Release.' }
}

$draft = Get-CheckedRelease
if (-not $draft.isDraft) {
    $published = Get-CheckedRelease
    $publishedErrors = @(Get-ReleaseAssetErrors -Release $published -ExpectedAssets $expected -ExpectedNames $assetNames)
    if ($publishedErrors.Count) {
        throw "Published Release exists but does not match this backup: $($publishedErrors -join ', ')"
    }
    Write-SuccessResult -Release $published -UploadedAssetCount 0 -PublishedThisRun $false -Operation 'verified_existing_release'
    return
}

Assert-DraftIdentity -Release $draft

$uploadedAssetCount = 0
foreach ($name in $assetNames) {
    # Refresh the server state before every mutation. Only an identity-bound
    # draft asset that is explicitly not uploaded may be removed for retry;
    # completed mismatched assets remain a hard stop.
    $draft = Get-CheckedRelease
    Assert-DraftIdentity -Release $draft
    $remote = @($draft.assets | Where-Object name -ceq $name)
    $action = Get-DraftAssetRecoveryAction -ReleaseIsDraft ([bool]$draft.isDraft) -ReleaseIdentityVerified $true -RemoteAssets $remote -ExpectedSize ([int64]$expected[$name].size) -ExpectedDigest ([string]$expected[$name].digest) -AssetName $name
    if ($action -eq 'reuse') {
        Write-Host "Already verified: $name"
        continue
    }
    if ($action -eq 'delete-incomplete') {
        [void](Get-PrivateRepositoryInfo)
        & $gh release delete-asset $ReleaseTag $name --repo $repositorySpec --yes | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not remove incomplete draft asset: $name" }
        $draft = Get-CheckedRelease
        Assert-DraftIdentity -Release $draft
        if (@($draft.assets | Where-Object name -ceq $name).Count -ne 0) {
            throw "Incomplete draft asset deletion was not verified: $name"
        }
    }
    $path = Join-Path $prepared $name
    Write-Host "Uploading: $name"
    $assetLock = [CrossDeviceProjectSyncSafeAssetFile]::OpenSingleLinkForRead($path)
    try {
        & $gh release upload $ReleaseTag $path --repo $repositorySpec
        $uploadExitCode = $LASTEXITCODE
    }
    finally {
        $assetLock.Dispose()
    }
    if ($uploadExitCode -ne 0) { throw "Release upload failed without replacing any existing asset: $name" }
    $uploadedAssetCount++

    $afterUpload = Get-CheckedRelease
    if (-not $afterUpload.isDraft) {
        throw 'Release was published by another actor during upload. The just-uploaded verified asset was not a replacement; no further assets will be modified.'
    }
    $uploaded = @($afterUpload.assets | Where-Object name -eq $name)
    if ($uploaded.Count -ne 1 -or
        [int64]$uploaded[0].size -ne [int64]$expected[$name].size -or
        [string]$uploaded[0].digest -ne [string]$expected[$name].digest -or
        [string]$uploaded[0].state -ne 'uploaded') {
        throw "The newly uploaded asset could not be verified: $name"
    }
}

$draft = Get-CheckedRelease
Assert-DraftIdentity -Release $draft
$errors = @(Get-ReleaseAssetErrors -Release $draft -ExpectedAssets $expected -ExpectedNames $assetNames)
if ($errors.Count) { throw "Remote Release verification failed: $($errors -join ', ')" }

[void](Get-PrivateRepositoryInfo)
$draft = Get-CheckedRelease
Assert-DraftIdentity -Release $draft
$errors = @(Get-ReleaseAssetErrors -Release $draft -ExpectedAssets $expected -ExpectedNames $assetNames)
if ($errors.Count) { throw "Immediate pre-publication verification failed: $($errors -join ', ')" }
& $gh release edit $ReleaseTag --repo $repositorySpec --draft=false | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Verified assets remain in a draft because publishing failed.' }
$final = Get-CheckedRelease
if ($final.isDraft) { throw 'Release is still a draft after publish.' }
Assert-ReleaseContentIdentity -Release $final
$finalErrors = @(Get-ReleaseAssetErrors -Release $final -ExpectedAssets $expected -ExpectedNames $assetNames)
if ($finalErrors.Count) { throw "Published Release verification failed: $($finalErrors -join ', ')" }
try {
    [void](Get-PrivateRepositoryInfo)
} catch {
    $privacyFailure = $_.Exception.Message
    & $gh release edit $ReleaseTag --repo $repositorySpec --draft=true | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Repository privacy could not be confirmed after publishing, and returning the Release to draft failed. Treat the backup as potentially exposed: $privacyFailure"
    }
    try {
        $rolledBack = Get-CheckedRelease
    } catch {
        throw "Repository privacy could not be confirmed after publishing, and the attempted draft rollback could not be verified. Treat the backup as potentially exposed: $privacyFailure"
    }
    if (-not $rolledBack.isDraft) {
        throw "Repository privacy could not be confirmed after publishing, and the Release remains published after the attempted draft rollback. Treat the backup as potentially exposed: $privacyFailure"
    }
    Assert-DraftIdentity -Release $rolledBack
    $rollbackErrors = @(Get-ReleaseAssetErrors -Release $rolledBack -ExpectedAssets $expected -ExpectedNames $assetNames)
    if ($rollbackErrors.Count) {
        throw "Repository privacy could not be confirmed after publishing. The Release returned to draft but its assets no longer match: $($rollbackErrors -join ', '); original privacy failure: $privacyFailure"
    }
    throw "Repository privacy could not be confirmed after publishing. The Release was verified as returned to draft: $privacyFailure"
}
$operation = if ($uploadedAssetCount -gt 0) { 'uploaded_and_published_release' } else { 'published_verified_draft' }
Write-SuccessResult -Release $final -UploadedAssetCount $uploadedAssetCount -PublishedThisRun $true -Operation $operation
