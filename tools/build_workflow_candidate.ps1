param(
    [string]$SciWritingRoot = "",
    [string]$SciFiguresRoot = "",
    [string]$SciJournalAuthorRoot = ""
)

$ErrorActionPreference = "Stop"

$root = [System.IO.Path]::GetFullPath(
    (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
)
Set-Location $root

$python = "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
$pythonSha256 = "4d6f5f81a4bca11191c4c7c6b43632694d0a4ce74e068619d8fdc161d469859a"
$git = "C:\Program Files\Git\cmd\git.exe"
$expectedPyInstallerVersion = "6.21.0"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Trusted Python executable is unavailable: $python"
}
$observedPythonSha256 = (
    Get-FileHash -LiteralPath $python -Algorithm SHA256
).Hash.ToLowerInvariant()
if ($observedPythonSha256 -ne $pythonSha256) {
    throw "Trusted Python executable hash mismatch."
}
if (-not (Test-Path -LiteralPath $git -PathType Leaf)) {
    throw "Trusted Git executable is unavailable: $git"
}

function ConvertTo-CandidateWindowsArgument {
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

function Get-CandidateEnvironment {
    $environment = [ordered]@{}
    foreach ($name in @(
        "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "APPDATA",
        "LOCALAPPDATA", "COMSPEC", "PATH", "PATHEXT"
    )) {
        $value = [System.Environment]::GetEnvironmentVariable($name)
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            $environment[$name] = $value
        }
    }
    $environment["PYTHONDONTWRITEBYTECODE"] = "1"
    $environment["PYTHONHASHSEED"] = "0"
    $environment["PYTHONIOENCODING"] = "utf-8"
    $environment["PYTHONNOUSERSITE"] = "1"
    $environment["PYTHONUTF8"] = "1"
    $environment["UNIFIED_PAPER_WORKSPACE_DISABLE_REGISTRY"] = "1"
    $environment["PYINSTALLER_CONFIG_DIR"] = Join-Path $root "build\workflow_candidate\pyinstaller-config"
    $environment["PYTHONPATH"] = @(
        $root,
        "C:\Users\Administrator\AppData\Roaming\Python\Python312\site-packages",
        "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\Lib\site-packages"
    ) -join ";"
    return $environment
}

function Invoke-CandidateProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FileName,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$FailureMessage
    )

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FileName
    $startInfo.Arguments = @(
        $Arguments | ForEach-Object {
            ConvertTo-CandidateWindowsArgument -Argument ([string]$_)
        }
    ) -join " "
    $startInfo.WorkingDirectory = $root
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.LoadUserProfile = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.Environment.Clear()
    foreach ($entry in (Get-CandidateEnvironment).GetEnumerator()) {
        $startInfo.Environment[[string]$entry.Key] = [string]$entry.Value
    }
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            throw $FailureMessage
        }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $output = $stdout.GetAwaiter().GetResult()
        $errorOutput = $stderr.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) {
            throw "$FailureMessage ExitCode=$($process.ExitCode) STDERR=$errorOutput STDOUT=$output"
        }
        return $output.Trim()
    }
    finally {
        $process.Dispose()
    }
}

function Get-CandidateFingerprint {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [ordered]@{ exists = $false; sha256 = ""; size = 0 }
    }
    $item = Get-Item -LiteralPath $Path
    return [ordered]@{
        exists = $true
        sha256 = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
        size = [int64]$item.Length
    }
}

function Assert-CandidateFingerprintUnchanged {
    param(
        [Parameter(Mandatory = $true)]$Before,
        [Parameter(Mandatory = $true)]$After,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if (($Before | ConvertTo-Json -Compress) -ne ($After | ConvertTo-Json -Compress)) {
        throw "$Label changed during the isolated candidate build."
    }
}

function Assert-NotCandidateReparsePoint {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing candidate cleanup because $Label is a reparse point: $Path"
    }
}

