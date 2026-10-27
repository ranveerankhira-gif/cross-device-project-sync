param(
    [string]$SourceRoot = (Get-Location).Path,
    [Parameter(Mandatory = $true)]
    [string]$OutputRoot,
    [ValidateRange(1, 1024)]
    [int]$PartSizeMiB = 512,
    [ValidateRange(2, 100)]
    [int]$MaxConvergencePasses = 20,
    [string[]]$ExcludeRelativePath = @(),
    [switch]$AllowBroadSourceRoot,
    [string]$PythonExe,
    [string]$DeviceLabel
)

$ErrorActionPreference = 'Stop'

function Resolve-WindowsSystemExecutable {
    param([Parameter(Mandatory = $true)][string]$Name)
    $systemDirectory = [Environment]::SystemDirectory
    if ([string]::IsNullOrWhiteSpace($systemDirectory)) {
        throw 'Windows did not provide a trusted system directory.'
    }
    $path = Join-Path $systemDirectory $Name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required Windows system executable is missing: $path"
    }
    return (Resolve-Path -LiteralPath $path).Path
}

$robocopyExecutable = Resolve-WindowsSystemExecutable -Name 'robocopy.exe'

function Resolve-DeviceIdentity {
    param([string]$Requested)
    $computer = [Environment]::MachineName
    if ([string]::IsNullOrWhiteSpace($computer) -or $computer.Length -gt 80 -or $computer -match '[\x00-\x1f\x7f]') {
        throw 'Windows did not provide a valid computer name.'
    }

    $codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
    $configPath = Join-Path $codexHome 'cross-device-project-sync-device.json'
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        throw "Local device identity is not configured: $configPath"
    }
    $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($null -eq $config -or $config -is [array]) {
        throw 'Local device identity configuration must be one JSON object.'
    }
    $configured = [string]$config.device_label
    if ([string]::IsNullOrWhiteSpace($configured) -or $configured.Length -gt 80 -or $configured -match '[\x00-\x1f\x7f]') {
        throw 'Configured device label is invalid.'
    }
    $configured = $configured.Trim()
    if (-not [string]::IsNullOrWhiteSpace($Requested) -and $Requested.Trim() -cne $configured) {
        throw "Requested device label '$Requested' does not match the local configuration '$configured'."
    }

    $configuredComputer = ([string]$config.computer_name).Trim()
    if ([string]::IsNullOrWhiteSpace($configuredComputer) -or $configuredComputer -ine $computer) {
        throw "Local device configuration must bind computer_name '$computer'."
    }

    return [pscustomobject]@{
        DeviceLabel = $configured
        ComputerName = $computer
        ConfigPath = $configPath
    }
}

$deviceIdentity = Resolve-DeviceIdentity -Requested $DeviceLabel
$device = [string]$deviceIdentity.DeviceLabel
$sourceComputer = [string]$deviceIdentity.ComputerName

function Resolve-Python {
    param([string]$Requested)
    if ($Requested) {
        return (Resolve-Path -LiteralPath $Requested).Path
    }
    foreach ($name in @('python.exe', 'python3.exe')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command) { return $command.Source }
    }
    throw 'Python 3 was not found. Pass -PythonExe with an absolute path.'
}

function Test-WindowsLongPathsEnabled {
    try {
        return [int](Get-ItemPropertyValue -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -ErrorAction Stop) -eq 1
    } catch {
        return $false
    }
}

function Get-WindowsDownloadsFolderPath {
    $registryPath = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders'
    $downloadsId = '{374DE290-123F-4565-9164-39C4925E467B}'
    try {
        $raw = [string](Get-ItemPropertyValue -LiteralPath $registryPath -Name $downloadsId -ErrorAction Stop)
        if (-not [string]::IsNullOrWhiteSpace($raw)) {
            return [Environment]::ExpandEnvironmentVariables($raw)
        }
    } catch {
        # Fall through to the conventional path when Windows has no explicit mapping.
    }
    if ($env:USERPROFILE) { return (Join-Path $env:USERPROFILE 'Downloads') }
    return $null
}

