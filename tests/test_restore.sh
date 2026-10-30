#!/usr/bin/env bash
# Tests restore.sh with a pretend home folder and a pretend sudo (docker and chown are only recorded).
#   bash tests/test_restore.sh
set -uo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export HOME="$T/home"; mkdir -p "$HOME" "$T/bin"
printf '#!/bin/sh\necho "[sudo] $*" >> "%s/sudo.log"\ncase "$1" in docker|chown) exit 0;; esac\nexec "$@"\n' "$T" > "$T/bin/sudo"; chmod +x "$T/bin/sudo"
export PATH="$T/bin:$PATH"
pass=0; fail=0
check() { if eval "$2"; then pass=$((pass+1)); echo "  ok   $1"; else fail=$((fail+1)); echo "  FAIL $1"; fi; }
cp -r "$SRC" "$HOME/wojtek-stock" && rm -rf "$HOME/wojtek-stock/data" && mkdir -p "$HOME/wojtek-stock/data"
echo "STOCK_PASSWORD=x" > "$HOME/wojtek-stock/stock.env"
R="$HOME/wojtek-stock/restore.sh"
mkdb() {   # mkdb FILE NAME: a real Wojtek Stock database with one person called NAME
  STOCK_DB="$1" python3 - "$2" <<'PY'
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("s", os.path.join(os.environ["SRC"], "server.py"))
s = importlib.util.module_from_spec(spec); spec.loader.exec_module(s)
s.conn.execute("INSERT INTO people(code,name,active,created_at) VALUES('X',?,1,'now')", (sys.argv[1],)); s.conn.commit()
PY
}
export SRC
people() { python3 -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('SELECT name FROM people').fetchone()[0])" "$1"; }
mkdb "$T/backup.db" "Backup Bob" && gzip -c "$T/backup.db" > "$T/stock-db.sqlite.gz"
mkdb "$HOME/wojtek-stock/data/stock.db" "Current Carl"

echo "1. Checking a backup"
"$R" check "$T/stock-db.sqlite.gz" > "$T/out" 2>&1; rc=$?
check "a good backup passes, and says what's in it" '[ $rc -eq 0 ] && grep -q "looks good" "$T/out" && grep -q "People: *1" "$T/out"'
head -c 300 "$T/stock-db.sqlite.gz" > "$T/partial.gz"
"$R" check "$T/partial.gz" > "$T/out" 2>&1; rc=$?
check "a partial download is refused" '[ $rc -ne 0 ] && grep -q "complete" "$T/out"'
python3 -c "import sqlite3; sqlite3.connect('$T/other.db').execute('CREATE TABLE x(a)')" && gzip -c "$T/other.db" > "$T/other.gz"
"$R" check "$T/other.gz" > "$T/out" 2>&1; rc=$?
check "a database that isn't Stock's is refused" '[ $rc -ne 0 ] && grep -q "isn.t a Wojtek Stock" "$T/out"'

echo "2. A test copy"
"$R" test "$T/stock-db.sqlite.gz" > "$T/out" 2>&1; rc=$?
check "started on its own port, locked, with the installed version" '[ $rc -eq 0 ] && grep -q "docker run -d --name wojtek-stock-restore-test -p 8092:8090" "$T/sudo.log" && grep -q "STOCK_SAFE_COPY=on" "$T/sudo.log" && grep -q "wojtek-stock:v" "$T/sudo.log"'
check "from the backup" '[ "$(people "$HOME/wojtek-stock-restore-test/data/stock.db")" = "Backup Bob" ]'
check "Stock itself untouched" '[ "$(people "$HOME/wojtek-stock/data/stock.db")" = "Current Carl" ] && ! grep -q "compose stop" "$T/sudo.log"'
"$R" test-stop > "$T/out" 2>&1
check "test-stop removes it" '[ ! -d "$HOME/wojtek-stock-restore-test" ] && grep -q "docker rm -f wojtek-stock-restore-test" "$T/sudo.log"'

echo "3. Restoring for real"
echo "no" | "$R" for-real "$T/stock-db.sqlite.gz" > "$T/out" 2>&1; rc=$?
check "anything but RESTORE changes nothing" '[ $rc -ne 0 ] && [ "$(people "$HOME/wojtek-stock/data/stock.db")" = "Current Carl" ]'
"$R" for-real "$T/partial.gz" < /dev/null > "$T/out" 2>&1; rc=$?
check "a bad backup is refused before anything stops" '[ $rc -ne 0 ] && [ "$(people "$HOME/wojtek-stock/data/stock.db")" = "Current Carl" ] && ! grep -q "compose stop" "$T/sudo.log"'
echo "RESTORE" | "$R" for-real "$T/stock-db.sqlite.gz" > "$T/out" 2>&1; rc=$?
check "restored" '[ $rc -eq 0 ] && [ "$(people "$HOME/wojtek-stock/data/stock.db")" = "Backup Bob" ]'
check "the old database kept aside" 'ls "$HOME/wojtek-stock/data/" | grep -q "stock.db.before-restore-" && [ "$(people "$HOME/wojtek-stock/data/"stock.db.before-restore-*)" = "Current Carl" ]'
check "Stock stopped, then started again" 'grep -q "docker compose stop" "$T/sudo.log" && grep -q "docker compose up -d" "$T/sudo.log"'
echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
