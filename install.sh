#!/usr/bin/env bash
# Instala esta configuración de Vibe en macOS/Linux.
# Uso:  ./install.sh [ruta-de-workspace ...]
#       PYTHON=python3.12 ./install.sh ~/dev
# Idempotente.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VIBE="${VIBE_HOME:-$HOME/.vibe}"
PY="${PYTHON:-python3}"

command -v "$PY" >/dev/null 2>&1 || { echo "Falta $PY"; exit 1; }
command -v node >/dev/null 2>&1 || { echo "Falta node"; exit 1; }
command -v npm >/dev/null 2>&1 || { echo "Falta npm"; exit 1; }

echo "[1/5] Directorios"
mkdir -p "$VIBE/agents" "$VIBE/hooks" "$VIBE/mcp/ompx"

echo "[2/5] Copiando configuracion"
if [ -f "$VIBE/config.toml" ]; then
  cp "$VIBE/config.toml" "$VIBE/config.toml.prev"
fi
cp "$SRC/vibe/config.toml" "$VIBE/config.toml"
cp "$SRC/vibe/AGENTS.md" "$VIBE/AGENTS.md"
cp "$SRC/vibe/hooks.toml" "$VIBE/hooks.toml"
cp "$SRC/vibe/hooks/intercept.py" "$VIBE/hooks/intercept.py"
cp "$SRC"/vibe/agents/*.toml "$VIBE/agents/"
cp "$SRC"/mcp/ompx/*.py "$VIBE/mcp/ompx/"
cp "$SRC/mcp/ompx/package.json" "$VIBE/mcp/ompx/package.json"

"$PY" "$SRC/scripts/port_config.py" --platform posix --vibe-home "$VIBE" --python "$PY"

echo "[3/5] Trust de workspaces"
if [ "$#" -gt 0 ]; then
  ROOTS=("$@")
else
  ROOTS=("$HOME/Documents/GitHub")
fi
for root in "${ROOTS[@]}"; do
  if [ -d "$root" ]; then
    "$PY" "$SRC/scripts/merge_trust.py" "$root"
  else
    echo "  (omitido, no existe): $root"
  fi
done

echo "[4/5] Dependencias del MCP"
( cd "$VIBE/mcp/ompx" && npm install --silent )
echo "  (opcional, PDFs: $PY -m pip install pypdf)"

echo "[5/5] Skills de superpowers (symlinks)"
SKILL_SRC="$HOME/.omp/plugins/node_modules/superpowers/skills"
SKILL_DST="$HOME/.agents/skills"
NAMES="brainstorming systematic-debugging test-driven-development writing-plans executing-plans verification-before-completion requesting-code-review receiving-code-review subagent-driven-development dispatching-parallel-agents using-git-worktrees finishing-a-development-branch writing-skills"
if [ -d "$SKILL_SRC" ]; then
  mkdir -p "$SKILL_DST"
  for name in $NAMES; do
    if [ -d "$SKILL_SRC/$name" ] && [ ! -e "$SKILL_DST/$name" ]; then
      ln -s "$SKILL_SRC/$name" "$SKILL_DST/$name"
      echo "  + $name"
    fi
  done
else
  echo "  (omitido, no existe $SKILL_SRC)"
fi

echo ""
echo "Listo. Verifica con:"
echo "  vibe --version"
echo "  vibe -p 'Responde OK' --max-turns 1"
echo "  vibe -p 'Lista las tools del servidor ompx, una por linea'"
echo "Si cambiaste las tools de un MCP:"
echo "  rm -f \"$VIBE/logs/mcp-descriptors/unified/\"*.json"
