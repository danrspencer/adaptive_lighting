#!/bin/sh
# Pulls this repo and re-runs link_into_ha.sh - the "deploy on push"
# half of the workflow. Wired up as a shell_command + time_pattern
# automation in packages/adaptive_lighting_sync.yaml (see README), so
# Home Assistant checks for new commits on its own instead of you
# SSHing in after every push. Polling, not a webhook - simpler, and
# doesn't need HA reachable from the internet. See CLAUDE.md.
#
# Safe to run repeatedly: a no-op if there's nothing new to pull, and
# link_into_ha.sh itself only touches files that actually changed.
#
# Usage:
#   ./scripts/sync_and_link.sh [config_dir]
#
# config_dir is passed straight through to link_into_ha.sh (defaults
# to /config there too).

set -eu

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_DIR="${1:-/config}"

cd "$REPO_DIR"

before="$(git rev-parse HEAD)"
git pull --ff-only --quiet
after="$(git rev-parse HEAD)"

if [ "$before" = "$after" ]; then
  echo "No changes ($after)"
  exit 0
fi

echo "Updated $before -> $after"
"$REPO_DIR/scripts/link_into_ha.sh" "$CONFIG_DIR"
