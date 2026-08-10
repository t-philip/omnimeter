#!/usr/bin/env bash
# Clean uninstall for a self-hosted OmniMeter install. Never deletes
# real data silently -- containers/image/anonymous volumes go first (routine,
# fully recreatable), then each host path that might hold actual data is
# listed and confirmed individually.
set -euo pipefail
cd "$(dirname "$0")"

echo "Stopping and removing containers, image, and anonymous volumes..."
docker compose down -v --rmi local

echo
echo "The following paths on this machine may still hold real data:"
echo

confirm_remove() {
    local path="$1" description="$2"
    if [ ! -e "$path" ]; then
        return
    fi
    read -r -p "Remove $path ($description)? [y/N] " reply
    if [[ "$reply" =~ ^[Yy]$ ]]; then
        rm -rf -- "$path"
        echo "  removed: $path"
    else
        echo "  kept: $path"
    fi
}

confirm_remove "./data" "SQLite database + CSV import history"
confirm_remove "./devices.json" "your paired device IPs/serials/tokens-by-reference"
confirm_remove "./.env" "device Bearer tokens, write-auth token, timezone, backup location"

backup_dir=$(grep -E '^OMNIMETER_BACKUP_HOST_DIR=' .env 2>/dev/null | cut -d= -f2-)
if [ -n "${backup_dir:-}" ]; then
    confirm_remove "$backup_dir" "backup snapshots"
fi

echo
echo "Uninstall complete."
