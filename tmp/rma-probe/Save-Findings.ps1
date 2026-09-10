$ErrorActionPreference = 'Stop'
$rmaSourceFolder = 'E:\devloop-recovery-20260905\output\rma-analysis-20260909'
$rmaTargetRoot = 'E:\LocalCode\eConnectorV2\docs\eshop\Rma-Dev'
$rmaTargetFolder = Join-Path $rmaTargetRoot 'EBS-LIVE-20260909'
$rmaLogPath = Join-Path $rmaSourceFolder 'save-result.json'
$rmaNames = @('EBS-LIVE-FINDINGS.md', 'VIEW-COLUMNS.md', 'Swagger.json', 'PublicQueryInfo.json', 'PublicQueryLayout.json', 'SimpleScroller.json', 'StagingTableInfo.json', 'verification.json')
$rmaCopied = @()
try {
    if (-not (Test-Path -LiteralPath $rmaTargetRoot -PathType Container)) { throw 'Rma-Dev folder is missing' }
    foreach ($rmaName in $rmaNames) {
        $rmaSourceFile = Join-Path $rmaSourceFolder $rmaName
        $rmaTargetFile = Join-Path $rmaTargetFolder $rmaName
        if (-not (Test-Path -LiteralPath $rmaSourceFile -PathType Leaf)) { throw "Missing source: $rmaName" }
        if ((Test-Path -LiteralPath $rmaTargetFile) -and ((Get-FileHash -LiteralPath $rmaSourceFile).Hash -ne (Get-FileHash -LiteralPath $rmaTargetFile).Hash)) { throw "Different destination file exists: $rmaName" }
    }
    [void][IO.Directory]::CreateDirectory($rmaTargetFolder)
    foreach ($rmaName in $rmaNames) {
        $rmaSourceFile = Join-Path $rmaSourceFolder $rmaName
        $rmaTargetFile = Join-Path $rmaTargetFolder $rmaName
        if (-not (Test-Path -LiteralPath $rmaTargetFile)) { [IO.File]::Copy($rmaSourceFile, $rmaTargetFile, $false) }
        if ((Get-FileHash -LiteralPath $rmaSourceFile).Hash -ne (Get-FileHash -LiteralPath $rmaTargetFile).Hash) { throw "Hash verification failed: $rmaName" }
        $rmaCopied += $rmaName
    }
    $rmaHandoffPath = Join-Path $rmaTargetRoot 'HANDOFF.md'
    if (Test-Path -LiteralPath $rmaHandoffPath) {
        $rmaHandoffText = Get-Content -Raw -Encoding UTF8 -LiteralPath $rmaHandoffPath
        if (-not $rmaHandoffText.Contains('EBS-LIVE-20260909/EBS-LIVE-FINDINGS.md')) {
            Add-Content -LiteralPath $rmaHandoffPath -Encoding UTF8 -Value "`n## Live EBS analysis update - 2026-09-09`n`nRead [EBS live findings](EBS-LIVE-20260909/EBS-LIVE-FINDINGS.md) after this handoff. The user authorized read-only EBS inspection using the stored connection. Live metadata now establishes 66 view columns; the original no-EBS-calls statement is historical. All analysis-only constraints remain. No team answers, templates or automation executions have occurred. The dated folder includes question coverage, schema evidence and probe limitations.`n"
        }
    }
    [pscustomobject]@{ success=$true; destination=$rmaTargetFolder; verifiedFiles=$rmaCopied; utc=[DateTime]::UtcNow.ToString('o') } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $rmaLogPath -Encoding UTF8
    Write-Output "Saved and verified findings: $rmaTargetFolder"
    Write-Output "Result log: $rmaLogPath"
} catch {
    [pscustomobject]@{ success=$false; verifiedFiles=$rmaCopied; error=$_.Exception.Message } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $rmaLogPath -Encoding UTF8
    throw
}
