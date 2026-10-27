param(
    [Parameter(Mandatory = $true)]
    [string]$Report,

    [Parameter(Mandatory = $true)]
    [string]$CodexHome,

    [string]$ProjectRoot,

    [Int64]$WindowHandle = 0,

    [ValidateRange(1, 100)]
    [int]$Limit = 8
)

$ErrorActionPreference = 'Stop'
$actualCallerHome = if ($env:CODEX_HOME) { [IO.Path]::GetFullPath($env:CODEX_HOME) } else { Join-Path $env:USERPROFILE '.codex' }

if ([Threading.Thread]::CurrentThread.ApartmentState -ne [Threading.ApartmentState]::STA) {
    $staHost = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $staHost -PathType Leaf)) {
        throw 'An STA-capable Windows PowerShell host is required to preserve the complete clipboard.'
    }
    $arguments = @('-NoLogo','-NoProfile','-NonInteractive','-Sta','-ExecutionPolicy','Bypass','-File',$PSCommandPath,'-Report',$Report,'-CodexHome',$CodexHome,'-Limit',[string]$Limit)
    if ($ProjectRoot) { $arguments += @('-ProjectRoot',$ProjectRoot) }
    if ($WindowHandle -ne 0) { $arguments += @('-WindowHandle',[string]$WindowHandle) }
    & $staHost @arguments
    exit $LASTEXITCODE
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$nativeSource = @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public static class CodexProjectPicker
{
    public delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr parameter);

    [StructLayout(LayoutKind.Sequential)]
    public struct RECT
    {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc callback, IntPtr parameter);

    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hwnd);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int maxCount);

    [DllImport("user32.dll")]
    public static extern void keybd_event(byte virtualKey, byte scanCode, uint flags, UIntPtr extraInfo);

    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hwnd);

    [DllImport("user32.dll")]
    public static extern bool ShowWindowAsync(IntPtr hwnd, int command);

    [DllImport("user32.dll")]
    public static extern IntPtr GetDlgItem(IntPtr dialog, int controlId);

    [DllImport("user32.dll")]
    public static extern IntPtr SendMessage(IntPtr hwnd, uint message, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetClassName(IntPtr hwnd, StringBuilder className, int maxCount);

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);

    [DllImport("user32.dll")]
    public static extern IntPtr GetWindow(IntPtr hwnd, uint command);

    public static IntPtr[] FindVisibleChatGptWindows()
    {
        var results = new List<IntPtr>();
        EnumWindows((hwnd, parameter) =>
        {
            if (!IsWindowVisible(hwnd))
            {
                return true;
            }

            var title = new StringBuilder(256);
            GetWindowText(hwnd, title, title.Capacity);
            if (title.ToString() == "ChatGPT")
            {
                results.Add(hwnd);
            }

            return true;
        }, IntPtr.Zero);
        return results.ToArray();
    }

    private static bool IsOwnedBy(IntPtr candidate, IntPtr expectedOwner)
    {
        var seen = new HashSet<IntPtr>();
        var current = GetWindow(candidate, 4); // GW_OWNER
        while (current != IntPtr.Zero && seen.Add(current))
        {
            if (current == expectedOwner)
            {
                return true;
            }
            current = GetWindow(current, 4);
        }
        return false;
    }

    public static IntPtr[] FindOwnedProjectPickerWindows(IntPtr expectedOwner)
    {
        uint expectedProcess;
        GetWindowThreadProcessId(expectedOwner, out expectedProcess);
        if (expectedProcess == 0)
        {
            return new IntPtr[0];
        }

        var results = new List<IntPtr>();
        EnumWindows((hwnd, parameter) =>
        {
            if (!IsWindowVisible(hwnd))
            {
                return true;
            }
            var title = new StringBuilder(256);
            var className = new StringBuilder(256);
            GetWindowText(hwnd, title, title.Capacity);
            GetClassName(hwnd, className, className.Capacity);
            uint processId;
            GetWindowThreadProcessId(hwnd, out processId);
            if (title.ToString() == "Select Project Root" &&
                className.ToString() == "#32770" &&
                processId == expectedProcess &&
                IsOwnedBy(hwnd, expectedOwner))
            {
                results.Add(hwnd);
            }
            return true;
        }, IntPtr.Zero);
        return results.ToArray();
    }
}
'@

Add-Type -TypeDefinition $nativeSource

function Send-ControlKey {
    param([byte]$VirtualKey)

    [CodexProjectPicker]::keybd_event(0x11, 0, 0, [UIntPtr]::Zero)
    [CodexProjectPicker]::keybd_event($VirtualKey, 0, 0, [UIntPtr]::Zero)
    [CodexProjectPicker]::keybd_event($VirtualKey, 0, 2, [UIntPtr]::Zero)
    [CodexProjectPicker]::keybd_event(0x11, 0, 2, [UIntPtr]::Zero)
}

