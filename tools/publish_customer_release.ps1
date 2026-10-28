[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ControlPlaneUrl,
    [Parameter(Mandatory = $true)]
    [string]$AdminApiKeyPath,
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [ValidatePattern("^[a-z0-9][a-z0-9._-]{0,31}$")]
    [string]$Channel = "preview",
    [string]$MinimumSupportedVersion = "0.11.6",
    [string]$NotesUrl = "",
    [string]$ArtifactKey = "",
    [ValidateRange(5, 128)]
    [int]$PartSizeMiB = 16,
    [ValidateRange(1, 8)]
    [int]$PartUploadAttempts = 4,
    [switch]$Mandatory,
    [switch]$AllowHttpLoopback
)

$ErrorActionPreference = "Stop"

function Test-LoopbackUri([Uri]$Uri) {
    return $Uri.Host -in @("127.0.0.1", "localhost", "::1")
}

function Read-RequiredSecret([string]$Path) {
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "Administrator key file is unavailable: $resolved"
    }
    $secret = (Get-Content -LiteralPath $resolved -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($secret)) {
        throw "Administrator key file is empty."
    }
    return $secret
}

function ConvertTo-CompactJson([object]$Value) {
    return $Value | ConvertTo-Json -Depth 20 -Compress
}

$baseText = $ControlPlaneUrl.Trim().TrimEnd("/")
$baseUri = $null
if (
    -not [Uri]::TryCreate(
        $baseText,
        [UriKind]::Absolute,
        [ref]$baseUri
    )
) {
    throw "Control-plane URL is invalid."
}
$isLoopback = Test-LoopbackUri -Uri $baseUri
if ($baseUri.Scheme -ne "https") {
    if (-not ($AllowHttpLoopback -and $isLoopback -and $baseUri.Scheme -eq "http")) {
        throw "Release publication requires HTTPS. Use -AllowHttpLoopback only for a local test server."
    }
}

$installer = [System.IO.Path]::GetFullPath($InstallerPath)
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
    throw "Installer is unavailable: $installer"
}
if ($Version -notmatch "^\d+\.\d+\.\d+(?:b\d+)?$") {
    throw "Version is invalid."
}
if ($MinimumSupportedVersion -notmatch "^\d+\.\d+\.\d+(?:b\d+)?$") {
    throw "Minimum supported version is invalid."
}

$artifactName = [System.IO.Path]::GetFileName($installer)
if ([string]::IsNullOrWhiteSpace($ArtifactKey)) {
    $ArtifactKey = "releases/$Channel/$Version/$artifactName"
}
if (
    -not $ArtifactKey.StartsWith("releases/") -or
    $ArtifactKey.Contains("..") -or
    $ArtifactKey -notmatch "^[A-Za-z0-9._/-]{1,240}$"
) {
    throw "Artifact key is invalid."
}
if ([string]::IsNullOrWhiteSpace($NotesUrl)) {
    $NotesUrl = $baseUri.GetLeftPart([UriPartial]::Authority).TrimEnd("/") + "/download"
}
$parsedNotes = $null
if (
    -not [Uri]::TryCreate(
        $NotesUrl,
        [UriKind]::Absolute,
        [ref]$parsedNotes
    ) -or
    $parsedNotes.Scheme -ne "https"
) {
    throw "Release notes URL must be an absolute HTTPS URL."
}

$adminKey = Read-RequiredSecret -Path $AdminApiKeyPath
$installerItem = Get-Item -LiteralPath $installer
$installerSize = [Int64]$installerItem.Length
$sha256 = (
    Get-FileHash -LiteralPath $installer -Algorithm SHA256
).Hash.ToLowerInvariant()

Add-Type -AssemblyName System.Net.Http
$handler = [System.Net.Http.HttpClientHandler]::new()
if ($isLoopback) {
    $handler.UseProxy = $false
}
$client = [System.Net.Http.HttpClient]::new($handler)
$client.Timeout = [TimeSpan]::FromMinutes(30)
$client.DefaultRequestHeaders.Add("X-Admin-Key", $adminKey)
$client.DefaultRequestHeaders.UserAgent.ParseAdd(
    "UnifiedPaperReleasePublisher/$Version"
)

