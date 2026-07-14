param([string]$Workspace)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
function Invoke-Native {
    param([string]$File, [string[]]$Arguments)
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$File failed with exit code $LASTEXITCODE"
    }
}
if (-not $Workspace) {
    $Workspace = Join-Path $root ".release-demo\workspace"
}
if (Test-Path -LiteralPath $Workspace) {
    throw "Demo workspace already exists: $Workspace"
}
New-Item -ItemType Directory -Path $Workspace | Out-Null
Copy-Item -Path (Join-Path $PSScriptRoot "repository\*") -Destination $Workspace -Recurse
Invoke-Native git @("-C", $Workspace, "init", "--quiet")
Invoke-Native git @("-C", $Workspace, "config", "user.name", "CodexCLI Demo")
Invoke-Native git @("-C", $Workspace, "config", "user.email", "codexcli-demo@example.invalid")
Invoke-Native git @("-C", $Workspace, "add", "README.md")
Invoke-Native git @("-C", $Workspace, "commit", "--quiet", "-m", "Initialize release demo")
Invoke-Native codexcli @("doctor", "--repo", $Workspace)
Write-Output "Submit the request in examples/release-demo/feature-request.md."
Invoke-Native codexcli @("run", "--repo", $Workspace)
