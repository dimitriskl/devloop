param([switch]$Reply)

$ErrorActionPreference = 'Stop'
$runnerPath = Join-Path $PSScriptRoot 'bin\devloop.ps1'
$transcriptPath = Join-Path $PSScriptRoot 'feedback-handoff-live.log'
$prdPath = 'E:\LocalCode\eConnectorV2\prd\dynamic-query-delta-timestamp\dynamic-query-delta-timestamp.md'

Start-Transcript -Path $transcriptPath -Append
try {
    [string[]]$replyArguments = @()
    if ($Reply) {
        $replyArguments += '--reply'
    }
    & $runnerPath --prd $prdPath @replyArguments
}
finally {
    Stop-Transcript
}
