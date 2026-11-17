param(
    [Parameter(Mandatory=$true)][string]$InputPath,
    [Parameter(Mandatory=$true)][string]$PdfPath
)
$ErrorActionPreference = 'Stop'
$sourceDocument = (Resolve-Path -LiteralPath $InputPath).Path
$targetPdf = [System.IO.Path]::GetFullPath($PdfPath)
if ([System.IO.Path]::GetExtension($sourceDocument) -ne '.docx') { throw 'Only DOCX inputs are supported.' }
if (Test-Path -LiteralPath $targetPdf) { throw 'PDF output must not already exist.' }
$wordInstance = $null
$wordDocument = $null
try {
    $wordInstance = New-Object -ComObject Word.Application
    $wordInstance.Visible = $false
    $wordInstance.DisplayAlerts = 0
    $wordInstance.AutomationSecurity = 3
    # A dedicated instance; input is read-only and is never saved back.
    $confirmConversions = $false
    $openReadOnly = $true
    $addRecent = $false
    $wordDocument = $wordInstance.Documents.Open([ref]$sourceDocument, [ref]$confirmConversions, [ref]$openReadOnly, [ref]$addRecent)
    $wordDocument.ExportAsFixedFormat($targetPdf, 17)
    if (-not (Test-Path -LiteralPath $targetPdf)) { throw 'Word did not export a PDF.' }
}
finally {
    if ($null -ne $wordDocument) {
        $saveChanges = 0
        $wordDocument.Close([ref]$saveChanges)
        [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($wordDocument)
    }
    if ($null -ne $wordInstance) {
        $saveChanges = 0
        $wordInstance.Quit([ref]$saveChanges)
        [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($wordInstance)
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
