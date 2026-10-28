param(
    [switch]$StageOnly,
    [ValidateSet("Internal", "CustomerClosedBeta")]
    [string]$ReleaseProfile = "Internal",
    [string]$CommercialPublicKeyPath = ""
)

$ErrorActionPreference = "Stop"

$root = [System.IO.Path]::GetFullPath((Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)))
Set-Location $root

$releaseProfilePath = switch ($ReleaseProfile) {
    "Internal" {
        Join-Path $root "release_profiles\internal.json"
    }
    "CustomerClosedBeta" {
        Join-Path $root "release_profiles\customer-closed-beta.json"
    }
}
if (-not (Test-Path -LiteralPath $releaseProfilePath -PathType Leaf)) {
    throw "Release profile is unavailable: $releaseProfilePath"
}
$resolvedCommercialPublicKey = ""
if ($ReleaseProfile -eq "CustomerClosedBeta") {
    if ([string]::IsNullOrWhiteSpace($CommercialPublicKeyPath)) {
        throw "CustomerClosedBeta builds require -CommercialPublicKeyPath."
    }
    $resolvedCommercialPublicKey = [System.IO.Path]::GetFullPath(
        $CommercialPublicKeyPath
    )
    if (-not (Test-Path -LiteralPath $resolvedCommercialPublicKey -PathType Leaf)) {
        throw "Commercial public key is unavailable: $resolvedCommercialPublicKey"
    }
}
elseif (-not [string]::IsNullOrWhiteSpace($CommercialPublicKeyPath)) {
    throw "-CommercialPublicKeyPath is only valid for CustomerClosedBeta builds."
}

$python = "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
$pythonSha256 = "4d6f5f81a4bca11191c4c7c6b43632694d0a4ce74e068619d8fdc161d469859a"
$trustedPowerShellHome = "C:\Windows\System32\WindowsPowerShell\v1.0"
$trustedPowerShellModulePath = Join-Path $trustedPowerShellHome "Modules"
$trustedPowerShell = Join-Path $trustedPowerShellHome "powershell.exe"
$trustedGitHome = "C:\Program Files\Git\cmd"
$trustedGit = Join-Path $trustedGitHome "git.exe"
$trustedGitSha256 = "81ef35ae005ca9318018d18e3327578ce939fb99feaad6b2d7c8ab15f3de8db5"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Trusted Python executable is unavailable: $python"
}
$observedPythonSha256 = (
    Get-FileHash -LiteralPath $python -Algorithm SHA256
).Hash.ToLowerInvariant()
if ($observedPythonSha256 -ne $pythonSha256) {
    throw "Trusted Python executable hash mismatch."
}
if (-not (Test-Path -LiteralPath $trustedPowerShell -PathType Leaf)) {
    throw "Trusted Windows PowerShell executable is unavailable: $trustedPowerShell"
}
if (-not (Test-Path -LiteralPath $trustedPowerShellModulePath -PathType Container)) {
    throw "Trusted Windows PowerShell module path is unavailable: $trustedPowerShellModulePath"
}
if (-not (Test-Path -LiteralPath $trustedGit -PathType Leaf)) {
    throw "Trusted Git executable is unavailable: $trustedGit"
}
$observedGitSha256 = (
    Get-FileHash -LiteralPath $trustedGit -Algorithm SHA256
).Hash.ToLowerInvariant()
if ($observedGitSha256 -ne $trustedGitSha256) {
    throw "Trusted Git executable hash mismatch."
}
$gitSignature = Get-AuthenticodeSignature -LiteralPath $trustedGit
if ($gitSignature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
    throw "Trusted Git executable signature is invalid: $($gitSignature.Status)"
}

