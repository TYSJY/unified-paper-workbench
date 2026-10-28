[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [string]$Root = "",
    [switch]$Generated,
    [switch]$StaleStateTemp,
    [switch]$PythonCaches
)

$ErrorActionPreference = "Stop"
if (-not $Root) {
    $Root = Split-Path -Parent $PSScriptRoot
}
$rootPath = (Resolve-Path -LiteralPath $Root).Path.TrimEnd("\")

function Resolve-SafeChild {
    param([Parameter(Mandatory = $true)][string]$Path)

    $candidate = [IO.Path]::GetFullPath($Path)
    $prefix = $rootPath + "\"
    if (-not $candidate.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to operate outside workspace: $candidate"
    }
    return $candidate
}

function Remove-SafeItem {
    param([Parameter(Mandatory = $true)][string]$Path)

    $target = Resolve-SafeChild -Path $Path
    if (-not (Test-Path -LiteralPath $target)) {
        return
    }
    if ($PSCmdlet.ShouldProcess($target, "Remove generated/runtime item")) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}

if (-not ($Generated -or $StaleStateTemp -or $PythonCaches)) {
    Write-Output "No cleanup category selected. Use -Generated, -StaleStateTemp, or -PythonCaches."
    Write-Output "Add -WhatIf to preview every removal."
    exit 0
}

if ($Generated) {
    Remove-SafeItem -Path (Join-Path $rootPath "build")
    Remove-SafeItem -Path (Join-Path $rootPath "dist")
    Remove-SafeItem -Path (Join-Path $rootPath "work\cdp-tools\node_modules")
    Get-ChildItem -LiteralPath $rootPath -File -Filter "*.spec" | ForEach-Object {
        Remove-SafeItem -Path $_.FullName
    }
}

if ($StaleStateTemp) {
    $smartRoot = Resolve-SafeChild -Path (Join-Path $rootPath "state\smart_submission")
    if (Test-Path -LiteralPath $smartRoot) {
        Get-ChildItem -LiteralPath $smartRoot -File | Where-Object {
            $_.Name -match '^smart_submission_state\.json\..+\.tmp$'
        } | ForEach-Object {
            Remove-SafeItem -Path $_.FullName
        }
    }
}

if ($PythonCaches) {
    Get-ChildItem -LiteralPath $rootPath -Directory -Recurse -Force | Where-Object {
        $_.Name -in @("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache")
    } | Sort-Object FullName -Descending | ForEach-Object {
        Remove-SafeItem -Path $_.FullName
    }
}
