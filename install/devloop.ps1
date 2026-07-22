# Install or update the portable Dev Loop bundle.
#
# Quick install:
#   irm https://raw.githubusercontent.com/dimitriskl/devloop/main/install/devloop.ps1 | iex
#
# Update an existing install:
#   irm https://raw.githubusercontent.com/dimitriskl/devloop/main/install/devloop.ps1 | iex
#
# Environment overrides:
#   DEVLOOP_INSTALL_DIR  bundle location (skips prompt when set)
#   DEVLOOP_BIN_DIR      command directory (default: %USERPROFILE%\.local\bin)
#   DEVLOOP_REPO_URL     git clone URL (default: https://github.com/dimitriskl/devloop.git)
#   DEVLOOP_REF          branch or tag (default: main)

[CmdletBinding()]
param(
    [string] $InstallDir,
    [string] $BinDir = $(if ($env:DEVLOOP_BIN_DIR) { $env:DEVLOOP_BIN_DIR } else { Join-Path $env:USERPROFILE '.local\bin' }),
    [string] $RepoUrl = $(if ($env:DEVLOOP_REPO_URL) { $env:DEVLOOP_REPO_URL } else { 'https://github.com/dimitriskl/devloop.git' }),
    [string] $Ref = $(if ($env:DEVLOOP_REF) { $env:DEVLOOP_REF } else { 'main' }),
    [switch] $NoSkills,
    [switch] $NoBinLinks,
    [switch] $Help
)

$ErrorActionPreference = 'Stop'
$DefaultInstallDir = 'C:\devloop'

function Write-InstallLog {
    param([string] $Message)
    Write-Host "devloop-install: $Message"
}

function Show-Usage {
    @'
Usage: devloop.ps1 [options]

Install or update the portable Dev Loop bundle.

Options:
  -InstallDir PATH   Install directory (skips prompt; default: C:\devloop)
  -BinDir PATH       Directory for devloop commands (default: %USERPROFILE%\.local\bin)
  -Ref REF           Git branch or tag (default: main)
  -RepoUrl URL       Git repository URL
  -NoSkills          Skip copying bundled Codex skills and agents
  -NoBinLinks         Skip creating devloop command launchers
  -Help              Show this help

Environment:
  DEVLOOP_INSTALL_DIR, DEVLOOP_BIN_DIR, DEVLOOP_REPO_URL, DEVLOOP_REF

Examples:
  irm https://raw.githubusercontent.com/dimitriskl/devloop/main/install/devloop.ps1 | iex
  .\install\devloop.ps1 -Ref main
'@ | Write-Host
}

function Assert-CommandAvailable {
    param(
        [string] $Name,
        [string] $Hint
    )

    if ($null -eq (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "devloop-install: error: $Hint"
    }
}

function Get-DevLoopPython {
    foreach ($candidate in @('python', 'python3', 'py')) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($null -eq $command) {
            continue
        }

        try {
            & $candidate -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' *> $null
            if ($LASTEXITCODE -eq 0) {
                return $candidate
            }
        }
        catch {
            continue
        }
    }

    throw 'devloop-install: error: Python 3.10+ is required. Install Python and rerun this installer.'
}

function Resolve-InstallDir {
    if ($env:DEVLOOP_INSTALL_DIR) {
        return $env:DEVLOOP_INSTALL_DIR
    }
    if ($PSBoundParameters.ContainsKey('InstallDir')) {
        if ([string]::IsNullOrWhiteSpace($InstallDir)) {
            return $DefaultInstallDir
        }
        return $InstallDir
    }

    $reply = Read-Host "Install directory [$DefaultInstallDir]"
    if ([string]::IsNullOrWhiteSpace($reply)) {
        return $DefaultInstallDir
    }
    return $reply
}