function ConvertTo-BuildWindowsArgument {
    param([Parameter(Mandatory = $true)][string]$Argument)

    if ($Argument.Length -gt 0 -and $Argument -notmatch '[\s"]') {
        return $Argument
    }
    $quoted = '"'
    $backslashes = 0
    foreach ($character in $Argument.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes += 1
            continue
        }
        if ($character -eq '"') {
            $quoted += ('\' * (($backslashes * 2) + 1)) + '"'
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            $quoted += ('\' * $backslashes)
            $backslashes = 0
        }
        $quoted += $character
    }
    if ($backslashes -gt 0) {
        $quoted += ('\' * ($backslashes * 2))
    }
    return $quoted + '"'
}

function Get-IsolatedBuildEnvironment {
    $environment = [ordered]@{}
    $environment["SYSTEMROOT"] = "C:\Windows"
    $environment["WINDIR"] = "C:\Windows"
    $environment["TEMP"] = "C:\Users\Administrator\AppData\Local\Temp"
    $environment["TMP"] = "C:\Users\Administrator\AppData\Local\Temp"
    $environment["USERPROFILE"] = "C:\Users\Administrator"
    $environment["APPDATA"] = "C:\Users\Administrator\AppData\Roaming"
    $environment["LOCALAPPDATA"] = "C:\Users\Administrator\AppData\Local"
    $environment["COMSPEC"] = "C:\Windows\System32\cmd.exe"
    $environment["PATH"] = @(
        "C:\Users\Administrator\AppData\Local\Programs\Python\Python312",
        "C:\Windows\System32",
        $trustedPowerShellHome,
        $trustedGitHome
    ) -join ";"
    $environment["PATHEXT"] = ".COM;.EXE;.BAT;.CMD"
    # ACL hardening invokes Windows PowerShell.  Keep module discovery limited
    # to the signed-in-box system module tree; never inherit user module paths.
    $environment["PSMODULEPATH"] = $trustedPowerShellModulePath
    $environment["PYTHONDONTWRITEBYTECODE"] = "1"
    $environment["PYTHONHASHSEED"] = "0"
    $environment["PYTHONIOENCODING"] = "utf-8"
    $environment["PYTHONNOUSERSITE"] = "1"
    $environment["PYTHONSAFEPATH"] = "1"
    $environment["PYTHONUTF8"] = "1"
    # urllib also consults the Windows proxy configuration.  Keep loopback
    # integration tests local after the inherited proxy environment is cleared.
    $environment["NO_PROXY"] = "localhost,127.0.0.1,::1"
    $environment["PYINSTALLER_CONFIG_DIR"] = Join-Path $root "build\gui-0.12.0b11\pyinstaller_config"
    $environment["PYTHONPATH"] = @(
        $root,
        (Join-Path $root "runtime\sdk-0.147.0"),
        (Join-Path $root "runtime\word-python"),
        "C:\Users\Administrator\AppData\Roaming\Python\Python312\site-packages",
        "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\Lib\site-packages"
    ) -join ";"
    return $environment
}

function Invoke-IsolatedPython([string[]]$Arguments, [string]$FailureMessage) {
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $python
    $startInfo.Arguments = @(
        $Arguments | ForEach-Object {
            ConvertTo-BuildWindowsArgument -Argument ([string]$_)
        }
    ) -join " "
    $startInfo.WorkingDirectory = $root
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.LoadUserProfile = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.Environment.Clear()
    foreach ($entry in (Get-IsolatedBuildEnvironment).GetEnumerator()) {
        $startInfo.Environment[[string]$entry.Key] = [string]$entry.Value
    }
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            throw $FailureMessage
        }
        $standardOutput = [Console]::OpenStandardOutput()
        $standardError = [Console]::OpenStandardError()
        $outputCopy = $process.StandardOutput.BaseStream.CopyToAsync($standardOutput)
        $errorCopy = $process.StandardError.BaseStream.CopyToAsync($standardError)
        $process.WaitForExit()
        [System.Threading.Tasks.Task]::WaitAll(@($outputCopy, $errorCopy))
        $standardOutput.Flush()
        $standardError.Flush()
        if ($process.ExitCode -ne 0) {
            throw "$FailureMessage ExitCode=$($process.ExitCode)"
        }
    }
    finally {
        $process.Dispose()
    }
}

$buildProbe = Join-Path $root "tools\isolated_build_probe.py"
if (-not (Test-Path -LiteralPath (Join-Path $root "runtime\sdk-0.147.0\openai_codex\__init__.py"))) {
    throw "Run tools/prepare_codex_runtime.ps1 before building."
}
Invoke-IsolatedPython -Arguments @("-S", "-P", "-B", $buildProbe) `
    -FailureMessage "Isolated build environment probe failed."

Write-Host "Running repository tests before packaging..."
Invoke-IsolatedPython -Arguments @("-S", "-P", "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider") `
    -FailureMessage "Tests failed; EXE build was not started."

function Remove-GeneratedTree([string]$Path, [string]$ExpectedLeaf) {
    $full = [System.IO.Path]::GetFullPath($Path)
    $rootPrefix = $root.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if (-not $full.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove path outside workspace: $full"
    }
    if ([System.IO.Path]::GetFileName($full) -ne $ExpectedLeaf) {
        throw "Refusing to remove unexpected generated directory: $full"
    }
    if (Test-Path -LiteralPath $full) {
        Remove-Item -LiteralPath $full -Recurse -Force
    }
}

