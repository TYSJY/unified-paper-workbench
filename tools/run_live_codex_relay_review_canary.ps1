param(
    [string]$PaperId = "2026-07-02_001_reconfiguration-debt-decoding",
    [string]$Model = "gpt-5.5",
    [string]$CodexCliPath = "",
    [string]$LoopbackProxy = "",
    [int]$TokenBudget = 50000,
    [int]$PosthocOutputTokenLimit = 8000,
    [int]$TimeoutSeconds = 600,
    [switch]$PauseOnExit
)

$ErrorActionPreference = "Stop"
$root = [System.IO.Path]::GetFullPath((Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)))
$exe = Join-Path $root "dist\UnifiedPaperOrchestrator.exe"
$expectedAction = "writing.review_round_1.qilin_codex_cli_canary"
$expectedExecutor = "qilin_codex_cli_review"
$genericProxyNames = @("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")

if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "0.4.4 executable is unavailable: $exe"
}
if (-not $Model.Trim()) {
    throw "An explicit relay model is required."
}

function Test-PathWithin([string]$Candidate, [string]$RootPath) {
    $candidatePath = [System.IO.Path]::GetFullPath($Candidate).TrimEnd('\')
    $rootFullPath = [System.IO.Path]::GetFullPath($RootPath).TrimEnd('\')
    return $candidatePath.Equals($rootFullPath, [StringComparison]::OrdinalIgnoreCase) -or
        $candidatePath.StartsWith("$rootFullPath\", [StringComparison]::OrdinalIgnoreCase)
}

function Test-StandaloneCodexCli([string]$Candidate) {
    if (-not $Candidate.Trim()) {
        return $false
    }
    try {
        $resolved = (Get-Item -LiteralPath $Candidate -ErrorAction Stop).FullName
    }
    catch {
        return $false
    }
    if (
        [System.IO.Path]::GetFileName($resolved) -cne "codex.exe" -or
        $resolved -match '(?i)\\WindowsApps\\' -or
        (Test-PathWithin $resolved (Join-Path $env:USERPROFILE ".codex"))
    ) {
        return $false
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $resolved
    return $signature.Status -eq "Valid" -and
        $signature.SignerCertificate.Subject -match '(?i)OpenAI'
}

function Resolve-StandaloneCodexCli([string]$RequestedPath) {
    if ($RequestedPath.Trim()) {
        $requested = [System.IO.Path]::GetFullPath($RequestedPath)
        if (-not (Test-StandaloneCodexCli $requested)) {
            throw "CodexCliPath must be a signed standalone OpenAI codex.exe outside WindowsApps and the Desktop .codex tree."
        }
        return $requested
    }

    $searchRoots = @(
        (Join-Path $root "state\v2\codex_cli"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"),
        (Join-Path $env:APPDATA "npm\node_modules\@openai\codex"),
        (Join-Path $env:LOCALAPPDATA "npm-cache")
    )
    foreach ($searchRoot in $searchRoots) {
        if (-not (Test-Path -LiteralPath $searchRoot -PathType Container)) {
            continue
        }
        $candidates = @(
            Get-ChildItem -LiteralPath $searchRoot -Filter "codex.exe" -File -Recurse -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -match '(?i)@openai.*vendor.*codex\\codex\.exe$' }
        )
        foreach ($candidate in $candidates) {
            if (Test-StandaloneCodexCli $candidate.FullName) {
                return $candidate.FullName
            }
        }
    }
    throw "No usable standalone Codex CLI was found. Reinstall @openai/codex, then pass its signed native codex.exe with -CodexCliPath. Do not use the Desktop WindowsApps binary or CC Switch."
}

function Resolve-LoopbackProxy([string]$Value) {
    if (-not $Value.Trim()) {
        return ""
    }
    try {
        $uri = [Uri]$Value.Trim()
    }
    catch {
        throw "Explicit relay proxy URL is invalid."
    }
    $address = $null
    $isLoopback = $uri.DnsSafeHost -eq "localhost"
    if (-not $isLoopback -and [Net.IPAddress]::TryParse($uri.DnsSafeHost, [ref]$address)) {
        $isLoopback = [Net.IPAddress]::IsLoopback($address)
    }
    if (
        -not $uri.IsAbsoluteUri -or
        $uri.Scheme -ne "http" -or
        -not $isLoopback -or
        $uri.Authority -notmatch ':\d+$' -or
        $uri.UserInfo -or
        $uri.AbsolutePath -ne "/" -or
        $uri.Query -or
        $uri.Fragment
    ) {
        throw "Explicit proxy must be a credential-free HTTP loopback URL with an explicit port."
    }
    return $uri.GetLeftPart([UriPartial]::Authority).TrimEnd("/")
}

$resolvedCodexCli = Resolve-StandaloneCodexCli $CodexCliPath
$validatedProxy = Resolve-LoopbackProxy $LoopbackProxy
$signature = Get-AuthenticodeSignature -LiteralPath $resolvedCodexCli
$binarySha256 = (Get-FileHash -LiteralPath $resolvedCodexCli -Algorithm SHA256).Hash.ToLowerInvariant()
$versionText = (& $resolvedCodexCli --version 2>$null | Select-Object -First 1).Trim()
if ($LASTEXITCODE -ne 0 -or $versionText -notmatch '^codex-cli\s+\d+\.\d+\.\d+') {
    throw "Standalone Codex CLI version could not be verified."
}

Write-Warning "Codex Desktop/ChatGPT is not used by this route. Its login, config, gpt-5.6-sol, Sol/Luna and ultra settings remain untouched."
Write-Warning "This task uses Qilin, an unverified third-party provider, with model claim '$Model' and CLI-only reasoning effort xhigh."
Write-Host "Standalone CLI: $resolvedCodexCli"
Write-Host "CLI version: $versionText"
Write-Host "CLI SHA256: $binarySha256"
Write-Host "Signer: $($signature.SignerCertificate.Subject)"

$previous = @{}
$managedEnvironment = @(
    "UNIFIED_RELAY_MODEL",
    "UNIFIED_RELAY_HTTPS_PROXY",
    "UNIFIED_CODEX_CLI_PATH",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY"
)
foreach ($name in $managedEnvironment) {
    $previous[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}

$secureKey = $null
$bstr = [IntPtr]::Zero
$plainKey = $null
$exitCode = 1
try {
    foreach ($name in $genericProxyNames) {
        [Environment]::SetEnvironmentVariable($name, $null, "Process")
    }
    [Environment]::SetEnvironmentVariable("UNIFIED_RELAY_API_KEY", $null, "Process")
    [Environment]::SetEnvironmentVariable("UNIFIED_RELAY_MODEL", $Model, "Process")
    [Environment]::SetEnvironmentVariable("UNIFIED_CODEX_CLI_PATH", $resolvedCodexCli, "Process")
    [Environment]::SetEnvironmentVariable(
        "UNIFIED_RELAY_HTTPS_PROXY",
        $(if ($validatedProxy) { $validatedProxy } else { $null }),
        "Process"
    )

    # The credential must be present before task creation because its
    # domain-separated fingerprint is part of the durable approval contract.
    $secureKey = Read-Host "Qilin API key (hidden; held only in process memory)" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    if (-not $plainKey) {
        throw "A Qilin API key is required."
    }
    [Environment]::SetEnvironmentVariable("UNIFIED_RELAY_API_KEY", $plainKey, "Process")

    $createLines = @(
        & $exe gateway-codex-relay-review-canary $PaperId `
            --model $Model `
            --codex-cli-path $resolvedCodexCli `
            --token-budget ([string]$TokenBudget) `
            --max-output-tokens ([string]$PosthocOutputTokenLimit) `
            --timeout-seconds ([string]$TimeoutSeconds)
    )
    $createExitCode = $LASTEXITCODE
    $createText = [string]::Join("`n", $createLines).Trim()
    if ($createText) {
        Write-Host $createText
    }
    $created = $createText | ConvertFrom-Json
    if (
        $createExitCode -ne 2 -or
        $created.status -ne "waiting_approval" -or
        $created.action -ne $expectedAction -or
        $created.executor -ne $expectedExecutor -or
        $created.approval.status -ne "pending"
    ) {
        throw "The isolated CLI task did not stop at its durable approval boundary."
    }
    $taskId = ([string]$created.task_id).Trim()
    $intentSha256 = ([string]$created.approval.intent_sha256).Trim().ToLowerInvariant()
    Write-Host "Approval intent:"
    Write-Host ($created.approval.intent | ConvertTo-Json -Depth 8)
    $confirmation = (Read-Host "Type APPROVE to authorize this exact CLI binary, key fingerprint, model, budget and Qilin disclosure").Trim()
    if ($confirmation -cne "APPROVE") {
        throw "Approval was not acknowledged; no provider request was sent."
    }

    $approvalLines = @(
        & $exe gateway-approve $taskId `
            --note "Explicit isolated Qilin Codex CLI approval" `
            --intent-sha256 $intentSha256
    )
    $approvalExitCode = $LASTEXITCODE
    $approved = ([string]::Join("`n", $approvalLines).Trim()) | ConvertFrom-Json
    if ($approvalExitCode -ne 0 -or $approved.status -ne "queued") {
        throw "Task approval failed; no provider request was sent."
    }

    $dispatchLines = @(& $exe gateway-dispatch $taskId)
    $dispatchExitCode = $LASTEXITCODE

    # Clear the plaintext credential before parsing or printing results.
    [Environment]::SetEnvironmentVariable("UNIFIED_RELAY_API_KEY", $null, "Process")
    $plainKey = $null
    if ($bstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        $bstr = [IntPtr]::Zero
    }

    $dispatchText = [string]::Join("`n", $dispatchLines).Trim()
    if ($dispatchText) {
        Write-Host $dispatchText
    }
    $exitCode = $dispatchExitCode
}
catch {
    Write-Host "Isolated Codex CLI canary failed: $($_.Exception.Message)" -ForegroundColor Red
    $exitCode = 1
}
finally {
    [Environment]::SetEnvironmentVariable("UNIFIED_RELAY_API_KEY", $null, "Process")
    $plainKey = $null
    if ($bstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        $bstr = [IntPtr]::Zero
    }
    $secureKey = $null
    foreach ($name in $managedEnvironment) {
        [Environment]::SetEnvironmentVariable($name, $previous[$name], "Process")
    }
    if ($PauseOnExit) {
        Write-Host "Isolated Codex CLI canary finished with exit code $exitCode."
        [void](Read-Host "Press Enter to close this window")
    }
}

exit $exitCode
