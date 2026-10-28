param([string]$Python = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe')
$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$runtime = Join-Path $workspace 'runtime\pandoc-3.11'
$url = 'https://github.com/jgm/pandoc/releases/download/3.11/pandoc-3.11-windows-x86_64.zip'
$expectedHash = '2ab72baf2399450e148ddf7a2a8689806c42e1bba71862b57e220fd9b8456d3d'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
$archive = Join-Path $runtime 'download.zip'
if (-not (Test-Path -LiteralPath $archive)) { Invoke-WebRequest -Uri $url -OutFile $archive }
$actualHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualHash -ne $expectedHash) { throw 'Pandoc archive hash mismatch. No files were extracted.' }
if (-not (Test-Path -LiteralPath (Join-Path $runtime 'pandoc-3.11\pandoc.exe'))) {
    Expand-Archive -LiteralPath $archive -DestinationPath $runtime
}
@{version='3.11'; url=$url; sha256=$actualHash} | ConvertTo-Json | Set-Content (Join-Path $runtime 'provenance.json') -Encoding UTF8
& $Python -m pip install --target (Join-Path $workspace 'runtime\word-python') 'python-docx==1.2.0' 'lxml==6.0.2'
if ($LASTEXITCODE -ne 0) { throw 'Word Python dependencies could not be prepared.' }
Write-Output 'Word export runtime ready. Microsoft Word is required for native Windows pagination and PDF rendering.'
