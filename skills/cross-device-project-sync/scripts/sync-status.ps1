[CmdletBinding()]
param(
    [string]$Path = (Get-Location).Path,
    [string]$GitExe,
    [ValidateRange(1000, 86400000)]
    [int]$GitTimeoutMilliseconds = 600000,
    [string]$DeviceLabel,
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot 'git-common.ps1')
Set-SyncGitExecutable -Path $GitExe
Set-SyncGitTimeout -Milliseconds $GitTimeoutMilliseconds
$device = Get-SyncDeviceLabel -Requested $DeviceLabel

function Get-FirstLine {
    param([string[]]$Lines)
    if ($Lines.Count -eq 0) { return "" }
    return [string]$Lines[0]
}

$resolved = Resolve-Path -LiteralPath $Path
$rootResult = Invoke-Git -Repo $resolved.Path -Arguments @("rev-parse", "--show-toplevel") -AllowFailure
if ($rootResult.Code -ne 0) {
    $result = [pscustomobject]@{
        IsGitRepository = $false
        Path = $resolved.Path
        Message = "Not a Git repository."
    }
    if ($Json) { $result | ConvertTo-Json -Depth 4 } else { $result.Message }
    exit 2
}

$repo = (Get-FirstLine $rootResult.Output).Trim()
$branch = (Get-FirstLine (Invoke-Git -Repo $repo -Arguments @("branch", "--show-current")).Output).Trim()
if ([string]::IsNullOrWhiteSpace($branch)) {
    $branch = "(detached HEAD)"
}

$upstreamResult = Invoke-Git -Repo $repo -Arguments @("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") -AllowFailure
$upstream = if ($upstreamResult.Code -eq 0) { (Get-FirstLine $upstreamResult.Output).Trim() } else { "" }

$originResult = Invoke-Git -Repo $repo -Arguments @("remote", "get-url", "origin") -AllowFailure
$origin = if ($originResult.Code -eq 0) { Protect-SyncText ((Get-FirstLine $originResult.Output).Trim()) } else { "" }

$statusLines = (Invoke-Git -Repo $repo -Arguments @("status", "--porcelain=v1", "-uall")).Output
$conflictLines = (Invoke-Git -Repo $repo -Arguments @("diff", "--name-only", "--diff-filter=U")).Output
$lastCommit = Protect-SyncText ((Get-FirstLine (Invoke-Git -Repo $repo -Arguments @("log", "-1", "--pretty=%h %s") -AllowFailure).Output).Trim())
$fullCommitResult = Invoke-Git -Repo $repo -Arguments @("rev-parse", "--verify", "HEAD^{commit}") -AllowFailure
$fullCommit = if ($fullCommitResult.Code -eq 0) { (Get-FirstLine $fullCommitResult.Output).Trim() } else { '' }
$checkedAt = (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')

$ahead = $null
$behind = $null
if (-not [string]::IsNullOrWhiteSpace($upstream)) {
    $countsResult = Invoke-Git -Repo $repo -Arguments @("rev-list", "--left-right", "--count", "HEAD...@{u}") -AllowFailure
    if ($countsResult.Code -eq 0) {
        $parts = ((Get-FirstLine $countsResult.Output).Trim() -split "\s+")
        if ($parts.Count -ge 2) {
            $ahead = [int]$parts[0]
            $behind = [int]$parts[1]
        }
    }
}

$workingTree = if ($statusLines.Count -eq 0) { "clean" } else { "$($statusLines.Count) changed path(s)" }
$remoteState = "no upstream configured"
if ($null -ne $ahead -and $null -ne $behind) {
    if ($ahead -eq 0 -and $behind -eq 0) {
        $remoteState = "up to date"
    } elseif ($ahead -eq 0 -and $behind -gt 0) {
        $remoteState = "remote ahead by $behind commit(s)"
    } elseif ($ahead -gt 0 -and $behind -eq 0) {
        $remoteState = "local ahead by $ahead commit(s)"
    } else {
        $remoteState = "diverged: local ahead $ahead, remote ahead $behind"
    }
}

$result = [pscustomobject]@{
    IsGitRepository = $true
    Device = $device
    CheckedAt = $checkedAt
    Repository = $repo
    Branch = $branch
    Upstream = $upstream
    Origin = $origin
    WorkingTree = $workingTree
    ChangedPathCount = $statusLines.Count
    ConflictPathCount = $conflictLines.Count
    Ahead = $ahead
    Behind = $behind
    RemoteState = $remoteState
    RemoteStateBasis = 'cached remote-tracking ref; no fetch performed'
    LastCommit = $lastCommit
    Commit = $fullCommit
}

if ($Json) {
    $result | ConvertTo-Json -Depth 4
} else {
    "Cross-device sync status"
    "Device: $($result.Device)"
    "Checked at: $($result.CheckedAt)"
    "Repository: $($result.Repository)"
    "Branch: $($result.Branch)"
    "Upstream: $(if ($result.Upstream) { $result.Upstream } else { '(none)' })"
    "Origin: $(if ($result.Origin) { $result.Origin } else { '(none)' })"
    "Working tree: $($result.WorkingTree)"
    "Cached local vs upstream ref: $($result.RemoteState)"
    "Remote-state basis: $($result.RemoteStateBasis)"
    "Conflicts: $($result.ConflictPathCount)"
    "Last commit: $(if ($result.LastCommit) { $result.LastCommit } else { '(none)' })"
    "Full commit: $(if ($result.Commit) { $result.Commit } else { '(none)' })"
}

if ($result.ConflictPathCount -gt 0) { exit 3 }
exit 0
