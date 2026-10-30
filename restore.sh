#!/usr/bin/env bash
# Bring Wojtek Stock back from a nightly Dropbox backup: the stock-db.sqlite.gz in a dated folder of
# "/Wojtek Stock backups" (download it to the server first; see the README, "Restoring from a backup").
#
#   ./restore.sh check     FILE   is the backup sound, and what's in it?
#   ./restore.sh test      FILE   start a test copy from it on port 8092: it reads your store but can't change it,
#                                 email, print or back up. Look around, then:
#   ./restore.sh test-stop        stop the test copy and delete it
#   ./restore.sh for-real  FILE   replace Stock's database with the backup (the current one is kept aside first)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TESTDIR="$HOME/wojtek-stock-restore-test"
TESTNAME="wojtek-stock-restore-test"
TESTPORT="${RESTORE_TEST_PORT:-8092}"
die() { echo "restore: $*" >&2; exit 1; }

image() {   # the image the running version uses, from docker-compose.yml
  sed -n 's/^ *image: *\(wojtek-stock:[^ ]*\).*/\1/p' "$HERE/docker-compose.yml" | head -1
}

unpack() {   # unpack FILE DEST: refuses anything that isn't a sound backup of Wojtek Stock
  local f="$1" dest="$2"
  [ -f "$f" ] || die "No file $f"
  gzip -t "$f" 2>/dev/null || die "$f isn't a complete .gz file (a partial download?)"
  gzip -dc "$f" > "$dest"
  python3 - "$dest" <<'PY' || die "That isn't a sound Wojtek Stock backup"
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
ok = db.execute("PRAGMA integrity_check").fetchone()[0]
if ok != "ok":
    print(f"  The database inside is damaged: {ok}"); sys.exit(1)
tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
if not {"items", "jobs", "people", "meta"} <= tables:
    print("  It isn't a Wojtek Stock database"); sys.exit(1)
PY
}

summary() {   # what a backup holds, in a few lines
  python3 - "$1" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
one = lambda sql: db.execute(sql).fetchone()[0]
has = lambda t: bool(one(f"SELECT COUNT(*) FROM sqlite_master WHERE name='{t}'"))
print(f"  Last change logged:  {one('SELECT MAX(at) FROM jobs') or 'none'}")
print(f"  In-shop products:    {one("SELECT COUNT(*) FROM items WHERE scope='shop'")}")
print(f"  Changes logged:      {one('SELECT COUNT(*) FROM jobs')}")
print(f"  People:              {one('SELECT COUNT(*) FROM people WHERE active=1')}")
for t, label in (("pos", "Purchase orders"), ("bills", "Vendor invoices"), ("rmas", "Returns to vendors"), ("bins", "Bins"), ("kits", "Kits")):
    if has(t):
        print(f"  {label + ':':20} {one(f'SELECT COUNT(*) FROM {t}')}")
PY
}

cmd="${1:-}"; shift || true
case "$cmd" in
  check)
    [ $# -eq 1 ] || die "Usage: ./restore.sh check FILE"
    tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
    unpack "$1" "$tmp"
    echo "The backup looks good:"
    summary "$tmp"
    ;;
  test)
    [ $# -eq 1 ] || die "Usage: ./restore.sh test FILE"
    img="$(image)"; [ -n "$img" ] || die "Can't tell which version is installed (docker-compose.yml)"
    sudo docker rm -f "$TESTNAME" >/dev/null 2>&1 || true
    rm -rf "$TESTDIR"; mkdir -p "$TESTDIR/data"
    unpack "$1" "$TESTDIR/data/stock.db"
    cp "$HERE/stock.env" "$TESTDIR/stock.env"
    sudo chown -R 10001:10001 "$TESTDIR/data"
    # STOCK_SAFE_COPY: the test copy reads your store but writes, emails, prints and backs up nothing.
    sudo docker run -d --name "$TESTNAME" -p "$TESTPORT:8090" --env-file "$TESTDIR/stock.env" \
      -e STOCK_SAFE_COPY=on -e STOCK_DB=/data/stock.db -e STOCK_HOST=0.0.0.0 -e STOCK_PORT=8090 \
      -v "$TESTDIR/data:/data" "$img" >/dev/null
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo "A test copy is starting from that backup: http://${ip:-this-server}:$TESTPORT"
    echo "(the shop password, as usual). It shows a TEST COPY banner and can't change your store."
    summary "$TESTDIR/data/stock.db"
    echo "When you're done looking: ./restore.sh test-stop"
    ;;
  test-stop)
    sudo docker rm -f "$TESTNAME" >/dev/null 2>&1 || true
    sudo rm -rf "$TESTDIR"
    echo "The test copy is stopped and deleted. Wojtek Stock itself was never touched."
    ;;
  for-real)
    [ $# -eq 1 ] || die "Usage: ./restore.sh for-real FILE"
    tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT   # staged here: data/ belongs to the container's user
    unpack "$1" "$tmp"
    echo "This replaces Wojtek Stock's database with the backup:"
    summary "$tmp"
    echo "The current database is kept as data/stock.db.before-restore-<date>, so this can be undone."
    read -r -p "Type RESTORE to go ahead: " answer
    if [ "$answer" != "RESTORE" ]; then echo "Nothing changed."; exit 1; fi
    stamp="$(date +%Y%m%d-%H%M%S)"
    ( cd "$HERE" && sudo docker compose stop )
    for f in stock.db stock.db-wal stock.db-shm; do
      [ -f "$HERE/data/$f" ] && sudo mv "$HERE/data/$f" "$HERE/data/$f.before-restore-$stamp"
    done
    sudo mv "$tmp" "$HERE/data/stock.db"
    sudo chown 10001:10001 "$HERE/data/stock.db"
    ( cd "$HERE" && sudo docker compose up -d )
    echo "Restored. Wojtek Stock is starting with the backup; its first sync brings stock numbers up to date"
    echo "from WooCommerce (WooCommerce always has the real numbers). The old database: data/stock.db.before-restore-$stamp"
    ;;
  *)
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