function Set-VerifiedForegroundWindow {
    param([IntPtr]$Window)

    [void][CodexProjectPicker]::ShowWindowAsync($Window, 9)
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        # A harmless Alt press lets a background helper request foreground on
        # current Windows versions without depending on screen coordinates.
        [CodexProjectPicker]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)
        [CodexProjectPicker]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)
        [void][CodexProjectPicker]::SetForegroundWindow($Window)
        Start-Sleep -Milliseconds 100
        if ([CodexProjectPicker]::GetForegroundWindow() -eq $Window) {
            return
        }
    }

    throw "Could not activate window handle $($Window.ToInt64())."
}

function Set-ClipboardTextWithRetry {
    param([string]$Text)

    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            [Windows.Forms.Clipboard]::SetText($Text)
            return
        } catch {
            Start-Sleep -Milliseconds 100
        }
    }

    throw 'The Windows clipboard remained busy after 30 retries.'
}

function Get-ClipboardSnapshotWithRetry {
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $snapshot = [Windows.Forms.DataObject]::new()
            $source = [Windows.Forms.Clipboard]::GetDataObject()
            if ($null -ne $source) {
                foreach ($format in @($source.GetFormats($false))) {
                    $data = $source.GetData($format, $false)
                    if ($null -eq $data) {
                        throw "Clipboard format could not be materialized safely: $format"
                    }
                    $snapshot.SetData($format, $false, $data)
                }
            }
            return [pscustomobject]@{ Data = $snapshot; HasData = (@($snapshot.GetFormats($false)).Count -gt 0) }
        } catch {
            Start-Sleep -Milliseconds 100
        }
    }
    throw 'The Windows clipboard could not be snapshotted safely; project registration was not started.'
}

function Restore-ClipboardSnapshotWithRetry {
    param([Parameter(Mandatory = $true)][object]$Snapshot)
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            if ($Snapshot.HasData) {
                [Windows.Forms.Clipboard]::SetDataObject($Snapshot.Data, $true)
            } else {
                [Windows.Forms.Clipboard]::Clear()
            }
            return
        } catch {
            Start-Sleep -Milliseconds 100
        }
    }
    throw 'Project registration finished, but the original multi-format clipboard could not be restored.'
}

function Wait-ForPickerPath {
    param([IntPtr]$Dialog, [string]$ExpectedPath)

    $root = [Windows.Automation.AutomationElement]::FromHandle($Dialog)
    $condition = New-Object Windows.Automation.PropertyCondition(
        [Windows.Automation.AutomationElement]::AutomationIdProperty,
        '1001'
    )

    for ($attempt = 0; $attempt -lt 50; $attempt++) {
        $address = $root.FindFirst([Windows.Automation.TreeScope]::Descendants, $condition)
        if ($null -ne $address) {
            $name = [string]$address.Current.Name
            if ($name.EndsWith($ExpectedPath, [StringComparison]::OrdinalIgnoreCase)) {
                return
            }
        }
        Start-Sleep -Milliseconds 100
    }

    throw "Project picker did not navigate to the expected path: $ExpectedPath"
}

function Open-ProjectPicker {
    param([IntPtr]$Window)

    Set-VerifiedForegroundWindow -Window $Window
    Send-ControlKey -VirtualKey 0x4F
}

function Wait-ForPicker {
    param([IntPtr]$Owner, [bool]$Present, [int]$Attempts = 100)

    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        $dialogs = @([CodexProjectPicker]::FindOwnedProjectPickerWindows($Owner))
        if ($dialogs.Count -gt 1) {
            throw "Multiple project pickers belong to the selected Codex window; refusing an ambiguous choice. Count=$($dialogs.Count)"
        }
        if ($Present -and $dialogs.Count -eq 1) {
            return $dialogs[0]
        }
        if (-not $Present -and $dialogs.Count -eq 0) {
            return [IntPtr]::Zero
        }
        Start-Sleep -Milliseconds 100
    }

    if ($Present) {
        throw 'Select Project Root did not open.'
    }
    throw 'Select Project Root did not close.'
}

