function New-ResumedProjectUploadState {
    [CmdletBinding()]
    param(
        [AllowNull()][object]$ExistingRecord,
        [Parameter(Mandatory = $true)][string]$ProjectId,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$SourcePath
    )

    if ($null -ne $ExistingRecord) {
        if ([string]$ExistingRecord.project_id -cne $ProjectId -or
            [string]$ExistingRecord.source_path -cne $SourcePath) {
            throw "Existing project upload state identity mismatch: $ProjectId"
        }
    }
    return [ordered]@{
        project_id = $ProjectId
        label = $Label
        source_path = $SourcePath
        status = 'running'
        error = $null
        project_release = if ($null -ne $ExistingRecord) { $ExistingRecord.project_release } else { $null }
        conversation_release = if ($null -ne $ExistingRecord) { $ExistingRecord.conversation_release } else { $null }
        prepared_backup = if ($null -ne $ExistingRecord) { $ExistingRecord.prepared_backup } else { $null }
        conversation_pack = if ($null -ne $ExistingRecord) { $ExistingRecord.conversation_pack } else { $null }
    }
}
