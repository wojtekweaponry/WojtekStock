#!/usr/bin/env bash
# Deploy Wojtek Stock, keep it in git, and restart:
#   ~/wojtek-stock/deploy.sh pull                       take what's been merged on GitHub (a pull request, say), rebuild, restart
#   ~/wojtek-stock/deploy.sh                            the newest wojtek-stock-*.zip in your home folder: unpack, commit, push, rebuild
#   ~/wojtek-stock/deploy.sh ~/wojtek-stock-v12.zip     a particular zip
#   ~/wojtek-stock/deploy.sh --force ZIP                a zip built from the current GitHub code that changes files others edited
# A zip is refused while GitHub has changes this server doesn't: pull them first, then send the current code to
# whoever makes the zips so the next one is built on top of them. Settings (stock.env) and data (data/) are never
# committed: see .gitignore. One-time setup: GITHUB.md.
set -euo pipefail
NAME="wojtek-stock"
DIR="$HOME/$NAME"
cd "$DIR" 2>/dev/null || { echo "No ~/$NAME folder"; exit 1; }
rebuild() { echo "== Building and restarting"; cd "$DIR" && sudo docker compose up -d --build; }
has_github() { [ -d "$DIR/.git" ] && git -C "$DIR" remote get-url origin >/dev/null 2>&1; }

if [ "${1:-}" = "pull" ]; then
  has_github || { echo "This folder isn't connected to GitHub yet: see GITHUB.md"; exit 1; }
  echo "== Taking what's on GitHub"
  if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "   ! There are uncommitted changes on this server. Commit or set them aside first (git status)."; exit 1
  fi
  git pull --ff-only origin main || { echo "   ! This server and GitHub have both changed: ask for help (git status, git log)."; exit 1; }
  git log -1 --format="   now at: %h %s (%an, %ar)"
  rebuild; echo "== Running what's on GitHub"; exit 0
fi

FORCE=""; [ "${1:-}" = "--force" ] && { FORCE=1; shift; }
ZIP="${1:-$(ls -t "$HOME"/$NAME-*.zip 2>/dev/null | head -1)}"
[ -n "$ZIP" ] && [ -f "$ZIP" ] || { echo "No $NAME zip found. Copy one to your home folder first, or run: deploy.sh pull"; exit 1; }
VER="$(basename "$ZIP" .zip)"; VER="${VER#$NAME-}"

# 1. GitHub ahead of this server? Always refused, even with --force: the push would be rejected, and merged work could
#    be lost. Pull first.
if has_github; then
  git fetch -q origin 2>/dev/null || echo "   (couldn't reach GitHub to check for changes)"
  if git rev-parse -q --verify origin/main >/dev/null && [ -n "$(git log --oneline HEAD..origin/main 2>/dev/null)" ]; then
    echo "== GitHub has changes this server doesn't:"; git log --oneline HEAD..origin/main | sed 's/^/     /'
    echo "   First: ~/$NAME/deploy.sh pull"
    echo "   Then send the current code (GitHub: Code → Download ZIP) to whoever makes your zips, so it's included."; exit 1
  fi
fi

echo "== Unpacking $VER"
cd "$HOME" && unzip -oq "$ZIP"
cd "$DIR"; chmod +x deploy.sh run-tests.sh setup-kiosk.sh 2>/dev/null || true

# 2. Would this zip change back files others edited (merged from GitHub) since the last zip? Stop, unless --force says
#    the zip was built from the current GitHub code.
if has_github && [ -z "$FORCE" ]; then
  LAST="$(git describe --tags --abbrev=0 2>/dev/null || true)"
  if [ -n "$LAST" ] && [ -n "$(git log --format=%h "$LAST"..HEAD)" ]; then
    THEIRS="$(git diff --name-only "$LAST" HEAD)"
    UNDONE=""
    if [ -n "$THEIRS" ]; then   # nothing changed on GitHub (an empty merge): nothing to protect, and an empty list
      UNDONE="$(git diff --name-only HEAD -- $THEIRS 2>/dev/null || true)"   # here would compare every file
    fi
    if [ -n "$UNDONE" ]; then
      git checkout -q -- . && git clean -fdq
      echo "== Stopped: these files were changed on GitHub since $LAST, and this zip would change them:"
      echo "$UNDONE" | sed 's/^/     /'
      git log --format="     %h %s (%an)" "$LAST"..HEAD
      echo "   Nothing was changed. If $VER was built from the current GitHub code, deploy it with:"
      echo "   ~/$NAME/deploy.sh --force $ZIP"
      exit 1
    fi
  fi
fi

if [ -d .git ]; then
  echo "== Saving $VER in git"
  git add -A
  MSG="$VER"
  [ -f CHANGES.txt ] && MSG="$(head -1 CHANGES.txt | cut -c1-200)"
  case "$MSG" in "$VER"*) ;; *) MSG="$VER: $MSG" ;; esac
  if git diff --cached --quiet; then echo "   (nothing changed)"; else git commit -qm "$MSG"; fi
  git tag -f "$VER" >/dev/null
  if has_github; then
    echo "== Pushing to GitHub"
    git push -q origin HEAD && git push -qf origin "$VER" && echo "   pushed $VER" \
      || echo "   ! Couldn't push to GitHub (Wojtek Stock still deploys). Check: git push"
  fi
fi
rebuild; echo "== $VER is running"
