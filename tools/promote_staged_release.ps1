param(
    [Parameter(Mandatory = $true)][string]$ExpectedVersion,
    [Parameter(Mandatory = $true)][string]$ExpectedSha256,
    [int]$TimeoutSeconds = 86400
)

$ErrorActionPreference = "Stop"
$root = [System.IO.Path]::GetFullPath(
    (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
)
$distRoot = [System.IO.Path]::GetFullPath((Join-Path $root "dist"))
$buildRoot = [System.IO.Path]::GetFullPath((Join-Path $root "build"))
$canonical = [System.IO.Path]::GetFullPath(
    (Join-Path $distRoot "UnifiedPaperOrchestrator.exe")
)
$staged = [System.IO.Path]::GetFullPath(
    (Join-Path $buildRoot "release_candidate\UnifiedPaperOrchestrator.exe")
)
$temporary = [System.IO.Path]::GetFullPath(
    (Join-Path $distRoot "UnifiedPaperOrchestrator.exe.$ExpectedVersion.new")
)
$backup = [System.IO.Path]::GetFullPath(
    (Join-Path $distRoot "UnifiedPaperOrchestrator.exe.pre-$ExpectedVersion.old")
)
$stateRoot = Join-Path $root "state"
$logPath = Join-Path $stateRoot "release_promotion_$ExpectedVersion.log"

function Assert-ChildPath([string]$Path, [string]$Parent) {
    $prefix = $Parent.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if (-not $Path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing release operation outside expected root: $Path"
    }
}

function Write-PromotionLog([string]$Message) {
    New-Item -Path $stateRoot -ItemType Directory -Force | Out-Null
    Add-Content -LiteralPath $logPath -Encoding UTF8 -Value (
        "{0} {1}" -f [DateTimeOffset]::Now.ToString("o"), $Message
    )
}

Assert-ChildPath -Path $canonical -Parent $distRoot
Assert-ChildPath -Path $temporary -Parent $distRoot
Assert-ChildPath -Path $backup -Parent $distRoot
Assert-ChildPath -Path $staged -Parent $buildRoot

$expectedHash = $ExpectedSha256.Trim().ToUpperInvariant()
if (-not (Test-Path -LiteralPath $staged -PathType Leaf)) {
    throw "Staged executable is missing: $staged"
}
$stagedHash = (Get-FileHash -LiteralPath $staged -Algorithm SHA256).Hash.ToUpperInvariant()
$stagedVersion = (Get-Item -LiteralPath $staged).VersionInfo.ProductVersion
if ($stagedHash -ne $expectedHash -or $stagedVersion -ne $ExpectedVersion) {
    throw "Staged executable identity check failed."
}

Write-PromotionLog "waiting_for_running_${ExpectedVersion}_predecessor_to_exit"
$started = [DateTimeOffset]::Now
while ($true) {
    $runningCanonical = @()
    foreach ($process in @(Get-Process -Name "UnifiedPaperOrchestrator" -ErrorAction SilentlyContinue)) {
        try {
            $processPath = [System.IO.Path]::GetFullPath($process.Path)
            if ([string]::Equals(
                $processPath,
                $canonical,
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
                $runningCanonical += $process
            }
        }
        catch {
            $runningCanonical += $process
        }
    }
    if ($runningCanonical.Count -eq 0) {
        break
    }
    if (([DateTimeOffset]::Now - $started).TotalSeconds -ge $TimeoutSeconds) {
        Write-PromotionLog "timed_out_without_replacing_canonical_exe"
        exit 3
    }
    Start-Sleep -Seconds 2
}

if (Test-Path -LiteralPath $temporary -PathType Leaf) {
    throw "Temporary release file already exists: $temporary"
}
if (Test-Path -LiteralPath $backup -PathType Leaf) {
    throw "Release backup file already exists: $backup"
}

Copy-Item -LiteralPath $staged -Destination $temporary
try {
    if (Test-Path -LiteralPath $canonical -PathType Leaf) {
        Move-Item -LiteralPath $canonical -Destination $backup
    }
    Move-Item -LiteralPath $temporary -Destination $canonical
    $publishedHash = (Get-FileHash -LiteralPath $canonical -Algorithm SHA256).Hash.ToUpperInvariant()
    $publishedVersion = (Get-Item -LiteralPath $canonical).VersionInfo.ProductVersion
    if ($publishedHash -ne $expectedHash -or $publishedVersion -ne $ExpectedVersion) {
        throw "Published executable identity check failed."
    }
    if (Test-Path -LiteralPath $backup -PathType Leaf) {
        Remove-Item -LiteralPath $backup -Force
    }
    Remove-Item -LiteralPath $staged -Force
    $candidateDir = Split-Path -Parent $staged
    if ((Test-Path -LiteralPath $candidateDir -PathType Container) -and
        -not (Get-ChildItem -LiteralPath $candidateDir -Force)) {
        Remove-Item -LiteralPath $candidateDir -Force
    }
    if ((Test-Path -LiteralPath $buildRoot -PathType Container) -and
        -not (Get-ChildItem -LiteralPath $buildRoot -Force)) {
        Remove-Item -LiteralPath $buildRoot -Force
    }
    Write-PromotionLog "published_$ExpectedVersion sha256=$publishedHash"
}
catch {
    if (-not (Test-Path -LiteralPath $canonical -PathType Leaf) -and
        (Test-Path -LiteralPath $backup -PathType Leaf)) {
        Move-Item -LiteralPath $backup -Destination $canonical
    }
    Write-PromotionLog "promotion_failed error_type=$($_.Exception.GetType().Name)"
    throw
}
