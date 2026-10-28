# Reproducible local dependencies. No global Python installation is changed.
param([string]$Python = "python")
$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$sdkTarget = Join-Path $projectRoot "runtime\sdk-0.147.0"
$nativeTarget = Join-Path $projectRoot "runtime\codex-0.153.4"
if (-not (Test-Path -LiteralPath (Join-Path $sdkTarget "openai_codex-0.147.0.dist-info\METADATA"))) {
    & $Python -m pip install --only-binary=:all: --target $sdkTarget "openai-codex==0.147.0" "pydantic==2.13.5"
    if ($LASTEXITCODE -ne 0) { throw "Codex Python SDK installation failed" }
}
# The SDK's required CLI 0.147.0 remains installed above. This independent
# native package is selected explicitly through CodexConfig.codex_bin.
if (-not (Test-Path -LiteralPath (Join-Path $nativeTarget "codex_cli_bin\bin\codex.exe"))) {
    & $Python -m pip install --no-deps --only-binary=:all: --target $nativeTarget "openai-codex-cli-bin==0.153.4"
    if ($LASTEXITCODE -ne 0) { throw "Codex native runtime installation failed" }
}
Write-Host "Prepared SDK 0.147.0 and application runtime 0.153.4 under runtime/."
