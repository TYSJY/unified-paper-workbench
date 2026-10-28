param(
    [Parameter(Mandatory = $true)]
    [string]$SourceExe,
    [Parameter(Mandatory = $true)]
    [string]$OutputPath,
    [string]$Version = "0.12.0b3",
    [string]$AdditionalPayloadDir = ""
)

$ErrorActionPreference = "Stop"

$root = [System.IO.Path]::GetFullPath(
    (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
)
$source = [System.IO.Path]::GetFullPath($SourceExe)
$output = [System.IO.Path]::GetFullPath($OutputPath)
$csc = "C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
$sevenZip = "C:\Program Files\7-Zip\7z.exe"
$icon = Join-Path $root "unified_paper_orchestrator\ui\assets\app_icon.ico"
$installerSource = Join-Path $root "tools\installer\UnifiedPaperInstaller.cs"
$uninstallerSource = Join-Path $root "tools\installer\UnifiedPaperUninstaller.cs"

if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
    throw "Customer EXE is unavailable: $source"
}
foreach ($required in @($csc, $sevenZip, $icon, $installerSource, $uninstallerSource)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Installer dependency is unavailable: $required"
    }
}
if ($Version -notmatch '^(\d+)\.(\d+)\.(\d+)b(\d+)$') {
    throw "Version must use the form 0.12.0b3."
}
$numericVersion = "{0}.{1}.{2}.{3}" -f $Matches[1], $Matches[2], $Matches[3], $Matches[4]

$tempRoot = [System.IO.Path]::GetFullPath(
    (Join-Path ([System.IO.Path]::GetTempPath()) (
        "UnifiedPaperInstaller-" + [Guid]::NewGuid().ToString("N")
    ))
)
$expectedTempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
if (-not $tempRoot.StartsWith($expectedTempRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Installer staging path escaped the temporary directory."
}
$payload = Join-Path $tempRoot "payload"
$stub = Join-Path $tempRoot "setup-stub.exe"
$uninstaller = Join-Path $payload "Uninstall.exe"
$installMarker = Join-Path $payload ".unified-paper-workbench-install"
$archive = Join-Path $tempRoot "payload.zip"
$assemblyInfo = Join-Path $tempRoot "AssemblyInfo.cs"

try {
    New-Item -ItemType Directory -Path $payload -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination (
        Join-Path $payload "UnifiedPaperOrchestrator.exe"
    )
    Copy-Item -LiteralPath (Join-Path $root "LICENSE") -Destination (
        Join-Path $payload "LICENSE.txt"
    )
    Copy-Item -LiteralPath (Join-Path $root "THIRD_PARTY_NOTICES.md") -Destination (
        Join-Path $payload "THIRD_PARTY_NOTICES.txt"
    )
    if (-not [string]::IsNullOrWhiteSpace($AdditionalPayloadDir)) {
        $additional = [System.IO.Path]::GetFullPath($AdditionalPayloadDir)
        if (-not (Test-Path -LiteralPath $additional -PathType Container)) {
            throw "Additional public payload directory is unavailable."
        }
        foreach ($entry in Get-ChildItem -LiteralPath $additional -Force) {
            if ($entry.Name -in @("UnifiedPaperOrchestrator.exe", "Uninstall.exe", ".unified-paper-workbench-install")) {
                throw "Additional payload cannot replace executable or installer ownership files."
            }
            Copy-Item -LiteralPath $entry.FullName -Destination $payload -Recurse
        }
    }
    [System.IO.File]::WriteAllText(
        $installMarker,
        "UnifiedPaperWorkbenchInstallRoot/v1",
        [System.Text.UTF8Encoding]::new($false)
    )

    @"
using System.Reflection;
[assembly: AssemblyCompany("Unified Paper")]
[assembly: AssemblyProduct("Unified Paper Workbench")]
[assembly: AssemblyVersion("$numericVersion")]
[assembly: AssemblyFileVersion("$numericVersion")]
[assembly: AssemblyInformationalVersion("$Version")]
"@ | Set-Content -LiteralPath $assemblyInfo -Encoding utf8

    $references = @(
        "/reference:System.dll",
        "/reference:System.Core.dll",
        "/reference:System.Windows.Forms.dll",
        "/reference:System.Drawing.dll"
        "/reference:System.Web.Extensions.dll"
    )
    & $csc /nologo /target:winexe /optimize+ /platform:anycpu `
        "/win32icon:$icon" "/out:$uninstaller" `
        @references $assemblyInfo $uninstallerSource
    if ($LASTEXITCODE -ne 0) {
        throw "Could not compile the uninstaller."
    }

    & $csc /nologo /target:winexe /optimize+ /platform:anycpu `
        "/win32icon:$icon" "/out:$stub" `
        @references `
        /reference:System.IO.Compression.dll `
        /reference:System.IO.Compression.FileSystem.dll `
        $assemblyInfo $installerSource
    if ($LASTEXITCODE -ne 0) {
        throw "Could not compile the installer."
    }

    Push-Location $payload
    try {
        & $sevenZip a -tzip $archive "*" -mx=9 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Could not create the installer payload."
        }
    }
    finally {
        Pop-Location
    }

    $outputDirectory = Split-Path -Parent $output
    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
    Copy-Item -LiteralPath $stub -Destination $output -Force
    $outputStream = [System.IO.File]::Open(
        $output,
        [System.IO.FileMode]::Append,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
    )
    try {
        $archiveBytes = [System.IO.File]::ReadAllBytes($archive)
        $outputStream.Write($archiveBytes, 0, $archiveBytes.Length)
        $writer = New-Object System.IO.BinaryWriter($outputStream)
        try {
            $writer.Write([Int64]$archiveBytes.Length)
            $writer.Write(
                [System.Text.Encoding]::ASCII.GetBytes("UPWPKG0120B3END")
            )
            $writer.Flush()
        }
        finally {
            $writer.Dispose()
        }
    }
    finally {
        $outputStream.Dispose()
    }

    $verify = Start-Process -FilePath $output -ArgumentList "/verify" `
        -WindowStyle Hidden -Wait -PassThru
    if ($verify.ExitCode -ne 0) {
        throw "The generated installer failed its embedded-payload verification."
    }

    $sourceHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant()
    $installerHash = (Get-FileHash -LiteralPath $output -Algorithm SHA256).Hash.ToLowerInvariant()
    [ordered]@{
        schema = "uap.customer-installer-build/v1"
        version = $Version
        source_exe = $source
        source_sha256 = $sourceHash
        output = $output
        output_sha256 = $installerHash
        size_bytes = (Get-Item -LiteralPath $output).Length
    } | ConvertTo-Json -Compress
}
finally {
    if (
        (Test-Path -LiteralPath $tempRoot) -and
        $tempRoot.StartsWith($expectedTempRoot, [StringComparison]::OrdinalIgnoreCase)
    ) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force
    }
}
