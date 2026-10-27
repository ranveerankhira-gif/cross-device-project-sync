[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$AssetDirectory,
    [Parameter(Mandatory = $true)][string]$ReleaseTag,
    [Parameter(Mandatory = $true)][string]$Title,
    [Parameter(Mandatory = $true)][ValidateSet('conversation-pack','device-index','skill-package')][string]$Kind,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$IdentitySha256,
    [string]$Repository = $env:CROSS_DEVICE_SYNC_REPOSITORY,
    [string]$GitHubCli,
    [string]$SourceDevice,
    [string]$SourceComputer,
    [string]$MigrationId
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$safetyHelper = Join-Path $PSScriptRoot 'release-upload-safety.ps1'
if (-not (Test-Path -LiteralPath $safetyHelper -PathType Leaf)) { throw 'Release upload safety helper is missing.' }
. $safetyHelper

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Invoke-GhJson([string[]]$Arguments) {
    $text = & $GitHubCli @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw "GitHub CLI failed: $($text -join ' ')" }
    return ($text -join "`n") | ConvertFrom-Json
}

function Resolve-GitHubCli([string]$Requested) {
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
        $timeout = [int]$configuration.http_timeout_seconds
        if ($timeout -lt 30 -or $timeout -gt 600) { throw 'Configured GitHub HTTP timeout must be between 30 and 600 seconds.' }
        if (-not $env:GH_HTTP_TIMEOUT) { $env:GH_HTTP_TIMEOUT = [string]$timeout }
        return $configured
    }
    throw 'GitHub CLI is not installed or configured.'
}

