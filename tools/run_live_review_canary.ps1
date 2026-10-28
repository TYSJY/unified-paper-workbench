param(
    [string]$PaperId = "2026-07-02_001_reconfiguration-debt-decoding",
    [string]$Model = "",
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
$root = [System.IO.Path]::GetFullPath((Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)))
$exe = Join-Path $root "dist\UnifiedPaperOrchestrator.exe"
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "0.4.4 executable is unavailable: $exe"
}
if (-not $Model.Trim()) {
    $Model = (Read-Host "OpenAI Responses API model ID").Trim()
}
if (-not $Model) {
    throw "A model ID is required."
}
if ($RetryTaskId -and $RetryTaskId -notmatch '^task_[0-9a-f]{32}$') {
    throw "RetryTaskId is invalid."
}

function Resolve-ValidatedLoopbackProxy([string]$Value) {
    if (-not $Value.Trim()) {
        return ""
    }
    try {
        $uri = [Uri]$Value.Trim()
    }
    catch {
        throw "Explicit OpenAI proxy URL is invalid."
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
        throw "Explicit OpenAI proxy must be a credential-free HTTP loopback URL with an explicit port."
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

function Test-OfficialOpenAITransport([string]$ValidatedLoopbackProxy) {
    $curl = (Get-Command curl.exe -ErrorAction Stop).Source
    if ($ValidatedLoopbackProxy) {
        $status = & $curl `
            --disable `
            --noproxy= `
            --proxy $ValidatedLoopbackProxy `
            --silent `
            --show-error `
            --output NUL `
            --write-out "%{http_code}" `
            --connect-timeout 10 `
            --max-time 20 `
            https://api.openai.com/v1/models 2>$null
    }
    else {
        $status = & $curl `
            --disable `
            --noproxy "*" `
            --silent `
            --show-error `
            --output NUL `
            --write-out "%{http_code}" `
            --connect-timeout 10 `
            --max-time 20 `
            https://api.openai.com/v1/models 2>$null
    }
    if ($LASTEXITCODE -ne 0 -or ([string]$status).Trim() -ne "401") {
        throw "Official OpenAI transport preflight failed; no task was requeued and no API key was requested."
    }
    return 401
}

$transportPreflightStatus = Test-OfficialOpenAITransport $LoopbackProxy
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
        official_unauthenticated_status = $transportPreflightStatus
        api_key_used = $false
        task_requeued = $false
    } | ConvertTo-Json
    exit 0
}

if ($RetryTaskId) {
    Write-Warning "The previous timeout may already have incurred an API charge."
    $confirmation = (Read-Host "Type RETRY to acknowledge a possible duplicate request and charge").Trim()
    if ($confirmation -cne "RETRY") {
        throw "Retry was not acknowledged; no task was requeued and no API request was sent."
    }
}

$secureKey = Read-Host "OpenAI API key (hidden; never written to disk)" -AsSecureString
$bstr = [IntPtr]::Zero
$plainKey = $null
$previousKey = [Environment]::GetEnvironmentVariable("OPENAI_API_KEY", "Process")
$previousModel = [Environment]::GetEnvironmentVariable("OPENAI_MODEL", "Process")
$previousBaseUrl = [Environment]::GetEnvironmentVariable("OPENAI_BASE_URL", "Process")
$previousExplicitProxy = [Environment]::GetEnvironmentVariable("UNIFIED_OPENAI_HTTPS_PROXY", "Process")
$genericProxyNames = @("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
$previousGenericProxies = @{}
foreach ($name in $genericProxyNames) {
    $previousGenericProxies[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}
$exitCode = 1
try {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    if (-not $plainKey) {
        throw "An API key is required."
    }
    [Environment]::SetEnvironmentVariable("OPENAI_API_KEY", $plainKey, "Process")
    [Environment]::SetEnvironmentVariable("OPENAI_MODEL", $Model, "Process")
    # The 0.4.4 executor also pins the official base URL internally. Clear an
    # inherited compatibility endpoint here so the process boundary is clear.
    [Environment]::SetEnvironmentVariable("OPENAI_BASE_URL", $null, "Process")
    # Generic proxy variables are never inherited by the executor.  This
    # script may copy one only after proving it is credential-free loopback.
    foreach ($name in $genericProxyNames) {
        [Environment]::SetEnvironmentVariable($name, $null, "Process")
    }
    [Environment]::SetEnvironmentVariable(
        "UNIFIED_OPENAI_HTTPS_PROXY",
        $(if ($LoopbackProxy) { $LoopbackProxy } else { $null }),
        "Process"
    )

    if ($RetryTaskId) {
        & $exe gateway-retry $RetryTaskId --acknowledge-possible-duplicate-charge
        $exitCode = $LASTEXITCODE
        if ($exitCode -eq 0) {
            & $exe gateway-dispatch $RetryTaskId
            $exitCode = $LASTEXITCODE
        }
    }
    else {
        & $exe gateway-review-canary $PaperId `
            --model $Model `
            --token-budget $TokenBudget `
            --max-output-tokens $MaxOutputTokens `
            --timeout-seconds $TimeoutSeconds `
            --execute
        $exitCode = $LASTEXITCODE
    }
    # Keep even a pre-existing process key out of the independent verifier.
    [Environment]::SetEnvironmentVariable("OPENAI_API_KEY", $null, "Process")
    $plainKey = $null
    if ($bstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        $bstr = [IntPtr]::Zero
    }

    if ($exitCode -eq 0) {
        # Verification runs while OPENAI_API_KEY is absent, even if this shell
        # had a different key before the canary started.
        $python = (Get-Command python.exe -ErrorAction Stop).Source
        & $python (Join-Path $root "tools\verify_release_044.py")
        $exitCode = $LASTEXITCODE
    }
}
finally {
    [Environment]::SetEnvironmentVariable("OPENAI_API_KEY", $previousKey, "Process")
    [Environment]::SetEnvironmentVariable("OPENAI_MODEL", $previousModel, "Process")
    [Environment]::SetEnvironmentVariable("OPENAI_BASE_URL", $previousBaseUrl, "Process")
    [Environment]::SetEnvironmentVariable("UNIFIED_OPENAI_HTTPS_PROXY", $previousExplicitProxy, "Process")
    foreach ($name in $genericProxyNames) {
        [Environment]::SetEnvironmentVariable($name, $previousGenericProxies[$name], "Process")
    }
    $plainKey = $null
    if ($bstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

if ($PauseOnExit) {
    Write-Host "Canary finished with exit code $exitCode."
    [void](Read-Host "Press Enter to close this window")
}

exit $exitCode
