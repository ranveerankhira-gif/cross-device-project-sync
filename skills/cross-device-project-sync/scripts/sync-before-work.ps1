[CmdletBinding()]
param(
    [string]$Path = (Get-Location).Path,
    [string]$GitExe,
    [ValidateRange(1000, 86400000)]
    [int]$GitTimeoutMilliseconds = 600000,
    [string]$DeviceLabel,
    [switch]$NoPull
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot 'git-common.ps1')
Set-SyncGitExecutable -Path $GitExe
Set-SyncGitTimeout -Milliseconds $GitTimeoutMilliseconds
$device = Get-SyncDeviceLabel -Requested $DeviceLabel

function First-Line {
    param([string[]]$Lines)
    if ($Lines.Count -eq 0) { return "" }
    return [string]$Lines[0]
}

function Open-SyncOperationLock {
    param([Parameter(Mandatory = $true)][string]$Repo)

    $lockPathResult = Invoke-Git -Repo $Repo -Arguments @("rev-parse", "--git-path", "cross-device-project-sync.lock") -AllowFailure
    if ($lockPathResult.Code -ne 0) {
        throw 'Unable to resolve the synchronization lock path.'
    }
    $rawLockPath = (First-Line $lockPathResult.Output).Trim()
    if ([string]::IsNullOrWhiteSpace($rawLockPath)) {
        throw 'Git returned an empty synchronization lock path.'
    }
    $lockPath = if ([IO.Path]::IsPathRooted($rawLockPath)) {
        [IO.Path]::GetFullPath($rawLockPath)
    } else {
        [IO.Path]::GetFullPath((Join-Path $Repo $rawLockPath))
    }
    return [IO.FileStream]::new(
        $lockPath,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::None,
        1,
        [IO.FileOptions]::DeleteOnClose
    )
}

$resolved = Resolve-Path -LiteralPath $Path
$rootResult = Invoke-Git -Repo $resolved.Path -Arguments @("rev-parse", "--show-toplevel") -AllowFailure
if ($rootResult.Code -ne 0) {
    "Error: not a Git repository: $($resolved.Path)"
    exit 2
}

$repo = (First-Line $rootResult.Output).Trim()
$branch = (First-Line (Invoke-Git -Repo $repo -Arguments @("branch", "--show-current")).Output).Trim()
if ([string]::IsNullOrWhiteSpace($branch)) {
    "Error: detached HEAD. Refusing to pull automatically."
    exit 22
}

$upstreamResult = Invoke-Git -Repo $repo -Arguments @("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") -AllowFailure
if ($upstreamResult.Code -ne 0) {
    "Error: no upstream branch configured for '$branch'."
    exit 23
}
$upstream = (First-Line $upstreamResult.Output).Trim()

"Fetching remote changes..."
Invoke-Git -Repo $repo -Arguments @("fetch", "--prune") | Out-Null

$statusLines = (Invoke-Git -Repo $repo -Arguments @("status", "--porcelain=v1", "-uall")).Output
$conflictLines = (Invoke-Git -Repo $repo -Arguments @("diff", "--name-only", "--diff-filter=U")).Output
$operationTime = (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')
$startingCommit = (First-Line (Invoke-Git -Repo $repo -Arguments @("rev-parse", "HEAD")).Output).Trim()

"Device: $device"
"Operation time: $operationTime"
"Starting version: $branch@$startingCommit"

if ($conflictLines.Count -gt 0) {
    "Error: repository has unresolved conflicts. Refusing to pull."
    exit 24
}

if ($statusLines.Count -gt 0) {
    "Cross-device sync status"
    "Branch: $branch"
    "Upstream: $upstream"
    "Working tree: $($statusLines.Count) changed path(s)"
    "Action: no pull; review or commit local work first."
    exit 20
}

$countsResult = Invoke-Git -Repo $repo -Arguments @("rev-list", "--left-right", "--count", "HEAD...@{u}") -AllowFailure
if ($countsResult.Code -ne 0) {
    "Error: could not compare HEAD with upstream '$upstream'."
    exit 25
}

$parts = ((First-Line $countsResult.Output).Trim() -split "\s+")
$ahead = [int]$parts[0]
$behind = [int]$parts[1]

"Cross-device sync status"
"Branch: $branch"
"Upstream: $upstream"
"Working tree: clean"

if ($ahead -eq 0 -and $behind -eq 0) {
    "Local vs remote: up to date"
    "Action: safe to continue working."
    exit 0
}

if ($ahead -eq 0 -and $behind -gt 0) {
    # Pin the fetched commit. A second fetch during pull could change the checked target.
    $target = (First-Line (Invoke-Git -Repo $repo -Arguments @("rev-parse", "--verify", "@{u}^{commit}")).Output).Trim()
    "Local vs remote: remote ahead by $behind commit(s)"
    if ($NoPull) {
        "Action: upstream can fast-forward; no update attempted because -NoPull was set. Local file collision checks remain pending."
        exit 0
    }

    "Action: applying fetched commit with ignored-file overwrite protection."
    try {
        $operationLock = Open-SyncOperationLock -Repo $repo
    }
    catch {
        "Error: another cross-device update is active or the synchronization lock could not be created. No merge was attempted."
        exit 27
    }
    try {
        $lockedBranch = (First-Line (Invoke-Git -Repo $repo -Arguments @("branch", "--show-current")).Output).Trim()
        $lockedCommit = (First-Line (Invoke-Git -Repo $repo -Arguments @("rev-parse", "HEAD")).Output).Trim()
        if ($lockedBranch -cne $branch -or $lockedCommit -cne $startingCommit) {
            "Error: the checked-out branch or commit changed before the update lock was acquired. No merge was attempted."
            exit 27
        }

        $lockedStatus = (Invoke-Git -Repo $repo -Arguments @("status", "--porcelain=v1", "-uall")).Output
        $lockedConflicts = (Invoke-Git -Repo $repo -Arguments @("diff", "--name-only", "--diff-filter=U")).Output
        if ($lockedConflicts.Count -gt 0 -or $lockedStatus.Count -gt 0) {
            "Error: the working tree changed before the update lock was acquired. No merge was attempted."
            exit 27
        }

        $merge = Invoke-Git -Repo $repo -Arguments @("merge", "--ff-only", "--no-overwrite-ignore", "--no-autostash", $target) -AllowFailure
        if ($merge.Code -ne 0) {
            "Error: fast-forward refused. Preserve local files and inspect conflicts before retrying."
            Protect-SyncText ($merge.Diagnostic + $merge.Text)
            exit 26
        }

        $resultBranch = (First-Line (Invoke-Git -Repo $repo -Arguments @("branch", "--show-current")).Output).Trim()
        $resultCommit = (First-Line (Invoke-Git -Repo $repo -Arguments @("rev-parse", "HEAD")).Output).Trim()
        if ($resultBranch -cne $branch -or $resultCommit -cne $target) {
            "Error: Git did not finish on the checked branch and pinned commit. Inspect the repository before continuing."
            exit 27
        }
    }
    finally {
        $operationLock.Dispose()
    }
    "Result: fast-forward applied with ignored-file overwrite protection."
    "Result version: $branch@$resultCommit"
    exit 0
}

if ($ahead -gt 0 -and $behind -eq 0) {
    "Local vs remote: local ahead by $ahead commit(s)"
    "Action: no pull needed; remember to push before switching devices."
    exit 0
}

"Local vs remote: diverged; local ahead $ahead, remote ahead $behind"
"Action: no pull. Inspect history and resolve intentionally."
exit 21
