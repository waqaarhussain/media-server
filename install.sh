#!/usr/bin/env bash
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
    exec sudo bash "$0" "$@"
fi
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec python3 "$repo/scripts/manage.py" install "$@"
