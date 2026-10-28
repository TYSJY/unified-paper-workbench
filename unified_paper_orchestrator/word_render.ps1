param([Parameter(Mandatory=$true)][string]$Request)
$ErrorActionPreference = 'Stop'
$job = Get-Content -LiteralPath $Request -Raw -Encoding UTF8 | ConvertFrom-Json
$word = $null
$doc = $null
try {
    # Create a private automation instance; never attach to an open user document.
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0
    $word.AutomationSecurity = 3
    $docPath = [string]$job.docx
    $confirm = $false
    $readOnly = $false
    $addRecent = $false
    $doc = $word.Documents.Open([ref]$docPath, [ref]$confirm, [ref]$readOnly, [ref]$addRecent)
    $doc.Fields.Update() | Out-Null
    foreach ($toc in $doc.TablesOfContents) { $toc.Update() }
    $doc.Repaginate()
    foreach ($toc in $doc.TablesOfContents) { $toc.UpdatePageNumbers() }
    $doc.Save()
    $doc.ExportAsFixedFormat([string]$job.pdf, 17)
} finally {
    $saveChanges = 0
    if ($null -ne $doc) { $doc.Close([ref]$saveChanges); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($doc) }
    if ($null -ne $word) { $word.Quit([ref]$saveChanges); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($word) }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
