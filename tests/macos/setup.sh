#!/usr/bin/env bash
# Local test environment: macOS + Claude Code. Not part of the shipped server.
# Step-by-step guide: tests/README.md
#
# Usage:  tests/macos/setup.sh [folder where you start Claude Code, default: project root]
#   - creates .venv and installs the server in editable mode with the test tools
#   - copies the config templates (existing files are kept)
#   - registers the MCP server 'jenkins' in Claude Code (local scope of that folder)
#   - runs the offline test suite
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CLAUDE_DIR="$(cd "${1:-$ROOT}" && pwd)"
PYTHON="${PYTHON:-python3}"
cd "$ROOT"

echo "==> [1/4] Virtual environment + editable install"
[ -x .venv/bin/python ] || "$PYTHON" -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -e '.[dev]'

echo "==> [2/4] Config files"
for pair in "config/jenkins_instances.example.yaml:config/jenkins_instances.yaml" "config/.env.example:config/.env"; do
  src="${pair%%:*}"; dst="${pair##*:}"
  if [ -e "$dst" ]; then echo "    keep existing $dst"; else cp "$src" "$dst"; echo "    created $dst - edit it"; fi
done
chmod 600 config/.env

echo "==> [3/4] Claude Code: register 'jenkins' for $CLAUDE_DIR"
if command -v claude >/dev/null; then
  cd "$CLAUDE_DIR"
  claude mcp remove --scope local jenkins >/dev/null 2>&1 || true
  claude mcp add --scope local jenkins -e "JENKINS_MCP_HOME=$ROOT" -- "$ROOT/.venv/bin/jenkins-mcp"
  cd "$ROOT"
else
  echo "    claude CLI not found - install Claude Code, then run this script again"
fi

echo "==> [4/4] Offline tests"
.venv/bin/pytest -q
