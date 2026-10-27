[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$InstanceRegistry,
    [Parameter(Mandatory = $true)][string]$CallerCodexHome,
    [Parameter(Mandatory = $true)][string]$WorkRoot,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$')][string]$MigrationId,
    [string]$PreviousCatalogOrIndex,
    [string]$Repository = $env:CROSS_DEVICE_SYNC_REPOSITORY,
    [string]$PythonExe,
    [string]$GitHubCli,
    [switch]$PreflightOnly
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$multi = Join-Path $scriptRoot 'multi-instance-sync.py'
$incremental = Join-Path $scriptRoot 'incremental-device-sync.py'
$uploader = Join-Path $scriptRoot 'run-incremental-upload.ps1'
$readinessTool = Join-Path $scriptRoot 'device-readiness.py'
$skillRoot = Split-Path -Parent $scriptRoot
if ([string]::IsNullOrWhiteSpace($Repository) -or $Repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' -or $Repository.StartsWith('example-owner/')) { throw 'Set CROSS_DEVICE_SYNC_REPOSITORY or pass a real private owner/repository.' }

function Resolve-Python([string]$Requested) {
    if ($Requested) { return (Resolve-Path -LiteralPath $Requested).Path }
    $known = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $known -PathType Leaf) { return $known }
    $command = Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
    throw 'PythonExe is required.'
}

function Invoke-JsonPython([string[]]$Arguments) {
    $lines = @(& $python -X utf8 @Arguments)
    if ($LASTEXITCODE -ne 0) { throw 'Python helper failed; see its diagnostic (arguments are omitted to protect operation tokens).' }
    if (-not $lines.Count) { throw 'Python helper returned no JSON.' }
    return (($lines[-1] | ConvertFrom-Json))
}

$python = Resolve-Python $PythonExe
$registry = (Resolve-Path -LiteralPath $InstanceRegistry).Path
$callerHome = (Resolve-Path -LiteralPath $CallerCodexHome).Path
$root = [IO.Path]::GetFullPath($WorkRoot)
[void](Invoke-JsonPython @($multi, 'validate-registry', '--registry', $registry, '--caller-home', $callerHome))
[void](Invoke-JsonPython @($multi, 'validate-work-root', '--registry', $registry, '--work-root', $root))
$readinessDirectory = Join-Path (Split-Path -Parent $registry) 'readiness'
$readinessReport = Join-Path $readinessDirectory ("upload-" + $MigrationId + '.json')
$readiness = Invoke-JsonPython @(
    $readinessTool, '--registry', $registry,
    '--installed-skill-root', $skillRoot,
    '--expected-skill-version', '20261019-v48',
    '--output', $readinessReport
)
if ([string]$readiness.status -cne 'ready' -or [string]::IsNullOrWhiteSpace([string]$readiness.readiness_sha256)) {
    throw 'Device readiness did not pass; upload was not started.'
}
New-Item -ItemType Directory -Path $root -Force | Out-Null
$discovery = Join-Path $root 'MULTI_INSTANCE_DISCOVERY.json'
$protectedDiscovery = Join-Path $root 'PROTECTED_MULTI_INSTANCE_DISCOVERY.json'
$branchState = Join-Path (Split-Path -Parent $registry) 'lineage-branches.json'
$snapshotRoot = Join-Path $root 'device-session-snapshot'
$catalog = Join-Path $root 'CONTENT_CATALOG.json'
$plan = Join-Path $root 'INCREMENTAL_PLAN.json'
$checkpoints = Join-Path $root 'scan-checkpoints'
$status = Join-Path $root 'scan-status.json'
$lockToken = $null

try {
    $lock = Invoke-JsonPython @($multi, 'acquire-lock', '--registry', $registry, '--caller-home', $callerHome, '--operation-id', $MigrationId, '--kind', 'upload')
    $lockToken = [string]$lock.lock_token
    if ([string]::IsNullOrWhiteSpace($lockToken)) { throw 'Machine lock helper returned no lock token.' }

    if (-not (Test-Path -LiteralPath $catalog -PathType Leaf)) {
        if (-not (Test-Path -LiteralPath $discovery -PathType Leaf)) {
            [void](Invoke-JsonPython @($multi, 'snapshot-discovery', '--registry', $registry, '--operation-id', $MigrationId, '--lock-token', $lockToken, '--snapshot-root', $snapshotRoot, '--output', $discovery))
        }
        if (-not (Test-Path -LiteralPath $protectedDiscovery -PathType Leaf)) {
            [void](Invoke-JsonPython @((Join-Path $scriptRoot 'add-protected-browser-exclusions.py'), '--discovery', $discovery, '--output', $protectedDiscovery))
        }
        [void](Invoke-JsonPython @($incremental, 'scan', '--discovery', $protectedDiscovery, '--output', $catalog, '--checkpoint-dir', $checkpoints, '--status-file', $status, '--branch-state', $branchState))
    } else {
        if (-not (Test-Path -LiteralPath $protectedDiscovery -PathType Leaf)) { throw 'Resume lacks protected discovery; use a new migration directory.' }
        $recheck = Join-Path $root 'CONTENT_CATALOG_RECHECK.json'
        [void](Invoke-JsonPython @($incremental, 'scan', '--discovery', $protectedDiscovery, '--output', $recheck, '--status-file', $status, '--branch-state', $branchState))
        $priorCatalog = Get-Content -LiteralPath $catalog -Raw -Encoding UTF8 | ConvertFrom-Json
        $freshCatalog = Get-Content -LiteralPath $recheck -Raw -Encoding UTF8 | ConvertFrom-Json
        if ([string]$priorCatalog.catalog_sha256 -cne [string]$freshCatalog.catalog_sha256) {
            throw 'Project bytes or frozen conversation evidence changed since this migration. Keep previous releases; start a new migration ID and directory.'
        }
    }
    [void](Invoke-JsonPython @($incremental, 'verify', '--catalog', $catalog))
    if (-not (Test-Path -LiteralPath $plan -PathType Leaf)) {
        $planArguments = @($incremental, 'plan', '--current', $catalog, '--output', $plan)
        if ($PreviousCatalogOrIndex) { $planArguments += @('--previous', (Resolve-Path -LiteralPath $PreviousCatalogOrIndex).Path) }
        [void](Invoke-JsonPython $planArguments)
    }
    [void](Invoke-JsonPython @($incremental, 'verify-plan', '--plan', $plan, '--catalog', $catalog))

    $uploadParameters = @{
        Catalog = $catalog
        Plan = $plan
        WorkRoot = $root
        MigrationId = $MigrationId
        Repository = $Repository
        PythonExe = $python
        PreflightOnly = $PreflightOnly
        InstanceRegistry = $registry
        MachineLockToken = $lockToken
        ReadinessReport = $readinessReport
    }
    if ($GitHubCli) { $uploadParameters.GitHubCli = $GitHubCli }
    & $uploader @uploadParameters
    if ($LASTEXITCODE -ne 0) { throw 'Incremental upload workflow failed.' }
} finally {
    if ($lockToken) {
        & $python -X utf8 $multi release-lock --registry $registry --operation-id $MigrationId --lock-token $lockToken | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Warning "Machine lock release failed for $MigrationId; inspect and recover it explicitly before another sync." }
    }
}
