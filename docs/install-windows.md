# Windows Install

For a plain setup guide, start with `docs/new-pc-setup.md`.

1. Copy the whole `devloop` folder to the target machine.
2. Install Python 3.10 or later and make sure `python --version` prints a version.
3. Install and authenticate Codex CLI.
4. Install Git.
5. Install .NET 10 SDK if the target repository or SQL MCP needs .NET builds.
6. Install copied skills and agents if you want them available globally:

```powershell
.\install\install-skills.ps1
```

7. Optionally build and configure MCP servers with `docs/mcp-setup.md`.

Verify:

```powershell
python --version
codex --version
git --version
.\bin\devloop.ps1 --help
```