$buildDir = Join-Path $root "build\gui-0.12.0b11"
$distDir = Join-Path $root "dist"
$candidateDir = Join-Path $buildDir "dist_candidate"
$promptDir = Join-Path $root "prompts"
$uiAssetDir = Join-Path $root "unified_paper_orchestrator\ui\assets"
$qtUiDir = Join-Path $root "unified_paper_orchestrator\ui_next\qml"
$qtLocaleDir = Join-Path $root "unified_paper_orchestrator\ui_next\locales"
$appIcon = Join-Path $uiAssetDir "app_icon.ico"
$entryPoint = Join-Path $root "unified_paper_orchestrator\app.py"
$versionFile = Join-Path $root "tools\windows_version_info.txt"
Remove-GeneratedTree -Path $buildDir -ExpectedLeaf "gui-0.12.0b11"
New-Item -Path $buildDir -ItemType Directory -Force | Out-Null
New-Item -Path $candidateDir -ItemType Directory -Force | Out-Null

$releaseAssetsDir = Join-Path $buildDir "release_profile"
$prepareReleaseProfile = Join-Path $root "tools\prepare_release_profile.py"
$prepareReleaseArguments = @(
    "-S",
    "-P",
    "-B",
    $prepareReleaseProfile,
    "--profile",
    $releaseProfilePath,
    "--output-dir",
    $releaseAssetsDir
)
if ($ReleaseProfile -eq "CustomerClosedBeta") {
    $prepareReleaseArguments += @("--public-key", $resolvedCommercialPublicKey)
}
Invoke-IsolatedPython -Arguments $prepareReleaseArguments `
    -FailureMessage "Release profile preparation failed."
$commercialTrustedKeys = Join-Path $releaseAssetsDir "trusted_keys.json"
$commercialDistributionPolicy = Join-Path $releaseAssetsDir "distribution_policy.json"
if (-not (Test-Path -LiteralPath $commercialTrustedKeys -PathType Leaf) -or
    -not (Test-Path -LiteralPath $commercialDistributionPolicy -PathType Leaf)) {
    throw "Release profile preparation did not produce both immutable assets."
}

Invoke-IsolatedPython -Arguments @(
    "-S",
    "-P",
    "-B",
    "-m",
    "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--console",
    "--hide-console",
    "hide-early",
    "--collect-all",
    "openai_codex",
    "--collect-all",
    "pypdfium2",
    "--collect-all",
    "pypdfium2_raw",
    "--collect-all",
    "docx",
    "--add-data",
    ((Join-Path $root "runtime\pandoc-3.11\pandoc-3.11") + ";runtime/pandoc-3.11/pandoc-3.11"),
    "--add-data",
    ((Join-Path $root "unified_paper_orchestrator\word_render.ps1") + ";unified_paper_orchestrator"),
    "--collect-submodules",
    "codex_cli_bin",
    "--add-data",
    ((Join-Path $root "runtime\codex-0.153.4\codex_cli_bin") + ";runtime/codex-0.153.4/codex_cli_bin"),
    "--hidden-import",
    "unified_paper_orchestrator.ui_next.main",
    "--hidden-import",
    "PySide6.QtQuick",
    "--hidden-import",
    "PySide6.QtQuickControls2",
    "--hidden-import",
    "unified_paper_orchestrator.figure_sdk_runtime",
    "--hidden-import",
    "unified_paper_orchestrator.journal_author_sdk_runtime",
    "--hidden-import",
    "unified_paper_orchestrator.smart_submission_sdk_runtime",
    "--hidden-import",
    "unified_paper_orchestrator.smart_browser_mcp",
    "--hidden-import",
    "unified_paper_orchestrator.smart_browser_cdp",
    "--hidden-import",
    "unified_paper_orchestrator.smart_mailbox",
    "--hidden-import",
    "unified_paper_orchestrator.ui.account_site_manager",
    "--exclude-module",
    "unified_paper_orchestrator.ui.main_window",
    "--name",
    "UnifiedPaperOrchestrator",
    "--icon",
    $appIcon,
    "--version-file",
    $versionFile,
    "--workpath",
    (Join-Path $buildDir "pyinstaller"),
    "--specpath",
    $buildDir,
    "--distpath",
    $candidateDir,
    "--add-data",
    "$promptDir;prompts",
    "--add-data",
    "$uiAssetDir;unified_paper_orchestrator/ui/assets",
    "--add-data",
    "$qtUiDir;unified_paper_orchestrator/ui_next/qml",
    "--add-data",
    "$qtLocaleDir;unified_paper_orchestrator/ui_next/locales",
    "--add-data",
    "$commercialTrustedKeys;unified_paper_orchestrator/commercial",
    "--add-data",
    "$commercialDistributionPolicy;unified_paper_orchestrator/commercial",
    $entryPoint
) -FailureMessage "PyInstaller failed."

$executables = @(Get-ChildItem -LiteralPath $candidateDir -File -Filter "*.exe")
if ($executables.Count -ne 1 -or $executables[0].Name -ne "UnifiedPaperOrchestrator.exe") {
    throw "Expected exactly one candidate EXE named UnifiedPaperOrchestrator.exe; found $($executables.Count)."
}

$cliProbeRoot = Join-Path $buildDir "cli_probe_root"
$cliProbeProfile = Join-Path $buildDir "cli_probe_profile"
New-Item -Path $cliProbeRoot -ItemType Directory -Force | Out-Null
New-Item -Path $cliProbeProfile -ItemType Directory -Force | Out-Null

function Invoke-PackagedCliProbe([string[]]$Arguments, [int]$ExpectedExitCode, [string]$ExpectedPattern) {
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $executables[0].FullName
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.LoadUserProfile = $false
    $startInfo.Environment.Clear()
    $startInfo.Environment["SYSTEMROOT"] = "C:\Windows"
    $startInfo.Environment["TEMP"] = "C:\Users\Administrator\AppData\Local\Temp"
    $startInfo.Environment["TMP"] = "C:\Users\Administrator\AppData\Local\Temp"
    # The CLI imports the scheduler controller, whose automation root is
    # resolved through Path.home().  Use an empty build-local profile so a
    # packaged negative probe can never inspect or mutate the user's Codex
    # Desktop configuration.
    $startInfo.Environment["USERPROFILE"] = $cliProbeProfile
    $startInfo.Environment["HOME"] = $cliProbeProfile
    $startInfo.Environment["APPDATA"] = (Join-Path $cliProbeProfile "AppData\Roaming")
    $startInfo.Environment["LOCALAPPDATA"] = (Join-Path $cliProbeProfile "AppData\Local")
    $startInfo.Environment["PYTHONUTF8"] = "1"
    $startInfo.Environment["UNIFIED_PAPER_ORCHESTRATOR_ROOT"] = $cliProbeRoot
    $startInfo.Environment["UNIFIED_PAPER_ORCHESTRATOR_V2_ROOT"] = $cliProbeRoot
    # Windows PowerShell 5.1 exposes ProcessStartInfo.Arguments, not the newer
    # ArgumentList collection. Probe arguments are intentionally whitespace-free.
    if ($Arguments | Where-Object { $_ -match '\s' }) {
        throw "Packaged CLI probe arguments must not contain whitespace."
    }
    $startInfo.Arguments = $Arguments -join " "
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            throw "Packaged CLI probe could not start."
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(60000)) {
            $process.Kill()
            $process.WaitForExit()
            throw "Packaged CLI probe timed out; argv may have been routed to the GUI entry point."
        }
        $process.WaitForExit()
        $output = $stdoutTask.GetAwaiter().GetResult() + $stderrTask.GetAwaiter().GetResult()
        if ($process.ExitCode -ne $ExpectedExitCode -or $output -notmatch $ExpectedPattern) {
            $commandText = $Arguments -join " "
            $preview = if ($output.Length -gt 1200) {
                $output.Substring(0, 1200)
            }
            else {
                $output
            }
            throw (
                "Packaged CLI probe failed. command=$commandText; " +
                "ExitCode=$($process.ExitCode); expected=$ExpectedExitCode; " +
                "pattern=$ExpectedPattern; output=$preview"
            )
        }
    }
    finally {
        $process.Dispose()
    }
}

Invoke-PackagedCliProbe -Arguments @("--help") -ExpectedExitCode 0 -ExpectedPattern "Unified paper workflow orchestrator"
Invoke-PackagedCliProbe -Arguments @("--version") -ExpectedExitCode 0 -ExpectedPattern "0\.12\.0b11"
$expectedEdition = if ($ReleaseProfile -eq "CustomerClosedBeta") { "customer" } else { "internal" }
$expectedEnforcement = if ($ReleaseProfile -eq "CustomerClosedBeta") { "true" } else { "false" }
Invoke-PackagedCliProbe -Arguments @("distribution-status") -ExpectedExitCode 0 `
    -ExpectedPattern ('"edition": "' + $expectedEdition + '"')
