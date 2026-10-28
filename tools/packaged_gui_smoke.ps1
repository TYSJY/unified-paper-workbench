param(
    [Parameter(Mandatory = $true)][string]$Exe,
    [Parameter(Mandatory = $true)][string]$Output
)
$ErrorActionPreference = "Stop"
$workspace = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$buildRoot = [IO.Path]::GetFullPath((Join-Path $workspace "build")) + [IO.Path]::DirectorySeparatorChar
$smokeRoot = [IO.Path]::GetFullPath($Output)
$executable = [IO.Path]::GetFullPath($Exe)
if (-not $smokeRoot.StartsWith($buildRoot, [StringComparison]::OrdinalIgnoreCase) -or (Test-Path -LiteralPath $smokeRoot)) {
    throw "Smoke output must be a new directory under this workspace's build directory."
}
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { throw "Executable not found." }
New-Item -ItemType Directory -Path $smokeRoot | Out-Null
$profileRoot = Join-Path $smokeRoot "empty-profile"
New-Item -ItemType Directory -Path $profileRoot | Out-Null
$start = [Diagnostics.ProcessStartInfo]::new()
$start.FileName = $executable
$start.WorkingDirectory = $smokeRoot
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
$start.LoadUserProfile = $false
$start.Environment.Clear()
$childEnvironment = @{
    "SYSTEMROOT" = "C:\Windows"; "WINDIR" = "C:\Windows";
    "TEMP" = $smokeRoot; "TMP" = $smokeRoot;
    "PATH" = "C:\Windows\System32;C:\Windows";
    "USERPROFILE" = $profileRoot; "HOME" = $profileRoot;
    "APPDATA" = (Join-Path $profileRoot "Roaming"); "LOCALAPPDATA" = (Join-Path $profileRoot "Local");
    "CODEX_HOME" = (Join-Path $smokeRoot "empty-codex");
    "UNIFIED_PAPER_ORCHESTRATOR_ROOT" = $smokeRoot; "UNIFIED_PAPER_ORCHESTRATOR_V2_ROOT" = $smokeRoot;
    "UNIFIED_PAPER_WORKSPACE_DISABLE_REGISTRY" = "1"; "UNIFIED_PAPER_WORKSPACE" = "";
    "UNIFIED_WRITING_ROOT" = (Join-Path $smokeRoot "writing");
    "UNIFIED_FIGURE_ROOT" = (Join-Path $smokeRoot "figures");
    "UNIFIED_JOURNAL_AUTHOR_ROOT" = (Join-Path $smokeRoot "journal_author");
    "UNIFIED_SUBMISSION_ROOT" = (Join-Path $smokeRoot "submission");
    "QT_QPA_PLATFORM" = "offscreen"; "QT_QUICK_BACKEND" = "software"; "QT_QUICK_CONTROLS_STYLE" = "Basic"
}
foreach ($entry in $childEnvironment.GetEnumerator()) { $start.Environment[$entry.Key] = $entry.Value }
$probe = [Diagnostics.Process]::new()
$probe.StartInfo = $start
try {
    if (-not $probe.Start()) { throw "GUI probe did not start." }
    $stdoutTask = $probe.StandardOutput.ReadToEndAsync()
    $stderrTask = $probe.StandardError.ReadToEndAsync()
    $logPath = Join-Path $smokeRoot "state\logs\qt_workbench.log"
    # A cold one-file launch can spend more than 20 seconds extracting the
    # bundled Qt/Word runtime. Wait for startup evidence before the alive check.
    $startupClock = [Diagnostics.Stopwatch]::StartNew()
    while (-not $probe.HasExited -and $startupClock.Elapsed.TotalSeconds -lt 60 -and -not (Test-Path -LiteralPath $logPath)) {
        $probe.WaitForExit(500) | Out-Null
    }
    $stayedRunning = -not $probe.WaitForExit(5000)
    $logExists = Test-Path -LiteralPath $logPath
    $fatal = $false
    if ($logExists) {
        $fatal = [bool]((Get-Content -Raw -LiteralPath $logPath) -match "Traceback|failed to load component|新版工作台启动失败|Could not load the Qt platform")
    }
    $result = @{ StayedRunning = $stayedRunning; QtLogCreated = $logExists; FatalStartupError = $fatal; IsolatedRoot = $smokeRoot; StartupSeconds = [Math]::Round($startupClock.Elapsed.TotalSeconds, 1) }
    $result | ConvertTo-Json -Compress
    if (-not $stayedRunning -or -not $logExists -or $fatal) { throw "Packaged GUI smoke failed." }
}
finally {
    if (-not $probe.HasExited) {
        # This PID belongs only to the just-started isolated probe. Terminate
        # its PyInstaller child too; never enumerate/stop unrelated app windows.
        & "$env:SYSTEMROOT\System32\taskkill.exe" /PID $probe.Id /T /F | Out-Null
        $probe.WaitForExit(5000) | Out-Null
    }
    if ($stdoutTask -and $stdoutTask.Wait(5000)) {
        [IO.File]::WriteAllText((Join-Path $smokeRoot "stdout.log"), $stdoutTask.Result)
    }
    if ($stderrTask -and $stderrTask.Wait(5000)) {
        [IO.File]::WriteAllText((Join-Path $smokeRoot "stderr.log"), $stderrTask.Result)
    }
    $probe.Dispose()
}
