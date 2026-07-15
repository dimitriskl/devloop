#!/usr/bin/env sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
workspace=${1:-"$root/.release-demo/workspace"}
if [ -e "$workspace" ]; then
  printf '%s\n' "Demo workspace already exists: $workspace" >&2
  exit 1
fi
mkdir -p "$workspace"
cp -R "$root/examples/release-demo/repository/." "$workspace/"
git -C "$workspace" init --quiet
git -C "$workspace" config user.name "CodexCLI Demo"
git -C "$workspace" config user.email "codexcli-demo@example.invalid"
git -C "$workspace" add README.md
git -C "$workspace" commit --quiet -m "Initialize release demo"
codexcli doctor --repo "$workspace"
printf '%s\n' 'Submit the request in examples/release-demo/feature-request.md.'
codexcli run --repo "$workspace"