$baselinePath = Join-Path $root "releases\binaries\0.12.0b3\UnifiedPaperOrchestrator.exe"
$expectedBaselineSha256 = "8378e75834a21fc56e8038c0b17d31edadf1b5c2231ed5b3f0954c5047c5d371"
$baselineBefore = Get-CandidateFingerprint -Path $baselinePath
if (-not $baselineBefore.exists -or $baselineBefore.sha256 -ne $expectedBaselineSha256) {
    throw "Authoritative 0.12.0b3 baseline is absent or has an unexpected hash."
}
$productionDistPath = Join-Path $root "dist\UnifiedPaperOrchestrator.exe"
$productionDistBefore = Get-CandidateFingerprint -Path $productionDistPath
$baselineVersionProbe = Invoke-CandidateProcess -FileName $baselinePath -Arguments @("--version") `
    -FailureMessage "Authoritative baseline --version probe failed."
if ($baselineVersionProbe -ne "UnifiedPaperOrchestrator.exe 0.12.0b3") {
    throw "Authoritative baseline returned an unexpected version: $baselineVersionProbe"
}
$baselineTag = "pre-generic-workflow-0.12.0b3-20260801"
$expectedBaselineTagCommit = "7f33ff4ee9ccd1dc34e7e01a21a9483c73147836"
$baselineTagCommit = Invoke-CandidateProcess -FileName $git -Arguments @(
    "rev-parse", "$baselineTag^{commit}"
) -FailureMessage "Unable to peel the authoritative baseline tag."
if ($baselineTagCommit -ne $expectedBaselineTagCommit) {
    throw "Authoritative baseline tag peeled to an unexpected commit: $baselineTagCommit"
}

$gitStatusOutput = Invoke-CandidateProcess -FileName $git -Arguments @(
    "-c", "core.quotepath=false", "status", "--porcelain=v1", "--untracked-files=all"
) -FailureMessage "Unable to verify the candidate source tree."
$unexpectedGitStatus = @(
    $gitStatusOutput -split "`r?`n" | Where-Object {
        -not [string]::IsNullOrWhiteSpace($_) -and
        $_ -notmatch '^\?\? \.codex(?:/|$)'
    }
)
if ($unexpectedGitStatus.Count -ne 0) {
    throw "Refusing to attribute a candidate build to a dirty Git tree: $($unexpectedGitStatus -join '; ')"
}

$candidateRoot = [System.IO.Path]::GetFullPath((Join-Path $root "build\workflow_candidate"))
$expectedCandidateRoot = [System.IO.Path]::GetFullPath((Join-Path $root "build\workflow_candidate"))
if ($candidateRoot -ne $expectedCandidateRoot) {
    throw "Candidate build root resolution is inconsistent."
}
if ([System.IO.Path]::GetFileName($candidateRoot) -ne "workflow_candidate") {
    throw "Refusing to clean an unexpected build directory."
}
$buildParent = [System.IO.Path]::GetFullPath((Split-Path -Parent $candidateRoot))
$expectedBuildParent = [System.IO.Path]::GetFullPath((Join-Path $root "build"))
if ($buildParent -ne $expectedBuildParent) {
    throw "Refusing to clean a candidate directory outside the workspace build root."
}
Assert-NotCandidateReparsePoint -Path $root -Label "workspace root"
Assert-NotCandidateReparsePoint -Path $expectedBuildParent -Label "workspace build root"
Assert-NotCandidateReparsePoint -Path $candidateRoot -Label "candidate build root"
if (Test-Path -LiteralPath $candidateRoot) {
    Remove-Item -LiteralPath $candidateRoot -Recurse -Force
}

$artifactDir = Join-Path $candidateRoot "artifact"
$workDir = Join-Path $candidateRoot "pyinstaller-work"
$specDir = Join-Path $candidateRoot "spec"
$releaseDir = Join-Path $candidateRoot "release"
$probeWorkspace = Join-Path $candidateRoot "probe-workspace"
$acceptanceWorkspace = Join-Path $candidateRoot "acceptance-workspace"
foreach ($path in @($artifactDir, $workDir, $specDir, $releaseDir)) {
    New-Item -Path $path -ItemType Directory -Force | Out-Null
}

$sciValues = @($SciWritingRoot, $SciFiguresRoot, $SciJournalAuthorRoot)
$sciSupplied = @($sciValues | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
if ($sciSupplied -ne 0 -and $sciSupplied -ne 3) {
    throw "SCI acceptance roots must be supplied together."
}
if ($sciSupplied -eq 3) {
    foreach ($path in $sciValues) {
        if (-not (Test-Path -LiteralPath $path -PathType Container)) {
            throw "SCI acceptance root is unavailable: $path"
        }
    }
}

Write-Host "Running isolated candidate tests..."
Invoke-CandidateProcess -FileName $python -Arguments @(
    "-m", "pytest", "-q", "-p", "no:cacheprovider",
    "tests/test_workflow_candidate.py", "tests/test_workflow_candidate_build.py"
) -FailureMessage "Candidate tests failed; packaging was not started." | Write-Host

$pyInstallerVersion = Invoke-CandidateProcess -FileName $python -Arguments @(
    "-m", "PyInstaller", "--version"
) -FailureMessage "PyInstaller is unavailable."
if ($pyInstallerVersion.Trim() -ne $expectedPyInstallerVersion) {
    throw "PyInstaller version mismatch: $pyInstallerVersion"
}

$entryPoint = Join-Path $root "tools\workflow_candidate.py"
$versionFile = Join-Path $root "tools\windows_workflow_candidate_version_info.txt"
$undergraduateAssets = Join-Path $root "unified_paper_orchestrator\workflow\packs\prompts\undergraduate"
$addData = "$undergraduateAssets;unified_paper_orchestrator/workflow/packs/prompts/undergraduate"
$pyInstallerArguments = @(
    "-m", "PyInstaller",
    "--noconfirm", "--clean", "--onefile", "--console", "--noupx",
    "--name", "GenericWorkflowCandidate",
    "--distpath", $artifactDir,
    "--workpath", $workDir,
    "--specpath", $specDir,
    "--paths", $root,
    "--version-file", $versionFile,
    "--add-data", $addData,
    "--hidden-import", "unified_paper_orchestrator.workflow.packs.examples",
    "--hidden-import", "unified_paper_orchestrator.workflow.packs.undergraduate",
    "--hidden-import", "unified_paper_orchestrator.workflow.packs.sci",
    "--hidden-import", "unified_paper_orchestrator.workflow.examples",
    "--exclude-module", "PySide6",
    "--exclude-module", "qasync",
    "--exclude-module", "unified_paper_orchestrator.app",
    "--exclude-module", "unified_paper_orchestrator.orchestrator",
    $entryPoint
)

Write-Host "Building isolated pure-console candidate..."
Invoke-CandidateProcess -FileName $python -Arguments $pyInstallerArguments `
    -FailureMessage "PyInstaller candidate build failed." | Write-Host