function Invoke-ControlPlaneJson {
    param(
        [Parameter(Mandatory = $true)]
        [System.Net.Http.HttpMethod]$Method,
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [object]$Body = $null
    )
    $request = [System.Net.Http.HttpRequestMessage]::new(
        $Method,
        $baseText + $Path
    )
    try {
        if ($null -ne $Body) {
            $request.Content = [System.Net.Http.StringContent]::new(
                (ConvertTo-CompactJson -Value $Body),
                [System.Text.UTF8Encoding]::new($false),
                "application/json"
            )
        }
        $response = $client.SendAsync($request).GetAwaiter().GetResult()
        try {
            $responseText = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
            if (-not $response.IsSuccessStatusCode) {
                $preview = if ($responseText.Length -gt 1200) {
                    $responseText.Substring(0, 1200)
                }
                else {
                    $responseText
                }
                throw (
                    "Control-plane request failed: " +
                    [int]$response.StatusCode + " " +
                    $response.ReasonPhrase + " " +
                    $preview
                )
            }
            if ([string]::IsNullOrWhiteSpace($responseText)) {
                return [pscustomobject]@{}
            }
            return $responseText | ConvertFrom-Json
        }
        finally {
            $response.Dispose()
        }
    }
    finally {
        $request.Dispose()
    }
}

function Send-UploadPart {
    param(
        [Parameter(Mandatory = $true)]
        [string]$UploadId,
        [Parameter(Mandatory = $true)]
        [int]$PartNumber,
        [Parameter(Mandatory = $true)]
        [byte[]]$Bytes
    )
    $query = (
        "?artifact_key=" +
        [Uri]::EscapeDataString($ArtifactKey) +
        "&upload_id=" +
        [Uri]::EscapeDataString($UploadId) +
        "&part_number=" +
        $PartNumber
    )
    $lastError = $null
    for ($attempt = 1; $attempt -le $PartUploadAttempts; $attempt += 1) {
        $nonRetryable = $false
        $request = [System.Net.Http.HttpRequestMessage]::new(
            [System.Net.Http.HttpMethod]::Put,
            $baseText + "/admin/releases/upload/part" + $query
        )
        try {
            $request.Content = [System.Net.Http.ByteArrayContent]::new($Bytes)
            $request.Content.Headers.ContentType = (
                [System.Net.Http.Headers.MediaTypeHeaderValue]::new(
                    "application/octet-stream"
                )
            )
            $response = $client.SendAsync($request).GetAwaiter().GetResult()
            try {
                $responseText = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
                if ($response.IsSuccessStatusCode) {
                    return $responseText | ConvertFrom-Json
                }
                $lastError = (
                    "HTTP " + [int]$response.StatusCode + " " +
                    $response.ReasonPhrase
                )
                if (
                    [int]$response.StatusCode -lt 500 -and
                    [int]$response.StatusCode -ne 429
                ) {
                    $nonRetryable = $true
                    throw "Upload part $PartNumber was rejected: $lastError $responseText"
                }
            }
            finally {
                $response.Dispose()
            }
        }
        catch {
            $lastError = $_.Exception.Message
            if ($nonRetryable -or $attempt -eq $PartUploadAttempts) {
                throw
            }
        }
        finally {
            $request.Dispose()
        }
        if ($attempt -lt $PartUploadAttempts) {
            Start-Sleep -Seconds ([Math]::Min(8, [Math]::Pow(2, $attempt - 1)))
        }
    }
    throw "Upload part $PartNumber failed: $lastError"
}

