#!/usr/bin/env bash
set -euo pipefail
# Download to a file before running: installer prompts read the real terminal.
if [[ $EUID -ne 0 ]]; then
    exec sudo bash "$0" "$@"
fi
apt-get update -qq
apt-get install -y git ca-certificates curl python3 gnupg sqlite3 libsqlite3-0 libicu-dev mediainfo
if [[ ! -d /opt/media-server/.git ]]; then
    if [[ -e /opt/media-server ]]; then
        echo '/opt/media-server already exists and is not a checkout. Nothing overwritten.' >&2
        exit 1
    fi
    git clone https://github.com/waqaarhussain/media-server.git /opt/media-server
fi
exec bash /opt/media-server/install.sh "$@"