$builtExe = Join-Path $artifactDir "GenericWorkflowCandidate.exe"
if (-not (Test-Path -LiteralPath $builtExe -PathType Leaf)) {
    throw "Candidate executable was not produced."
}
$releaseExe = Join-Path $releaseDir "GenericWorkflowCandidate.exe"
Copy-Item -LiteralPath $builtExe -Destination $releaseExe

$versionProbe = Invoke-CandidateProcess -FileName $releaseExe -Arguments @("--version") `
    -FailureMessage "Packaged candidate --version probe failed."
if ($versionProbe -ne "GenericWorkflowCandidate 0.13.1-candidate") {
    throw "Packaged candidate version probe returned an unexpected value: $versionProbe"
}

$initProbe = Invoke-CandidateProcess -FileName $releaseExe -Arguments @(
    "--workspace", $probeWorkspace, "--locale", "zh-CN", "init"
) -FailureMessage "Packaged candidate init probe failed." | ConvertFrom-Json
if ($initProbe.status -ne "initialized" -or $initProbe.installed_workflows -ne 6) {
    throw "Packaged candidate init probe did not install six workflows."
}
$listProbe = Invoke-CandidateProcess -FileName $releaseExe -Arguments @(
    "--workspace", $probeWorkspace, "--locale", "en-US", "list"
) -FailureMessage "Packaged candidate list probe failed." | ConvertFrom-Json
if ($listProbe.workflows.Count -ne 6 -or $listProbe.locale -ne "en-US") {
    throw "Packaged candidate list/localization probe failed."
}
$createProbe = Invoke-CandidateProcess -FileName $releaseExe -Arguments @(
    "--workspace", $probeWorkspace, "--locale", "en-US", "demo-create",
    "--idempotency-key", "packaged-restart-probe"
) -FailureMessage "Packaged candidate demo-create probe failed." | ConvertFrom-Json
$runId = [string]$createProbe.run.run_id
if ([string]::IsNullOrWhiteSpace($runId)) {
    throw "Packaged candidate did not return a run id."
}
$tickProbe = Invoke-CandidateProcess -FileName $releaseExe -Arguments @(
    "--workspace", $probeWorkspace, "--locale", "en-US", "tick", $runId
) -FailureMessage "Packaged candidate tick probe failed." | ConvertFrom-Json
if ($tickProbe.scheduler.completed -ne 1) {
    throw "Packaged candidate bounded tick did not complete exactly one node."
}
$resumeProbe = Invoke-CandidateProcess -FileName $releaseExe -Arguments @(
    "--workspace", $probeWorkspace, "--locale", "zh-CN", "resume", $runId
) -FailureMessage "Packaged candidate restart/resume probe failed." | ConvertFrom-Json
if ($resumeProbe.status -ne "completed") {
    throw "Packaged candidate did not recover the run in a new process."
}
$timelineProbe = Invoke-CandidateProcess -FileName $releaseExe -Arguments @(
    "--workspace", $probeWorkspace, "--locale", "en-US", "timeline", $runId
) -FailureMessage "Packaged candidate timeline probe failed." | ConvertFrom-Json
if ($timelineProbe.run_id -ne $runId -or $timelineProbe.status -ne "completed") {
    throw "Packaged candidate timeline probe is inconsistent."
}

$acceptanceArguments = @(
    "--workspace", $acceptanceWorkspace, "--locale", "en-US", "accept-offline",
    "--report", "reports/offline-acceptance.json"
)
if ($sciSupplied -eq 3) {
    $acceptanceArguments += @(
        "--sci-writing-root", [System.IO.Path]::GetFullPath($SciWritingRoot),
        "--sci-figures-root", [System.IO.Path]::GetFullPath($SciFiguresRoot),
        "--sci-journal-author-root", [System.IO.Path]::GetFullPath($SciJournalAuthorRoot)
    )
}
$acceptanceProbe = Invoke-CandidateProcess -FileName $releaseExe -Arguments $acceptanceArguments `
    -FailureMessage "Packaged offline acceptance probe failed." | ConvertFrom-Json
