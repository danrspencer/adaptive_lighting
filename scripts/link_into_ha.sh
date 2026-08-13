#!/bin/sh
# Copies this repo's blueprint/integration/dashboard files into a
# Home Assistant config directory. Run this ON THE HA HOST
# ITSELF (e.g. from the Advanced SSH & Web Terminal add-on) - a
# symlink created from another machine over a network share points at
# a path meaningful to that machine, not to Home Assistant. See
# CLAUDE.md for the full story.
#
# This is a stopgap for local testing before Adaptive Lighting Helpers
# is actually published as a HACS repository - once it is, HACS
# handles install/update for custom_components/adaptive_lighting_helpers
# itself and this script's job shrinks to just the blueprint (and the
# dashboard card, if that also ends up HACS-distributed - see CLAUDE.md's
# "Open question" section).
#
# The blueprint and the integration are both COPIED, not symlinked: a
# symlink created in one shell session had its target baked in as that
# session's own path (e.g. /root/config/... from the SSH add-on's home
# directory), not /config/... - meaningless from whatever container
# actually reads it back. Copying sidesteps the whole class of problem
# instead of trying to get the path right. See CLAUDE.md lesson 7.
#
# The dashboard card is COPIED too, not symlinked - it hit exactly the
# problem this comment used to warn was untested: the live host's card
# was frozen at a pre-multi-sensor-rework snapshot (months of unrelated
# card changes never reached it) while every other deployment path in
# this repo (HACS for the integration, ha_import_blueprint for the
# blueprint) kept the rest of the install current. Confirmed live via
# ha_read_file before concluding it wasn't just a browser cache issue -
# the served bytes themselves were stale.
#
# A NEW custom_components/adaptive_lighting_helpers install needs a
# full HA restart to be discovered at all, then adding once through
# Settings -> Devices & Services -> Add Integration -> "Adaptive
# Lighting Helpers" (there's nothing this script can do to automate
# that UI step).
#
# Existing regular files at a copied path are backed up (renamed with
# a .bak-<timestamp> suffix) rather than overwritten, but only if their
# content actually differs - re-running with no changes is a no-op.
# Existing regular files at a symlinked path are backed up the same
# way. An existing symlink pointing somewhere else is just repointed;
# one already pointing at the right place is left alone.
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

copy() {
  src="$REPO_DIR/$1"
  dest="$CONFIG_DIR/$2"

  if [ ! -e "$src" ]; then
    echo "SKIP    $2 (source $src does not exist)"
    return
  fi

  if [ -d "$src" ]; then
    if [ -e "$dest" ] && [ ! -L "$dest" ] && diff -rq "$src" "$dest" >/dev/null 2>&1; then
      echo "OK      $2 (already up to date)"
      return
    fi
  else
    if [ -e "$dest" ] && [ ! -L "$dest" ] && cmp -s "$src" "$dest"; then
      echo "OK      $2 (already up to date)"
      return
    fi
  fi

  if [ -e "$dest" ] || [ -L "$dest" ]; then
    backup="$dest.bak-$(date +%Y%m%d-%H%M%S)"
    echo "BACKUP  $2 -> $(basename "$backup")"
    if [ "$DRY_RUN" = 0 ]; then
      mv "$dest" "$backup"
    fi
  fi

  echo "COPY    $2"
  if [ "$DRY_RUN" = 0 ]; then
    mkdir -p "$(dirname "$dest")"
    cp -r "$src" "$dest"
  fi
}

echo "Repo:   $REPO_DIR"
echo "Config: $CONFIG_DIR"
if [ "$DRY_RUN" = 1 ]; then
  echo "(dry run - no changes will be made)"
fi
echo

copy "blueprints/automation/danspencer/adaptive_lighting.yaml" \
     "blueprints/automation/danspencer/adaptive_lighting.yaml"
copy "custom_components/adaptive_lighting_helpers" "custom_components/adaptive_lighting_helpers"
copy "www/adaptive-lighting-curve-card.js" "www/adaptive-lighting-curve-card.js"

echo
echo "Done. Restart Home Assistant to pick up the integration (new custom_components"
echo "aren't discovered by a plain reload), then add it once via Settings -> Devices"
echo "& Services -> Add Integration -> \"Adaptive Lighting Helpers\" if not already added."
