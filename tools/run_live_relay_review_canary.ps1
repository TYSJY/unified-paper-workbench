param(
    [string]$PaperId = "2026-07-02_001_reconfiguration-debt-decoding",
    [string]$Model = "gpt-5.5",
    [string]$RetryTaskId = "",
    [string]$LoopbackProxy = "",
    [int]$TokenBudget = 50000,
    [int]$MaxOutputTokens = 8000,
    [int]$TimeoutSeconds = 600,
    [switch]$TransportProbeOnly,
    [switch]$TransportPreflightOnly,
    [switch]$PauseOnExit
)

$ErrorActionPreference = "Stop"
$relayBaseUrl = "https://www.qilinapi.com"
$relayResponsesUrl = "$relayBaseUrl/responses"
$relayAction = "writing.review_round_1.compatible_relay_canary"
$relayExecutor = "openai_compatible_relay_review"
$releasePaperId = "2026-07-02_001_reconfiguration-debt-decoding"
$root = [System.IO.Path]::GetFullPath((Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)))

if ($PaperId -cne $releasePaperId) {
    throw "The 0.4.4 release canary is pinned to $releasePaperId. Use the gateway CLI for other papers."
}
if ($RetryTaskId -and $RetryTaskId -notmatch '^task_[0-9a-f]{32}$') {
    throw "RetryTaskId is invalid."
}
if (-not $Model.Trim()) {
    throw "A relay model claim is required."
}

function Resolve-ValidatedLoopbackProxy([string]$Value) {
    if (-not $Value.Trim()) {
        return ""
    }
    try {
        $uri = [Uri]$Value.Trim()
    }
    catch {
        throw "Explicit relay proxy URL is invalid."
    }
    $proxyHost = $uri.DnsSafeHost.ToLowerInvariant()
    $hasExplicitPort = $uri.Authority -match ':\d+$'
    $address = $null
    $isLoopback = $proxyHost -eq "localhost"
    if (-not $isLoopback -and [Net.IPAddress]::TryParse($proxyHost, [ref]$address)) {
        $isLoopback = [Net.IPAddress]::IsLoopback($address)
    }
    if (
        -not $uri.IsAbsoluteUri -or
        $uri.Scheme -ne "http" -or
        -not $isLoopback -or
        -not $hasExplicitPort -or
        $uri.Port -le 0 -or
        $uri.UserInfo -or
        $uri.AbsolutePath -ne "/" -or
        $uri.Query -or
        $uri.Fragment
    ) {
        throw "Explicit relay proxy must be a credential-free HTTP loopback URL with an explicit port."
    }
    return $uri.GetLeftPart([UriPartial]::Authority).TrimEnd("/")
}

$proxyValidationError = ""
if (-not $LoopbackProxy.Trim()) {
    foreach ($name in @("HTTPS_PROXY", "ALL_PROXY", "HTTP_PROXY")) {
        $candidate = [Environment]::GetEnvironmentVariable($name, "Process")
        if (-not $candidate) {
            continue
        }
        try {
            $LoopbackProxy = Resolve-ValidatedLoopbackProxy $candidate
            break
        }
        catch {
            $proxyValidationError = $_.Exception.Message
            continue
        }
    }
}
else {
    $LoopbackProxy = Resolve-ValidatedLoopbackProxy $LoopbackProxy
}

if ($TransportProbeOnly) {
    [pscustomobject]@{
        transport_policy = $(
            if ($LoopbackProxy) {
                "explicit_loopback_proxy_no_env"
            }
            else {
                "direct_no_env_proxy"
            }
        )
        generic_proxy_environment_ignored = $true
        explicit_proxy_url_disclosed = $false
        proxy_validation_error = $proxyValidationError
    } | ConvertTo-Json
    exit 0
}

function Test-RelayAuthenticationBoundary([string]$ValidatedLoopbackProxy) {
    $curl = (Get-Command curl.exe -ErrorAction Stop).Source
    $arguments = @(
        "--silent",
        "--show-error",
        "--request", "POST",
        "--header", "Content-Type: application/json",
        "--data", "{}",
        "--write-out", "`n%{http_code}",
        "--connect-timeout", "10",
        "--max-time", "20",
        $relayResponsesUrl
    )
    if ($ValidatedLoopbackProxy) {
        $responseLines = @(& $curl --disable --noproxy= --proxy $ValidatedLoopbackProxy @arguments 2>$null)
    }
    else {
        $responseLines = @(& $curl --disable --noproxy "*" @arguments 2>$null)
    }
    $curlExitCode = $LASTEXITCODE
    if ($curlExitCode -ne 0 -or $responseLines.Count -lt 2) {
        throw "Relay transport preflight failed before any API key was requested."
    }
    $status = ([string]$responseLines[-1]).Trim()
    $body = [string]::Join("`n", @($responseLines[0..($responseLines.Count - 2)]))
    if ($status -ne "401" -or $body -notmatch '(?i)(API_KEY_REQUIRED|API[ -]?key|Authorization|authentication)') {
        throw "Relay preflight did not reach the expected unauthenticated 401 boundary; no task was created and no API key was requested."
    }
    return 401
}

