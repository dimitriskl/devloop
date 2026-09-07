[CmdletBinding()]
param(
    [string] $InstallDir,
    [string] $BinDir,
    [string] $CodexSkillsPath,
    [string] $CodexAgentsPath,
    [switch] $KeepSkills,
    [switch] $Help
)
$ErrorActionPreference = 'Stop'
if ($Help) {
    Write-Host 'Usage: uninstall-devloop.ps1 [-InstallDir PATH] [-KeepSkills] [-CodexSkillsPath PATH] [-CodexAgentsPath PATH]'
    Write-Host 'Interrupted uninstall resumes its original bound plan; incompatible retry options fail before mutation.'
    return
}
$retryArguments = @()
foreach ($entry in @(
    @('InstallDir', '--install-root', $InstallDir),
    @('BinDir', '--bin-directory', $BinDir),
    @('CodexSkillsPath', '--skills-destination', $CodexSkillsPath),
    @('CodexAgentsPath', '--agents-destination', $CodexAgentsPath)
)) {
    if ($PSBoundParameters.ContainsKey($entry[0])) {
        if ([string]::IsNullOrWhiteSpace($entry[2])) { throw "Dev Loop uninstall requires a path for $($entry[0])." }
        $retryArguments += @($entry[1], $entry[2])
    }
}
if ($KeepSkills) { $retryArguments += '--keep-capabilities' }
$python = $null
foreach ($candidate in @('python', 'python3', 'py')) {
    if ($null -eq (Get-Command $candidate -ErrorAction SilentlyContinue)) { continue }
    & $candidate -B -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' *> $null
    if ($LASTEXITCODE -eq 0) { $python = $candidate; break }
}
if ($null -eq $python) { throw 'Dev Loop uninstall requires Python 3.10+.' }
$recoveryProbe = @'
import hashlib, json, pathlib, re, stat, sys
base = pathlib.Path(sys.argv[1]).absolute()
if (base / 'journal.json').is_file():
    staging = base
else:
    install = base.parent
    lock = install.parent / ('.' + install.name + '.install-lock') / 'owner.json'
    if not lock.is_file():
        raise SystemExit(0)
    owner = json.loads(lock.read_text(encoding='utf-8'))
    if owner.get('operation') != 'uninstall':
        raise SystemExit(0)
    identity = owner.get('transaction_id', '')
    if re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}', identity) is None:
        raise RuntimeError('uninstall recovery lock identity is invalid')
    staging = install.parent / ('.' + install.name + '.uninstall-' + identity)
    if not (staging / 'journal.json').is_file():
        raise SystemExit(0)
journal = json.loads((staging / 'journal.json').read_text(encoding='utf-8'))
install = pathlib.Path(journal['install_root'])
if not install.is_absolute() or staging != install.parent / ('.' + install.name + '.uninstall-' + journal['transaction_id']):
    raise RuntimeError('uninstall recovery path is not owned')
evidence = json.loads((staging / 'action-evidence.json').read_text(encoding='utf-8'))
if any(evidence[key] != journal[key] for key in ('install_root', 'transaction_id', 'plan_hash', 'layout_hash')):
    raise RuntimeError('uninstall recovery evidence mismatch')
for name in ('transaction.py', 'verify.py'):
    path = staging / name
    for ancestor in (path, *path.parents):
        status = ancestor.lstat()
        if stat.S_ISLNK(status.st_mode) or getattr(status, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise RuntimeError('uninstall recovery rejects linked paths')
    expected = evidence['layout']['assets']['bootstrap/' + name]
    if hashlib.sha256(path.read_bytes()).hexdigest().upper() != expected:
        raise RuntimeError('uninstall recovery executable was modified')
print(staging)
'@
$recoveryRoot = & $python -B -c $recoveryProbe $PSScriptRoot
if ($LASTEXITCODE -ne 0) { throw 'Dev Loop uninstall recovery validation failed.' }
if (-not [string]::IsNullOrWhiteSpace($recoveryRoot)) {
    & $python -B (Join-Path $recoveryRoot 'transaction.py') resume-uninstall $recoveryRoot --owner-pid $PID --protocol 2 @retryArguments
    exit $LASTEXITCODE
}
if ($InstallDir -and [IO.Path]::GetFullPath($InstallDir).TrimEnd('\') -ine (Split-Path -Parent $PSScriptRoot)) {
    throw 'Dev Loop uninstall install directory differs from this installed launcher.'
}
$arguments = @()
foreach ($entry in @(
    @('-BinDir', $BinDir),
    @('-CodexSkillsPath', $CodexSkillsPath),
    @('-CodexAgentsPath', $CodexAgentsPath)
)) {
    if (-not [string]::IsNullOrWhiteSpace($entry[1])) { $arguments += $entry }
}
if ($KeepSkills) { $arguments += '-KeepSkills' }
& (Join-Path (Split-Path -Parent $PSScriptRoot) 'bootstrap\dispatch.ps1') -Command uninstall -ArgumentsJson ($arguments | ConvertTo-Json -Compress)
exit $LASTEXITCODE
