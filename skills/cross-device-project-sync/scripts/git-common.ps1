# Shared by the entry scripts. Compatible with Windows PowerShell 5.1 and 7.
function Protect-SyncText {
    param([AllowEmptyString()][string]$Text)
    $safe = [regex]::Replace($Text, '(?i)\b[a-z][a-z0-9+.-]*://[^\s<>"'']+', {
        param($match)
        $url = [regex]::Replace($match.Value, '^([a-zA-Z][a-zA-Z0-9+.-]*://)[^/?#]*@', '$1[REDACTED]@')
        $url = [regex]::Replace($url, '\?[^#]*', '?[REDACTED]')
        return [regex]::Replace($url, '#.*', '#[REDACTED]')
    })
    return [regex]::Replace($safe, '(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{50,}|\bsk(-proj)?-[A-Za-z0-9_-]{20,}', '[REDACTED]')
}

function ConvertTo-SyncNativeArgument {
    param([AllowEmptyString()][string]$Value)
    # Windows argv quoting: double backslashes before a quote or closing quote.
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

$script:SyncGitExecutable = $null
$script:SyncGitTimeoutMilliseconds = 600000

function Stop-SyncProcessTree {
    param([Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process)

    if ($Process.HasExited) { return $true }
    $treeKill = [System.Diagnostics.Process].GetMethods() |
        Where-Object { $_.Name -eq 'Kill' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType -eq [bool] } |
        Select-Object -First 1
    if ($treeKill) {
        try {
            [void]$treeKill.Invoke($Process, [object[]]@($true))
            return $Process.WaitForExit(5000)
        } catch {
            return $false
        }
    }

    $systemDirectory = [Environment]::SystemDirectory
    if ([string]::IsNullOrWhiteSpace($systemDirectory)) { return $false }
    $taskkill = Join-Path $systemDirectory 'taskkill.exe'
    if (-not (Test-Path -LiteralPath $taskkill -PathType Leaf)) { return $false }
    & $taskkill /PID $Process.Id /T /F 2>$null | Out-Null
    $taskkillCode = $LASTEXITCODE
    $parentStopped = $Process.WaitForExit(5000)
    return ($taskkillCode -eq 0 -and $parentStopped)
}

function Set-SyncGitExecutable {
    param([AllowEmptyString()][string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        $command = @(Get-Command git -CommandType Application -ErrorAction Stop)[0]
        $script:SyncGitExecutable = $command.Source
        return
    }
    $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw 'GitExe must point to an executable file.'
    }
    $script:SyncGitExecutable = $resolved
}

function Set-SyncGitTimeout {
    param(
        [ValidateRange(1000, 86400000)]
        [int]$Milliseconds = 600000
    )
    $script:SyncGitTimeoutMilliseconds = $Milliseconds
}

function Get-SyncDeviceLabel {
    param([AllowEmptyString()][string]$Requested)
    $label = $Requested
    $configPath = $null
    if ([string]::IsNullOrWhiteSpace($label)) {
        $codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
        $configPath = Join-Path $codexHome 'cross-device-project-sync-device.json'
        if (Test-Path -LiteralPath $configPath -PathType Leaf) {
            $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $label = [string]$config.device_label
        } else {
            $label = '(unconfigured device)'
        }
    }
    if ([string]::IsNullOrWhiteSpace($label) -or $label.Length -gt 80 -or $label -match '[\x00-\x1f\x7f]') {
        throw "Device label is invalid$(if ($configPath) { ": $configPath" })."
    }
    return $label.Trim()
}

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)][string]$Repo,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$AllowFailure
    )
    $start = New-Object System.Diagnostics.ProcessStartInfo
    if (-not $script:SyncGitExecutable) { Set-SyncGitExecutable }
    $start.FileName = $script:SyncGitExecutable
    $nativeArgs = @('-C', $Repo) + $Arguments
    $start.Arguments = (($nativeArgs | ForEach-Object { ConvertTo-SyncNativeArgument $_ }) -join ' ')
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = New-Object System.Text.UTF8Encoding($false, $true)
    $start.StandardErrorEncoding = New-Object System.Text.UTF8Encoding($false, $true)
    $start.EnvironmentVariables['GIT_TERMINAL_PROMPT'] = '0'
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    try {
        if (-not $process.Start()) { throw 'Git could not be started.' }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($script:SyncGitTimeoutMilliseconds)) {
            $seconds = [math]::Round($script:SyncGitTimeoutMilliseconds / 1000, 1)
            if (-not (Stop-SyncProcessTree -Process $process)) {
                throw "Git timed out after $seconds seconds, and termination of its process tree could not be confirmed. Do not run another Git operation until the processes and repository state are inspected."
            }
            throw "Git timed out after $seconds seconds; its process tree was terminated. Inspect repository state before retrying or increase -GitTimeoutMilliseconds."
        }
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        $code = $process.ExitCode
    } finally {
        $process.Dispose()
    }
    if ($code -ne 0 -and -not $AllowFailure) {
        throw ('Git failed (exit ' + $code + '): ' + (Protect-SyncText ($stderr + $stdout)))
    }
    # Text is internal raw UTF-8 data, including NUL delimiters. Only emit redacted diagnostics.
    $lines = @()
    if ($stdout.Length -gt 0) { $lines = @($stdout.TrimEnd([char[]]"`r`n") -split '\r?\n') }
    [pscustomobject]@{ Code = $code; Output = $lines; Text = $stdout; Diagnostic = (Protect-SyncText $stderr) }
}

function Invoke-GitBytes {
    param(
        [Parameter(Mandatory = $true)][string]$Repo,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$AllowFailure
    )
    $start = New-Object System.Diagnostics.ProcessStartInfo
    if (-not $script:SyncGitExecutable) { Set-SyncGitExecutable }
    $start.FileName = $script:SyncGitExecutable
    $nativeArgs = @('-C', $Repo) + $Arguments
    $start.Arguments = (($nativeArgs | ForEach-Object { ConvertTo-SyncNativeArgument $_ }) -join ' ')
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardErrorEncoding = New-Object System.Text.UTF8Encoding($false, $true)
    $start.EnvironmentVariables['GIT_TERMINAL_PROMPT'] = '0'
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    $memory = New-Object System.IO.MemoryStream
    try {
        if (-not $process.Start()) { throw 'Git could not be started.' }
        $stdoutTask = $process.StandardOutput.BaseStream.CopyToAsync($memory)
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($script:SyncGitTimeoutMilliseconds)) {
            if (-not (Stop-SyncProcessTree -Process $process)) {
                throw 'Git timed out while reading an index blob, and termination of its process tree could not be confirmed.'
            }
            throw 'Git timed out while reading an index blob; its process tree was terminated.'
        }
        [void]$stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        $code = $process.ExitCode
        $bytes = $memory.ToArray()
    } finally {
        $memory.Dispose()
        $process.Dispose()
    }
    if ($code -ne 0 -and -not $AllowFailure) {
        throw ('Git failed (exit ' + $code + '): ' + (Protect-SyncText $stderr))
    }
    [pscustomobject]@{ Code = $code; Bytes = $bytes; Diagnostic = (Protect-SyncText $stderr) }
}

function Get-SyncGitPaths {
    param([string]$Repo, [string[]]$Arguments)
    $result = Invoke-Git -Repo $Repo -Arguments $Arguments
    if ($result.Text.Length -gt 0 -and -not $result.Text.EndsWith([string][char]0)) {
        throw 'Expected NUL-terminated Git path output; refusing to guess filenames.'
    }
    $result.Text.Split(@([char]0), [StringSplitOptions]::RemoveEmptyEntries)
}