Invoke-PackagedCliProbe -Arguments @("distribution-status") -ExpectedExitCode 0 `
    -ExpectedPattern ('"commercial_enforcement": ' + $expectedEnforcement)
if ($ReleaseProfile -eq "CustomerClosedBeta") {
    Invoke-PackagedCliProbe -Arguments @("distribution-status") -ExpectedExitCode 0 `
        -ExpectedPattern '"trusted_public_key_count": 1'
}
Invoke-PackagedCliProbe -Arguments @("ai-execution-status") -ExpectedExitCode 0 -ExpectedPattern '"distribution": "openai-codex"'
Invoke-PackagedCliProbe -Arguments @("ai-execution-status") -ExpectedExitCode 0 -ExpectedPattern '"version": "0\.153\.4"'
Invoke-PackagedCliProbe -Arguments @("writing-sdk-run", "--help") -ExpectedExitCode 0 -ExpectedPattern "local event-driven writing dispatcher"
Invoke-PackagedCliProbe -Arguments @("writing-sdk-run", "--help") -ExpectedExitCode 0 -ExpectedPattern "--paper-key"
Invoke-PackagedCliProbe -Arguments @("writing-sdk-run", "--help") -ExpectedExitCode 0 -ExpectedPattern "--until-step"
Invoke-PackagedCliProbe -Arguments @("writing-sdk-requeue-recoverable", "--help") -ExpectedExitCode 0 -ExpectedPattern "--archived-thread-id"
Invoke-PackagedCliProbe -Arguments @("writing-sdk-revalidate-last-completed", "--help") -ExpectedExitCode 0 -ExpectedPattern "--paper-key"
Invoke-PackagedCliProbe -Arguments @("writing-evidence-import", "--help") -ExpectedExitCode 0 -ExpectedPattern "--check-only"
Invoke-PackagedCliProbe -Arguments @("smart-browser-mcp-serve", "--help") -ExpectedExitCode 0 -ExpectedPattern "short-lived Smart browser"
Invoke-PackagedCliProbe -Arguments @("smart-submission-sdk-run", "--help") -ExpectedExitCode 0 -ExpectedPattern "default dispatches every currently eligible"
Invoke-PackagedCliProbe -Arguments @("gateway-status") -ExpectedExitCode 0 -ExpectedPattern "OPENAI_API_KEY is not configured"
Invoke-PackagedCliProbe -Arguments @("gateway-status") -ExpectedExitCode 0 -ExpectedPattern "credential_free_http_loopback_only"
Invoke-PackagedCliProbe -Arguments @("gateway-relay-review-canary", "--help") -ExpectedExitCode 0 -ExpectedPattern "Relay model claim"
Invoke-PackagedCliProbe -Arguments @("gateway-codex-relay-review-canary", "--help") -ExpectedExitCode 0 -ExpectedPattern "standalone native codex executable"
Invoke-PackagedCliProbe -Arguments @("definitely-not-a-command") -ExpectedExitCode 2 -ExpectedPattern "invalid choice"