function ConvertTo-AbsoluteInstallPath {
    param([string] $Path)

    try {
        if ([System.IO.Path]::IsPathRooted($Path)) {
            return [System.IO.Path]::GetFullPath($Path)
        }
        $providerPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
        return [System.IO.Path]::GetFullPath($providerPath)
    }
    catch {
        throw "devloop-install: error: Install directory '$Path' is not a valid filesystem path: $($_.Exception.Message)"
    }
}

function Assert-InstallRootAvailable {
    param([string] $Path)

    $root = [System.IO.Path]::GetPathRoot($Path)
    if ([string]::IsNullOrWhiteSpace($root)) {
        throw "devloop-install: error: Install directory '$Path' does not have a filesystem root."
    }

    $rootAvailable = $false
    try {
        $rootAvailable = Test-Path -LiteralPath $root -PathType Container
    }
    catch {
        $rootAvailable = $false
    }
    if (-not $rootAvailable) {
        throw "devloop-install: error: Install drive '$root' is not available for '$Path'. Choose a mounted, writable drive."
    }
}

function Assert-InstallParentWritable {
    param([string] $Path)

    $root = [System.IO.Path]::GetPathRoot($Path)
    $parent = Split-Path -Parent $Path
    if ([string]::IsNullOrWhiteSpace($parent)) {
        $parent = $root
    }

    $writeProbe = $null
    try {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
        $writeProbe = Join-Path $parent ".devloop-write-test-$([guid]::NewGuid().ToString('N'))"
        New-Item -ItemType Directory -Path $writeProbe | Out-Null
        New-Item -ItemType File -Path (Join-Path $writeProbe 'write-test') | Out-Null
    }
    catch {
        $failureMessage = $_.Exception.Message
        if ($null -ne $writeProbe -and (Test-Path -LiteralPath $writeProbe -ErrorAction SilentlyContinue)) {
            Remove-Item -LiteralPath $writeProbe -Recurse -Force -ErrorAction SilentlyContinue
        }
        throw "devloop-install: error: Install directory parent '$parent' cannot be created or written for '$Path': $failureMessage"
    }

    try {
        if (Test-Path -LiteralPath $writeProbe) {
            Remove-Item -LiteralPath $writeProbe -Recurse -Force
        }
    }
    catch {
        throw "devloop-install: error: Install directory parent '$parent' is not safely writable for '$Path': $($_.Exception.Message)"
    }
}

function Invoke-GitCloneAttempt {
    param([string[]] $Arguments)

    $previousErrorActionPreference = $ErrorActionPreference
    $nativeErrorPreference = Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue
    $previousNativeErrorPreference = if ($null -ne $nativeErrorPreference) {
        $nativeErrorPreference.Value
    }
    else {
        $null
    }
    try {
        $ErrorActionPreference = 'Continue'
        if ($null -ne $nativeErrorPreference) {
            $PSNativeCommandUseErrorActionPreference = $false
        }
        $outputLines = @(& git @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
        if ($null -ne $nativeErrorPreference) {
            $PSNativeCommandUseErrorActionPreference = $previousNativeErrorPreference
        }
    }
    foreach ($line in $outputLines) {
        Write-Host "$line"
    }

    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = $outputLines -join [Environment]::NewLine
    }
}

function Test-GitCloneRefFailure {
    param([string] $Output)

    return $Output -match '(Could not find remote branch .+ to clone|Remote branch .+ not found in upstream origin)'
}

function Remove-FailedCloneDirectory {
    param([string] $Path)

    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}

