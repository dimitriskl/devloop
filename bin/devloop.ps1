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
# Dev Loop has a single interactive mode; there is no redirected-output variant.
$env:DEVLOOP_UI_MODE = 'application'

if ($Help) {
    & $python -B -m devloopv2 @RemainingArgs --help
    exit $LASTEXITCODE
}

& $python -B -m devloopv2 @RemainingArgs
exit $LASTEXITCODE