if ($acceptanceProbe.offline_candidate_acceptance -eq "FAIL") {
    throw "Packaged offline candidate acceptance failed."
}
if (
    $acceptanceProbe.online_production_acceptance.status -ne "NOT_RUN" -or
    $acceptanceProbe.online_production_acceptance.production_equivalence -ne $false -or
    $acceptanceProbe.production_promotion_eligible -ne $false
) {
    throw "Packaged candidate misstated its online/production boundary."
}
if ($sciSupplied -eq 3) {
    $sciCheck = @($acceptanceProbe.checks | Where-Object {
        $_.id -eq "two-subject-sci-four-workflow-smoke"
    })
    if ($sciCheck.Count -ne 1 -or $sciCheck[0].status -ne "PASS") {
        throw "Packaged SCI contract simulation did not pass."
    }
}

$acceptanceReport = Join-Path $acceptanceWorkspace "reports\offline-acceptance.json"
$releaseAcceptanceReport = Join-Path $releaseDir "offline-acceptance.json"
Copy-Item -LiteralPath $acceptanceReport -Destination $releaseAcceptanceReport

$baselineAfter = Get-CandidateFingerprint -Path $baselinePath
$productionDistAfter = Get-CandidateFingerprint -Path $productionDistPath
Assert-CandidateFingerprintUnchanged -Before $baselineBefore -After $baselineAfter `
    -Label "Authoritative baseline"
Assert-CandidateFingerprintUnchanged -Before $productionDistBefore -After $productionDistAfter `
    -Label "Production dist executable"