function Sync-Bundle {
    Assert-InstallRootAvailable -Path $InstallDir

    if (Test-Path -LiteralPath (Join-Path $InstallDir '.git')) {
        Write-InstallLog "Updating existing install at $InstallDir"
        & git -C $InstallDir fetch --depth 1 origin $Ref
        if ($LASTEXITCODE -ne 0) {
            throw "devloop-install: error: git fetch failed for $InstallDir"
        }
        & git -C $InstallDir reset --hard FETCH_HEAD
        if ($LASTEXITCODE -ne 0) {
            throw "devloop-install: error: git reset failed for $InstallDir"
        }
        & git -C $InstallDir clean -fd
        if ($LASTEXITCODE -ne 0) {
            throw "devloop-install: error: git clean failed for $InstallDir"
        }
        return
    }

    if (Test-Path -LiteralPath $InstallDir) {
        throw "devloop-install: error: Install directory exists but is not a git checkout: $InstallDir"
    }

    Write-InstallLog "Installing Dev Loop to $InstallDir"
    Assert-InstallParentWritable -Path $InstallDir

    $cloneResult = Invoke-GitCloneAttempt -Arguments @(
        'clone', '--depth', '1', '--branch', $Ref, $RepoUrl, $InstallDir
    )
    if ($cloneResult.ExitCode -ne 0) {
        Remove-FailedCloneDirectory -Path $InstallDir

        if (-not (Test-GitCloneRefFailure -Output $cloneResult.Output)) {
            throw "devloop-install: error: git clone failed for ref '$Ref' into '$InstallDir'. See the Git diagnostic above."
        }

        $fallbackResult = Invoke-GitCloneAttempt -Arguments @(
            'clone', '--depth', '1', $RepoUrl, $InstallDir
        )
        if ($fallbackResult.ExitCode -ne 0) {
            Remove-FailedCloneDirectory -Path $InstallDir
            throw "devloop-install: error: git clone fallback failed for ref '$Ref' into '$InstallDir'. See the Git diagnostic above."
        }
        & git -C $InstallDir checkout -f $Ref
        if ($LASTEXITCODE -ne 0) {
            Remove-FailedCloneDirectory -Path $InstallDir
            throw "devloop-install: error: git checkout failed for ref '$Ref' in '$InstallDir'"
        }
    }
}

function Install-BundledSkills {
    if ($NoSkills) {
        return
    }

    Write-InstallLog 'Installing bundled Codex skills and agent references'
    & (Join-Path $InstallDir 'install\install-skills.ps1')
    if ($LASTEXITCODE -ne 0) {
        throw 'devloop-install: error: install-skills.ps1 failed'
    }
}

function Install-PortableRuntime {
    if ($env:DEVLOOP_TESTING -eq '1') {
        return
    }

    $basePython = Get-DevLoopPython
    $runtimePath = Join-Path $InstallDir '.venv'
    $nextPath = Join-Path $InstallDir '.venv.next'
    $previousPath = Join-Path $InstallDir '.venv.previous'
    $lockPath = Join-Path $InstallDir 'requirements-portable.lock'

    Write-InstallLog 'Preparing isolated portable terminal runtime'
    if (Test-Path -LiteralPath $nextPath) {
        Remove-Item -LiteralPath $nextPath -Recurse -Force
    }
    & $basePython -m venv $nextPath
    if ($LASTEXITCODE -ne 0) {
        throw 'devloop-install: error: could not create the replacement runtime'
    }
    $nextPython = Join-Path $nextPath 'Scripts\python.exe'
    & $nextPython -m pip install --disable-pip-version-check --requirement $lockPath
    if ($LASTEXITCODE -ne 0) {
        throw 'devloop-install: error: portable runtime dependency installation failed'
    }
    & $nextPython -c 'import textual; raise SystemExit(0 if textual.__version__ == "8.2.8" else 1)'
    if ($LASTEXITCODE -ne 0) {
        throw 'devloop-install: error: portable runtime validation failed'
    }

    if (Test-Path -LiteralPath $previousPath) {
        Remove-Item -LiteralPath $previousPath -Recurse -Force
    }
    if (Test-Path -LiteralPath $runtimePath) {
        Move-Item -LiteralPath $runtimePath -Destination $previousPath
    }
    try {
        Move-Item -LiteralPath $nextPath -Destination $runtimePath
    }
    catch {
        if (Test-Path -LiteralPath $previousPath) {
            Move-Item -LiteralPath $previousPath -Destination $runtimePath
        }
        throw
    }
    if (Test-Path -LiteralPath $previousPath) {
        Remove-Item -LiteralPath $previousPath -Recurse -Force
    }
}

