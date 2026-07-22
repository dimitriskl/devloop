# Remove artifacts created by the portable Dev Loop installer.
#
# The source checkout, project PRDs, worktrees, and branches are never removed.

[CmdletBinding()]
param(
    [string] $InstallDir = $(if ($env:DEVLOOP_INSTALL_DIR) { $env:DEVLOOP_INSTALL_DIR } else { Split-Path -Parent $PSScriptRoot }),
    [string] $BinDir = $(if ($env:DEVLOOP_BIN_DIR) { $env:DEVLOOP_BIN_DIR } else { Join-Path $env:USERPROFILE '.local\bin' }),
    [string] $CodexSkillsPath = "$env:USERPROFILE\.codex\skills",
    [string] $CodexAgentsPath = "$env:USERPROFILE\.codex\agents",
    [switch] $KeepSkills,
    [switch] $Help
)

$ErrorActionPreference = 'Stop'
$bundleRoot = Split-Path -Parent $PSScriptRoot

function Show-Usage {
    @'
Usage: uninstall-devloop.ps1 [options]

Remove artifacts created by the portable Dev Loop installer while preserving
the source checkout, project PRDs, worktrees, and branches.

Options:
  -InstallDir PATH       Bundle whose local runtime should be removed
  -BinDir PATH           Command-launcher directory (default: ~/.local/bin)
  -CodexSkillsPath PATH  Installed Codex skills directory
  -CodexAgentsPath PATH  Installed Codex agent-reference directory
  -KeepSkills            Keep installed Codex skills and agent references
  -Help                  Show this help
'@ | Write-Host
}

function Resolve-SafeInstallDirectory {
    param([string] $Path)

    $resolved = [System.IO.Path]::GetFullPath($Path)
    $root = [System.IO.Path]::GetPathRoot($resolved)
    if ($resolved.TrimEnd('\') -ieq $root.TrimEnd('\')) {
        throw "devloop-uninstall: refusing to use filesystem root '$resolved' as the install directory"
    }
    return $resolved.TrimEnd('\')
}

function Remove-LocalRuntime {
    param([string] $Root)

    foreach ($name in @('.venv', '.venv.next', '.venv.previous')) {
        $path = Join-Path $Root $name
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Recurse -Force
            Write-Host "Removed runtime artifact: $path"
        }
    }
}

function Test-ManagedCommandLauncher {
    param(
        [string] $Path,
        [string] $CommandName
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }
    $content = Get-Content -LiteralPath $Path -Raw
    $scriptName = [regex]::Escape("$CommandName.ps1")
    $pattern = "(?im)^\s*(?:powershell|pwsh)(?:\.exe)?\s+.*-File\s+`"[^`"]*\\bin\\$scriptName`"\s+%\*\s*$"
    return $content -match $pattern
}

function Remove-CommandLaunchers {
    param([string] $Directory)

    foreach ($name in @('devloop', 'devloop-plan')) {
        $launcher = Join-Path $Directory "$name.cmd"
        if (Test-ManagedCommandLauncher -Path $launcher -CommandName $name) {
            Remove-Item -LiteralPath $launcher -Force
            Write-Host "Removed command launcher: $launcher"
        }
    }
}

function Remove-EmptyCommandDirectoryFromPath {
    param([string] $Directory)

    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) {
        return
    }
    if (@(Get-ChildItem -LiteralPath $Directory -Force).Count -ne 0) {
        return
    }

    Remove-Item -LiteralPath $Directory -Force
    if ($env:DEVLOOP_TESTING -eq '1') {
        return
    }

    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ([string]::IsNullOrWhiteSpace($userPath)) {
        return
    }
    $remaining = @(
        $userPath -split ';' |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) -and $_.TrimEnd('\') -ine $Directory.TrimEnd('\') }
    )
    [Environment]::SetEnvironmentVariable('Path', ($remaining -join ';'), 'User')
    Write-Host "Removed empty command directory from user PATH: $Directory"
}

function Get-Sha256 {
    param([string] $Path)

    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        return [System.BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '')
    }
    finally {
        $stream.Dispose()
        $algorithm.Dispose()
    }
}

function Remove-MatchingInstalledFiles {
    param(
        [string] $SourceRoot,
        [string] $DestinationRoot
    )

    if (-not (Test-Path -LiteralPath $SourceRoot -PathType Container)) {
        return
    }
    $sourcePrefix = $SourceRoot.TrimEnd('\') + '\'
    foreach ($sourceFile in Get-ChildItem -LiteralPath $SourceRoot -File -Recurse) {
        $relative = $sourceFile.FullName.Substring($sourcePrefix.Length)
        $destination = Join-Path $DestinationRoot $relative
        if (-not (Test-Path -LiteralPath $destination -PathType Leaf)) {
            continue
        }
        if ((Get-Sha256 -Path $sourceFile.FullName) -ne (Get-Sha256 -Path $destination)) {
            Write-Host "Kept modified capability: $destination"
            continue
        }
        Remove-Item -LiteralPath $destination -Force
        Write-Host "Removed installed capability: $destination"
    }

    Get-ChildItem -LiteralPath $SourceRoot -Directory -Recurse |
        Sort-Object { $_.FullName.Length } -Descending |
        ForEach-Object {
            $relative = $_.FullName.Substring($sourcePrefix.Length)
            $installedDirectory = Join-Path $DestinationRoot $relative
            if (
                (Test-Path -LiteralPath $installedDirectory -PathType Container) -and
                @(Get-ChildItem -LiteralPath $installedDirectory -Force).Count -eq 0
            ) {
                Remove-Item -LiteralPath $installedDirectory -Force
            }
        }
}

if ($Help) {
    Show-Usage
    return
}

$InstallDir = Resolve-SafeInstallDirectory -Path $InstallDir
$BinDir = [System.IO.Path]::GetFullPath($BinDir)

Remove-LocalRuntime -Root $InstallDir
Remove-CommandLaunchers -Directory $BinDir
Remove-EmptyCommandDirectoryFromPath -Directory $BinDir

if (-not $KeepSkills) {
    Remove-MatchingInstalledFiles `
        -SourceRoot (Join-Path $bundleRoot 'skills\codex') `
        -DestinationRoot $CodexSkillsPath
    Remove-MatchingInstalledFiles `
        -SourceRoot (Join-Path $bundleRoot 'agents\codex') `
        -DestinationRoot $CodexAgentsPath
}

Write-Host ''
Write-Host 'Dev Loop installer artifacts were removed.'
Write-Host "Source checkout preserved: $InstallDir"
