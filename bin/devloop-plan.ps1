[CmdletBinding()]
param(
    [Alias('h')]
    [switch] $Help,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $RemainingArgs
)

$ErrorActionPreference = 'Stop'

$bundleRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $bundleRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Dev Loop runtime is missing or damaged. Rerun install\devloop.ps1 to repair it.'
}

$pythonPath = Join-Path $bundleRoot 'src'
$env:PYTHONPATH = if ([string]::IsNullOrWhiteSpace($env:PYTHONPATH)) {
    $pythonPath
}
else {
    "$pythonPath$([IO.Path]::PathSeparator)$env:PYTHONPATH"
}
$env:DEVLOOP_UI_MODE = if (
    -not [Console]::IsInputRedirected -and
    -not [Console]::IsOutputRedirected
) {
    'application'
}
else {
    'plain'
}

if ($Help) {
    & $python -m devloop.interactive_runner --help
    exit $LASTEXITCODE
}

& $python -m devloop.interactive_runner @RemainingArgs
exit $LASTEXITCODE