function Assert-ProjectSizedSourceRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [switch]$AllowBroadRoot
    )
    $normalized = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $broadRoots = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($candidate in @(
        [IO.Path]::GetPathRoot($normalized),
        $env:USERPROFILE,
        [Environment]::GetFolderPath([Environment+SpecialFolder]::Desktop),
        [Environment]::GetFolderPath([Environment+SpecialFolder]::MyDocuments),
        (Get-WindowsDownloadsFolderPath),
        $(if ($env:USERPROFILE) { Join-Path $env:USERPROFILE 'Downloads' } else { $null })
    )) {
        if (-not [string]::IsNullOrWhiteSpace($candidate)) {
            [void]$broadRoots.Add([IO.Path]::GetFullPath($candidate).TrimEnd([IO.Path]::DirectorySeparatorChar))
        }
    }
    if ($broadRoots.Contains($normalized)) {
        if (-not $AllowBroadRoot) {
            throw "SourceRoot is a broad container rather than an ordinary project: $Root. Exclude it from an all-project migration. Use -AllowBroadSourceRoot only after the user explicitly names this exact root."
        }
        Write-Warning "Explicit broad SourceRoot override accepted: $Root"
    }
}

function Invoke-RobocopyPass {
    param(
        [string]$Source,
        [string]$Target,
        [switch]$Mirror,
        [string[]]$ExcludedDirectories = @()
    )
    $mode = if ($Mirror) { '/MIR' } else { '/E' }
    $arguments = @($Source, $Target, $mode, '/COPY:DAT', '/DCOPY:DAT', '/R:2', '/W:1', '/MT:16', '/J', '/XJD', '/XJF', '/NFL', '/NDL', '/NP', '/NJH', '/NJS')
    if ($ExcludedDirectories.Count -gt 0) {
        $arguments += '/XD'
        $arguments += $ExcludedDirectories
    }
    & $robocopyExecutable @arguments | Out-Null
    $code = $LASTEXITCODE
    if ($code -ge 8) {
        throw "Robocopy failed with exit code $code"
    }
    return $code
}

function Assert-SafeSourceTree {
    param(
        [string]$Root,
        [string[]]$ExcludedDirectories
    )
    $rootItem = Get-Item -LiteralPath $Root -Force
    if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "SourceRoot may not be a reparse point: $Root"
    }
    if ($rootItem.Name -match '(?i)^(?:chrome|chromium|edge|msedge|browser|playwright).*test[-_ ]?profile(?:[-_ ].*)?$') {
        throw "A protected browser test profile may not be used as SourceRoot: $Root"
    }

    $foundReparsePoints = @()
    $pendingDirectories = [Collections.Generic.Stack[string]]::new()
    $pendingDirectories.Push($Root)
    while ($pendingDirectories.Count -gt 0) {
        $currentDirectory = $pendingDirectories.Pop()
        foreach ($item in Get-ChildItem -LiteralPath $currentDirectory -Force -ErrorAction Stop) {
            $full = [IO.Path]::GetFullPath($item.FullName)
            $isExcluded = $false
            foreach ($excluded in $ExcludedDirectories) {
                if ($full.Equals($excluded, [StringComparison]::OrdinalIgnoreCase) -or
                    $full.StartsWith($excluded.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
                    $isExcluded = $true
                    break
                }
            }
            if ($isExcluded) { continue }
            if ($item.PSIsContainer -and $item.Name -match '(?i)^(?:chrome|chromium|edge|msedge|browser|playwright).*test[-_ ]?profile(?:[-_ ].*)?$') {
                throw "Protected browser test profile must be declared with -ExcludeRelativePath: $full"
            }
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                $foundReparsePoints += $item
            }
            elseif ($item.PSIsContainer) {
                $pendingDirectories.Push($full)
            }
        }
    }
    if ($foundReparsePoints.Count -gt 0) {
        throw "The source contains $($foundReparsePoints.Count) reparse point(s). Stop instead of silently following or excluding them."
    }
}