$gitHead = Invoke-CandidateProcess -FileName $git -Arguments @("rev-parse", "HEAD") `
    -FailureMessage "Unable to record Git HEAD."
$releaseFingerprint = Get-CandidateFingerprint -Path $releaseExe
$reportFingerprint = Get-CandidateFingerprint -Path $releaseAcceptanceReport
$sha256Path = Join-Path $releaseDir "GenericWorkflowCandidate.exe.sha256"
"$($releaseFingerprint.sha256)  GenericWorkflowCandidate.exe" | Set-Content `
    -LiteralPath $sha256Path -Encoding Ascii
$rollbackDrill = [ordered]@{
    schema = "unified-paper-orchestrator/generic-workflow-candidate-rollback-drill/v1"
    created_at = [DateTimeOffset]::UtcNow.ToString("o")
    mode = "read-only-verification"
    result = "PASS"
    authoritative_baseline = [ordered]@{
        path = $baselinePath
        version_argument = "--version"
        observed_version = $baselineVersionProbe
        expected_product_version = "0.12.0b3"
        sha256 = $baselineAfter.sha256
        expected_sha256 = $expectedBaselineSha256
        size = $baselineAfter.size
    }
    rollback_tag = [ordered]@{
        name = $baselineTag
        peeled_commit = $baselineTagCommit
        expected_commit = $expectedBaselineTagCommit
    }
    production_dist = [ordered]@{
        before = $productionDistBefore
        after = $productionDistAfter
        unchanged = $true
    }
    candidate = [ordered]@{
        path = $releaseExe
        sha256 = $releaseFingerprint.sha256
    }
    operations = [ordered]@{
        git_switch_performed = $false
        production_file_replacement_performed = $false
        dist_write_performed = $false
    }
    conclusion = "The preserved 0.12.0b3 binary and rollback tag were verified without switching Git or replacing production files."
}
$rollbackDrillPath = Join-Path $releaseDir "rollback-drill.json"
$rollbackDrill | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $rollbackDrillPath -Encoding UTF8
$manifest = [ordered]@{
    schema = "unified-paper-orchestrator/generic-workflow-candidate-build/v1"
    candidate_version = "0.13.1-candidate"
    created_at = [DateTimeOffset]::UtcNow.ToString("o")
    git_head = $gitHead
    executable = [ordered]@{
        path = $releaseExe
        sha256 = $releaseFingerprint.sha256
        size = $releaseFingerprint.size
        console = $true
        production_entry = $false
    }
    offline_acceptance = [ordered]@{
        path = $releaseAcceptanceReport
        sha256 = $reportFingerprint.sha256
        status = $acceptanceProbe.offline_candidate_acceptance
        sci_contract_simulation = if ($sciSupplied -eq 3) { "PASS" } else { "NOT_RUN" }
    }
    online_production_acceptance = "NOT_RUN"
    production_promotion_eligible = $false
    authoritative_baseline = $baselineAfter
    production_dist_unchanged = $true
    sha256_file = $sha256Path
    rollback_drill = $rollbackDrillPath
}
$manifestPath = Join-Path $releaseDir "build-manifest.json"
$manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

Write-Host "Candidate build completed."
Write-Host "EXE: $releaseExe"
Write-Host "SHA256: $($releaseFingerprint.sha256)"
Write-Host "Acceptance: $releaseAcceptanceReport"
Write-Host "Manifest: $manifestPath"
Write-Host "Rollback drill: $rollbackDrillPath"