$transportPreflightStatus = Test-RelayAuthenticationBoundary $LoopbackProxy
if ($TransportPreflightOnly) {
    [pscustomobject]@{
        transport_policy = $(
            if ($LoopbackProxy) {
                "explicit_loopback_proxy_no_env"
            }
            else {
                "direct_no_env_proxy"
            }
        )
        relay_unauthenticated_status = $transportPreflightStatus
        api_key_used = $false
        task_created = $false
    } | ConvertTo-Json
    exit 0
}

$exe = Join-Path $root "dist\UnifiedPaperOrchestrator.exe"
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "0.4.4 executable is unavailable: $exe"
}

Write-Warning "THIRD-PARTY RELAY: $relayBaseUrl is not OpenAI. Its claimed upstream model identity cannot be verified."
Write-Warning "The review prompt and selected paper data will be sent to this third-party relay."
if ($RetryTaskId) {
    Write-Warning "Retrying a prior API task can duplicate a request or charge."
    $confirmation = (Read-Host "Type RETRY to requeue the exact approved relay task").Trim()
    if ($confirmation -cne "RETRY") {
        throw "Relay retry was not acknowledged; no task was requeued and no API request was sent."
    }
}
else {
    $confirmation = (Read-Host "Type RELAY to acknowledge the third-party disclosure and create a required-approval task").Trim()
    if ($confirmation -cne "RELAY") {
        throw "Third-party relay disclosure was not acknowledged; no task was created and no API request was sent."
    }
}

