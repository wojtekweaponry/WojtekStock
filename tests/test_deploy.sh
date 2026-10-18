#!/usr/bin/env bash
# Tests deploy.sh end to end, without GitHub, Docker or the real server: a pretend home folder, a local repository
# standing in for GitHub, and a pretend sudo that only records the rebuild. Needs git and zip.
#   bash tests/test_deploy.sh
set -uo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export HOME="$T/home"; mkdir -p "$HOME" "$T/bin"
printf '#!/bin/sh\necho "[sudo] $*" >> "%s/rebuilds"\n' "$T" > "$T/bin/sudo"; chmod +x "$T/bin/sudo"
export PATH="$T/bin:$PATH"
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
git init -q --bare -b main "$T/github.git"
pass=0; fail=0
check() { if eval "$2"; then pass=$((pass+1)); echo "  ok   $1"; else fail=$((fail+1)); echo "  FAIL $1"; fi; }
mkzip() {   # mkzip VERSION [EXTRA-SHELL-TO-RUN-IN-THE-BUILD-COPY]: a zip like the ones Claude builds
  rm -rf "$T/build" && mkdir -p "$T/build" && cp -r "$SRC" "$T/build/wojtek-stock"
  rm -rf "$T/build/wojtek-stock/.git" "$T/build/wojtek-stock/data"
  echo "$1: test version" > "$T/build/wojtek-stock/CHANGES.txt"
  ( cd "$T/build/wojtek-stock" && eval "${2:-true}" )
  ( cd "$T/build" && zip -qr "$HOME/wojtek-stock-$1.zip" wojtek-stock )
  touch -d "+${3:-0} seconds" "$HOME/wojtek-stock-$1.zip" 2>/dev/null || true
}

echo "1. First version, by hand as GITHUB.md says"
mkzip v8
( cd "$HOME" && unzip -oq wojtek-stock-v8.zip )
echo "STOCK_PASSWORD=secret" > "$HOME/wojtek-stock/stock.env"
mkdir -p "$HOME/wojtek-stock/data" && echo db > "$HOME/wojtek-stock/data/stock.db"
( cd "$HOME/wojtek-stock" && git init -q -b main && git remote add origin "$T/github.git" && git add -A \
  && git commit -qm "v8: first version on GitHub" && git tag v8 && git push -q -u origin main --tags )
check "settings never committed" '! git -C "$T/github.git" ls-tree -r --name-only main | grep -q "stock.env$"'
check "data never committed" '! git -C "$T/github.git" ls-tree -r --name-only main | grep -q "^data/"'
check "example settings committed" 'git -C "$T/github.git" ls-tree -r --name-only main | grep -q "stock.env.example"'

echo "2. A new zip: committed with CHANGES.txt, tagged, pushed, rebuilt"
mkzip v9 'echo "# v9" >> README.md' 5
"$HOME/wojtek-stock/deploy.sh" > "$T/out" 2>&1
check "deploy succeeds" 'grep -q "v9 is running" "$T/out"'
check "commit message from CHANGES.txt" 'git -C "$T/github.git" log -1 --format=%s main | grep -q "^v9: test version"'
check "tag pushed" 'git -C "$T/github.git" tag | grep -qx v9'
check "rebuilt with docker compose" 'grep -q "docker compose up -d --build" "$T/rebuilds"'
check "settings and data still there" 'grep -q secret "$HOME/wojtek-stock/stock.env" && [ -f "$HOME/wojtek-stock/data/stock.db" ]'

echo "3. Someone's pull request merged on GitHub"
git clone -q "$T/github.git" "$T/them"
( cd "$T/them" && echo "# their change" >> stock.env.example && git commit -qam "Their improvement" && git push -q origin main )
mkzip v10 'echo "# v10" >> README.md' 10
"$HOME/wojtek-stock/deploy.sh" > "$T/out" 2>&1; rc=$?
check "zip refused while GitHub is ahead" '[ $rc -ne 0 ] && grep -q "GitHub has changes this server" "$T/out"'
check "nothing was unpacked" '! grep -q "# v10" "$HOME/wojtek-stock/README.md"'
"$HOME/wojtek-stock/deploy.sh" pull > "$T/out" 2>&1
check "pull takes the merge" 'grep -q "their change" "$HOME/wojtek-stock/stock.env.example" && grep -q "Running what" "$T/out"'

echo "4. A zip built on old code would undo their change"
"$HOME/wojtek-stock/deploy.sh" "$HOME/wojtek-stock-v10.zip" > "$T/out" 2>&1; rc=$?
check "stopped, naming the file" '[ $rc -ne 0 ] && grep -q "stock.env.example" "$T/out"'
check "their change still there" 'grep -q "their change" "$HOME/wojtek-stock/stock.env.example"'
check "settings untouched by the clean-up" 'grep -q secret "$HOME/wojtek-stock/stock.env" && [ -f "$HOME/wojtek-stock/data/stock.db" ]'

echo "5. A zip built from the current GitHub code, with --force"
mkzip v11 'echo "# their change" >> stock.env.example; echo "# v11" >> README.md' 15
"$HOME/wojtek-stock/deploy.sh" --force "$HOME/wojtek-stock-v11.zip" > "$T/out" 2>&1
check "deployed" 'grep -q "v11 is running" "$T/out"'
check "their change kept" 'grep -q "their change" "$HOME/wojtek-stock/stock.env.example"'
check "on GitHub with its tag" 'git -C "$T/github.git" tag | grep -qx v11'

echo
echo "$pass passed, $fail failed"
[ $fail -eq 0 ]
