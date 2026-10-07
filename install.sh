#!/usr/bin/env bash
set -euo pipefail

PREFIX="${PREFIX:-/usr/local/bin}"
TARGET="$PREFIX/pacsy"

if [[ "${EUID}" -eq 0 ]]; then
    install -Dm755 pacsy.py "$TARGET"
else
    sudo install -Dm755 pacsy.py "$TARGET"
fi

echo
echo "✓ Pacma-sy installiert: $TARGET"
echo
echo "Demo:"
echo "  pacsy --demo"
echo
echo "Live:"
echo "  pacsy"
