#!/usr/bin/env bash
set -euo pipefail

PREFIX="${PREFIX:-/usr/local/bin}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$PREFIX/pacsy"
SHELL_TARGET="$PREFIX/pacsy-shell"
SOURCE="$ROOT/pacsy.py"
SHELL_SOURCE="$ROOT/pacsy-shell.py"

if [[ "${EUID}" -eq 0 ]]; then
    ln -sfn "$SOURCE" "$TARGET"
    ln -sfn "$SHELL_SOURCE" "$SHELL_TARGET"
    chmod +x "$SOURCE" "$SHELL_SOURCE"
else
    sudo ln -sfn "$SOURCE" "$TARGET"
    sudo ln -sfn "$SHELL_SOURCE" "$SHELL_TARGET"
    chmod +x "$SOURCE" "$SHELL_SOURCE"
fi

echo
echo "✓ Pacma-sy linked: $TARGET -> $SOURCE"
echo "✓ Pacsy Shell linked: $SHELL_TARGET -> $SHELL_SOURCE"
echo
echo "Update UI demo:"
echo "  pacsy --demo"
echo
echo "Live package update:"
echo "  pacsy"
echo
echo "Fullscreen shell dashboard:"
echo "  pacsy-shell"
