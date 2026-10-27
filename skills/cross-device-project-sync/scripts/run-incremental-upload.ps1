[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Catalog,
    [Parameter(Mandatory = $true)][string]$Plan,
    [Parameter(Mandatory = $true)][string]$WorkRoot,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$')][string]$MigrationId,
    [string]$Repository = $env:CROSS_DEVICE_SYNC_REPOSITORY,
    [string]$PythonExe,
    [string]$GitHubCli,
    [string]$InstanceRegistry,
    [string]$MachineLockToken,
    [string]$ReadinessReport,
    [switch]$PreflightOnly
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$stateHelper = Join-Path $scriptRoot 'incremental-upload-state.ps1'
if (-not (Test-Path -LiteralPath $stateHelper -PathType Leaf)) { throw 'Incremental upload state helper is missing.' }
. $stateHelper
$statePath = Join-Path ([IO.Path]::GetFullPath($WorkRoot)) 'INCREMENTAL_UPLOAD_STATE.json'

function Resolve-Python([string]$Requested) {
    if ($Requested) { return (Resolve-Path -LiteralPath $Requested).Path }
    $command = Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
    throw 'PythonExe is required or python.exe must be on PATH.'
}

function Resolve-Gh([string]$Requested) {
    if ($Requested) { return (Resolve-Path -LiteralPath $Requested).Path }
    $command = Get-Command gh.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
    $codexHome = if ($env:CODEX_HOME) { [IO.Path]::GetFullPath($env:CODEX_HOME) } else { Join-Path $env:USERPROFILE '.codex' }
    $configPath = Join-Path $codexHome 'cross-device-project-sync-github.json'
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) { throw 'GitHub CLI is not installed or configured.' }
    $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([string]$config.hostname -cne 'github.com') { throw 'Local GitHub configuration must use github.com.' }
    if (-not $env:GH_HTTP_TIMEOUT) { $env:GH_HTTP_TIMEOUT = [string][int]$config.http_timeout_seconds }
    return (Resolve-Path -LiteralPath ([string]$config.github_cli)).Path
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-TextHash([string]$Text) {
    $bytes = [Text.Encoding]::UTF8.GetBytes($Text)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($algorithm.ComputeHash($bytes))).Replace('-','').ToLowerInvariant() }
    finally { $algorithm.Dispose() }
}

function Get-HashPrefix([string]$Value, [int]$Length = 16) {
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -notmatch '^[0-9a-fA-F]{64}$') { throw 'Expected a 64-character SHA-256 value.' }
    if ($Length -lt 1 -or $Length -gt 64) { throw 'Hash prefix length is out of range.' }
    return $Value.Substring(0, $Length).ToLowerInvariant()
}

function Resolve-LocalDeviceIdentity([string]$ExpectedDevice, [string]$ExpectedComputer) {
    $computer = [Environment]::MachineName
    if ([string]::IsNullOrWhiteSpace($computer)) { throw 'Windows did not provide a computer name.' }
    $codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
    $configPath = Join-Path $codexHome 'cross-device-project-sync-device.json'
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) { throw "Local device identity is not configured: $configPath" }
    $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($null -eq $config -or $config -is [array]) { throw 'Local device identity configuration must be one JSON object.' }
    $label = ([string]$config.device_label).Trim()
    if ([string]::IsNullOrWhiteSpace($label)) { throw 'Local device label is empty.' }
    $configuredComputer = ([string]$config.computer_name).Trim()
    if ([string]::IsNullOrWhiteSpace($configuredComputer) -or $configuredComputer -ine $computer) {
        throw "Local device configuration must bind computer_name '$computer'."
    }
    if ($label -cne $ExpectedDevice -or $computer -ine $ExpectedComputer) {
        throw "Catalog source identity '$ExpectedDevice/$ExpectedComputer' does not match this computer '$label/$computer'."
    }
    return [pscustomobject]@{DeviceLabel=$label;ComputerName=$computer;ConfigPath=$configPath}
}

function Get-DeviceTagSlug([string]$Label, [string]$Computer) {
    if ($Label -match '^([A-Za-z0-9]+)设备$') { return $Matches[1].ToLowerInvariant() }
    $base = ([regex]::Replace($Computer.ToLowerInvariant(), '[^a-z0-9]+', '-')).Trim('-')
    if ([string]::IsNullOrWhiteSpace($base)) { $base = 'device' }
    if ($base.Length -gt 24) { $base = $base.Substring(0, 24).TrimEnd('-') }
    return "$base-$((Get-TextHash $Label).Substring(0,8))"
}

