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
    $developmentSetup = Join-Path $bundleRoot 'install\setup-development.ps1'
    if (-not (Test-Path -LiteralPath $developmentSetup -PathType Leaf)) {
        throw "Dev Loop runtime and bootstrap script are missing from $bundleRoot"
    }
    Write-Host 'Dev Loop runtime not found; preparing the checkout-local runtime.'
    & $developmentSetup
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Dev Loop could not prepare its checkout-local runtime.'
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
