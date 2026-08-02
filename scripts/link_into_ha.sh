#!/bin/sh
# Symlinks this repo's blueprint/pyscript/dashboard files into a Home
# Assistant config directory. Run this ON THE HA HOST ITSELF (e.g. from
# the Advanced SSH & Web Terminal add-on) - a symlink created from
# another machine over a network share points at a path meaningful to
# that machine, not to Home Assistant. See CLAUDE.md for the full story.
#
# Existing regular files at the target paths are backed up (renamed
# with a .bak-<timestamp> suffix) rather than deleted or overwritten.
# An existing symlink pointing somewhere else is just repointed; one
# already pointing at the right place is left alone.
#
# Usage:
#   ./scripts/link_into_ha.sh [--dry-run] [config_dir]
#
# config_dir defaults to /config.
#
# This blueprint is named adaptive_lighting.yaml specifically so it
# can't collide with an existing adaptive_lighting_unified.yaml (or
# any other differently-named blueprint) already in place - it shows
# up as a separate "Adaptive Lighting" entry in the blueprint picker,
# and existing room automations using something else are entirely
# unaffected until you deliberately switch them over, room by room.

set -eu

DRY_RUN=0
CONFIG_DIR="/config"

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    *) CONFIG_DIR="$arg" ;;
  esac
done

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

link() {
  src="$REPO_DIR/$1"
  dest="$CONFIG_DIR/$2"

  if [ ! -e "$src" ]; then
    echo "SKIP    $2 (source $src does not exist)"
    return
  fi

  if [ -L "$dest" ]; then
    current_target="$(readlink "$dest")"
    if [ "$current_target" = "$src" ]; then
      echo "OK      $2 (already linked)"
      return
    fi
    echo "RELINK  $2 (was -> $current_target)"
    if [ "$DRY_RUN" = 0 ]; then
      rm "$dest"
      ln -s "$src" "$dest"
    fi
    return
  fi

  if [ -e "$dest" ]; then
    backup="$dest.bak-$(date +%Y%m%d-%H%M%S)"
    echo "BACKUP  $2 -> $(basename "$backup")"
    if [ "$DRY_RUN" = 0 ]; then
      mv "$dest" "$backup"
    fi
  fi

  echo "LINK    $2"
  if [ "$DRY_RUN" = 0 ]; then
    mkdir -p "$(dirname "$dest")"
    ln -s "$src" "$dest"
  fi
}

echo "Repo:   $REPO_DIR"
echo "Config: $CONFIG_DIR"
if [ "$DRY_RUN" = 1 ]; then
  echo "(dry run - no changes will be made)"
fi
echo

link "blueprints/automation/danspencer/adaptive_lighting.yaml" \
     "blueprints/automation/danspencer/adaptive_lighting.yaml"
link "pyscript/modules/adaptive_lighting" "pyscript/modules/adaptive_lighting"
link "pyscript/apps/adaptive_lighting"    "pyscript/apps/adaptive_lighting"
link "www/adaptive-lighting-curve-card.js" "www/adaptive-lighting-curve-card.js"

echo
echo "Done. Reload automations (Developer Tools -> YAML -> Automations) or restart Home Assistant to pick up changes."