function Write-State {
    $state.projects = @($projectStates.Values | Sort-Object project_id)
    $json = $state | ConvertTo-Json -Depth 20
    $temp = "$statePath.new"
    [IO.File]::WriteAllText($temp, $json + "`n", [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temp -Destination $statePath -Force
}

function Get-RemoteAsset($Release, [string]$Name) {
    $matches = @($Release.assets | Where-Object name -ceq $Name)
    if ($matches.Count -ne 1) { throw "Release asset count mismatch for $Name" }
    if ([string]$matches[0].state -cne 'uploaded') { throw "Release asset is not uploaded: $Name" }
    return $matches[0]
}

function Assert-RemoteAsset($Release, [string]$Name, [int64]$Size, [string]$Sha256) {
    $asset = Get-RemoteAsset $Release $Name
    if ([int64]$asset.size -ne $Size -or [string]$asset.digest -cne ('sha256:' + $Sha256.ToLowerInvariant())) {
        throw "Release asset size or digest mismatch: $Name"
    }
}

function Verify-Release([string]$Tag, [ValidateSet('project-backup','conversation-pack','device-index','skill-package')][string]$Kind, [string]$Identity, [string]$ManifestSha256) {
    $json = & $gh release view $Tag --repo $Repository --json isDraft,tagName,body,url,assets
    if ($LASTEXITCODE -ne 0) { throw "Release lookup failed: $Tag" }
    $release = ($json -join "`n") | ConvertFrom-Json
    if ([bool]$release.isDraft -or [string]$release.tagName -cne $Tag) { throw "Release is missing or draft: $Tag" }
    $manifestName = if ($Kind -eq 'project-backup') { 'BACKUP_MANIFEST.json' } elseif ($Kind -eq 'conversation-pack') { 'CONVERSATION_PACK_MANIFEST.json' } elseif ($Kind -eq 'device-index') { 'INCREMENTAL_DEVICE_INDEX.json' } else { 'SKILL_PACKAGE_MANIFEST.json' }
    Assert-RemoteAsset $release $manifestName ([int64](Get-RemoteAsset $release $manifestName).size) $ManifestSha256
    $verifyRoot = Join-Path ([IO.Path]::GetTempPath()) ('cross-device-release-verify-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $verifyRoot | Out-Null
    try {
        if ($Kind -eq 'project-backup') {
            & $gh release download $Tag --repo $Repository --pattern 'RELEASE_BINDING.json' --pattern 'BACKUP_MANIFEST.json' --dir $verifyRoot
            if ($LASTEXITCODE -ne 0) { throw "Release binding download failed: $Tag" }
            $bindingPath = Join-Path $verifyRoot 'RELEASE_BINDING.json'
            $manifestPath = Join-Path $verifyRoot 'BACKUP_MANIFEST.json'
            if ((Get-Sha256 $manifestPath) -cne $ManifestSha256) { throw "Project manifest hash mismatch: $Tag" }
            Assert-RemoteAsset $release 'RELEASE_BINDING.json' ([int64](Get-Item -LiteralPath $bindingPath).Length) (Get-Sha256 $bindingPath)
            $binding = Get-Content -LiteralPath $bindingPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ([string]$binding.repository -cne $Repository -or [string]$binding.release_tag -cne $Tag -or [string]$binding.manifest_sha256 -cne $ManifestSha256) {
                throw "Project Release binding mismatch: $Tag"
            }
            $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ([string]$manifest.source_device -cne $sourceDevice -or [string]$manifest.source_computer -ine $sourceComputer) { throw "Project Release source identity mismatch: $Tag" }
            & $python -X utf8 (Join-Path $scriptRoot 'incremental-device-sync.py') manifest-fingerprint --manifest $manifestPath --expected $Identity | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Project Release tree fingerprint mismatch: $Tag" }
            foreach ($part in @($manifest.parts)) { Assert-RemoteAsset $release ([string]$part.name) ([int64]$part.size) ([string]$part.sha256) }
            $expectedNames = @('BACKUP_MANIFEST.json','RELEASE_BINDING.json','README.md','RESTORE.txt','restore.ps1','verify_split_tar.py','verify_restored_tree.py') + @($manifest.parts | ForEach-Object { [string]$_.name })
            if (@($release.assets).Count -ne $expectedNames.Count -or @($release.assets | Where-Object { $_.name -notin $expectedNames }).Count) { throw "Project Release asset inventory mismatch: $Tag" }
            foreach ($name in $expectedNames) { [void](Get-RemoteAsset $release $name) }
        } else {
            $expectedKind = $Kind
            if ([string]$release.body -notmatch [regex]::Escape("Content-Kind: $expectedKind") -or [string]$release.body -notmatch [regex]::Escape("Content-Identity-SHA256: $Identity")) {
                throw "Content Release notes identity mismatch: $Tag"
            }
            & $gh release download $Tag --repo $Repository --pattern 'CONTENT_RELEASE_BINDING.json' --dir $verifyRoot
            if ($LASTEXITCODE -ne 0) { throw "Content Release binding download failed: $Tag" }
            $bindingPath = Join-Path $verifyRoot 'CONTENT_RELEASE_BINDING.json'
            $binding = Get-Content -LiteralPath $bindingPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ([string]$binding.format -cne 'cross-device-project-sync-content-release-binding-v1' -or [string]$binding.repository -cne $Repository -or [string]$binding.release_tag -cne $Tag -or [string]$binding.kind -cne $expectedKind -or [string]$binding.identity_sha256 -cne $Identity) {
                throw "Content Release binding mismatch: $Tag"
            }
            $boundNames = @()
            foreach ($asset in @($binding.assets)) {
                $boundNames += [string]$asset.name
                Assert-RemoteAsset $release ([string]$asset.name) ([int64]$asset.size) ([string]$asset.sha256)
            }
            $manifestRecords = @($binding.assets | Where-Object name -ceq $manifestName)
            if ($manifestRecords.Count -ne 1 -or [string]$manifestRecords[0].sha256 -cne $ManifestSha256) { throw "Content Release manifest binding mismatch: $Tag" }
            $bindingAsset = Get-RemoteAsset $release 'CONTENT_RELEASE_BINDING.json'
            if ([string]$bindingAsset.digest -cne ('sha256:' + (Get-Sha256 $bindingPath))) { throw "Content Release binding digest mismatch: $Tag" }
            $expectedNames = @($boundNames + 'CONTENT_RELEASE_BINDING.json')
            if (@($release.assets).Count -ne $expectedNames.Count -or @($release.assets | Where-Object { $_.name -notin $expectedNames }).Count) { throw "Content Release asset inventory mismatch: $Tag" }
        }
        return $release
    } finally {
        $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
        $resolvedVerifyRoot = [IO.Path]::GetFullPath($verifyRoot)
        if ($resolvedVerifyRoot.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $resolvedVerifyRoot)) {
            [IO.Directory]::Delete($resolvedVerifyRoot, $true)
        }
    }
}

if ([string]::IsNullOrWhiteSpace($Repository) -or $Repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' -or $Repository.StartsWith('example-owner/')) { throw 'Set CROSS_DEVICE_SYNC_REPOSITORY or pass a real private owner/repository.' }
if ($env:CROSS_DEVICE_SYNC_REPOSITORY -and $Repository -cne $env:CROSS_DEVICE_SYNC_REPOSITORY) { throw 'Repository does not match CROSS_DEVICE_SYNC_REPOSITORY.' }
$python = Resolve-Python $PythonExe
$catalogPath = (Resolve-Path -LiteralPath $Catalog).Path
$planPath = (Resolve-Path -LiteralPath $Plan).Path
$readinessData = $null
if ($ReadinessReport) {
    $readinessPath = (Resolve-Path -LiteralPath $ReadinessReport).Path
    $readinessData = Get-Content -LiteralPath $readinessPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([string]$readinessData.status -cne 'ready' -or [string]::IsNullOrWhiteSpace([string]$readinessData.readiness_sha256)) {
        throw 'Bound device readiness report is not ready.'
    }
}
& $python -X utf8 (Join-Path $scriptRoot 'incremental-device-sync.py') verify --catalog $catalogPath | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Content catalog verification failed before upload.' }
& $python -X utf8 (Join-Path $scriptRoot 'incremental-device-sync.py') verify-plan --plan $planPath --catalog $catalogPath | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Incremental plan verification failed before upload.' }
$catalogData = Get-Content -LiteralPath $catalogPath -Raw -Encoding UTF8 | ConvertFrom-Json
$planData = Get-Content -LiteralPath $planPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ([string]$catalogData.format -ceq 'cross-device-project-sync-content-catalog-v2') {
    if (-not $InstanceRegistry -or -not $MachineLockToken) { throw 'Device v2 uploads require the machine lock from run-multi-instance-upload.ps1.' }
    if (-not $ReadinessReport) { throw 'Device v2 uploads require a fresh, hash-verified readiness report.' }
    & $python -X utf8 (Join-Path $scriptRoot 'multi-instance-sync.py') validate-lock --registry $InstanceRegistry --operation-id $MigrationId --lock-token $MachineLockToken --kind upload | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Machine-wide upload lock validation failed.' }
    $publicSkillRoot = Split-Path -Parent $scriptRoot
    & $python -X utf8 (Join-Path $scriptRoot 'device-readiness.py') --registry $InstanceRegistry --installed-skill-root $publicSkillRoot --expected-skill-version '20261019-v48' --verify-report $readinessPath --operation-id $MigrationId --lock-token $MachineLockToken --kind upload | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Device readiness report is stale, forged, or no longer matches this upload.' }
    & $python -X utf8 (Join-Path $scriptRoot 'multi-instance-sync.py') validate-work-root --registry $InstanceRegistry --work-root $WorkRoot | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Machine upload work root validation failed.' }
}
if (-not [bool]$catalogData.conversation_inventory_complete) {
    throw 'Content catalog does not prove a complete per-project conversation inventory; upload is blocked.'
}
$sourceDevice = [string]$catalogData.source_device
$sourceComputer = [string]$catalogData.source_computer
$identity = Resolve-LocalDeviceIdentity $sourceDevice $sourceComputer
$deviceTagSlug = Get-DeviceTagSlug $identity.DeviceLabel $identity.ComputerName
if ($PreflightOnly) {
    [pscustomobject]@{status='preflight-passed';source_device=$sourceDevice;source_computer=$sourceComputer;device_tag_slug=$deviceTagSlug;catalog_sha256=[string]$catalogData.catalog_sha256;plan_sha256=[string]$planData.plan_sha256;repository=$Repository} | ConvertTo-Json
    return
}
$gh = Resolve-Gh $GitHubCli
& $gh auth status --hostname github.com | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'GitHub authentication is invalid. Complete one official login and retry; valid keyring credentials are reused.' }
$repo = (& $gh repo view $Repository --json nameWithOwner,isPrivate,url | ConvertFrom-Json)
if (-not [bool]$repo.isPrivate -or [string]$repo.nameWithOwner -cne $Repository) { throw 'Canonical repository is not private or has unexpected identity.' }

$root = [IO.Path]::GetFullPath($WorkRoot)
New-Item -ItemType Directory -Path $root -Force | Out-Null
$auditPath = Join-Path $root 'SECRET_AUDIT.json'
$auditBindingPath = Join-Path $root 'SECRET_AUDIT_BINDING.json'
$auditScriptPath = Join-Path $scriptRoot 'audit-catalog-secrets.py'
$auditPolicySha = Get-Sha256 $auditScriptPath
$gitCommand = Get-Command git.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $gitCommand) { throw 'Git is required for the project-history credential audit.' }
$reuseAudit = $false
if ((Test-Path -LiteralPath $auditPath -PathType Leaf) -and (Test-Path -LiteralPath $auditBindingPath -PathType Leaf)) {
    try {
        $auditReport = Get-Content -LiteralPath $auditPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $auditBinding = Get-Content -LiteralPath $auditBindingPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $reuseAudit = (
            [string]$auditBinding.format -ceq 'cross-device-project-sync-secret-audit-binding-v1' -and
            [string]$auditBinding.status -ceq 'passed' -and
            [string]$auditReport.status -ceq 'passed' -and
            [string]$auditBinding.catalog_sha256 -ceq [string]$catalogData.catalog_sha256 -and
            [string]$auditReport.catalog_sha256 -ceq [string]$catalogData.catalog_sha256 -and
            [string]$auditBinding.audit_policy_sha256 -ceq $auditPolicySha -and
            [string]$auditBinding.report_sha256 -ceq (Get-Sha256 $auditPath)
        )
    } catch {
        $reuseAudit = $false
    }
}
if ($reuseAudit) {
    Write-Host 'Reusing the passed secret audit bound to this exact catalog and audit policy.'
} else {
    & $python -X utf8 $auditScriptPath --catalog $catalogPath --output $auditPath --git $gitCommand.Source
    if ($LASTEXITCODE -ne 0) { throw "Secret audit blocked upload. Review paths and categories in $auditPath; no project payload was uploaded." }
    $auditReport = Get-Content -LiteralPath $auditPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([string]$auditReport.status -cne 'passed' -or [string]$auditReport.catalog_sha256 -cne [string]$catalogData.catalog_sha256) { throw 'Secret audit output is not a passed report for this exact catalog.' }
    $auditBinding = [ordered]@{format='cross-device-project-sync-secret-audit-binding-v1';status='passed';catalog_sha256=[string]$catalogData.catalog_sha256;audit_policy_sha256=$auditPolicySha;report_sha256=(Get-Sha256 $auditPath)}
    $auditBindingJson = $auditBinding | ConvertTo-Json -Compress
    $auditBindingTemp = "$auditBindingPath.new"
    [IO.File]::WriteAllText($auditBindingTemp, $auditBindingJson + "`n", [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $auditBindingTemp -Destination $auditBindingPath -Force
}
$projectStates = @{}
if (Test-Path -LiteralPath $statePath -PathType Leaf) {
    $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([string]$state.migration_id -cne $MigrationId -or
        [string]$state.catalog_sha256 -cne [string]$catalogData.catalog_sha256 -or
        [string]$state.repository -cne $Repository -or
        [string]$state.source_device -cne $sourceDevice -or
        [string]$state.source_computer -ine $sourceComputer) {
        throw 'Existing upload state belongs to another migration, catalog, repository, or source device.'
    }
    foreach ($item in @($state.projects)) { $projectStates[[string]$item.project_id] = $item }
} else {
    $state = [ordered]@{format='cross-device-project-sync-incremental-upload-state-v1';migration_id=$MigrationId;catalog_sha256=[string]$catalogData.catalog_sha256;repository=$Repository;source_device=$sourceDevice;source_computer=$sourceComputer;skill_release=$null;projects=@();index_release=$null;status='running';last_error=$null}
    Write-State
}

try {
    $skillPackage = Join-Path $root ("skill-package-current-" + [Guid]::NewGuid().ToString('N'))
    & $python -X utf8 (Join-Path $scriptRoot 'package-installed-skill.py') build --skill-root (Split-Path -Parent $scriptRoot) --output $skillPackage
    if ($LASTEXITCODE -ne 0) { throw 'Skill package creation failed.' }
    & $python -X utf8 (Join-Path $scriptRoot 'package-installed-skill.py') verify --package $skillPackage
    if ($LASTEXITCODE -ne 0) { throw 'Skill package verification failed.' }
    $skillManifest = Get-Content -LiteralPath (Join-Path $skillPackage 'SKILL_PACKAGE_MANIFEST.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $skillManifestSha = Get-Sha256 (Join-Path $skillPackage 'SKILL_PACKAGE_MANIFEST.json')
    $skillPayloadPrefix = Get-HashPrefix ([string]$skillManifest.payload_sha256)
    $skillVersion = [string]$skillManifest.version
    if ($skillVersion -notmatch '^\d{8}-v\d+$') { throw 'Skill package version must use complete YYYYMMDD-vN form.' }
    $skillTag = "skill-cross-device-project-sync-$skillVersion-$skillPayloadPrefix"
    $skillStateMatches = $state.skill_release -and [string]$state.skill_release.tag -ceq $skillTag -and [string]$state.skill_release.manifest_sha256 -ceq $skillManifestSha
    if (-not $skillStateMatches) {
        & (Join-Path $scriptRoot 'upload-immutable-assets.ps1') -AssetDirectory $skillPackage -ReleaseTag $skillTag -Title "cross-device-project-sync $skillVersion" -Kind skill-package -IdentitySha256 ([string]$skillManifest.payload_sha256) -Repository $Repository -GitHubCli $gh
        if ($LASTEXITCODE -ne 0) { throw 'Skill package upload failed.' }
        [void](Verify-Release $skillTag 'skill-package' ([string]$skillManifest.payload_sha256) $skillManifestSha)
        $state.skill_release = [ordered]@{tag=$skillTag;manifest_sha256=$skillManifestSha;reused=$false}
        Write-State
    } else {
        [void](Verify-Release $skillTag 'skill-package' ([string]$skillManifest.payload_sha256) $skillManifestSha)
    }

    $decisions = @{}
    foreach ($item in @($planData.decisions)) { $decisions[[string]$item.project_id] = $item }
    foreach ($project in @($catalogData.projects)) {
        $projectId = [string]$project.project_id
        $decision = $decisions[$projectId]
        if (-not $decision) { throw "Missing plan decision for $projectId" }
        $record = $projectStates[$projectId]
        if ($record -and [string]$record.status -eq 'complete') {
            if ([bool]$project.has_project_payload -or $null -eq $project.has_project_payload) {
                [void](Verify-Release ([string]$record.project_release.tag) 'project-backup' ([string]$project.tree_sha256) ([string]$record.project_release.manifest_sha256))
            } elseif ($record.project_release) {
                throw "Conversation-only group unexpectedly has a project Release: $projectId"
            }
            [void](Verify-Release ([string]$record.conversation_release.tag) 'conversation-pack' ([string]$project.conversation_set_sha256) ([string]$record.conversation_release.manifest_sha256))
            continue
        }
        # Preserve verified local preparation and remote-release checkpoints
        # across retries. Replacing this row would turn resume into a full
        # rebuild and could publish a duplicate immutable snapshot.
        $record = New-ResumedProjectUploadState -ExistingRecord $record -ProjectId $projectId -Label ([string]$project.label) -SourcePath ([string]$project.source_path)
        $projectStates[$projectId] = $record
        Write-State
        try {
            if ([string]$decision.project_action -eq 'none') {
                if ([bool]$project.has_project_payload -or $null -eq $project.has_project_payload) { throw "Project payload cannot use project_action=none: $projectId" }
                $record.project_release = $null
            } elseif ([string]$decision.project_action -eq 'reuse') {
                $record.project_release = [ordered]@{tag=[string]$decision.project_release.tag;manifest_sha256=[string]$decision.project_release.manifest_sha256;reused=$true}
                [void](Verify-Release ([string]$record.project_release.tag) 'project-backup' ([string]$project.tree_sha256) ([string]$record.project_release.manifest_sha256))
            } else {
                $idHash = Get-TextHash $projectId
                $projectOutput = Join-Path (Join-Path $root 'projects') $idHash.Substring(0,16)
                New-Item -ItemType Directory -Path $projectOutput -Force | Out-Null
                $prepared = $null
                if ($record.prepared_backup -and (Test-Path -LiteralPath ([string]$record.prepared_backup) -PathType Container)) { $prepared = [string]$record.prepared_backup }
                if (-not $prepared) {
                    $prepareParameters = @{
                        SourceRoot = [string]$project.source_path
                        OutputRoot = $projectOutput
                        PartSizeMiB = 512
                        PythonExe = $python
                        DeviceLabel = $sourceDevice
                        ExcludeRelativePath = @($project.excluded_relative_paths | ForEach-Object { [string]$_ })
                    }
                    & (Join-Path $scriptRoot 'prepare-full-backup.ps1') @prepareParameters
                    if ($LASTEXITCODE -ne 0) { throw 'Project backup preparation failed.' }
                    $candidates = @(Get-ChildItem -LiteralPath $projectOutput -Directory -Filter 'full-backup-*' | Sort-Object LastWriteTimeUtc -Descending | ForEach-Object { Join-Path $_.FullName 'prepared-release-assets' } | Where-Object { Test-Path -LiteralPath (Join-Path $_ 'BACKUP_MANIFEST.json') -PathType Leaf })
                    if ($candidates.Count -lt 1) { throw 'Prepared backup output was not found.' }
                    $prepared = $candidates[0]
                    $record.prepared_backup = $prepared
                    Write-State
                }
                & $python -X utf8 (Join-Path $scriptRoot 'incremental-device-sync.py') manifest-fingerprint --manifest (Join-Path $prepared 'BACKUP_MANIFEST.json') --expected ([string]$project.tree_sha256)
                if ($LASTEXITCODE -ne 0) { throw 'Prepared project manifest does not match the approved content catalog.' }
                $projectManifestSha = Get-Sha256 (Join-Path $prepared 'BACKUP_MANIFEST.json')
                $projectTreePrefix = Get-HashPrefix ([string]$project.tree_sha256)
                $projectManifestPrefix = Get-HashPrefix $projectManifestSha
                $projectTag = "project-$deviceTagSlug-$($idHash.Substring(0,12))-$projectTreePrefix-$projectManifestPrefix"
                & (Join-Path $scriptRoot 'upload-prepared-backup.ps1') -PreparedBackupPath $prepared -Repository $Repository -ReleaseTag $projectTag -Title "$sourceDevice 项目备份 · $([string]$project.label)" -GitHubCli $gh -PythonExe $python -DeviceLabel $sourceDevice
                if ($LASTEXITCODE -ne 0) { throw 'Project Release upload failed.' }
                [void](Verify-Release $projectTag 'project-backup' ([string]$project.tree_sha256) $projectManifestSha)
                $record.project_release = [ordered]@{tag=$projectTag;manifest_sha256=$projectManifestSha;reused=$false}
                Write-State
            }

            if ([string]$decision.conversation_action -eq 'reuse') {
                $record.conversation_release = [ordered]@{tag=[string]$decision.conversation_release.tag;manifest_sha256=[string]$decision.conversation_release.manifest_sha256;reused=$true}
                [void](Verify-Release ([string]$record.conversation_release.tag) 'conversation-pack' ([string]$project.conversation_set_sha256) ([string]$record.conversation_release.manifest_sha256))
            } else {
                $idHash = Get-TextHash $projectId
                $conversationPrefix = Get-HashPrefix ([string]$project.conversation_set_sha256)
                $conversationTag = "conversation-$deviceTagSlug-$($idHash.Substring(0,12))-$conversationPrefix"
                $pack = Join-Path (Join-Path $root 'conversations') "$($idHash.Substring(0,16))-$conversationPrefix"
                if (-not (Test-Path -LiteralPath (Join-Path $pack 'CONVERSATION_PACK_MANIFEST.json') -PathType Leaf)) {
                    if (Test-Path -LiteralPath $pack) {
                        $conversationRoot = [IO.Path]::GetFullPath((Join-Path $root 'conversations')).TrimEnd('\') + '\'
                        $resolvedPack = [IO.Path]::GetFullPath($pack)
                        if (-not $resolvedPack.StartsWith($conversationRoot, [StringComparison]::OrdinalIgnoreCase) -or
                            [IO.Path]::GetDirectoryName($resolvedPack).TrimEnd('\') -ine $conversationRoot.TrimEnd('\')) {
                            throw 'Unexpected incomplete conversation pack is outside the bounded work directory.'
                        }
                        $quarantine = "$resolvedPack.incomplete-$([Guid]::NewGuid().ToString('N'))"
                        Move-Item -LiteralPath $resolvedPack -Destination $quarantine
                        if ((Test-Path -LiteralPath $resolvedPack) -or -not (Test-Path -LiteralPath $quarantine -PathType Container)) {
                            throw 'Incomplete conversation pack could not be quarantined safely.'
                        }
                    }
                    & $python -X utf8 (Join-Path $scriptRoot 'bulk-conversation-pack.py') build --catalog $catalogPath --project-id $projectId --output $pack
                    if ($LASTEXITCODE -ne 0) { throw 'Conversation pack creation failed.' }
                }
                & $python -X utf8 (Join-Path $scriptRoot 'bulk-conversation-pack.py') verify --pack $pack
                if ($LASTEXITCODE -ne 0) { throw 'Conversation pack verification failed.' }
                $conversationManifestSha = Get-Sha256 (Join-Path $pack 'CONVERSATION_PACK_MANIFEST.json')
                & (Join-Path $scriptRoot 'upload-immutable-assets.ps1') -AssetDirectory $pack -ReleaseTag $conversationTag -Title "$sourceDevice 对话包 · $([string]$project.label)" -Kind conversation-pack -IdentitySha256 ([string]$project.conversation_set_sha256) -Repository $Repository -GitHubCli $gh
                if ($LASTEXITCODE -ne 0) { throw 'Conversation pack upload failed.' }
                [void](Verify-Release $conversationTag 'conversation-pack' ([string]$project.conversation_set_sha256) $conversationManifestSha)
                $record.conversation_pack = $pack
                $record.conversation_release = [ordered]@{tag=$conversationTag;manifest_sha256=$conversationManifestSha;reused=$false}
            }
            $record.status = 'complete'
            $record.error = $null
            Write-State
        } catch {
            $record.status = 'failed'
            $record.error = $_.Exception.Message
            Write-State
            Write-Error "Project failed: $projectId · $($record.error)" -ErrorAction Continue
        }
    }

    $failed = @($projectStates.Values | Where-Object status -ne 'complete')
    if ($failed.Count) { throw "$($failed.Count) project(s) remain failed; final device index was not published." }
    $bindings = [ordered]@{migration_id=$MigrationId;skill_release=$state.skill_release;projects=@($projectStates.Values | Sort-Object project_id | ForEach-Object { [ordered]@{project_id=$_.project_id;project_release=$_.project_release;conversation_release=$_.conversation_release} })}
    $indexDir = Join-Path $root 'device-index'
    New-Item -ItemType Directory -Path $indexDir -Force | Out-Null
    $bindingsPath = Join-Path $root 'RELEASE_BINDINGS.json'
    [IO.File]::WriteAllText($bindingsPath, ($bindings | ConvertTo-Json -Depth 12) + "`n", [Text.UTF8Encoding]::new($false))
    $indexPath = Join-Path $indexDir 'INCREMENTAL_DEVICE_INDEX.json'
    $handoffPath = Join-Path $indexDir 'RECEIVER_READ_FIRST.md'
    & $python -X utf8 (Join-Path $scriptRoot 'incremental-device-sync.py') build-index --catalog $catalogPath --bindings $bindingsPath --output $indexPath --handoff $handoffPath
    if ($LASTEXITCODE -ne 0) { throw 'Final device index build failed.' }
    & $python -X utf8 (Join-Path $scriptRoot 'incremental-device-sync.py') verify-index --index $indexPath
    if ($LASTEXITCODE -ne 0) { throw 'Final device index verification failed.' }
    $indexData = Get-Content -LiteralPath $indexPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $indexPrefix = Get-HashPrefix ([string]$indexData.index_sha256)
    $indexTag = "device-index-$deviceTagSlug-$indexPrefix"
    & (Join-Path $scriptRoot 'upload-immutable-assets.ps1') -AssetDirectory $indexDir -ReleaseTag $indexTag -Title "$sourceDevice 增量迁移索引 · $MigrationId" -Kind device-index -IdentitySha256 ([string]$indexData.index_sha256) -Repository $Repository -GitHubCli $gh -SourceDevice $sourceDevice -SourceComputer $sourceComputer -MigrationId $MigrationId
    if ($LASTEXITCODE -ne 0) { throw 'Device index upload failed.' }
    [void](Verify-Release $indexTag 'device-index' ([string]$indexData.index_sha256) (Get-Sha256 $indexPath))
    $verifyDir = Join-Path $root 'redownloaded-device-index'
    New-Item -ItemType Directory -Path $verifyDir -Force | Out-Null
    & $gh release download $indexTag --repo $Repository --pattern 'INCREMENTAL_DEVICE_INDEX.json' --pattern 'RECEIVER_READ_FIRST.md' --pattern 'CONTENT_RELEASE_BINDING.json' --dir $verifyDir --clobber
    if ($LASTEXITCODE -ne 0) { throw 'Device index re-download failed.' }
    foreach ($name in @('INCREMENTAL_DEVICE_INDEX.json','RECEIVER_READ_FIRST.md','CONTENT_RELEASE_BINDING.json')) {
        if ((Get-Sha256 (Join-Path $verifyDir $name)) -cne (Get-Sha256 (Join-Path $indexDir $name))) { throw "Re-downloaded device index asset differs: $name" }
    }
    & $python -X utf8 (Join-Path $scriptRoot 'incremental-device-sync.py') verify-index --index (Join-Path $verifyDir 'INCREMENTAL_DEVICE_INDEX.json')
    if ($LASTEXITCODE -ne 0) { throw 'Re-downloaded device index verification failed.' }
    $state.index_release = [ordered]@{tag=$indexTag;manifest_sha256=(Get-Sha256 $indexPath);reused=$false}
    $state.status = 'complete'
    $state.last_error = $null
    Write-State
    $payloadProjects = @($catalogData.projects | Where-Object { [bool]$_.has_project_payload -or $null -eq $_.has_project_payload }).Count
    $conversationOnlyGroups = @($catalogData.projects | Where-Object { $null -ne $_.has_project_payload -and -not [bool]$_.has_project_payload }).Count
    [pscustomobject]@{status='complete';migration_id=$MigrationId;index_release_tag=$indexTag;index_sha256=[string]$indexData.index_sha256;project_count=$payloadProjects;conversation_only_group_count=$conversationOnlyGroups;repository=$Repository;state_path=$statePath;readiness_sha256=if($readinessData){[string]$readinessData.readiness_sha256}else{$null}} | ConvertTo-Json
} catch {
    $state.status = 'failed-or-interrupted'
    if ($state.PSObject.Properties.Name -contains 'last_error') { $state.last_error = $_.Exception.Message }
    else { $state | Add-Member -NotePropertyName last_error -NotePropertyValue $_.Exception.Message }
    Write-State
    throw
}
