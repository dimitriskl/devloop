# Skills And Agents

The bundle includes copied Codex skills and Codex agent-reference files.

Use the helper script:

Windows:

```powershell
.\install\install-skills.ps1
```

Ubuntu/Linux:

```bash
chmod +x ./install/install-skills.sh
./install/install-skills.sh
```

Or install manually:

## Install Codex Skills

Windows:

```powershell
$target = "$env:USERPROFILE\.codex\skills"
New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item -Recurse -Force .\skills\codex\* $target
```

Ubuntu/Linux:

```bash
mkdir -p "$HOME/.codex/skills"
cp -R ./skills/codex/* "$HOME/.codex/skills/"
```

## Install Codex Agent References

Windows:

```powershell
$target = "$env:USERPROFILE\.codex\agents"
New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item -Force .\agents\codex\*.md $target
```

Ubuntu/Linux:

```bash
mkdir -p "$HOME/.codex/agents"
cp ./agents/codex/*.md "$HOME/.codex/agents/"
```

The runner can also read the bundled copies directly through the preset, so
global installation is useful but not required for the loop prompts.
