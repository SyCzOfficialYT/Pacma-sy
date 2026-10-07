#!/usr/bin/env bash
set -euo pipefail

PREFIX="${PREFIX:-/usr/local/bin}"
TARGET="$PREFIX/pacsy"
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/pacsy.py"

# Use a symlink so `git pull` immediately updates the installed command.
# This avoids the old copied /usr/local/bin/pacsy surviving a repository update.
if [[ "${EUID}" -eq 0 ]]; then
    ln -sfn "$SOURCE" "$TARGET"
    chmod +x "$SOURCE"
else
    sudo ln -sfn "$SOURCE" "$TARGET"
    chmod +x "$SOURCE"
fi

echo
echo "✓ Pacma-sy linked: $TARGET -> $SOURCE"
echo
echo "Demo:"
echo "  pacsy --demo"
echo
echo "Live:"
echo "  pacsy"