if ([string]::IsNullOrWhiteSpace($Repository) -or $Repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' -or $Repository.StartsWith('example-owner/')) { throw 'Set CROSS_DEVICE_SYNC_REPOSITORY or pass a real private owner/repository.' }
if ($env:CROSS_DEVICE_SYNC_REPOSITORY -and $Repository -cne $env:CROSS_DEVICE_SYNC_REPOSITORY) { throw 'Repository does not match CROSS_DEVICE_SYNC_REPOSITORY.' }
$repositoryParts = $Repository.Split('/', 2)
if ($ReleaseTag -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$') { throw 'ReleaseTag contains unsupported characters.' }

$root = [IO.Path]::GetFullPath($AssetDirectory)
if (-not (Test-Path -LiteralPath $root -PathType Container)) { throw 'AssetDirectory does not exist.' }
$bindingPath = Join-Path $root 'CONTENT_RELEASE_BINDING.json'
$payloadFiles = @(Get-ChildItem -LiteralPath $root -Force -File | Where-Object Name -cne 'CONTENT_RELEASE_BINDING.json' | Sort-Object Name)
if ($payloadFiles.Count -eq 0) { throw 'AssetDirectory contains no files.' }
foreach ($file in $payloadFiles) {
    if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Release asset is a reparse point: $($file.Name)" }
}

$GitHubCli = Resolve-GitHubCli $GitHubCli
& $GitHubCli auth status --hostname github.com | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'GitHub authentication is missing or invalid. Complete one official device login, then retry; do not log out a valid account.' }
$repo = Invoke-GhJson @('repo','view',$Repository,'--json','nameWithOwner,isPrivate,url,defaultBranchRef')
if (-not [bool]$repo.isPrivate -or [string]$repo.nameWithOwner -cne $Repository) { throw 'Canonical backup repository is missing, public, or has unexpected identity.' }

$binding = [ordered]@{
    format = 'cross-device-project-sync-content-release-binding-v1'
    repository = $Repository
    release_tag = $ReleaseTag
    kind = $Kind
    identity_sha256 = $IdentitySha256
    assets = @($payloadFiles | ForEach-Object { [ordered]@{ name=$_.Name; size=[int64]$_.Length; sha256=(Get-Sha256 $_.FullName) } })
}
$bindingJson = $binding | ConvertTo-Json -Depth 6
if (Test-Path -LiteralPath $bindingPath -PathType Leaf) {
    $existingBinding = Get-Content -LiteralPath $bindingPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $existingAssets = @($existingBinding.assets | ForEach-Object { "{0}|{1}|{2}" -f [string]$_.name,[int64]$_.size,[string]$_.sha256 } | Sort-Object)
    $desiredAssets = @($binding.assets | ForEach-Object { "{0}|{1}|{2}" -f [string]$_.name,[int64]$_.size,[string]$_.sha256 } | Sort-Object)
    $sameAssets = ($existingAssets.Count -eq $desiredAssets.Count) -and (($existingAssets -join "`n") -ceq ($desiredAssets -join "`n"))
    if ([string]$existingBinding.format -cne [string]$binding.format -or
        [string]$existingBinding.repository -cne [string]$binding.repository -or
        [string]$existingBinding.release_tag -cne [string]$binding.release_tag -or
        [string]$existingBinding.kind -cne [string]$binding.kind -or
        [string]$existingBinding.identity_sha256 -cne [string]$binding.identity_sha256 -or
        -not $sameAssets) {
        throw 'Existing CONTENT_RELEASE_BINDING.json does not match the requested immutable release.'
    }
} else {
    [IO.File]::WriteAllText($bindingPath, $bindingJson + "`n", [Text.UTF8Encoding]::new($false))
}
$allFiles = @($payloadFiles + (Get-Item -LiteralPath $bindingPath)) | Sort-Object Name
$expected = @{}
foreach ($file in $allFiles) {
    $expected[$file.Name] = [ordered]@{ path=$file.FullName; size=[int64]$file.Length; digest=('sha256:' + (Get-Sha256 $file.FullName)) }
}

$notes = "Content-Kind: $Kind`nContent-Identity-SHA256: $IdentitySha256`nRepository: $Repository"
if ($Kind -eq 'device-index') {
    if (-not $SourceDevice -or -not $SourceComputer -or -not $MigrationId) {
        throw 'Device-index publication requires SourceDevice, SourceComputer, and MigrationId.'
    }
    $notes += "`nSource-Device: $SourceDevice`nSource-Computer: $SourceComputer`nMigration-ID: $MigrationId"
}

function Assert-ReleaseIdentity($Release) {
    if ([string]$Release.tagName -cne $ReleaseTag) {
        throw 'Existing Release is not bound to this exact content identity and kind.'
    }
    $expectedMetadata = @{
        'Content-Kind' = $Kind
        'Content-Identity-SHA256' = $IdentitySha256
        'Repository' = $Repository
    }
    if ($Kind -eq 'device-index') {
        $expectedMetadata['Source-Device'] = $SourceDevice
        $expectedMetadata['Source-Computer'] = $SourceComputer
        $expectedMetadata['Migration-ID'] = $MigrationId
    }
    [void](Assert-ExactReleaseMetadataLines -Body ([string]$Release.body) -Expected $expectedMetadata -Context 'Existing Release')
    return $true
}
$exists = $true
$release = $null
try {
    $release = Invoke-GhJson @('release','view',$ReleaseTag,'--repo',$Repository,'--json','isDraft,url,tagName,body,assets')
} catch {
    $query = 'query($owner:String!,$name:String!,$tag:String!){repository(owner:$owner,name:$name){release(tagName:$tag){tagName}}}'
    $probe = Invoke-GhJson @('api','graphql','-f',"query=$query",'-F',"owner=$($repositoryParts[0])",'-F',"name=$($repositoryParts[1])",'-F',"tag=$ReleaseTag")
    if ($null -ne $probe.data.repository.release) { throw 'Release lookup was inconsistent; no mutation performed.' }
    $exists = $false
}

if (-not $exists) {
    & $GitHubCli release create $ReleaseTag --repo $Repository --draft --title $Title --notes $notes | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not create draft Release.' }
    $release = Invoke-GhJson @('release','view',$ReleaseTag,'--repo',$Repository,'--json','isDraft,url,tagName,body,assets')
}
[void](Assert-ReleaseIdentity $release)

function Get-AssetErrors($Release) {
    $errors = @()
    $remoteByName = @{}
    foreach ($asset in @($Release.assets)) { $remoteByName[[string]$asset.name] = $asset }
    foreach ($name in $expected.Keys) {
        $remote = $remoteByName[$name]
        if ($null -eq $remote) { $errors += "missing:$name"; continue }
        if ([int64]$remote.size -ne $expected[$name].size -or [string]$remote.digest -cne $expected[$name].digest) { $errors += "mismatch:$name" }
        if ([string]$remote.state -cne 'uploaded') { $errors += "state:$name" }
    }
    foreach ($name in $remoteByName.Keys) { if (-not $expected.ContainsKey($name)) { $errors += "unexpected:$name" } }
    return @($errors)
}

if (-not [bool]$release.isDraft) {
    $errors = @(Get-AssetErrors $release)
    if ($errors.Count) { throw "Published Release asset verification failed: $($errors -join ', ')" }
    [pscustomobject]@{operation='verified_existing_release';release_tag=$ReleaseTag;uploaded_asset_count_this_run=0;identity_sha256=$IdentitySha256;url=$release.url;verified=$true} | ConvertTo-Json
    exit 0
}

$uploaded = 0
foreach ($file in $allFiles) {
    $release = Invoke-GhJson @('release','view',$ReleaseTag,'--repo',$Repository,'--json','isDraft,url,tagName,body,assets')
    if (-not [bool]$release.isDraft) { throw 'Release was published while upload was in progress.' }
    [void](Assert-ReleaseIdentity $release)
    $remote = @($release.assets | Where-Object name -ceq $file.Name)
    $action = Get-DraftAssetRecoveryAction -ReleaseIsDraft ([bool]$release.isDraft) -ReleaseIdentityVerified $true -RemoteAssets $remote -ExpectedSize $expected[$file.Name].size -ExpectedDigest $expected[$file.Name].digest -AssetName $file.Name
    if ($action -eq 'reuse') {
        continue
    }
    if ($action -eq 'delete-incomplete') {
        $private = Invoke-GhJson @('repo','view',$Repository,'--json','nameWithOwner,isPrivate,url')
        if (-not [bool]$private.isPrivate -or [string]$private.nameWithOwner -cne $Repository) { throw 'Repository privacy changed before incomplete-asset recovery.' }
        & $GitHubCli release delete-asset $ReleaseTag $file.Name --repo $Repository --yes | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not remove incomplete draft asset: $($file.Name)" }
        $release = Invoke-GhJson @('release','view',$ReleaseTag,'--repo',$Repository,'--json','isDraft,url,tagName,body,assets')
        if (-not [bool]$release.isDraft) { throw 'Release changed from draft during incomplete-asset recovery.' }
        [void](Assert-ReleaseIdentity $release)
        if (@($release.assets | Where-Object name -ceq $file.Name).Count -ne 0) { throw "Incomplete draft asset deletion was not verified: $($file.Name)" }
    }
    & $GitHubCli release upload $ReleaseTag $file.FullName --repo $Repository
    if ($LASTEXITCODE -ne 0) { throw "Upload failed: $($file.Name)" }
    $release = Invoke-GhJson @('release','view',$ReleaseTag,'--repo',$Repository,'--json','isDraft,url,tagName,body,assets')
    if (-not [bool]$release.isDraft) { throw 'Release was published while upload verification was in progress.' }
    [void](Assert-ReleaseIdentity $release)
    $verifiedAsset = @($release.assets | Where-Object name -ceq $file.Name)
    if ($verifiedAsset.Count -ne 1 -or [string]$verifiedAsset[0].state -cne 'uploaded' -or
        [int64]$verifiedAsset[0].size -ne $expected[$file.Name].size -or
        [string]$verifiedAsset[0].digest -cne $expected[$file.Name].digest) {
        throw "Uploaded draft asset verification failed: $($file.Name)"
    }
    $uploaded++
}

$release = Invoke-GhJson @('release','view',$ReleaseTag,'--repo',$Repository,'--json','isDraft,url,tagName,body,assets')
$releaseIsDraft = [bool]$release.isDraft
if (-not $releaseIsDraft) { throw 'Release changed from draft before final verification.' }
[void](Assert-ReleaseIdentity $release)
$errors = @(Get-AssetErrors $release)
if ($errors.Count) { throw "Draft verification failed: $($errors -join ', ')" }
$repoBeforePublish = Invoke-GhJson @('repo','view',$Repository,'--json','nameWithOwner,isPrivate,url')
if (-not [bool]$repoBeforePublish.isPrivate -or [string]$repoBeforePublish.nameWithOwner -cne $Repository) { throw 'Repository privacy verification failed immediately before publication.' }
$release = Invoke-GhJson @('release','view',$ReleaseTag,'--repo',$Repository,'--json','isDraft,url,tagName,body,assets')
if (-not [bool]$release.isDraft) { throw 'Release changed from draft immediately before publication.' }
[void](Assert-ReleaseIdentity $release)
$errors = @(Get-AssetErrors $release)
if ($errors.Count) { throw "Immediate pre-publication verification failed: $($errors -join ', ')" }
& $GitHubCli release edit $ReleaseTag --repo $Repository --draft=false | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Publishing verified draft failed.' }
$final = Invoke-GhJson @('release','view',$ReleaseTag,'--repo',$Repository,'--json','isDraft,url,tagName,body,assets')
[void](Assert-ReleaseIdentity $final)
$errors = @(Get-AssetErrors $final)
if ([bool]$final.isDraft -or $errors.Count) { throw "Published Release verification failed: $($errors -join ', ')" }
try {
    $repoAfter = Invoke-GhJson @('repo','view',$Repository,'--json','nameWithOwner,isPrivate,url')
    if (-not [bool]$repoAfter.isPrivate -or [string]$repoAfter.nameWithOwner -cne $Repository) { throw 'Repository is not confirmed private.' }
} catch {
    $privacyFailure = $_.Exception.Message
    & $GitHubCli release edit $ReleaseTag --repo $Repository --draft=true | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Privacy verification failed and draft rollback failed; treat the Release as potentially exposed: $privacyFailure" }
    try {
        $rolledBack = Invoke-GhJson @('release','view',$ReleaseTag,'--repo',$Repository,'--json','isDraft,url,tagName,body,assets')
    } catch {
        throw "Privacy verification failed and the attempted draft rollback could not be verified; treat the Release as potentially exposed: $privacyFailure"
    }
    if (-not [bool]$rolledBack.isDraft) { throw "Privacy verification failed and the Release remains published: $privacyFailure" }
    [void](Assert-ReleaseIdentity $rolledBack)
    throw "Privacy verification failed; the Release was verified as returned to draft: $privacyFailure"
}
[pscustomobject]@{operation='uploaded_and_published_release';release_tag=$ReleaseTag;uploaded_asset_count_this_run=$uploaded;identity_sha256=$IdentitySha256;url=$final.url;verified=$true} | ConvertTo-Json
