[CmdletBinding()]
param(
    [string]$Path = (Get-Location).Path,
    [string]$GitExe,
    [ValidateRange(1000, 86400000)]
    [int]$GitTimeoutMilliseconds = 600000,
    [ValidateRange(1, 2147483647)]
    [int]$MaxFileBytes = 1048576,
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot 'git-common.ps1')
Set-SyncGitExecutable -Path $GitExe
Set-SyncGitTimeout -Milliseconds $GitTimeoutMilliseconds

$protectedProfilePattern = '(?i)^(?:chrome|chromium|edge|msedge|browser|playwright).*test[-_ ]?profile(?:[-_ ].*)?$'

function Test-ProtectedBrowserProfilePath {
    param([string]$RelativePath)
    foreach ($segment in ($RelativePath -split '[\\/]')) {
        if ($segment -match $protectedProfilePattern) { return $true }
    }
    return $false
}

function Get-SafeTreeFiles {
    param([string]$Root)
    $skipDirs = @('.git', 'node_modules', '.venv', 'venv', '__pycache__', '.next', 'dist', 'build', 'target', 'coverage')
    $rootItem = Get-Item -LiteralPath $Root -Force
    if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Secret scan root may not be a reparse point: $Root"
    }
    if ($rootItem.Name -match $protectedProfilePattern) {
        throw "Refusing to inspect a protected browser test profile: $Root"
    }
    $pending = [Collections.Generic.Stack[string]]::new()
    $pending.Push($Root)
    while ($pending.Count -gt 0) {
        $current = $pending.Pop()
        foreach ($item in Get-ChildItem -LiteralPath $current -Force -ErrorAction Stop) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Refusing to follow a filesystem reparse point during secret scan: $($item.FullName)"
            }
            if ($item.PSIsContainer) {
                if ($item.Name -match $protectedProfilePattern) {
                    throw "Refusing to inspect a protected browser test profile: $($item.FullName)"
                }
                if ($item.Name -notin $skipDirs) { $pending.Push($item.FullName) }
            }
            else {
                $item
            }
        }
    }
}