function Test-PathInUserPath {
    param([string] $Directory)

    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ([string]::IsNullOrWhiteSpace($userPath)) {
        return $false
    }

    return ($userPath -split ';' | Where-Object { $_.TrimEnd('\') -ieq $Directory.TrimEnd('\') }).Count -gt 0
}

function Add-BinDirToUserPath {
    if (Test-PathInUserPath -Directory $BinDir) {
        return
    }

    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $updated = if ([string]::IsNullOrWhiteSpace($userPath)) {
        $BinDir
    }
    else {
        "$BinDir;$userPath"
    }
    [Environment]::SetEnvironmentVariable('Path', $updated, 'User')
    Write-InstallLog "Added $BinDir to the user PATH. Open a new terminal to use devloop commands."
}

function New-CommandLauncher {
    param(
        [string] $Name,
        [string] $ScriptPath
    )

    $launcher = Join-Path $BinDir "$Name.cmd"
    $content = @"
@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "$ScriptPath" %*
"@
  Set-Content -LiteralPath $launcher -Value $content -Encoding ASCII
}

function Install-CommandLaunchers {
    if ($NoBinLinks) {
        return
    }

    Write-InstallLog "Creating command launchers in $BinDir"
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
    New-CommandLauncher -Name 'devloop' -ScriptPath (Join-Path $InstallDir 'bin\devloop.ps1')
    New-CommandLauncher -Name 'devloop-plan' -ScriptPath (Join-Path $InstallDir 'bin\devloop-plan.ps1')
    Add-BinDirToUserPath
}

function Show-NextSteps {
    $python = if ($env:DEVLOOP_TESTING -eq '1') {
        Get-DevLoopPython
    }
    else {
        Join-Path $InstallDir '.venv\Scripts\python.exe'
    }
    $pythonVersion = & $python --version 2>&1

    Write-Host ''
    Write-Host 'Dev Loop is installed at:'
    Write-Host "  $InstallDir"
    Write-Host ''
    Write-Host 'Commands:'
    Write-Host '  devloop --help'
    Write-Host '  devloop-plan --help'
    Write-Host ''

    if (-not $NoBinLinks -and -not (Test-PathInUserPath -Directory $BinDir)) {
        Write-Host 'Add this directory to your PATH:'
        Write-Host "  $BinDir"
        Write-Host ''
    }

    if ($null -eq (Get-Command codex -ErrorAction SilentlyContinue)) {
        Write-Host 'Codex CLI was not found on PATH. Install and authenticate Codex before running Dev Loop:'
        Write-Host '  codex --version'
        Write-Host '  codex login'
        Write-Host ''
    }

    Write-Host 'Optional isolated CodexCLI install from the bundle checkout:'
    Write-Host "  cd `"$InstallDir`" && uv tool install ."
    Write-Host ''
    Write-Host "Verified isolated runtime:"
    Write-Host "  $pythonVersion"
    Write-Host ''
    Write-Host 'Uninstall installer-managed artifacts while preserving source and project data:'
    Write-Host "  & '$InstallDir\install\uninstall-devloop.ps1' -InstallDir '$InstallDir'"
}

if ($Help) {
    Show-Usage
    return
}

Assert-CommandAvailable -Name 'git' -Hint 'Git is required. Install Git and rerun this installer.'
[void](Get-DevLoopPython)
$InstallDir = ConvertTo-AbsoluteInstallPath -Path (Resolve-InstallDir)
Sync-Bundle
Install-PortableRuntime
Install-BundledSkills
Install-CommandLaunchers
Show-NextSteps
