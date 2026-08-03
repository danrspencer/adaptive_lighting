#!/bin/sh
# Copies/symlinks this repo's blueprint/pyscript/dashboard files into a
# Home Assistant config directory. Run this ON THE HA HOST ITSELF (e.g.
# from the Advanced SSH & Web Terminal add-on) - a symlink created from
# another machine over a network share points at a path meaningful to
# that machine, not to Home Assistant. See CLAUDE.md for the full story.
#
# The blueprint and pyscript are both COPIED, not symlinked: Home
# Assistant's blueprint loader failed to read a symlinked blueprint on
# this setup (returned "Blueprint body could not be read or parsed",
# even though the file was listed), and pyscript showed the same
# symptom - no pyscript.compute_lighting_groups service, no error, no
# log line of any kind, even after pyscript.reload - plausibly an
# AppArmor/container-boundary issue between whatever add-on container
# created the symlink and the core homeassistant container that reads
# it. A plain copy fixed the blueprint immediately; pyscript gets the
# same treatment now. See CLAUDE.md lesson 7.
#
# The dashboard card is still SYMLINKED - untested so far, but if it
# turns out to have the same problem (a stale/missing card in the
# Lovelace resource that doesn't update), switch it to `copy` too.
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

  if [ -e "$dest" ]; then
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
copy "pyscript/modules/adaptive_lighting" "pyscript/modules/adaptive_lighting"
copy "pyscript/apps/adaptive_lighting"    "pyscript/apps/adaptive_lighting"
link "www/adaptive-lighting-curve-card.js" "www/adaptive-lighting-curve-card.js"

echo
echo "Done. Reload automations (Developer Tools -> YAML -> Automations) or restart Home Assistant to pick up changes."
