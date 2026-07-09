[CmdletBinding()]
param(
    [Alias('h')]
    [switch] $Help,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $RemainingArgs
)

$ErrorActionPreference = 'Stop'

function Show-DevLoopLogo {
    param(
        [Parameter(Mandatory = $true)]
        [string] $BundleRoot
    )

    $logoPath = Join-Path $BundleRoot 'docs\devloop-logo.txt'
    if (Test-Path -LiteralPath $logoPath) {
        Get-Content -LiteralPath $logoPath | ForEach-Object { Write-Host $_ }
        Write-Host ''
    }
}

function Get-DevLoopPython {
    $candidates = @('python', 'python3', 'py')

    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($null -eq $command) {
            continue
        }

        try {
            & $candidate --version *> $null
            if ($LASTEXITCODE -eq 0) {
                return $candidate
            }
        }
        catch {
            continue
        }
    }

    throw 'Python 3.10+ was not found on PATH. Install Python and rerun this script.'
}

$bundleRoot = Split-Path -Parent $PSScriptRoot
Show-DevLoopLogo -BundleRoot $bundleRoot

$pythonPath = Join-Path $bundleRoot 'src'
$env:PYTHONPATH = if ([string]::IsNullOrWhiteSpace($env:PYTHONPATH)) {
    $pythonPath
}
else {
    "$pythonPath$([IO.Path]::PathSeparator)$env:PYTHONPATH"
}

$python = Get-DevLoopPython

if ($Help) {
    & $python -m devloop.interactive_runner --help
    exit $LASTEXITCODE
}

& $python -m devloop.interactive_runner @RemainingArgs
exit $LASTEXITCODE