$previousModel = [Environment]::GetEnvironmentVariable("UNIFIED_RELAY_MODEL", "Process")
$previousExplicitProxy = [Environment]::GetEnvironmentVariable("UNIFIED_RELAY_HTTPS_PROXY", "Process")
$genericProxyNames = @("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
$previousGenericProxies = @{}
foreach ($name in $genericProxyNames) {
    $previousGenericProxies[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}

$secureKey = $null
$bstr = [IntPtr]::Zero
$plainKey = $null
$exitCode = 1
try {
    # Never inherit or restore a relay key. This run accepts only the hidden
    # value entered below, and clears it before verification and exit.
    [Environment]::SetEnvironmentVariable("UNIFIED_RELAY_API_KEY", $null, "Process")
    [Environment]::SetEnvironmentVariable("UNIFIED_RELAY_MODEL", $Model, "Process")
    foreach ($name in $genericProxyNames) {
        [Environment]::SetEnvironmentVariable($name, $null, "Process")
    }
    [Environment]::SetEnvironmentVariable(
        "UNIFIED_RELAY_HTTPS_PROXY",
        $(if ($LoopbackProxy) { $LoopbackProxy } else { $null }),
        "Process"
    )

    if ($RetryTaskId) {
        $taskId = $RetryTaskId
        $retrySourceLines = @(& $exe gateway-task $taskId)
        $retrySourceExitCode = $LASTEXITCODE
        $retrySourceText = [string]::Join("`n", $retrySourceLines).Trim()
        try {
            $retrySource = $retrySourceText | ConvertFrom-Json
        }
        catch {
            throw "Relay retry source returned an invalid record."
        }
        if (
            $retrySourceExitCode -ne 2 -or
            $retrySource.task_id -ne $taskId -or
            $retrySource.action -ne $relayAction -or
            $retrySource.executor -ne $relayExecutor -or
            $retrySource.status -notin @("blocked", "retryable_failure") -or
            $retrySource.approval.status -ne "approved"
        ) {
            throw "Relay retry source is not an approved failed task."
        }
        $retryLines = @(
            & $exe gateway-retry $taskId `
                --acknowledge-possible-duplicate-charge
        )
        $retryExitCode = $LASTEXITCODE
        $retryText = [string]::Join("`n", $retryLines).Trim()
        if ($retryText) {
            Write-Host $retryText
        }
        try {
            $retried = $retryText | ConvertFrom-Json
        }
        catch {
            throw "Relay retry returned an invalid record."
        }
        if ($retryExitCode -ne 0 -or $retried.task_id -ne $taskId -or $retried.status -ne "queued") {
            throw "Relay task was not safely requeued; no API request was sent."
        }
    }
    else {
        $createArguments = @(
            "gateway-relay-review-canary",
            $PaperId,
            "--model", $Model,
            "--token-budget", [string]$TokenBudget,
            "--max-output-tokens", [string]$MaxOutputTokens,
            "--timeout-seconds", [string]$TimeoutSeconds
        )
        $createLines = @(& $exe @createArguments)
        $createExitCode = $LASTEXITCODE
        $createText = [string]::Join("`n", $createLines).Trim()
        if ($createText) {
            Write-Host $createText
        }
        if ($createExitCode -ne 2) {
            throw "Relay task creation did not stop at the required approval boundary."
        }
        try {
            $created = $createText | ConvertFrom-Json
        }
        catch {
            throw "Relay task creation returned an invalid record."
        }
        if (
            $created.status -ne "waiting_approval" -or
            $created.action -ne $relayAction -or
            $created.executor -ne $relayExecutor -or
            $created.approval.status -ne "pending"
        ) {
            throw "Relay task did not preserve its required-approval route contract."
        }
        $taskId = ([string]$created.task_id).Trim()
        $intentSha256 = ([string]$created.approval.intent_sha256).Trim().ToLowerInvariant()
        if ($taskId -notmatch '^task_[0-9a-f]{32}$' -or $intentSha256 -notmatch '^[0-9a-f]{64}$') {
            throw "Relay task approval identity is invalid."
        }

        Write-Host "Approval intent:"
        Write-Host ($created.approval.intent | ConvertTo-Json -Depth 8 -Compress)
        Write-Host "Approval intent SHA256: $intentSha256"

        $approvalLines = @(
            & $exe gateway-approve $taskId `
                --note "Explicit third-party relay canary approval" `
                --intent-sha256 $intentSha256
        )
        $approvalExitCode = $LASTEXITCODE
        $approvalText = [string]::Join("`n", $approvalLines).Trim()
        if ($approvalText) {
            Write-Host $approvalText
        }
        if ($approvalExitCode -ne 0) {
            throw "Relay task approval failed; no API request was sent."
        }
        try {
            $approved = $approvalText | ConvertFrom-Json
        }
        catch {
            throw "Relay task approval returned an invalid record."
        }
        if ($approved.task_id -ne $taskId -or $approved.status -ne "queued") {
            throw "Relay task was not durably approved and queued; no API request was sent."
        }
    }

    $secureKey = Read-Host "Newly rotated relay API key (hidden; never written to disk)" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    if (-not $plainKey) {
        throw "A newly rotated relay API key is required."
    }
    [Environment]::SetEnvironmentVariable("UNIFIED_RELAY_API_KEY", $plainKey, "Process")

    $dispatchLines = @(& $exe gateway-dispatch $taskId)
    $dispatchExitCode = $LASTEXITCODE

    # Remove secret material before parsing, printing, or running the verifier.
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
    if ($exitCode -eq 0) {
        $python = (Get-Command python.exe -ErrorAction Stop).Source
        & $python (Join-Path $root "tools\verify_release_044.py") `
            --expected-canary-task-id $taskId `
            --expected-paper-id $PaperId
        $exitCode = $LASTEXITCODE
    }
}
catch {
    Write-Host "Relay canary failed: $($_.Exception.Message)" -ForegroundColor Red
    $exitCode = 1
}
finally {
    [Environment]::SetEnvironmentVariable("UNIFIED_RELAY_API_KEY", $null, "Process")
    [Environment]::SetEnvironmentVariable("UNIFIED_RELAY_MODEL", $previousModel, "Process")
    [Environment]::SetEnvironmentVariable("UNIFIED_RELAY_HTTPS_PROXY", $previousExplicitProxy, "Process")
    foreach ($name in $genericProxyNames) {
        [Environment]::SetEnvironmentVariable($name, $previousGenericProxies[$name], "Process")
    }
    $plainKey = $null
    if ($bstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        $bstr = [IntPtr]::Zero
    }
    $secureKey = $null
    if ($PauseOnExit) {
        Write-Host "Relay canary finished with exit code $exitCode."
        [void](Read-Host "Press Enter to close this window")
    }
}

exit $exitCode