function Test-RegisteredProject {
    param([string]$StatePath, [string]$ExpectedPath)
    $expected = [IO.Path]::GetFullPath($ExpectedPath).TrimEnd('\')
    $fresh = [IO.File]::ReadAllText($StatePath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    foreach ($property in $fresh.'local-projects'.PSObject.Properties) {
        foreach ($root in $property.Value.rootPaths) {
            if ([IO.Path]::GetFullPath([string]$root).TrimEnd('\').Equals($expected, [StringComparison]::OrdinalIgnoreCase)) {
                return $true
            }
        }
    }
    return $false
}

function Wait-ForRegisteredProject {
    param([string]$StatePath, [string]$ExpectedPath)
    for ($attempt = 0; $attempt -lt 100; $attempt++) {
        if (Test-RegisteredProject -StatePath $StatePath -ExpectedPath $ExpectedPath) { return }
        Start-Sleep -Milliseconds 100
    }
    throw "Codex project registry did not record the selected path: $ExpectedPath"
}

$reportPath = (Resolve-Path -LiteralPath $Report).Path
$codexHomePath = (Resolve-Path -LiteralPath $CodexHome).Path
$statePath = Join-Path $codexHomePath '.codex-global-state.json'
$restoreReport = [IO.File]::ReadAllText($reportPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
$state = [IO.File]::ReadAllText($statePath, [Text.Encoding]::UTF8) | ConvertFrom-Json

$registeredRoots = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
foreach ($property in $state.'local-projects'.PSObject.Properties) {
    foreach ($root in $property.Value.rootPaths) {
        [void]$registeredRoots.Add([IO.Path]::GetFullPath([string]$root).TrimEnd('\'))
    }
}

if ($restoreReport.destination_codex_home -and -not ([IO.Path]::GetFullPath([string]$restoreReport.destination_codex_home).TrimEnd('\').Equals($actualCallerHome.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase))) {
    throw 'Restore report belongs to a different Codex account.'
}
$requestedProjects = @($restoreReport.projects | Where-Object {
    $_.restored_project_root -and [string]$_.kind -notin @('projectless-root', 'projectless-conversation-group')
})
if ($ProjectRoot) {
    $normalizedRequestedRoot = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
    $requestedProjects = @(
        $requestedProjects | Where-Object {
            [IO.Path]::GetFullPath([string]$_.restored_project_root).TrimEnd('\').Equals(
                $normalizedRequestedRoot,
                [StringComparison]::OrdinalIgnoreCase
            )
        }
    )
    if ($requestedProjects.Count -ne 1) {
        throw "Expected exactly one restore-report project for: $normalizedRequestedRoot"
    }
}

$allPending = @(
    $requestedProjects |
        Where-Object {
            $root = [IO.Path]::GetFullPath([string]$_.restored_project_root).TrimEnd('\')
            -not $registeredRoots.Contains($root)
        }
)
$pending = @($allPending | Select-Object -First $Limit)
$remainingAfterBatch = $allPending.Count - $pending.Count

if ($pending.Count -eq 0) {
    Write-Output '{"status":"complete","registered":0}'
    exit 0
}

if (-not [IO.Path]::GetFullPath($CodexHome).TrimEnd('\').Equals($actualCallerHome.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Project registration may target only the calling CODEX_HOME.'
}

$clipboardSnapshot = Get-ClipboardSnapshotWithRetry

$registered = 0
try {
    $visibleWindows = @([CodexProjectPicker]::FindVisibleChatGptWindows())
    if ($WindowHandle -ne 0) {
        $mainWindow = [IntPtr]$WindowHandle
        if (-not ($visibleWindows -contains $mainWindow)) {
            throw "The requested ChatGPT window handle is not visible: $WindowHandle"
        }
    } elseif ($visibleWindows.Count -eq 1) {
        $mainWindow = $visibleWindows[0]
    } elseif ($visibleWindows.Count -eq 0) {
        throw 'A visible ChatGPT window was not found.'
    } else {
        throw "Multiple ChatGPT windows are visible. Pass -WindowHandle for the exact Codex account window; refusing to choose one arbitrarily. Count=$($visibleWindows.Count)"
    }
    foreach ($project in $pending) {
        $path = [IO.Path]::GetFullPath([string]$project.restored_project_root).TrimEnd('\')
        if (-not (Test-Path -LiteralPath $path -PathType Container)) {
            throw "Restored project root is missing: $path"
        }

        Open-ProjectPicker -Window $mainWindow

        $dialog = Wait-ForPicker -Owner $mainWindow -Present $true
        Set-VerifiedForegroundWindow -Window $dialog
        Set-ClipboardTextWithRetry -Text $path
        Send-ControlKey -VirtualKey 0x4C
        Start-Sleep -Milliseconds 150
        Send-ControlKey -VirtualKey 0x56
        [CodexProjectPicker]::keybd_event(0x0D, 0, 0, [UIntPtr]::Zero)
        [CodexProjectPicker]::keybd_event(0x0D, 0, 2, [UIntPtr]::Zero)
        Start-Sleep -Milliseconds 500

        $ownedDialogs = @([CodexProjectPicker]::FindOwnedProjectPickerWindows($mainWindow))
        if ($ownedDialogs.Count -ne 1) {
            throw "Project picker vanished while navigating to: $path"
        }
        $dialog = $ownedDialogs[0]

        Wait-ForPickerPath -Dialog $dialog -ExpectedPath $path

        $selectButton = [CodexProjectPicker]::GetDlgItem($dialog, 1)
        if ($selectButton -eq [IntPtr]::Zero) {
            throw "Select Folder button was not found for: $path"
        }

        [void][CodexProjectPicker]::SendMessage($selectButton, 0x00F5, [IntPtr]::Zero, [IntPtr]::Zero)
        [void](Wait-ForPicker -Owner $mainWindow -Present $false)
        Wait-ForRegisteredProject -StatePath $statePath -ExpectedPath $path

        $registered++
        Write-Output ("REGISTERED {0}/{1} {2}" -f $registered, $pending.Count, $path)
    }
} finally {
    Restore-ClipboardSnapshotWithRetry -Snapshot $clipboardSnapshot
}

Write-Output (ConvertTo-Json -Compress -InputObject ([ordered]@{
    status = if ($remainingAfterBatch -eq 0) { 'complete' } else { 'partial' }
    registered = $registered
    requested = $pending.Count
    total_pending_before = $allPending.Count
    remaining = $remainingAfterBatch
}))