$uploadId = ""
$uploadCompleted = $false
try {
    $created = Invoke-ControlPlaneJson `
        -Method ([System.Net.Http.HttpMethod]::Post) `
        -Path "/admin/releases/upload/create" `
        -Body @{
            artifact_key = $ArtifactKey
            content_type = "application/vnd.microsoft.portable-executable"
            version = $Version
        }
    $uploadId = [string]$created.upload_id
    $minimumPartSize = [Int64]$created.minimum_part_size
    if ([string]::IsNullOrWhiteSpace($uploadId) -or $minimumPartSize -le 0) {
        throw "Control plane returned an invalid multipart upload."
    }
    $partSize = [Math]::Max(
        $minimumPartSize,
        [Int64]$PartSizeMiB * 1024 * 1024
    )
    $partCount = [int][Math]::Ceiling($installerSize / [double]$partSize)
    $parts = [System.Collections.Generic.List[object]]::new()
    $stream = [System.IO.File]::Open(
        $installer,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
    )
    try {
        for ($partNumber = 1; $partNumber -le $partCount; $partNumber += 1) {
            $remaining = $installerSize - $stream.Position
            $currentSize = [int][Math]::Min($partSize, $remaining)
            $bytes = [byte[]]::new($currentSize)
            $offset = 0
            while ($offset -lt $currentSize) {
                $read = $stream.Read($bytes, $offset, $currentSize - $offset)
                if ($read -le 0) {
                    throw "Installer ended before upload part $partNumber was complete."
                }
                $offset += $read
            }
            Write-Progress `
                -Activity "Uploading customer release $Version" `
                -Status "Part $partNumber of $partCount" `
                -PercentComplete (
                    [Math]::Round(($partNumber - 1) * 100 / $partCount)
                )
            $uploaded = Send-UploadPart `
                -UploadId $uploadId `
                -PartNumber $partNumber `
                -Bytes $bytes
            $parts.Add(
                [pscustomobject]@{
                    part_number = [int]$uploaded.part_number
                    etag = [string]$uploaded.etag
                }
            )
        }
    }
    finally {
        $stream.Dispose()
        Write-Progress -Activity "Uploading customer release $Version" -Completed
    }

    $completed = Invoke-ControlPlaneJson `
        -Method ([System.Net.Http.HttpMethod]::Post) `
        -Path "/admin/releases/upload/complete" `
        -Body @{
            artifact_key = $ArtifactKey
            upload_id = $uploadId
            parts = @($parts)
        }
    if ([Int64]$completed.size -ne $installerSize) {
        throw "Completed artifact size does not match the installer."
    }
    $uploadCompleted = $true

    $published = Invoke-ControlPlaneJson `
        -Method ([System.Net.Http.HttpMethod]::Post) `
        -Path "/admin/releases/publish" `
        -Body @{
            version = $Version
            channel = $Channel
            artifact_key = $ArtifactKey
            artifact_name = $artifactName
            sha256 = $sha256
            size = $installerSize
            minimum_supported_version = $MinimumSupportedVersion
            mandatory = [bool]$Mandatory
            notes_url = $NotesUrl
        }
    $latest = Invoke-ControlPlaneJson `
        -Method ([System.Net.Http.HttpMethod]::Get) `
        -Path (
            "/releases/latest?channel=" +
            [Uri]::EscapeDataString($Channel)
        )
    $payload = $latest.release.payload
    if (
        [string]$payload.version -ne $Version -or
        [string]$payload.channel -ne $Channel -or
        [string]$payload.artifact.sha256 -ne $sha256 -or
        [Int64]$payload.artifact.size -ne $installerSize
    ) {
        throw "Published release could not be verified through the public metadata endpoint."
    }

    [ordered]@{
        schema = "uap.customer-release-publication/v1"
        version = $Version
        channel = $Channel
        artifact_key = $ArtifactKey
        artifact_name = $artifactName
        artifact_url = [string]$payload.artifact.url
        sha256 = $sha256
        size_bytes = $installerSize
        signing_key_id = [string]$latest.release.key_id
        published_at = [string]$payload.published_at
        mandatory = [bool]$payload.mandatory
        verified = $true
    } | ConvertTo-Json -Depth 10
}
catch {
    if (-not [string]::IsNullOrWhiteSpace($uploadId) -and -not $uploadCompleted) {
        try {
            Invoke-ControlPlaneJson `
                -Method ([System.Net.Http.HttpMethod]::Post) `
                -Path "/admin/releases/upload/abort" `
                -Body @{
                    artifact_key = $ArtifactKey
                    upload_id = $uploadId
                } | Out-Null
        }
        catch {
            Write-Warning "The incomplete multipart upload could not be aborted."
        }
    }
    throw
}
finally {
    $client.Dispose()
    $handler.Dispose()
    $adminKey = $null
}