function Get-BackupCapacityEstimate {
    param(
        [string]$Root,
        [string[]]$ExcludedDirectories
    )
    [decimal]$fileBytes = 0
    [int64]$entryCount = 1
    [int]$maximumPathLength = $Root.Length
    [int]$maximumRelativePathLength = 0
    $rootPrefix = $Root.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    $pendingDirectories = [Collections.Generic.Stack[string]]::new()
    $pendingDirectories.Push($Root)
    while ($pendingDirectories.Count -gt 0) {
        $currentDirectory = $pendingDirectories.Pop()
        foreach ($item in Get-ChildItem -LiteralPath $currentDirectory -Force -ErrorAction Stop) {
            $full = [IO.Path]::GetFullPath($item.FullName)
            if ($full.Length -gt $maximumPathLength) { $maximumPathLength = $full.Length }
            $relativeLength = $full.Substring($rootPrefix.Length).Length
            if ($relativeLength -gt $maximumRelativePathLength) { $maximumRelativePathLength = $relativeLength }
            $isExcluded = $false
            foreach ($excluded in $ExcludedDirectories) {
                if ($full.Equals($excluded, [StringComparison]::OrdinalIgnoreCase) -or
                    $full.StartsWith($excluded.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
                    $isExcluded = $true
                    break
                }
            }
            if ($isExcluded) { continue }
            $entryCount++
            if ($item.PSIsContainer) {
                $pendingDirectories.Push($full)
            } else {
                $fileBytes += [decimal]$item.Length
            }
        }
    }
    # Allow for one full snapshot, one TAR, filesystem allocation for many small files,
    # TAR/PAX metadata, the manifest, and temporary growth during convergence.
    [decimal]$requiredBytes = ($fileBytes * 2) + ([decimal]$entryCount * 65536) + 536870912
    [pscustomobject]@{
        FileBytes = $fileBytes
        EntryCount = $entryCount
        RequiredBytes = $requiredBytes
        MaximumPathLength = $maximumPathLength
        MaximumRelativePathLength = $maximumRelativePathLength
    }
}

$source = (Resolve-Path -LiteralPath $SourceRoot).Path
if (-not (Test-Path -LiteralPath $source -PathType Container)) {
    throw 'SourceRoot must be a directory.'
}
Assert-ProjectSizedSourceRoot -Root $source -AllowBroadRoot:$AllowBroadSourceRoot
$sourceCursor = Get-Item -LiteralPath $source -Force
while ($sourceCursor) {
    if ($sourceCursor.Name -match '(?i)^(?:chrome|chromium|edge|msedge|browser|playwright).*test[-_ ]?profile(?:[-_ ].*)?$') {
        throw "SourceRoot is inside a protected browser test profile: $($sourceCursor.FullName)"
    }
    if (($sourceCursor.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "SourceRoot may not be inside a reparse-point path: $($sourceCursor.FullName)"
    }
    $sourceCursor = $sourceCursor.Parent
}

$sourcePrefix = $source.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
$excludedSourceDirectories = @()
$normalizedExclusions = @()
foreach ($relative in $ExcludeRelativePath) {
    if ([IO.Path]::IsPathRooted($relative) -or $relative -split '[\\/]' -contains '..') {
        throw "ExcludeRelativePath must stay inside SourceRoot: $relative"
    }
    $candidate = [IO.Path]::GetFullPath((Join-Path $source $relative))
    if (-not $candidate.StartsWith($sourcePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "ExcludeRelativePath resolved outside SourceRoot: $relative"
    }
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        throw "ExcludeRelativePath must name a directory root, not a file: $relative"
    }
    $excludedSourceDirectories += $candidate
    $normalizedExclusions += ($candidate.Substring($sourcePrefix.Length) -replace '\\', '/')
}

Assert-SafeSourceTree -Root $source -ExcludedDirectories $excludedSourceDirectories
$capacityEstimate = Get-BackupCapacityEstimate -Root $source -ExcludedDirectories $excludedSourceDirectories
if ($capacityEstimate.MaximumPathLength -ge 32767) {
    throw "The source contains a path with $($capacityEstimate.MaximumPathLength) characters, exceeding the supported Windows extended-path limit. Shorten the project path before backup."
}
$longPathsEnabled = Test-WindowsLongPathsEnabled
if ($capacityEstimate.MaximumPathLength -ge 260 -and -not $longPathsEnabled) {
    throw "The source contains a path with $($capacityEstimate.MaximumPathLength) characters, but Windows LongPathsEnabled is not active. Enable long-path support before preparing this backup."
}

$python = Resolve-Python -Requested $PythonExe
$scriptRoot = $PSScriptRoot
$createScript = Join-Path $scriptRoot 'create-split-tar.py'
$verifyMirrorScript = Join-Path $scriptRoot 'verify-mirror-tree.py'
$verifyScript = Join-Path $scriptRoot 'verify-split-tar.py'
$verifyRestoreScript = Join-Path $scriptRoot 'verify-restored-tree.py'
$restoreScript = Join-Path $scriptRoot 'restore-full-backup.ps1'
foreach ($required in @($createScript, $verifyMirrorScript, $verifyScript, $verifyRestoreScript, $restoreScript)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required helper is missing: $required"
    }
}

$resolvedOutputBase = [IO.Path]::GetFullPath($OutputRoot)
if ($resolvedOutputBase.Equals($source, [StringComparison]::OrdinalIgnoreCase) -or
    $resolvedOutputBase.StartsWith($sourcePrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'OutputRoot must be outside SourceRoot to prevent recursive self-backup.'
}
$outputPathRoot = [IO.Path]::GetPathRoot($resolvedOutputBase)
try {
    $outputDrive = [IO.DriveInfo]::new($outputPathRoot)
    if (-not $outputDrive.IsReady) { throw 'destination drive is not ready' }
    [decimal]$availableBytes = $outputDrive.AvailableFreeSpace
} catch {
    throw "Unable to determine free space for OutputRoot: $($_.Exception.Message)"
}
if ($availableBytes -lt $capacityEstimate.RequiredBytes) {
    $requiredGiB = [math]::Ceiling([double]($capacityEstimate.RequiredBytes / 1GB) * 100) / 100
    $availableGiB = [math]::Floor([double]($availableBytes / 1GB) * 100) / 100
    throw "OutputRoot needs about $requiredGiB GiB free, but only $availableGiB GiB is available. Choose another drive before creating the snapshot."
}
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$plannedSnapshot = Join-Path (Join-Path (Join-Path $resolvedOutputBase "full-backup-$stamp") 'snapshot') (Split-Path -Leaf $source)
$maximumSnapshotPathLength = $plannedSnapshot.Length + $(if ($capacityEstimate.MaximumRelativePathLength -gt 0) { 1 + $capacityEstimate.MaximumRelativePathLength } else { 0 })
if ($maximumSnapshotPathLength -ge 32767) {
    throw "The staging location would create a path with $maximumSnapshotPathLength characters, exceeding the supported Windows extended-path limit. Choose a shorter OutputRoot."
}
if ($maximumSnapshotPathLength -ge 260 -and -not $longPathsEnabled) {
    throw "The staging location would create a path with $maximumSnapshotPathLength characters, but Windows LongPathsEnabled is not active. Enable long-path support or choose a shorter OutputRoot."
}
Write-Host "Capacity and path checks passed: $($capacityEstimate.EntryCount) entries; longest source path $($capacityEstimate.MaximumPathLength) characters; longest projected staging path $maximumSnapshotPathLength characters; LongPathsEnabled=$longPathsEnabled; estimated minimum $([math]::Ceiling([double]($capacityEstimate.RequiredBytes / 1GB) * 100) / 100) GiB free."
$existingOutputAncestor = $resolvedOutputBase
while (-not (Test-Path -LiteralPath $existingOutputAncestor)) {
    $parent = Split-Path -Parent $existingOutputAncestor
    if (-not $parent -or $parent -eq $existingOutputAncestor) { break }
    $existingOutputAncestor = $parent
}
$cursor = Get-Item -LiteralPath $existingOutputAncestor -Force
while ($cursor) {
    if (($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "OutputRoot may not pass through a reparse point: $($cursor.FullName)"
    }
    $cursor = $cursor.Parent
}
New-Item -ItemType Directory -Force -Path $resolvedOutputBase | Out-Null
$outputBase = (Resolve-Path -LiteralPath $resolvedOutputBase).Path
$runRoot = Join-Path $outputBase "full-backup-$stamp"
$snapshotRoot = Join-Path $runRoot 'snapshot'
$snapshot = Join-Path $snapshotRoot (Split-Path -Leaf $source)
$prepared = Join-Path $runRoot 'prepared-release-assets'
New-Item -ItemType Directory -Path $snapshot | Out-Null

$resolvedRunRoot = [IO.Path]::GetFullPath($runRoot)
$resolvedSnapshot = [IO.Path]::GetFullPath($snapshot)
$requiredPrefix = $resolvedRunRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
if (-not $resolvedSnapshot.StartsWith($requiredPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Snapshot target resolved outside the backup run directory.'
}

Write-Host 'Creating converged point-in-time snapshot...'
[void](Invoke-RobocopyPass -Source $source -Target $snapshot -ExcludedDirectories $excludedSourceDirectories)
$stablePasses = 0
for ($pass = 1; $pass -le $MaxConvergencePasses; $pass++) {
    $code = Invoke-RobocopyPass -Source $source -Target $snapshot -Mirror -ExcludedDirectories $excludedSourceDirectories
    if ($code -eq 0) { $stablePasses++ } else { $stablePasses = 0 }
    Write-Host "Convergence pass ${pass}: robocopy exit $code"
    if ($stablePasses -ge 2) { break }
    Start-Sleep -Seconds 2
}
if ($stablePasses -lt 2) {
    throw "The source did not converge after $MaxConvergencePasses passes. Keep the partial snapshot and retry when writes slow down."
}
Assert-SafeSourceTree -Root $source -ExcludedDirectories $excludedSourceDirectories
Assert-SafeSourceTree -Root $snapshot -ExcludedDirectories @()

Write-Host 'Comparing source and snapshot inventory and SHA-256 values...'
$mirrorArguments = @($verifyMirrorScript, $source, $snapshot)
foreach ($relative in $normalizedExclusions) {
    $mirrorArguments += @('--exclude-relative', $relative)
}
& $python @mirrorArguments
if ($LASTEXITCODE -ne 0) {
    throw "Source/snapshot content verification failed with exit code $LASTEXITCODE"
}

Write-Host 'Creating split archive and per-file manifest...'
$createArguments = @($createScript, $snapshot, $prepared, '--part-size-mib', $PartSizeMiB, '--source-device', $device, '--source-computer', $sourceComputer)
foreach ($relative in $normalizedExclusions) {
    $createArguments += @('--exclude-relative', $relative)
}
& $python @createArguments
if ($LASTEXITCODE -ne 0) {
    throw "Archive creation failed with exit code $LASTEXITCODE"
}

Copy-Item -LiteralPath $verifyScript -Destination (Join-Path $prepared 'verify_split_tar.py')
Copy-Item -LiteralPath $verifyRestoreScript -Destination (Join-Path $prepared 'verify_restored_tree.py')
Copy-Item -LiteralPath $restoreScript -Destination (Join-Path $prepared 'restore.ps1')

Write-Host 'Performing full second-pass verification...'
& $python (Join-Path $prepared 'verify_split_tar.py') $prepared
if ($LASTEXITCODE -ne 0) {
    throw "Backup verification failed with exit code $LASTEXITCODE"
}

Write-Host 'Confirming the live source still matches the frozen snapshot...'
& $python @mirrorArguments
if ($LASTEXITCODE -ne 0) {
    throw "Source changed after snapshot convergence; the prepared archive remains local but is not current or uploadable. Verification exit code $LASTEXITCODE"
}

$manifest = Get-Content -LiteralPath (Join-Path $prepared 'BACKUP_MANIFEST.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if (([int]$manifest.parts.Count + 7) -gt 1000) {
    throw "The prepared backup needs $([int]$manifest.parts.Count + 7) GitHub Release assets after upload adds its Release binding, exceeding the supported 1000-asset limit. Prepare again with larger parts."
}
$readme = @"
# Complete project backup

This private backup contains the permitted project scope as a lossless split TAR snapshot. Ignore rules were not applied.

- Source folder: $($manifest.source_name)
- Entries: $($manifest.entry_count)
- Archive bytes: $($manifest.archive_total_bytes)
- Parts: $($manifest.parts.Count)
- Source changes during archive: $($manifest.source_state_changed_during_backup)
- Deep verification: passed
- Explicitly excluded source paths: $($manifest.excluded_source_paths.Count)

Restore on Windows by downloading every release asset into one directory, then run the locally installed trusted helper:

    & "`$env:USERPROFILE\.agents\skills\cross-device-project-sync\scripts\restore-full-backup.ps1" -BackupPath (Get-Location).Path -Destination '<new-empty-directory-on-selected-drive>' -ReleaseTag '<known-release-tag>' -ExpectedRepository '<confirmed-owner/repository>' -ExpectedManifestSha256 '<confirmed-manifest-sha256>'

Keep this repository private. Browser authentication profiles must be explicitly excluded; other logs, media, caches, hidden files, and local state may still be sensitive.

At upload time, the trusted upload helper replaces this preliminary README and RESTORE.txt with instructions containing the actual repository, Release tag, and manifest binding.
"@
Set-Content -LiteralPath (Join-Path $prepared 'README.md') -Value $readme -Encoding utf8

$result = [ordered]@{
    prepared_backup_path = $prepared
    snapshot_path = $snapshot
    entry_count = [int64]$manifest.entry_count
    archive_bytes = [int64]$manifest.archive_total_bytes
    parts = [int]$manifest.parts.Count
    verified = $true
    uploaded = $false
    device = $device
    source_computer = $sourceComputer
    operation_time = (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')
}
$result | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runRoot 'PREPARED_BACKUP.json') -Encoding utf8
$result | ConvertTo-Json
Write-Host "PREPARED_BACKUP_PATH=$prepared"
