function Get-DraftAssetRecoveryAction {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][bool]$ReleaseIsDraft,
        [Parameter(Mandatory = $true)][bool]$ReleaseIdentityVerified,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$RemoteAssets,
        [Parameter(Mandatory = $true)][Int64]$ExpectedSize,
        [Parameter(Mandatory = $true)][string]$ExpectedDigest,
        [Parameter(Mandatory = $true)][string]$AssetName
    )

    if (-not $ReleaseIsDraft -or -not $ReleaseIdentityVerified) {
        throw "Draft asset recovery is not authorized for an unverified Release: $AssetName"
    }
    if ($RemoteAssets.Count -eq 0) { return 'upload' }
    if ($RemoteAssets.Count -ne 1) { throw "Duplicate remote asset: $AssetName" }

    $remote = $RemoteAssets[0]
    if ([string]$remote.state -ceq 'uploaded') {
        if ([Int64]$remote.size -eq $ExpectedSize -and [string]$remote.digest -ceq $ExpectedDigest) {
            return 'reuse'
        }
        throw "Uploaded draft asset differs from the verified local asset: $AssetName"
    }

    # GitHub may retain an asset in a non-uploaded state after an interrupted
    # transfer. Only that state is eligible for automatic deletion and retry.
    return 'delete-incomplete'
}

function Assert-ExactReleaseMetadataLines {
    [CmdletBinding()]
    param(
        [AllowEmptyString()][string]$Body,
        [Parameter(Mandatory = $true)][hashtable]$Expected,
        [string]$Context = 'Release'
    )

    $lines = @([regex]::Split($Body, "\r?\n"))
    foreach ($key in $Expected.Keys) {
        $prefix = "${key}:"
        $matches = @($lines | Where-Object {
            $_.StartsWith($prefix, [StringComparison]::Ordinal)
        })
        if ($matches.Count -ne 1) {
            throw "$Context must contain exactly one '$prefix' metadata line."
        }
        $actual = $matches[0].Substring($prefix.Length).Trim()
        if (-not $actual.Equals([string]$Expected[$key], [StringComparison]::Ordinal)) {
            throw "$Context metadata mismatch for '$key'."
        }
    }
    return $true
}

function Assert-ExactDelimitedMetadataField {
    [CmdletBinding()]
    param(
        [AllowEmptyString()][string]$Body,
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][string]$ExpectedValue,
        [string]$Context = 'Release'
    )

    $prefix = "${Key}:"
    $segments = @(
        [regex]::Split($Body, '(?:\r?\n|;)') |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_.StartsWith($prefix, [StringComparison]::Ordinal) }
    )
    if ($segments.Count -ne 1) {
        throw "$Context must contain exactly one '$prefix' metadata field."
    }
    $actual = $segments[0].Substring($prefix.Length).Trim()
    if (-not $actual.Equals($ExpectedValue, [StringComparison]::Ordinal)) {
        throw "$Context metadata mismatch for '$Key'."
    }
    return $true
}
