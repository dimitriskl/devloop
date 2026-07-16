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

## Drop-In Extensions

Dev Loop discovers agents and skills from the bundle at startup:

- A skill is a folder under `skills/codex/` containing a `SKILL.md` file.
- An agent is a single `.md` file under `agents/codex/`.

Copy a folder or file into place and it appears in the `/options` pickers on
the next planning session. Nothing needs registering.

Capability selection is per Workflow Step instance. Two Review steps do not
share a mutable profile: each begins with its component defaults and can toggle
replaceable Skills and Agent References independently. Component-required
entries remain locked and explain why the Step Contract needs them.

An installed preset role can also advertise a custom portable Workflow Step
Type by selecting one of the portable step adapters. The role's installed skill
and agent paths become defaults copied into each new instance, while the
adapter supplies the component-owned
scope, ports, and outcomes. For example:

```json
{
  "roles": {
    "security-review": {
      "step_adapter": "reviewer",
      "component_id": "example.security-review",
      "display_name": "Security Review",
      "skills": ["skills/codex/security-review/SKILL.md"],
      "agents": []
    }
  }
}
```

The portable adapters are `coder`, `reviewer`, and `qa`. Referenced skill and
agent files must already be installed in the bundle. A custom component then
appears in the Workflow Editor Type picker, persists in future-run defaults and
snapshots, and executes with its configured role instructions through the
selected adapter.

## Install From GitHub

Inside the planning chat, follow `/options` → `capabilities` → **Add skill or
agent from GitHub**. The installer accepts a repository URL with an optional
`#subpath`:

    https://github.com/someone/skills-repo#skills/my-skill

Dev Loop clones the repository shallowly to a temporary folder, lists every
skill (folder with `SKILL.md`) and agent (`.md` file inside an `agents/`
directory) it finds under the subpath, asks for confirmation, and moves the
approved items into `skills/codex/` and `agents/codex/`. Existing names are
never overwritten. Review third-party skill content before using it: skills
are instructions that Codex will follow inside your repository.
