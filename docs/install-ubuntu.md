# Ubuntu Install

For a plain setup guide, start with `docs/new-pc-setup.md`.

1. Copy the whole `devloop` folder to the target machine.
2. Install Python 3.10 or later and make sure `python3 --version` prints a version.
3. Install and authenticate Codex CLI.
4. Install Git.
5. Install .NET 10 SDK if the target repository or SQL MCP needs .NET builds.
6. Make scripts executable:

```bash
chmod +x ./bin/devloop.sh
chmod +x ./bin/devloop-plan.sh
chmod +x ./install/*.sh
```

7. Install copied skills and agents if you want them available globally:

```bash
chmod +x ./bin/devloop.sh
chmod +x ./bin/devloop-plan.sh
chmod +x ./install/*.sh
./install/install-skills.sh
```

Verify:

```bash
python3 --version
codex --version
git --version
./bin/devloop.sh --help
./bin/devloop-plan.sh --help
```
