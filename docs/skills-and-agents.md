# Skills And Agents

The bundle includes copied Codex skills and Claude agent files.

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

## Install Claude Agents

Windows:

```powershell
$target = "$env:USERPROFILE\.claude\agents"
New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item -Force .\agents\claude\*.md $target
```

Ubuntu/Linux:

```bash
mkdir -p "$HOME/.claude/agents"
cp ./agents/claude/*.md "$HOME/.claude/agents/"
```

The runner can also read the bundled copies directly through the preset, so
global installation is useful but not required for the loop prompts.