function Assert-SafeCandidateFile {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path
    )
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $pathFull = [IO.Path]::GetFullPath($Path)
    $rootPrefix = $rootFull + [IO.Path]::DirectorySeparatorChar
    if (-not $pathFull.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Candidate file resolved outside the repository: $Path"
    }
    $cursorPath = $pathFull
    while ($cursorPath -and -not $cursorPath.Equals($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
        $cursorItem = Get-Item -LiteralPath $cursorPath -Force
        if (($cursorItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Refusing to follow a filesystem reparse point during secret scan: $($cursorItem.FullName)"
        }
        $cursorPath = Split-Path -Parent $cursorPath
    }
}

function Get-CandidateFiles {
    param([string]$Root)

    $gitRootResult = Invoke-Git -Repo $Root -Arguments @("rev-parse", "--show-toplevel") -AllowFailure
    if ($gitRootResult.Code -eq 0) {
        $repo = ([string]$gitRootResult.Output[0]).Trim()
        $unmerged = @(Get-SyncGitPaths -Repo $repo -Arguments @("ls-files", "-z", "--unmerged"))
        if ($unmerged.Count -gt 0) {
            throw 'Repository has unmerged index entries. Resolve conflicts before relying on a pre-commit secret scan.'
        }
        $staged = @(Get-SyncGitPaths -Repo $repo -Arguments @("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"))
        $worktree = @(Get-SyncGitPaths -Repo $repo -Arguments @("ls-files", "-z", "--modified", "--others", "--exclude-standard"))
        foreach ($file in $staged) {
            if ($file.Length -eq 0) { continue }
            if (Test-ProtectedBrowserProfilePath -RelativePath ([string]$file)) {
                throw "Refusing to inspect a protected browser test profile path: $file"
            }
            [pscustomobject]@{
                Root = $repo
                RelativePath = [string]$file
                FullPath = Join-Path $repo ([string]$file)
                Source = 'index'
            }
        }
        foreach ($file in $worktree) {
            if ($file.Length -eq 0) { continue }
            if (Test-ProtectedBrowserProfilePath -RelativePath ([string]$file)) {
                throw "Refusing to inspect a protected browser test profile path: $file"
            }
            [pscustomobject]@{
                Root = $repo
                RelativePath = [string]$file
                FullPath = Join-Path $repo ([string]$file)
                Source = 'worktree'
            }
        }
        return
    }

    Get-SafeTreeFiles -Root $Root | ForEach-Object {
        [pscustomobject]@{
            Root = $Root
            RelativePath = $_.FullName.Substring($Root.Length).TrimStart("\", "/")
            FullPath = $_.FullName
            Source = 'worktree'
        }
    }
}

$resolved = Resolve-Path -LiteralPath $Path
if (-not (Test-Path -LiteralPath $resolved.Path -PathType Container)) {
    throw 'Path must be a directory.'
}

function ConvertFrom-ScannableBytes {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)
    if ($Bytes.Length -eq 0) { return '' }
    try {
        if ($Bytes.Length -ge 2 -and $Bytes[0] -eq 0xff -and $Bytes[1] -eq 0xfe) {
            return [Text.Encoding]::Unicode.GetString($Bytes, 2, $Bytes.Length - 2)
        }
        if ($Bytes.Length -ge 2 -and $Bytes[0] -eq 0xfe -and $Bytes[1] -eq 0xff) {
            return [Text.Encoding]::BigEndianUnicode.GetString($Bytes, 2, $Bytes.Length - 2)
        }
        if ($Bytes -contains 0) { return $null }
        $strictUtf8 = New-Object Text.UTF8Encoding($false, $true)
        return $strictUtf8.GetString($Bytes)
    } catch [Text.DecoderFallbackException] {
        return $null
    }
}
$scanRootCursor = Get-Item -LiteralPath $resolved.Path -Force
while ($scanRootCursor) {
    if ($scanRootCursor.Name -match $protectedProfilePattern) {
        throw "Scan root is inside a protected browser test profile: $($scanRootCursor.FullName)"
    }
    if (($scanRootCursor.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Scan root may not pass through a reparse point: $($scanRootCursor.FullName)"
    }
    $scanRootCursor = $scanRootCursor.Parent
}
$namePattern = '(?i)(^|[\\/])(\.env(\..*)?|.*credential.*|.*secret.*|.*token.*|cookies?.*|auth.*|.*service-account.*\.json|credentials\.json|id_rsa.*|id_ed25519.*|.*\.pem|.*\.key)$'
$contentPatterns = [ordered]@{
    "private key block" = '-----BEGIN ([A-Z ]+ )?PRIVATE KEY-----'
    "github token" = '(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{50,}'
    "openai-style api key" = '\bsk(-proj)?-[A-Za-z0-9_-]{20,}'
    "aws access key" = '\b(AKIA|ASIA)[A-Z0-9]{16}\b'
    "slack token" = '\bxox[baprs]-[A-Za-z0-9-]{20,}'
    "google api key" = '\bAIza[0-9A-Za-z_-]{35}\b'
}

$findings = New-Object System.Collections.Generic.List[object]

foreach ($candidate in Get-CandidateFiles -Root $resolved.Path) {
    if ($candidate.Source -eq 'worktree') {
        if (-not (Test-Path -LiteralPath $candidate.FullPath -PathType Leaf)) { continue }
        Assert-SafeCandidateFile -Root $candidate.Root -Path $candidate.FullPath
    }

    if ($candidate.RelativePath -match $namePattern) {
        $findings.Add([pscustomobject]@{
            Severity = "review"
            Type = "risky file name"
            Path = $candidate.RelativePath
            Detail = "Name suggests secret, credential, token, cookie, or key material in the $($candidate.Source)."
        })
    }

    if ($candidate.Source -eq 'index') {
        $sizeResult = Invoke-Git -Repo $candidate.Root -Arguments @('cat-file', '-s', ":$($candidate.RelativePath)") -AllowFailure
        if ($sizeResult.Code -ne 0) { throw "Unable to inspect staged Git blob: $($candidate.RelativePath)" }
        [int64]$length = ([string]$sizeResult.Output[0]).Trim()
        if ($length -gt $MaxFileBytes) { continue }
        $blob = Invoke-GitBytes -Repo $candidate.Root -Arguments @('show', '--no-textconv', ":$($candidate.RelativePath)")
        $bytes = [byte[]]$blob.Bytes
        if ($bytes.Length -ne $length) { throw "Staged Git blob size changed while scanning: $($candidate.RelativePath)" }
    } else {
        $item = Get-Item -LiteralPath $candidate.FullPath
        if ($item.Length -gt $MaxFileBytes) { continue }
        $bytes = [System.IO.File]::ReadAllBytes($candidate.FullPath)
    }

    $text = ConvertFrom-ScannableBytes -Bytes $bytes
    if ($null -eq $text) { continue }
    foreach ($label in $contentPatterns.Keys) {
        if ($text -match $contentPatterns[$label]) {
            $findings.Add([pscustomobject]@{
                Severity = "block"
                Type = "possible secret content"
                Path = $candidate.RelativePath
                Detail = "$label ($($candidate.Source))"
            })
        }
    }
}

if ($Json) {
    $jsonItems = @($findings | ForEach-Object { $_ })
    if ($jsonItems.Count -eq 0) { '[]' }
    else { ConvertTo-Json -InputObject $jsonItems -Depth 4 }
} else {
    if ($findings.Count -eq 0) {
        "Secret scan: no risky names or high-confidence secret patterns found."
    } else {
        "Secret scan findings"
        $findings | Sort-Object Severity, Path, Type | Format-Table Severity, Type, Path, Detail -AutoSize
    }
}

if (@($findings | Where-Object { $_.Severity -eq "block" }).Count -gt 0) { exit 2 }
if ($findings.Count -gt 0) { exit 1 }
exit 0
