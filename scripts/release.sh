#!/usr/bin/env bash
# Usage: scripts/release.sh 0.5.0
# Bumps the version everywhere, runs the tests, commits, pushes to main,
# packs the Claude Desktop extension and publishes it as a GitHub release.
set -euo pipefail

v="${1:?usage: scripts/release.sh X.Y.Z}"
[[ "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "version must be X.Y.Z" >&2; exit 1; }
cd "$(dirname "$0")/.."
[[ "$(git branch --show-current)" == main ]] || { echo "not on main" >&2; exit 1; }
git diff --quiet HEAD -- || { echo "uncommitted changes" >&2; exit 1; }
[[ -z "$(git ls-remote --tags origin "v$v")" ]] || { echo "v$v already exists" >&2; exit 1; }

sed -i.bak -E "s/^version = \"[^\"]+\"/version = \"$v\"/" pyproject.toml
sed -i.bak -E "s/^__version__ = \"[^\"]+\"/__version__ = \"$v\"/" src/archicad_mcp/__init__.py
sed -i.bak -E "s/\"version\": \"[^\"]+\"/\"version\": \"$v\"/" manifest.json .claude-plugin/plugin.json
rm -f pyproject.toml.bak src/archicad_mcp/__init__.py.bak manifest.json.bak .claude-plugin/plugin.json.bak
uv lock -q

uv run --extra test python -m pytest -q

git add pyproject.toml uv.lock src/archicad_mcp/__init__.py manifest.json .claude-plugin/plugin.json
git commit -q -m "chore: release $v"
git push -q origin main

bundle="$(mktemp -d)/archicad-$v.mcpb"
npx -y @anthropic-ai/mcpb pack . "$bundle"
gh release create "v$v" "$bundle" --target main --title "$v" --generate-notes