# Keep the last known-good dist intact until tests, packaging, and both argv probes pass.
if ($StageOnly) {
    $stagedDir = Join-Path $buildDir "release_candidate"
    New-Item -Path $stagedDir -ItemType Directory -Force | Out-Null
    $stagedExe = Join-Path $stagedDir "UnifiedPaperOrchestrator.exe"
    Move-Item -LiteralPath $executables[0].FullName -Destination $stagedExe -Force
    Write-Host "Built and verified staged executable: $stagedExe"
    $stagedSha256 = (Get-FileHash -LiteralPath $stagedExe -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Host "SHA256: $stagedSha256"
    return
}

Remove-GeneratedTree -Path $distDir -ExpectedLeaf "dist"
New-Item -Path $distDir -ItemType Directory -Force | Out-Null
$finalExe = Join-Path $distDir "UnifiedPaperOrchestrator.exe"
Move-Item -LiteralPath $executables[0].FullName -Destination $finalExe
Remove-GeneratedTree -Path $buildDir -ExpectedLeaf "gui-0.12.0b11"

$publishedExecutables = @(Get-ChildItem -LiteralPath $distDir -File -Filter "*.exe")
if ($publishedExecutables.Count -ne 1 -or $publishedExecutables[0].Name -ne "UnifiedPaperOrchestrator.exe") {
    throw "Expected exactly one published EXE named UnifiedPaperOrchestrator.exe; found $($publishedExecutables.Count)."
}

Write-Host "Built and verified unique executable: $finalExe"
$finalSha256 = (Get-FileHash -LiteralPath $finalExe -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Host "SHA256: $finalSha256"
