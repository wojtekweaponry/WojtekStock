#!/usr/bin/env python3
"""
Wojtek Stock: a barcode stock station for Wojtek Weaponry's in-shop WooCommerce products.

Single-file server, Python 3.10+ standard library only. Keeps a local copy of the product list in SQLite for fast
scanning, serves index.html, and writes stock changes back to WooCommerce, which stays the single source of truth.
Distributor feed products (SKUs starting with a feed prefix) and serialized guns are never changed: they are left out
of the station, and every write re-reads the product live and checks again before sending anything.
Configuration comes from environment variables; see stock.env.example.
"""
import base64
import csv
import gzip
import hashlib
import hmac
import html
import io
import json
import logging
import os
import re
import secrets
import socket
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "v21"
HERE = os.path.dirname(os.path.abspath(__file__))


def env(name, default=""):
    return os.environ.get(name, default).strip()


def env_list(name, default=""):
    return [x.strip() for x in re.split(r"[,\n]", env(name, default)) if x.strip()]


def env_on(name, default="off"):
    return env(name, default).lower() in ("1", "on", "yes", "true")


CFG = {
    "host": env("STOCK_HOST", "0.0.0.0"),
    "port": int(env("STOCK_PORT", "8090") or 8090),
    "db": env("STOCK_DB", os.path.join(HERE, "stock.db")),
    "user": env("STOCK_USER", "shop"),
    "password": env("STOCK_PASSWORD"),
    "woo_url": env("WOO_URL").rstrip("/"),
    "woo_key": env("WOO_KEY"),
    "woo_secret": env("WOO_SECRET"),
    "woo_auth": (env("WOO_AUTH", "auto") or "auto").lower(),          # auto | basic | query
    "woo_write": (env("WOO_WRITE", "dryrun") or "dryrun").lower(),     # off | dryrun | on
    "woo_timeout": float(env("WOO_TIMEOUT", "60") or 60),
    "woo_gap": float(env("WOO_MIN_GAP", "0.25") or 0.25),             # seconds between requests, to be polite
    "feed_prefixes": [p.upper() for p in env_list("FEED_PREFIXES")],
    "gun_categories": env_list("GUN_CATEGORIES"),
    "sync_minutes": max(2.0, float(env("SYNC_MINUTES", "10") or 10)),
    "full_sync_time": env("FULL_SYNC_TIME", "03:15") or "03:15",
    "allow_big_drop": env_on("ALLOW_BIG_DROP"),
    "down_reasons": env_list("ADJUST_DOWN_REASONS", "Damaged,Used on gunsmithing job,Returned to vendor,Theft/loss,Other"),
    "up_reasons": env_list("ADJUST_UP_REASONS", "Found,Customer return,Count correction,Other"),
    "sku_prefix": (env("SKU_PREFIX", "WW-") or "WW-").upper(),
    "sku_digits": int(env("SKU_DIGITS", "6") or 6),
    "label_printer": env("LABEL_PRINTER"),
    "label_dpi": int(env("LABEL_DPI", "203") or 203),
    "label_w_in": float(env("LABEL_WIDTH_IN", "2") or 2),
    "label_l_in": float(env("LABEL_LENGTH_IN", "1") or 1),
    "shop_tz": env("SHOP_TZ", "America/New_York"),
    # printed on purchase orders
    "shop_name": env("SHOP_NAME", "Wojtek Weaponry"),
    "shop_address": env("SHOP_ADDRESS"),          # lines separated by |
    "shop_phone": env("SHOP_PHONE"),
    "shop_email": env("SHOP_EMAIL"),
    # sending purchase orders by email (Proton Mail: an SMTP token, smtp.protonmail.ch, port 587)
    "smtp_host": env("SMTP_HOST"),
    "smtp_port": int(env("SMTP_PORT", "587") or 587),
    "smtp_user": env("SMTP_USER"),
    "smtp_token": env("SMTP_TOKEN"),
    "smtp_from": env("SMTP_FROM"),
    "po_cc": env("PO_EMAIL_CC"),                  # a copy of every PO email, to you
    "auto_po_time": env("AUTO_PO_TIME", "15:00") or "15:00",   # when vendors set to send automatically get their POs
    # GunBroker: a developer key from GunBroker, and your GunBroker login (exchanged for an access token)
    "gb_devkey": env("GB_DEVKEY"),
    "gb_username": env("GB_USERNAME"),
    "gb_password": env("GB_PASSWORD"),
    "gb_sandbox": env_on("GB_SANDBOX"),
    "gb_api": env("GB_API_URL"),                  # only for testing; normally worked out from GB_SANDBOX
    "gb_sync": (env("GB_SYNC", "dryrun") or "dryrun").lower(),   # off | dryrun | on
    "gb_buffer": int(env("GB_BUFFER", "0") or 0), # hold this many back from GunBroker
    "gb_minutes": max(1.0, float(env("GB_MINUTES", "5") or 5)),
    "gb_create_orders": env("GB_CREATE_ORDERS", "on").lower() not in ("off", "0", "no", "false"),
    # the morning email and alerts: to SHOP_EMAIL unless set
    "morning_to": env("MORNING_EMAIL_TO"),
    "morning_time": env("MORNING_EMAIL_TIME", "07:00") or "07:00",   # "off" for none
    "alert_to": env("ALERT_EMAIL_TO"),
    "alerts_on": env("ALERT_EMAILS", "on").lower() not in ("off", "0", "no", "false"),
    "safe_copy": env_on("STOCK_SAFE_COPY"),   # a test copy restored from a backup (restore.sh test): changes nothing
}
if CFG["safe_copy"]:   # read the store, show everything, but write, send and print nothing
    CFG.update(woo_write="dryrun", gb_sync="off", label_printer="", alerts_on=False, morning_time="off", auto_po_time="off")
if CFG["woo_write"] not in ("off", "dryrun", "on"):
    CFG["woo_write"] = "dryrun"
WOO_API = CFG["woo_url"] + "/wp-json/wc/v3"
WOO_READY = bool(CFG["woo_url"] and CFG["woo_key"] and CFG["woo_secret"])

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stock")

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(CFG["shop_tz"])
except Exception as _e:  # no time zone database: fall back to UTC, and say so
    log.warning("Time zone %s couldn't be loaded (%s); using UTC", CFG["shop_tz"], _e)
    LOCAL_TZ = timezone.utc

BOOT = uuid.uuid4().hex[:8]
_ver = {"state": 0, "items": 0}
_ver_lock = threading.Lock()


def bump(items=False):
    with _ver_lock:
        _ver["state"] += 1
        if items:
            _ver["items"] += 1


def state_version():
    return f"{BOOT}-{_ver['state']}-{_ver['items']}"


def items_version():
    return f"{BOOT}-{_ver['items']}"


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s):
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


class ApiError(Exception):
    def __init__(self, status, msg, extra=None):
        super().__init__(msg)
        self.status, self.msg, self.extra = status, msg, extra or {}


# ---------------------------------------------------------------- database
DB_LOCK = threading.RLock()
_db_dir = os.path.dirname(os.path.abspath(CFG["db"]))
os.makedirs(_db_dir, exist_ok=True)
if not os.access(_db_dir, os.W_OK) or (os.path.exists(CFG["db"]) and not os.access(CFG["db"], os.W_OK)):
    log.error("Can't write to %s. In the wojtek-stock folder, run:  sudo chown -R 10001:10001 data   then start again.", _db_dir)
    raise SystemExit(1)
conn = sqlite3.connect(CFG["db"], check_same_thread=False, isolation_level=None)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA journal_mode=WAL")
conn.executescript("""
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS items(
    id INTEGER PRIMARY KEY, parent_id INTEGER DEFAULT 0, type TEXT, name TEXT, attrs TEXT, sku TEXT, skey TEXT,
    gtin TEXT, gkey TEXT, stock INTEGER, manage TEXT, status TEXT, cats TEXT, price TEXT, modified TEXT,
    seen_at TEXT, scope TEXT, why TEXT);
CREATE INDEX IF NOT EXISTS items_skey ON items(skey);
CREATE INDEX IF NOT EXISTS items_gkey ON items(gkey);
CREATE INDEX IF NOT EXISTS items_parent ON items(parent_id);
CREATE INDEX IF NOT EXISTS items_scope ON items(scope);
CREATE TABLE IF NOT EXISTS cats(id INTEGER PRIMARY KEY, name TEXT, slug TEXT);
CREATE TABLE IF NOT EXISTS people(code TEXT PRIMARY KEY, name TEXT NOT NULL, active INTEGER DEFAULT 1, created_at TEXT);
CREATE TABLE IF NOT EXISTS terminals(id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
    active INTEGER DEFAULT 1, created_at TEXT, last_seen TEXT, last_ip TEXT);
CREATE TABLE IF NOT EXISTS jobs(
    id INTEGER PRIMARY KEY AUTOINCREMENT, batch TEXT, at TEXT, kind TEXT, item_id INTEGER, parent_id INTEGER,
    name TEXT, sku TEXT, delta INTEGER, target INTEGER, expected INTEGER, value TEXT, replace_ok INTEGER DEFAULT 0,
    reason TEXT, note TEXT, by TEXT, operator TEXT, status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0,
    next_at REAL DEFAULT 0, before TEXT, after TEXT, error TEXT, mode TEXT, done_at TEXT);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_item ON jobs(item_id);
CREATE INDEX IF NOT EXISTS jobs_batch ON jobs(batch);
CREATE TABLE IF NOT EXISTS order_touch(item_id INTEGER, order_id INTEGER, at TEXT, PRIMARY KEY(item_id, order_id));
CREATE TABLE IF NOT EXISTS drift(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, item_id INTEGER, name TEXT, sku TEXT,
    before INTEGER, after INTEGER);
CREATE TABLE IF NOT EXISTS labels(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, item_id INTEGER, copies INTEGER,
    by TEXT, via TEXT);
CREATE INDEX IF NOT EXISTS labels_item ON labels(item_id);
CREATE TABLE IF NOT EXISTS changes(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, kind TEXT, action TEXT, subject TEXT,
    detail TEXT, by TEXT);
""")
conn.execute("UPDATE jobs SET status='pending' WHERE status='working'")  # interrupted by a restart: pick up again


def add_col(table, col, decl):
    if col not in [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


# v2: returns against an order
add_col("jobs", "order_id", "INTEGER")
add_col("jobs", "order_number", "TEXT")
add_col("jobs", "qty", "INTEGER")          # a return: how many came back
add_col("jobs", "restock", "INTEGER")      # a return: 1 = back in stock, 0 = logged only (damaged)
conn.execute("CREATE INDEX IF NOT EXISTS jobs_order ON jobs(order_id)")
# v9: purchasing
conn.executescript("""
CREATE TABLE IF NOT EXISTS vendors(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, account TEXT, contact TEXT,
    email TEXT, phone TEXT, terms TEXT, lead_days INTEGER, feed_prefix TEXT, notes TEXT, active INTEGER DEFAULT 1,
    created_at TEXT);
CREATE TABLE IF NOT EXISTS vendor_items(vendor_id INTEGER, item_id INTEGER, vendor_sku TEXT, last_cost REAL,
    updated_at TEXT, PRIMARY KEY(vendor_id, item_id));
CREATE TABLE IF NOT EXISTS pos(id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT UNIQUE, vendor_id INTEGER, status TEXT,
    notes TEXT, created_at TEXT, created_by TEXT, sent_at TEXT, closed_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS po_lines(id INTEGER PRIMARY KEY AUTOINCREMENT, po_id INTEGER, item_id INTEGER, name TEXT,
    sku TEXT, vendor_sku TEXT, qty INTEGER, cost REAL, cancelled INTEGER DEFAULT 0, extra INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS po_lines_po ON po_lines(po_id);
CREATE INDEX IF NOT EXISTS po_lines_item ON po_lines(item_id);
CREATE TABLE IF NOT EXISTS receipts(id INTEGER PRIMARY KEY AUTOINCREMENT, po_id INTEGER, batch TEXT, at TEXT, by TEXT,
    freight REAL, fees REAL, invoice TEXT, note TEXT, mode TEXT);
CREATE TABLE IF NOT EXISTS receipt_lines(id INTEGER PRIMARY KEY AUTOINCREMENT, receipt_id INTEGER, po_line_id INTEGER,
    item_id INTEGER, qty INTEGER, unit_cost REAL, landed_unit REAL, job_id INTEGER);
CREATE INDEX IF NOT EXISTS receipt_lines_item ON receipt_lines(item_id);
CREATE INDEX IF NOT EXISTS receipt_lines_line ON receipt_lines(po_line_id);
CREATE TABLE IF NOT EXISTS feed_links(feed_id INTEGER PRIMARY KEY, item_id INTEGER, how TEXT, at TEXT, by TEXT);
CREATE TABLE IF NOT EXISTS alt_barcodes(gkey TEXT PRIMARY KEY, code TEXT, item_id INTEGER, source TEXT, at TEXT);
""")
add_col("jobs", "po_id", "INTEGER")
# v19: roles per person
add_col("people", "role", "TEXT DEFAULT 'staff'")
add_col("people", "pin_hash", "TEXT")
add_col("people", "pin_fails", "INTEGER DEFAULT 0")
add_col("people", "pin_locked_until", "REAL DEFAULT 0")
# v10: reorder points, automatic drafts, email
conn.executescript("""
CREATE TABLE IF NOT EXISTS bills(id INTEGER PRIMARY KEY AUTOINCREMENT, vendor_id INTEGER, kind TEXT DEFAULT 'bill',
    number TEXT, date TEXT, due TEXT, terms TEXT, subtotal REAL, freight REAL, fees REAL, tax REAL, total REAL, notes TEXT,
    file TEXT, file_name TEXT, apply_costs INTEGER DEFAULT 1, void INTEGER DEFAULT 0, created_at TEXT, by TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS bills_vendor ON bills(vendor_id);
CREATE TABLE IF NOT EXISTS bill_lines(id INTEGER PRIMARY KEY AUTOINCREMENT, bill_id INTEGER, po_line_id INTEGER, po_id INTEGER,
    item_id INTEGER, description TEXT, qty REAL, price REAL, amount REAL);
CREATE INDEX IF NOT EXISTS bill_lines_bill ON bill_lines(bill_id);
CREATE INDEX IF NOT EXISTS bill_lines_poline ON bill_lines(po_line_id);
CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY AUTOINCREMENT, vendor_id INTEGER, date TEXT, amount REAL, method TEXT,
    reference TEXT, notes TEXT, void INTEGER DEFAULT 0, created_at TEXT, by TEXT);
CREATE TABLE IF NOT EXISTS applications(id INTEGER PRIMARY KEY AUTOINCREMENT, bill_id INTEGER, payment_id INTEGER, credit_id INTEGER,
    amount REAL, at TEXT, by TEXT);
CREATE INDEX IF NOT EXISTS applications_bill ON applications(bill_id);
CREATE TABLE IF NOT EXISTS sales(order_id INTEGER, item_id INTEGER, day TEXT, qty REAL, total REAL, channel TEXT,
    PRIMARY KEY(order_id, item_id));
CREATE INDEX IF NOT EXISTS sales_item ON sales(item_id);
CREATE INDEX IF NOT EXISTS sales_day ON sales(day);
CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, kind TEXT, at TEXT, by TEXT, items INTEGER,
    units REAL, value REAL, no_cost INTEGER);
CREATE TABLE IF NOT EXISTS snapshot_items(snapshot_id INTEGER, item_id INTEGER, name TEXT, sku TEXT, category TEXT, vendor TEXT,
    stock INTEGER, cost REAL, value REAL);
CREATE INDEX IF NOT EXISTS snapshot_items_snap ON snapshot_items(snapshot_id);
CREATE TABLE IF NOT EXISTS bins(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, name TEXT, notes TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS item_bins(item_id INTEGER, bin_id INTEGER, pos INTEGER DEFAULT 0, PRIMARY KEY(item_id, bin_id));
CREATE INDEX IF NOT EXISTS item_bins_bin ON item_bins(bin_id);
CREATE TABLE IF NOT EXISTS item_counts(id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER, at TEXT, by TEXT, counted INTEGER,
    expected INTEGER, bin_id INTEGER, full_id INTEGER);
CREATE INDEX IF NOT EXISTS item_counts_item ON item_counts(item_id);
CREATE TABLE IF NOT EXISTS full_counts(id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, by TEXT, finished_at TEXT, finished_by TEXT,
    status TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS rmas(id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT UNIQUE, vendor_id INTEGER, status TEXT,
    vendor_rma TEXT, reason TEXT, notes TEXT, created_at TEXT, by TEXT, shipped_at TEXT, ship_date TEXT, carrier TEXT, tracking TEXT,
    credit_bill_id INTEGER, closed_at TEXT, close_note TEXT, emailed_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS rma_lines(id INTEGER PRIMARY KEY AUTOINCREMENT, rma_id INTEGER, item_id INTEGER, name TEXT, sku TEXT,
    qty INTEGER, cost REAL, reason TEXT, remove INTEGER DEFAULT 1, source_job_id INTEGER, job_id INTEGER);
CREATE INDEX IF NOT EXISTS rma_lines_rma ON rma_lines(rma_id);
CREATE INDEX IF NOT EXISTS rma_lines_source ON rma_lines(source_job_id);
CREATE TABLE IF NOT EXISTS manager_sessions(token_hash TEXT PRIMARY KEY, code TEXT, created_at TEXT, last_seen REAL, where_ TEXT);
CREATE TABLE IF NOT EXISTS kits(item_id INTEGER PRIMARY KEY, created_at TEXT, by TEXT);
CREATE TABLE IF NOT EXISTS kit_parts(kit_id INTEGER, part_id INTEGER, qty INTEGER, PRIMARY KEY(kit_id, part_id));
CREATE INDEX IF NOT EXISTS kit_parts_part ON kit_parts(part_id);
CREATE TABLE IF NOT EXISTS kit_sales(order_id INTEGER, kit_id INTEGER, taken REAL DEFAULT 0, returned REAL DEFAULT 0,
    PRIMARY KEY(order_id, kit_id));
CREATE TABLE IF NOT EXISTS gb_listings(gb_id INTEGER PRIMARY KEY, title TEXT, sku TEXT, qty INTEGER, fixed INTEGER,
    price REAL, item_id INTEGER, why TEXT, want INTEGER, pushed_qty INTEGER, pushed_at TEXT, error TEXT, seen_at TEXT);
CREATE TABLE IF NOT EXISTS gb_orders(gb_order_id INTEGER PRIMARY KEY, at TEXT, buyer TEXT, total REAL, status TEXT,
    woo_order_id INTEGER, woo_number TEXT, detail TEXT, items TEXT, imported_at TEXT, mode TEXT);
""")
conn.execute("""CREATE TABLE IF NOT EXISTS reorder(item_id INTEGER PRIMARY KEY, min_qty INTEGER, max_qty INTEGER,
    vendor_id INTEGER, updated_at TEXT, by TEXT)""")
add_col("vendors", "auto_send", "INTEGER DEFAULT 0")
add_col("pos", "auto", "INTEGER DEFAULT 0")
add_col("pos", "emailed_at", "TEXT")
add_col("pos", "emailed_to", "TEXT")
add_col("pos", "email_error", "TEXT")
add_col("jobs", "po_number", "TEXT")
conn.execute("""CREATE TABLE IF NOT EXISTS restock_watch(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, order_id INTEGER,
    order_number TEXT, item_id INTEGER, name TEXT, qty INTEGER, note_id INTEGER UNIQUE, note TEXT)""")


def q(sql, *args):
    with DB_LOCK:
        return conn.execute(sql, args).fetchall()


def q1(sql, *args):
    r = q(sql, *args)
    return r[0] if r else None


def meta_get(k, default=None):
    r = q1("SELECT v FROM meta WHERE k=?", k)
    return json.loads(r["v"]) if r else default


def meta_set(k, v):
    with DB_LOCK:
        conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, json.dumps(v)))


class Tx:
    def __enter__(self):
        DB_LOCK.acquire()
        conn.execute("BEGIN IMMEDIATE")
        return conn

    def __exit__(self, et, ev, tb):
        try:
            conn.execute("ROLLBACK" if et else "COMMIT")
        finally:
            DB_LOCK.release()


def note_change(kind, action, subject, detail="", by=""):
    with DB_LOCK:
        conn.execute("INSERT INTO changes(at,kind,action,subject,detail,by) VALUES(?,?,?,?,?,?)",
                     (now_iso(), kind, action, subject, detail, by))


# ---------------------------------------------------------------- codes: SKUs and barcodes
def skey(v):
    """How a SKU is matched: trimmed, upper case."""
    return re.sub(r"\s+", " ", str(v or "")).strip().upper()


def gkey(v):
    """How a barcode is matched: digits only, leading zeros dropped, so UPC-A 012345678905, EAN-13 0012345678905
    and GTIN-14 00012345678905 are the same product. Anything that isn't all digits has no barcode key."""
    s = re.sub(r"[\s-]", "", str(v or ""))
    if not s.isdigit():
        return ""
    return s.lstrip("0") or "0"


def gtin_ok(v):
    s = re.sub(r"[\s-]", "", str(v or ""))
    return s.isdigit() and len(s) in (8, 12, 13, 14)


def gtin_check_digit_ok(v):
    """UPC/EAN/GTIN check digit (mod 10). Used to warn, never to refuse: some shops use internal codes."""
    s = re.sub(r"[\s-]", "", str(v or ""))
    if not s.isdigit() or len(s) not in (8, 12, 13, 14):
        return False
    digits = [int(c) for c in s]
    body, check = digits[:-1], digits[-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def prefix_group(sku):
    """The group a SKU is counted under on the Setup page: the part before the first -, _, space or . when that part
    is 2 to 6 letters (IRV-12345 -> IRV, CSSI_77 -> CSSI); otherwise its first 3 characters (IRV12345 -> IRV)."""
    s = skey(sku)
    if not s:
        return ""
    m = re.match(r"([A-Z]{2,6})[-_ .]", s)
    if m:
        return m.group(1)
    return s[:3]


# ---------------------------------------------------------------- what may be changed: feed prefixes and gun categories
def feed_prefixes():
    """Feed prefixes in force: those in stock.env plus those ticked on the Setup page."""
    ui = meta_get("feed_prefixes_ui", []) or []
    return sorted(set(CFG["feed_prefixes"]) | {skey(p) for p in ui if skey(p)})


def feed_prefix_of(sku, prefixes=None):
    s = skey(sku)
    if not s:
        return ""
    for p in (prefixes if prefixes is not None else feed_prefixes()):
        if p and s.startswith(p):
            return p
    return ""


def _env_gun_cat_ids():
    """GUN_CATEGORIES in stock.env may name categories by id, slug or name."""
    want = {c.strip().lower() for c in CFG["gun_categories"]}
    if not want:
        return set()
    ids = set()
    for r in q("SELECT id, name, slug FROM cats"):
        if str(r["id"]) in want or (r["slug"] or "").lower() in want or (r["name"] or "").lower() in want:
            ids.add(r["id"])
    return ids


def gun_categories():
    ui = {int(x) for x in (meta_get("gun_categories_ui", []) or []) if str(x).isdigit()}
    return ui | _env_gun_cat_ids()


def setup_confirmed():
    return bool(meta_get("setup_confirmed", False)) and bool(feed_prefixes())


def cat_names():
    return {r["id"]: r["name"] for r in q("SELECT id, name FROM cats")}


def classify(row, parent, prefixes, guns, names=None):
    """(scope, why) for one product or variation. Scopes:
    shop      in-shop, stock managed: the station can change it
    readonly  in-shop, but WooCommerce doesn't manage its stock (or its parent does): shown, never changed
    feed      a distributor feed product: never shown at the station, never changed
    gun       in a serialized-gun category: never changed (stock moves through FastBound and sales)
    parent    a variable product: its variations are the items
    other     grouped or external products, variations whose parent isn't loaded"""
    names = names or {}
    typ = row["type"]
    if typ == "variation" and parent is None:
        return "other", "Its parent product isn't in the local copy yet"
    p = feed_prefix_of(row["sku"], prefixes) or (feed_prefix_of(parent["sku"], prefixes) if parent else "")
    if p:
        return "feed", f"Distributor feed product (SKU starts with {p})"
    cats = json.loads((parent if typ == "variation" else row)["cats"] or "[]")
    hit = [c for c in cats if c in guns]
    if hit:
        return "gun", f"Serialized gun ({names.get(hit[0], 'category ' + str(hit[0]))})"
    if typ == "variable":
        return "parent", "Variable product: scan its variations"
    if typ not in ("simple", "variation"):
        return "other", f"{(typ or 'This').capitalize()} products don't carry stock"
    if row["manage"] == "parent":
        return "readonly", "Stock is managed on the parent product"
    if row["manage"] != "yes":
        return "readonly", "WooCommerce doesn't track stock for this product"
    return "shop", ""


def rescope():
    """Work out every item's scope again (after a sync or a Setup change)."""
    prefixes, guns, names = feed_prefixes(), gun_categories(), cat_names()
    with DB_LOCK:
        rows = conn.execute("SELECT * FROM items").fetchall()
        by_id = {r["id"]: r for r in rows}
        changes = []
        for r in rows:
            parent = by_id.get(r["parent_id"]) if r["type"] == "variation" else None
            sc, why = classify(r, parent, prefixes, guns, names)
            if sc != r["scope"] or why != r["why"]:
                changes.append((sc, why, r["id"]))
        if changes:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.executemany("UPDATE items SET scope=?, why=? WHERE id=?", changes)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    if changes:
        bump(items=True)
    return len(changes)


# ---------------------------------------------------------------- WooCommerce REST client
class WooError(Exception):
    def __init__(self, msg, status=0, code="", transient=False):
        super().__init__(msg)
        self.status, self.code, self.transient = status, code, transient


_woo_lock = threading.Lock()
_woo_last = [0.0]
_woo_auth = {"mode": None}   # what worked: basic or query


def _woo_once(method, path, params, body, mode):
    params = dict(params or {})
    headers = {"Accept": "application/json", "User-Agent": f"WojtekStock/{VERSION}"}
    if mode == "basic":
        tok = base64.b64encode(f"{CFG['woo_key']}:{CFG['woo_secret']}".encode()).decode()
        headers["Authorization"] = "Basic " + tok
    else:
        params["consumer_key"], params["consumer_secret"] = CFG["woo_key"], CFG["woo_secret"]
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    url = WOO_API + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    with _woo_lock:  # one request at a time, with a small gap: the store is a shared, live website
        wait = CFG["woo_gap"] - (time.time() - _woo_last[0])
        if wait > 0:
            time.sleep(wait)
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=CFG["woo_timeout"]) as r:
                raw, hdrs = r.read(), dict(r.headers)
        finally:
            _woo_last[0] = time.time()
    try:
        return json.loads(raw or b"null"), hdrs
    except ValueError:
        raise WooError("WooCommerce answered with a web page instead of data (a firewall, maintenance or login page?)",
                       transient=True)


def woo(method, path, params=None, body=None, tries=4):
    if not WOO_READY:
        raise WooError("WooCommerce isn't set up: fill in WOO_URL, WOO_KEY and WOO_SECRET in stock.env")
    mode = _woo_auth["mode"] or ("query" if CFG["woo_auth"] == "query" else "basic")
    last = None
    switched = False
    for attempt in range(tries):
        try:
            res = _woo_once(method, path, params, body, mode)
            _woo_auth["mode"] = mode
            return res
        except urllib.error.HTTPError as e:
            raw = e.read()[:2000]
            code, msg = "", ""
            try:
                j = json.loads(raw)
                code, msg = str(j.get("code") or ""), str(j.get("message") or "")
            except ValueError:
                msg = ""
            if e.code == 401 and CFG["woo_auth"] == "auto" and mode == "basic" and not switched:
                # some hosts drop the Authorization header; WooCommerce also takes the key as query parameters
                mode, switched = "query", True
                continue
            if e.code in (429, 500, 502, 503, 504):
                last = WooError(f"WooCommerce is busy or having trouble ({e.code}{': ' + msg if msg else ''})",
                                e.code, code, transient=True)
                ra = e.headers.get("Retry-After") if e.headers else None
                time.sleep(min(30, float(ra)) if ra and ra.isdigit() else min(20, 2 * (2 ** attempt)))
                continue
            if e.code in (401, 403):
                raise WooError(msg or ("WooCommerce refused the API key" + (" (check that it's Read/Write)" if method != "GET" else "")),
                               e.code, code)
            if e.code == 404:
                raise WooError(msg or "Not found in WooCommerce", 404, code or "not_found")
            raise WooError(msg or f"WooCommerce said {e.code}", e.code, code)
        except WooError as e:
            last = e
            if e.transient and attempt < tries - 1:
                time.sleep(min(20, 2 * (2 ** attempt)))
                continue
            raise
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            last = WooError(f"Can't reach the store ({getattr(e, 'reason', e)})", transient=True)
            if attempt < tries - 1:
                time.sleep(min(20, 2 * (2 ** attempt)))
                continue
            raise last
    raise last or WooError("WooCommerce didn't answer")


def woo_pages(path, params, limit_pages=1000, on_page=None):
    """Every page of a list endpoint, 100 at a time. on_page(page, pages, items) after each one, for progress."""
    params = dict(params, per_page=100)
    page = 1
    while page <= limit_pages:
        params["page"] = page
        data, hdrs = woo("GET", path, params)
        if not isinstance(data, list):
            raise WooError("WooCommerce answered with something that isn't a list", transient=True)
        for x in data:
            yield x
        h = {k.lower(): v for k, v in hdrs.items()}
        total = int(h.get("x-wp-totalpages", "1") or 1)
        if on_page:
            on_page(page, total, int(h.get("x-wp-total", "0") or 0))
        if page >= total or not data:
            return
        page += 1


PROD_FIELDS = ("id,name,type,status,sku,global_unique_id,stock_quantity,manage_stock,categories,parent_id,"
               "date_modified_gmt,price,variations")
VAR_FIELDS = "id,sku,global_unique_id,stock_quantity,manage_stock,status,attributes,date_modified_gmt,price,name"


def _int_or_none(v):
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None


def product_row(p):
    cats = [c.get("id") for c in (p.get("categories") or []) if isinstance(c, dict) and c.get("id") is not None]
    return {"id": int(p["id"]), "parent_id": 0, "type": p.get("type") or "simple", "name": p.get("name") or "",
            "attrs": "", "sku": p.get("sku") or "", "gtin": p.get("global_unique_id") or "",
            "stock": _int_or_none(p.get("stock_quantity")), "manage": "yes" if p.get("manage_stock") is True else "no",
            "status": p.get("status") or "", "cats": json.dumps(cats), "price": str(p.get("price") or ""),
            "modified": p.get("date_modified_gmt") or ""}


def variation_row(v, parent_id, parent_name):
    attrs = ", ".join(f"{a.get('name')}: {a.get('option')}" for a in (v.get("attributes") or [])
                      if isinstance(a, dict) and a.get("option"))
    ms = v.get("manage_stock")
    return {"id": int(v["id"]), "parent_id": int(parent_id), "type": "variation",
            "name": parent_name or v.get("name") or "", "attrs": attrs, "sku": v.get("sku") or "",
            "gtin": v.get("global_unique_id") or "", "stock": _int_or_none(v.get("stock_quantity")),
            "manage": "yes" if ms is True else "parent" if ms == "parent" else "no", "status": v.get("status") or "",
            "cats": "[]", "price": str(v.get("price") or ""), "modified": v.get("date_modified_gmt") or ""}


def note_cats(products):
    seen = {}
    for p in products:
        for c in p.get("categories") or []:
            if isinstance(c, dict) and c.get("id") is not None:
                seen[int(c["id"])] = (c.get("name") or "", c.get("slug") or "")
    if seen:
        with DB_LOCK:
            conn.executemany("INSERT OR REPLACE INTO cats(id,name,slug) VALUES(?,?,?)",
                             [(i, n, s) for i, (n, s) in seen.items()])


def upsert_rows(rows, at):
    if not rows:
        return
    with Tx() as c:
        for r in rows:
            c.execute("""INSERT INTO items(id,parent_id,type,name,attrs,sku,skey,gtin,gkey,stock,manage,status,cats,price,
                         modified,seen_at,scope,why) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'','')
                         ON CONFLICT(id) DO UPDATE SET parent_id=excluded.parent_id, type=excluded.type,
                         name=excluded.name, attrs=excluded.attrs, sku=excluded.sku, skey=excluded.skey,
                         gtin=excluded.gtin, gkey=excluded.gkey, stock=excluded.stock, manage=excluded.manage,
                         status=excluded.status, cats=excluded.cats, price=excluded.price, modified=excluded.modified,
                         seen_at=excluded.seen_at""",
                      (r["id"], r["parent_id"], r["type"], r["name"], r["attrs"], r["sku"], skey(r["sku"]), r["gtin"],
                       gkey(r["gtin"]), r["stock"], r["manage"], r["status"], r["cats"], r["price"], r["modified"], at))
    bump(items=True)


# ---------------------------------------------------------------- sync progress (in memory: the header and Setup tab show it)
PROG = {}
_prog_lock = threading.Lock()


def prog(**kw):
    """Update the running sync's progress: kind (full/quick), phase, detail, done and total (requests), exact."""
    with _prog_lock:
        if kw.pop("start", False):
            PROG.clear()
            PROG.update(started=time.time(), done=0, total=1, phase="Starting", detail="", exact=False)
        PROG.update(kw)
    bump()


def prog_end():
    with _prog_lock:
        PROG.clear()
    bump()


def prog_public():
    with _prog_lock:
        if not PROG:
            return None
        p = dict(PROG)
    done, total = p["done"], max(1, p["total"])
    elapsed = time.time() - p["started"]
    out = {"kind": p.get("kind"), "phase": p["phase"], "detail": p["detail"], "done": done, "total": total,
           "elapsed": int(elapsed), "pct": None, "eta": None}
    if p.get("exact"):
        out["pct"] = max(0, min(99, int(done * 100 / total)))
        if done >= 3 and done < total:
            out["eta"] = int(elapsed / done * (total - done))
    return out


# ---------------------------------------------------------------- sales history (for reports): each order's lines
ORDER_FIELDS = "id,status,date_created_gmt,date_modified_gmt,created_via,meta_data,line_items"
NOT_SALES = ("cancelled", "failed", "refunded", "trash", "checkout-draft", "pending", "draft")


def order_channel(o):
    """Where an order came from: GunBroker (brought in by Wojtek Stock), the register (WooCommerce POS), the website."""
    for m in o.get("meta_data") or []:
        if isinstance(m, dict) and m.get("key") == "_gunbroker_order_id":
            return "GunBroker"
    via = str(o.get("created_via") or "").lower()
    if "pos" in via:
        return "Register"
    if via in ("checkout", "store-api", ""):
        return "Website"
    return via.replace("-", " ").capitalize()


def record_sales(orders):
    """Each order's lines, kept per product (a variation counts as itself). An order cancelled or refunded later is
    taken back out."""
    rows, gone = [], []
    for o in orders:
        oid = int(o.get("id") or 0)
        if not oid:
            continue
        gone.append(oid)
        if o.get("status") in NOT_SALES:
            continue
        d = parse_iso(o.get("date_created_gmt"))
        day = d.astimezone(LOCAL_TZ).strftime("%Y-%m-%d") if d else today_local_day()
        ch = order_channel(o)
        per = {}
        for li in o.get("line_items") or []:
            iid = int(li.get("variation_id") or 0) or int(li.get("product_id") or 0)
            if not iid:
                continue
            q_, t_ = per.get(iid, (0.0, 0.0))
            try:
                per[iid] = (q_ + float(li.get("quantity") or 0), t_ + float(li.get("total") or 0))
            except (TypeError, ValueError):
                continue
        rows += [(oid, iid, day, qn, round(tot, 2), ch) for iid, (qn, tot) in per.items()]
    if not gone:
        return 0
    with Tx() as c:
        c.executemany("DELETE FROM sales WHERE order_id=?", [(g,) for g in gone])
        c.executemany("INSERT OR REPLACE INTO sales(order_id,item_id,day,qty,total,channel) VALUES(?,?,?,?,?,?)", rows)
    return len(rows)


def today_local_day():
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")


SALES_LOCK = threading.Lock()


def load_sales_history(days=400, by="Wojtek Stock"):
    """Read the past orders once (and on request), for the sales reports: page by page, in the background."""
    if not SALES_LOCK.acquire(blocking=False):
        raise ApiError(409, "Sales history is already being read")
    try:
        after = (datetime.now(timezone.utc) - timedelta(days=int(days))).strftime("%Y-%m-%dT%H:%M:%S")
        meta_set("sales_history", {"running": True, "from": after[:10], "pages": 0, "started": now_iso()})
        bump()
        n = 0

        def page(p, total, items):
            meta_set("sales_history", {"running": True, "from": after[:10], "pages": p, "of": total, "orders": items, "started": now_iso()})
            bump()
        batch = []
        for o in woo_pages("/orders", {"after": after, "dates_are_gmt": "true", "status": "any", "_fields": ORDER_FIELDS}, on_page=page):
            batch.append(o)
            if len(batch) >= 200:
                n += record_sales(batch)
                batch = []
        n += record_sales(batch)
        meta_set("sales_history", {"running": False, "from": after[:10], "loaded": now_iso(), "lines": n})
        log.info("Sales history: %d order lines since %s", n, after[:10])
        bump()
        return n
    except WooError as e:
        meta_set("sales_history", {"running": False, "error": str(e), "from": after[:10]})
        bump()
        raise
    finally:
        SALES_LOCK.release()


# ---------------------------------------------------------------- sync: the local copy of the product list
SYNC_LOCK = threading.Lock()
_sync_now = {"full": False, "inc": False}


def sync_status(**kw):
    s = meta_get("sync", {}) or {}
    s.update(kw)
    meta_set("sync", s)
    bump()
    return s


def _stock_snapshot():
    return {r["id"]: (r["stock"], r["seen_at"]) for r in q("SELECT id, stock, seen_at FROM items WHERE scope IN ('shop','readonly')")}


def note_drift(before):
    """Stock that changed since the last look without a WooCommerce order or a Wojtek Stock change explaining it:
    an edit in wp-admin, another app or plugin (BuckFFL, a marketplace sync), or a POS that doesn't make orders."""
    rows = q("SELECT id, stock, name, attrs, sku FROM items WHERE scope IN ('shop','readonly')")
    found = []
    for r in rows:
        if r["id"] not in before:
            continue
        old, seen = before[r["id"]]
        if old is None or r["stock"] is None or old == r["stock"]:
            continue
        since = (parse_iso(seen) or datetime.now(timezone.utc)) - timedelta(minutes=5)
        s_iso = since.isoformat(timespec="seconds")
        if q1("SELECT 1 FROM order_touch WHERE item_id=? AND at>=?", r["id"], s_iso):
            continue
        if q1("SELECT 1 FROM jobs WHERE item_id=? AND status='done' AND done_at>=?", r["id"], s_iso):
            continue
        found.append((now_iso(), r["id"], r["name"] + (f" ({r['attrs']})" if r["attrs"] else ""), r["sku"], old, r["stock"]))
    if found:
        with DB_LOCK:
            conn.executemany("INSERT INTO drift(at,item_id,name,sku,before,after) VALUES(?,?,?,?,?,?)", found)
        log.info("Stock changed outside sales and Wojtek Stock on %d item(s)", len(found))
    with DB_LOCK:
        conn.execute("DELETE FROM drift WHERE at<?", ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds"),))
        conn.execute("DELETE FROM order_touch WHERE at<?", ((datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds"),))
    return len(found)


def refresh_variations(parent_ids, at, base=None):
    """Read every variation of these variable products again (and drop ones that are gone). base: requests already
    done by this sync, when it reports progress."""
    n = 0
    parent_ids = list(parent_ids)
    for i, pid in enumerate(parent_ids):
        par = q1("SELECT name FROM items WHERE id=?", pid)
        if base is not None:
            prog(phase="Reading variations", detail=f"product {i + 1} of {len(parent_ids)}" + (f": {par['name']}" if par else ""),
                 done=base + i)
        try:
            vs = list(woo_pages(f"/products/{pid}/variations", {"_fields": VAR_FIELDS}))
        except WooError as e:
            if e.status == 404:
                with DB_LOCK:
                    conn.execute("DELETE FROM items WHERE parent_id=? AND type='variation'", (pid,))
                continue
            raise
        upsert_rows([variation_row(v, pid, par["name"] if par else "") for v in vs], at)
        keep = {int(v["id"]) for v in vs}
        with DB_LOCK:
            old = [r["id"] for r in conn.execute("SELECT id FROM items WHERE parent_id=? AND type='variation'", (pid,))]
            gone = [i for i in old if i not in keep]
            if gone:
                conn.executemany("DELETE FROM items WHERE id=?", [(i,) for i in gone])
        n += len(vs)
    return n


def in_shop_variable_parents(ids=None):
    """Variable products whose variations belong in the local copy: not feed, not guns."""
    rows = q("SELECT id FROM items WHERE type='variable' AND scope='parent'")
    out = [r["id"] for r in rows]
    return [i for i in out if ids is None or i in ids]


def full_sync(reason="scheduled"):
    if not SYNC_LOCK.acquire(blocking=False):
        raise ApiError(409, "A sync is already running")
    try:
        started = now_iso()
        sync_status(running="full", started=started, error="")
        t0 = time.time()
        n_var = len(in_shop_variable_parents()) if setup_confirmed() else 0
        prog(start=True, kind="full", phase="Reading products", total=int(meta_get("last_full_steps", 0) or 0) or 250)

        def on_page(page, pages, items):
            prog(phase="Reading products", detail=f"page {page} of {pages} ({min(items, page * 100):,} of {items:,} products)",
                 done=page, total=pages + n_var + 1, exact=True)
        before = _stock_snapshot()
        prods = list(woo_pages("/products", {"status": "any", "orderby": "id", "order": "asc", "_fields": PROD_FIELDS},
                               on_page=on_page))
        pages = -(-len(prods) // 100) or 1
        prev = q1("SELECT COUNT(*) n FROM items WHERE type!='variation'")["n"]
        if not prods or (prev >= 50 and len(prods) < prev * 0.5 and not CFG["allow_big_drop"]):
            msg = (f"WooCommerce returned {len(prods)} products, far fewer than the {prev} in the local copy. "
                   "Kept the local copy. If this drop is real, set ALLOW_BIG_DROP=on in stock.env for one sync.")
            log.warning(msg)
            sync_status(running="", error=msg, last_try=started)
            return {"ok": False, "error": msg}
        note_cats(prods)
        upsert_rows([product_row(p) for p in prods], started)
        seen = {int(p["id"]) for p in prods}
        with DB_LOCK:
            old = [r["id"] for r in conn.execute("SELECT id FROM items WHERE type!='variation'")]
            gone = [i for i in old if i not in seen]
            if gone:
                conn.executemany("DELETE FROM items WHERE id=? OR parent_id=?", [(i, i) for i in gone])
        rescope()
        nvar = 0
        if setup_confirmed():  # variations only once the feed prefixes are known: feed products can have thousands
            parents = in_shop_variable_parents()
            prog(total=pages + len(parents) + 1, done=pages)
            nvar = refresh_variations(parents, started, base=pages)
            rescope()
            pages += len(parents)
        else:
            with DB_LOCK:
                conn.execute("DELETE FROM items WHERE type='variation'")
        prog(phase="Finishing", detail="checking for stock changed outside sales", done=pages)
        drifted = note_drift(before)
        meta_set("last_full", started)
        meta_set("last_sync", started)
        meta_set("last_full_steps", pages + 1)
        sync_status(running="", error="", last_ok=now_iso(), last_full=started, products=len(prods), variations=nvar,
                    full_secs=int(time.time() - t0))
        log.info("Full sync (%s): %d products, %d variations, %d removed, %d unexplained changes",
                 reason, len(prods), nvar, len(gone), drifted)
        bump(items=True)
        return {"ok": True, "products": len(prods), "variations": nvar, "removed": len(gone), "drift": drifted}
    except WooError as e:
        sync_status(running="", error=str(e), last_try=now_iso())
        log.warning("Full sync failed: %s", e)
        raise
    finally:
        prog_end()
        SYNC_LOCK.release()


def refresh_products(ids, at):
    ids = list(ids)
    rows = []
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        got = list(woo_pages("/products", {"include": ",".join(map(str, chunk)), "status": "any", "_fields": PROD_FIELDS}))
        note_cats(got)
        rows += [product_row(p) for p in got]
    upsert_rows(rows, at)
    return rows


def inc_sync(reason="scheduled"):
    """Products changed since the last look, plus everything on orders changed since then (a sale changes stock
    without always changing the product's modified date)."""
    if not meta_get("last_full"):
        return full_sync("first")
    if not SYNC_LOCK.acquire(blocking=False):
        raise ApiError(409, "A sync is already running")
    try:
        started = now_iso()
        since = (parse_iso(meta_get("last_sync")) or datetime.now(timezone.utc) - timedelta(hours=1)) - timedelta(minutes=2)
        since_s = since.strftime("%Y-%m-%dT%H:%M:%S")
        sync_status(running="quick", started=started, error="")
        prog(start=True, kind="quick", phase="Products changed since the last sync")
        before = _stock_snapshot()
        changed = list(woo_pages("/products", {"status": "any", "modified_after": since_s, "dates_are_gmt": "true",
                                               "_fields": PROD_FIELDS},
                                 on_page=lambda p, t, n: prog(detail=f"{n:,} product{'' if n == 1 else 's'}")))
        prog(phase="Orders since the last sync", detail="")
        note_cats(changed)
        upsert_rows([product_row(p) for p in changed], started)
        orders = list(woo_pages("/orders", {"modified_after": since_s, "dates_are_gmt": "true", "status": "any",
                                            "_fields": ORDER_FIELDS}))
        record_sales(orders)
        process_kit_sales(orders)
        touched, var_parents = set(), set()
        touch_rows = []
        for o in orders:
            for li in o.get("line_items") or []:
                pid, vid = int(li.get("product_id") or 0), int(li.get("variation_id") or 0)
                if vid:
                    var_parents.add(pid)
                    touch_rows.append((vid, int(o.get("id") or 0), started))
                elif pid:
                    touched.add(pid)
                    touch_rows.append((pid, int(o.get("id") or 0), started))
        if touch_rows:
            with DB_LOCK:
                conn.executemany("INSERT OR REPLACE INTO order_touch(item_id,order_id,at) VALUES(?,?,?)", touch_rows)
        keep = {r["id"] for r in q("SELECT id FROM items WHERE scope IN ('shop','readonly','parent')")}
        changed_ids = {int(p["id"]) for p in changed}
        simple = [i for i in touched if i in keep and i not in changed_ids]
        prog(phase="Re-reading sold products", detail=f"{len(orders)} order{'' if len(orders) == 1 else 's'}, {len(simple) + len(var_parents)} product{'' if len(simple) + len(var_parents) == 1 else 's'}")
        if simple:
            refresh_products(simple, started)
        rescope()
        if setup_confirmed():
            parents = set(in_shop_variable_parents()) & (var_parents | changed_ids)
            refresh_variations(sorted(parents), started)
            rescope()
        drifted = note_drift(before)
        kits_refresh()
        watched = {r["order_id"] for r in q("SELECT DISTINCT order_id FROM jobs WHERE kind='return' AND restock=1 AND status='done'")}
        for oid in sorted({int(o.get("id") or 0) for o in orders} & watched):
            check_double_restock(oid)
        meta_set("last_sync", started)
        sync_status(running="", error="", last_ok=now_iso(), last_quick=started)
        if changed or orders:
            log.info("Quick sync (%s): %d products changed, %d orders", reason, len(changed), len(orders))
        return {"ok": True, "changed": len(changed), "orders": len(orders), "drift": drifted}
    except WooError as e:
        sync_status(running="", error=str(e), last_try=now_iso())
        log.warning("Quick sync failed: %s", e)
        raise
    finally:
        prog_end()
        SYNC_LOCK.release()


def sync_loop():
    time.sleep(3)
    while True:
        try:
            if WOO_READY:
                last_full = parse_iso(meta_get("last_full"))
                last_sync = parse_iso(meta_get("last_sync"))
                now = datetime.now(timezone.utc)
                local = now.astimezone(LOCAL_TZ)
                h, _, m = CFG["full_sync_time"].partition(":")
                due_at = local.replace(hour=int(h or 3), minute=int(m or 0), second=0, microsecond=0)
                if due_at > local:
                    due_at -= timedelta(days=1)
                want_full = _sync_now["full"] or not last_full or last_full < due_at.astimezone(timezone.utc)
                synced = False
                if want_full:
                    _sync_now["full"] = False
                    full_sync("requested" if last_full else "first")
                    synced = True
                elif _sync_now["inc"] or not last_sync or (now - last_sync).total_seconds() >= CFG["sync_minutes"] * 60:
                    _sync_now["inc"] = False
                    inc_sync()
                    synced = True
                if synced and setup_confirmed():
                    reorder_run()   # parts that just fell to their reorder point go on their vendor's automatic draft
        except (WooError, ApiError):
            pass
        except Exception as e:
            log.exception("Sync loop: %s", e)
            sync_status(running="", error=f"Unexpected sync error: {e}")
        for _ in range(30):
            if _sync_now["full"] or _sync_now["inc"]:
                break
            time.sleep(1)


# ---------------------------------------------------------------- live reads (the truth, straight from WooCommerce)
def live_one(item_id, parent_id):
    """The item as WooCommerce has it right now, plus its parent for a variation. Updates the local copy too."""
    at = now_iso()
    if parent_id:
        par, _ = woo("GET", f"/products/{parent_id}", {"_fields": PROD_FIELDS})
        v, _ = woo("GET", f"/products/{parent_id}/variations/{item_id}", {"_fields": VAR_FIELDS})
        prow, row = product_row(par), variation_row(v, parent_id, par.get("name") or "")
        upsert_rows([prow, row], at)
        return row, prow
    p, _ = woo("GET", f"/products/{item_id}", {"_fields": PROD_FIELDS})
    if p.get("type") == "variation":   # an id that turned out to be a variation
        raise WooError("That id is a variation, not a product", 400, "is_variation")
    row = product_row(p)
    upsert_rows([row], at)
    return row, None


def live_many(ids):
    """Current stock for up to 300 items in a handful of requests: products 100 at a time, variations by parent."""
    ids = [int(i) for i in ids][:300]
    rows = {r["id"]: r for r in q(f"SELECT id, parent_id, type FROM items WHERE id IN ({','.join('?' * len(ids))})", *ids)} if ids else {}
    at = now_iso()
    simple = [i for i in ids if i in rows and not rows[i]["parent_id"]]
    by_parent = {}
    for i in ids:
        if i in rows and rows[i]["parent_id"]:
            by_parent.setdefault(rows[i]["parent_id"], []).append(i)
    if simple:
        refresh_products(simple, at)
    for pid, vids in by_parent.items():
        par = q1("SELECT name FROM items WHERE id=?", pid)
        vs = list(woo_pages(f"/products/{pid}/variations", {"include": ",".join(map(str, vids)), "_fields": VAR_FIELDS}))
        upsert_rows([variation_row(v, pid, par["name"] if par else "") for v in vs], at)
    rescope()
    out = {}
    for r in q(f"SELECT * FROM items WHERE id IN ({','.join('?' * len(ids))})", *ids) if ids else []:
        out[str(r["id"])] = {"stock": r["stock"], "scope": r["scope"], "why": r["why"], "sku": r["sku"], "gtin": r["gtin"],
                             "fresh": r["seen_at"] == at}
    return out


# ---------------------------------------------------------------- the write guard
STOCK_KINDS = ("receive", "count", "adjust", "return", "sale", "kit", "kitset")


def guard(kind, item, parent):
    """Why this live item must not be changed, or "" if it may. Checked on the LIVE record right before every
    write, whatever the local copy says."""
    prefixes = feed_prefixes()
    if not prefixes:
        return "No feed prefixes are set: finish Setup first"
    if not setup_confirmed():
        return "Setup isn't confirmed yet"
    p = feed_prefix_of(item["sku"], prefixes) or (feed_prefix_of(parent["sku"], prefixes) if parent else "")
    if p:
        return f"Distributor feed product (SKU starts with {p}): Wojtek Stock never changes these"
    cats = set(json.loads((parent or item)["cats"] or "[]"))
    if cats & gun_categories():
        return "Serialized gun category: Wojtek Stock doesn't change guns"
    if item["type"] not in ("simple", "variation"):
        return f"{item['type']} products can't be changed here"
    if parent is not None and parent["type"] != "variable":
        return "The parent product isn't a variable product"
    if item["status"] == "trash":
        return "The product is in the trash"
    if kind in STOCK_KINDS:
        if item["manage"] == "parent":
            return "Stock is managed on the parent product (read-only here)"
        if item["manage"] != "yes" or item["stock"] is None:
            return "WooCommerce doesn't track stock for this product (read-only here)"
    return ""


# ---------------------------------------------------------------- the outbox: every change, in order, with retries
_worker_wake = threading.Event()


def writes_paused():
    return meta_get("writes_paused") or None


def pause_writes(reason):
    meta_set("writes_paused", {"reason": reason, "at": now_iso()})
    log.warning("Writes paused: %s", reason)
    bump()


def job_finish(job_id, status, before=None, after=None, error="", mode=None):
    with DB_LOCK:
        conn.execute("UPDATE jobs SET status=?, before=?, after=?, error=?, mode=COALESCE(?,mode), done_at=? WHERE id=?",
                     (status, None if before is None else str(before), None if after is None else str(after),
                      error, mode, now_iso(), job_id))
    j = q1("SELECT po_id, kind, item_id FROM jobs WHERE id=?", job_id)
    if j and j["po_id"]:
        refresh_po_status(j["po_id"])   # a PO moves to partly received / received as its items are confirmed
    if j and status == "done" and j["kind"] in STOCK_KINDS and j["kind"] != "kitset":
        ks = kits_of_part(j["item_id"])
        if ks:
            kits_refresh(ks)   # a part's stock changed: the kits made from it follow
    bump()


def process_job(job):
    mode = CFG["woo_write"]
    if mode == "off":
        return job_finish(job["id"], "refused", error="WooCommerce writes are off (WOO_WRITE=off in stock.env)", mode=mode)
    if job["kind"] == "note":
        return process_order_note(job, mode)
    try:
        item, parent = live_one(job["item_id"], job["parent_id"])
    except WooError as e:
        if e.status == 404:
            return job_finish(job["id"], "refused", error="This product no longer exists in WooCommerce", mode=mode)
        raise
    why = guard(job["kind"], item, parent)
    if why:
        log.warning("Refused job %s on item %s: %s", job["id"], job["item_id"], why)
        rescope()
        return job_finish(job["id"], "refused", error=why, mode=mode)
    kind = job["kind"]
    if kind in ("receive", "adjust", "return", "sale", "kit"):
        before = item["stock"]
        after = before + int(job["delta"])
        if after < 0 and kind not in ("sale", "kit"):   # a sale already happened: record it, even oversold
            return job_finish(job["id"], "refused", before, after,
                              f"Would leave stock below zero (WooCommerce has {before})", mode)
        field, value = "stock_quantity", after
    elif kind == "kitset":   # a kit's stock: how many can be made from its parts, worked out again now
        before = item["stock"]
        live_many([p["part_id"] for p in kit_parts(job["item_id"])])   # the parts' stock as it is right now
        after = kit_available(job["item_id"])
        if after is None or after == before:
            return job_finish(job["id"], "done" if mode == "on" else "dryrun", before, before, "Already right", mode)
        field, value = "stock_quantity", after
    elif kind == "count":
        before = item["stock"]
        if job["expected"] is not None and before != job["expected"]:
            return job_finish(job["id"], "conflict", before, job["target"],
                              f"Stock changed from {job['expected']} to {before} after the review (a sale?). "
                              "Not changed: count it again.", mode)
        after = int(job["target"])
        if after == before:
            return job_finish(job["id"], "done" if mode == "on" else "dryrun", before, after, "Already matched", mode)
        field, value = "stock_quantity", after
    elif kind == "gtin":
        before, after = item["gtin"] or "", job["value"]
        if before and gkey(before) == gkey(after):
            return job_finish(job["id"], "done" if mode == "on" else "dryrun", before, after, "Already had this barcode", mode)
        if before and not job["replace_ok"]:
            return job_finish(job["id"], "conflict", before, after, f"It already has barcode {before}", mode)
        field, value = "global_unique_id", after
    elif kind == "sku":
        before, after = item["sku"] or "", job["value"]
        if before:
            return job_finish(job["id"], "conflict", before, after, f"It already has SKU {before}", mode)
        if feed_prefix_of(after):
            return job_finish(job["id"], "refused", before, after, "That SKU would start with a feed prefix", mode)
        field, value = "sku", after
    else:
        return job_finish(job["id"], "refused", error=f"Unknown change type {kind}", mode=mode)
    if mode == "dryrun":
        log.info("Dry run: item %s %s %s -> %s", job["item_id"], field, before, after)
        return job_finish(job["id"], "dryrun", before, after, "", mode)
    path = (f"/products/{job['parent_id']}/variations/{job['item_id']}" if job["parent_id"]
            else f"/products/{job['item_id']}")
    try:
        woo("PUT", path, None, {field: value}, tries=3)
    except WooError as e:
        if e.status in (400, 409) or "unique" in (e.code or "") or "invalid" in (e.code or ""):
            return job_finish(job["id"], "failed", before, after, str(e), mode)
        raise
    check, _ = live_one(job["item_id"], job["parent_id"])   # read it back: did it take?
    got = {"stock_quantity": check["stock"], "global_unique_id": check["gtin"], "sku": check["sku"]}[field]
    if str(got) != str(value):
        return job_finish(job["id"], "failed", before, got,
                          f"WooCommerce accepted the change but now shows {got!r} instead of {value!r}", mode)
    log.info("Changed item %s %s %s -> %s (job %s, %s)", job["item_id"], field, before, after, job["id"], job["by"])
    return job_finish(job["id"], "done", before, after, "", mode)


RETRY_WAITS = [15, 30, 60, 120, 300, 600, 900]


def worker_loop():
    while True:
        _worker_wake.wait(2)
        _worker_wake.clear()
        while True:
            if writes_paused():
                break
            job = q1("SELECT * FROM jobs WHERE status='pending' AND next_at<=? ORDER BY id LIMIT 1", time.time())
            if not job:
                break
            with DB_LOCK:
                conn.execute("UPDATE jobs SET status='working', attempts=attempts+1 WHERE id=?", (job["id"],))
            job = q1("SELECT * FROM jobs WHERE id=?", job["id"])
            bump()
            try:
                process_job(job)
            except WooError as e:
                if e.status in (401, 403):
                    with DB_LOCK:
                        conn.execute("UPDATE jobs SET status='pending', error=? WHERE id=?", (str(e), job["id"]))
                    pause_writes(f"WooCommerce refused a change ({e}). Check that the API key is Read/Write, "
                                 "then resume on the Log tab.")
                    break
                n = job["attempts"]
                if n >= len(RETRY_WAITS):
                    job_finish(job["id"], "failed", error=f"Gave up after {n} tries: {e}")
                else:
                    with DB_LOCK:
                        conn.execute("UPDATE jobs SET status='pending', next_at=?, error=? WHERE id=?",
                                     (time.time() + RETRY_WAITS[n - 1], f"Will retry: {e}", job["id"]))
                    bump()
            except Exception as e:
                log.exception("Job %s: %s", job["id"], e)
                job_finish(job["id"], "failed", error=f"Unexpected error: {e}")


def jobs_pending():
    return q1("SELECT COUNT(*) n FROM jobs WHERE status IN ('pending','working')")["n"]


# ---------------------------------------------------------------- people and badges
def norm_code(v):
    return re.sub(r"[^A-Z0-9]", "", str(v or "").upper())[:20]


def add_person(name, code="", by=""):
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:60]
    if not name:
        raise ApiError(400, "Give the person a name")
    code = norm_code(code) or norm_code(name.split(" ")[0])
    if not code:
        raise ApiError(400, "Give the badge a code of letters and digits, such as TREVOR")
    old = q1("SELECT * FROM people WHERE code=?", code)
    if old and old["active"]:
        raise ApiError(409, f"Badge USR-{code} already belongs to {old['name']}")
    with DB_LOCK:
        conn.execute("INSERT OR REPLACE INTO people(code,name,active,created_at) VALUES(?,?,1,?)", (code, name, now_iso()))
    note_change("person", "added", f"{name} (USR-{code})", by=by)
    bump()
    return {"code": code, "name": name}


def import_people(text, by=""):
    """The Locator's People export (Name, Badge, Active, ...) or any CSV with a name and a USR- badge per row."""
    added, skipped = [], 0
    for row in csv.reader(io.StringIO(str(text or ""))):
        cells = [c.strip() for c in row]
        badge = next((c for c in cells if re.fullmatch(r"USR-[A-Za-z0-9]+", c)), "")
        if not badge:
            continue   # a header or blank line
        if any(c.lower() == "removed" for c in cells):
            skipped += 1
            continue
        name = next((c for c in cells if c and c != badge and not re.fullmatch(r"(yes|no|removed|\d{4}-.*)", c, re.I)), "")
        code = norm_code(badge[4:])
        if not name or q1("SELECT 1 FROM people WHERE code=? AND active=1", code):
            skipped += 1
            continue
        with DB_LOCK:
            conn.execute("INSERT OR REPLACE INTO people(code,name,active,created_at) VALUES(?,?,1,?)", (code, name[:60], now_iso()))
        added.append(name)
    if added:
        note_change("person", "imported", f"{len(added)} people", ", ".join(added)[:500], by)
        bump()
    return {"added": len(added), "skipped": skipped}


def remove_person(code, by=""):
    code = norm_code(code)
    p = q1("SELECT * FROM people WHERE code=? AND active=1", code)
    if not p:
        raise ApiError(404, "No such person")
    with DB_LOCK:
        conn.execute("UPDATE people SET active=0 WHERE code=?", (code,))
    note_change("person", "removed", f"{p['name']} (USR-{code})", by=by)
    bump()


def operator_for(code):
    code = norm_code(code)
    if not code:
        return None
    p = q1("SELECT code, name FROM people WHERE code=? AND active=1", code)
    return dict(p) if p else None



# ---------------------------------------------------------------- roles: staff and managers (a badge plus a PIN)
# Staff (everyone, by default) scan, receive, count, adjust, take returns and print labels. A manager who scans their
# badge and enters their PIN gets manager powers on that station until they sign out or step away (30 idle minutes on
# the server; the page signs out after 10): purchasing, vendor invoices and payments, reports, the full count, settings
# and people. The shop password always has everything (it's how the first managers are set up, and the way back in).
MGR_IDLE = 30 * 60
PIN_TRIES, PIN_LOCK = 5, 15 * 60


def pin_hash(pin, salt=None):
    salt = salt or secrets.token_hex(8)
    return f"pbkdf2${salt}${hashlib.pbkdf2_hmac('sha256', pin.encode(), salt.encode(), 120_000).hex()}"


def pin_ok(pin, stored):
    try:
        _, salt, _h = stored.split("$")
    except (AttributeError, ValueError):
        return False
    return hmac.compare_digest(pin_hash(pin, salt), stored)


def set_role(code, role, by=""):
    code = norm_code(code)
    p = q1("SELECT * FROM people WHERE code=? AND active=1", code)
    if not p:
        raise ApiError(404, "No such person")
    if role not in ("staff", "manager"):
        raise ApiError(400, "A role is staff or manager")
    with DB_LOCK:
        conn.execute("UPDATE people SET role=? WHERE code=?", (role, code))
        if role == "staff":
            conn.execute("DELETE FROM manager_sessions WHERE code=?", (code,))   # their manager sessions end now
    note_change("person", "role", f"{p['name']} (USR-{code})", role, by)
    bump()


def set_pin(code, pin, by=""):
    code = norm_code(code)
    p = q1("SELECT * FROM people WHERE code=? AND active=1", code)
    if not p:
        raise ApiError(404, "No such person")
    pin = str(pin or "")
    if not re.fullmatch(r"\d{4,8}", pin):
        raise ApiError(400, "A PIN is 4 to 8 digits")
    if len(set(pin)) == 1 or pin in "0123456789" or pin in "9876543210":
        raise ApiError(400, "Pick a PIN that isn't all one digit or a run like 1234")
    with DB_LOCK:
        conn.execute("UPDATE people SET pin_hash=?, pin_fails=0, pin_locked_until=0 WHERE code=?", (pin_hash(pin), code))
        conn.execute("DELETE FROM manager_sessions WHERE code=?", (code,))
    note_change("person", "PIN set", f"{p['name']} (USR-{code})", by=by)
    bump()


def manager_login(code, pin, where=""):
    code = norm_code(code)
    p = q1("SELECT * FROM people WHERE code=? AND active=1", code)
    if not p or p["role"] != "manager":
        raise ApiError(403, "That badge isn't a manager's")
    if not p["pin_hash"]:
        raise ApiError(403, "No PIN set yet: set one on the People tab (with the shop password)")
    if (p["pin_locked_until"] or 0) > time.time():
        mins = int(((p["pin_locked_until"] or 0) - time.time()) // 60) + 1
        raise ApiError(429, f"Too many wrong PINs: try again in {mins} minute{'s' if mins != 1 else ''}")
    if not pin_ok(str(pin or ""), p["pin_hash"]):
        fails = (p["pin_fails"] or 0) + 1
        with DB_LOCK:
            conn.execute("UPDATE people SET pin_fails=?, pin_locked_until=? WHERE code=?",
                         (0 if fails >= PIN_TRIES else fails, time.time() + PIN_LOCK if fails >= PIN_TRIES else 0, code))
        if fails >= PIN_TRIES:
            log.warning("Manager PIN locked for %s after %d wrong tries", p["name"], fails)
            raise ApiError(429, f"Too many wrong PINs: locked for {PIN_LOCK // 60} minutes")
        raise ApiError(403, f"Wrong PIN ({PIN_TRIES - fails} tr{'ies' if PIN_TRIES - fails != 1 else 'y'} left)")
    token = secrets.token_urlsafe(24)
    with DB_LOCK:
        conn.execute("UPDATE people SET pin_fails=0 WHERE code=?", (code,))
        conn.execute("DELETE FROM manager_sessions WHERE last_seen<?", (time.time() - MGR_IDLE,))
        conn.execute("INSERT INTO manager_sessions(token_hash,code,created_at,last_seen,where_) VALUES(?,?,?,?,?)",
                     (token_hash(token), code, now_iso(), time.time(), where))
    note_change("person", "manager sign-in", f"{p['name']} (USR-{code})", where, p["name"])
    return {"token": token, "name": p["name"], "code": code}


def manager_for(token):
    """The active manager behind a manager token, if it's current (and refresh it)."""
    if not token or len(token) > 100:
        return None
    s_ = q1("SELECT * FROM manager_sessions WHERE token_hash=?", token_hash(token))
    if not s_ or s_["last_seen"] < time.time() - MGR_IDLE:
        return None
    p = q1("SELECT code, name, role FROM people WHERE code=? AND active=1", s_["code"])
    if not p or p["role"] != "manager":
        return None
    if time.time() - s_["last_seen"] > 30:
        with DB_LOCK:
            conn.execute("UPDATE manager_sessions SET last_seen=? WHERE token_hash=?", (time.time(), s_["token_hash"]))
    return dict(p)


# ---------------------------------------------------------------- terminals (a station's own key, instead of the password)
TERMINAL_COOKIE = "stock_terminal"
_seen_at = {}


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def terminal_for(token):
    if not token or len(token) > 200:
        return None
    return q1("SELECT * FROM terminals WHERE token_hash=? AND active=1", token_hash(token))


def note_seen(term, ip):
    now = time.time()
    if now - _seen_at.get(term["id"], 0) > 60:
        _seen_at[term["id"]] = now
        with DB_LOCK:
            conn.execute("UPDATE terminals SET last_seen=?, last_ip=? WHERE id=?", (now_iso(), ip, term["id"]))


def add_terminal(name, by=""):
    name = str(name or "").strip()[:60]
    if not name:
        raise ApiError(400, "Give the terminal a name, such as Back room")
    token = secrets.token_urlsafe(32)
    tid = uuid.uuid4().hex[:10]
    with DB_LOCK:
        conn.execute("INSERT INTO terminals(id,name,token_hash,active,created_at) VALUES(?,?,?,1,?)",
                     (tid, name, token_hash(token), now_iso()))
    note_change("terminal", "added", name, by=by)
    bump()
    return {"id": tid, "name": name, "path": "/terminal/" + token}


def revoke_terminal(tid, by=""):
    t = q1("SELECT * FROM terminals WHERE id=? AND active=1", tid)
    if not t:
        raise ApiError(404, "No such terminal")
    with DB_LOCK:
        conn.execute("UPDATE terminals SET active=0 WHERE id=?", (tid,))
    note_change("terminal", "revoked", t["name"], by=by)
    bump()


# ---------------------------------------------------------------- station actions: receive, count, adjust, link, SKU
def _item(i):
    r = q1("SELECT * FROM items WHERE id=?", int(i))
    if not r:
        raise ApiError(404, f"Item {i} isn't in the local copy (sync, then scan again)")
    return r


def label_name(r):
    return r["name"] + (f" ({r['attrs']})" if r["attrs"] else "")


def _int(v, lo, hi, what):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ApiError(400, f"{what} should be a whole number")
    if not lo <= n <= hi:
        raise ApiError(400, f"{what} should be between {lo} and {hi}")
    return n


def check_can_write():
    if not setup_confirmed():
        raise ApiError(409, "Writes are locked until Setup is confirmed (Setup tab)")


def queue_jobs(kind, specs, by, operator, reason="", note=""):
    batch = uuid.uuid4().hex[:10]
    at = now_iso()
    ids = []
    with Tx() as c:
        for s in specs:
            r = s["item"]
            cur = c.execute("""INSERT INTO jobs(batch,at,kind,item_id,parent_id,name,sku,delta,target,expected,value,
                               replace_ok,reason,note,by,operator,status,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)""",
                            (batch, at, kind, r["id"], r["parent_id"] or 0, label_name(r), r["sku"], s.get("delta"),
                             s.get("target"), s.get("expected"), s.get("value"), 1 if s.get("replace") else 0,
                             s.get("reason", reason), note, by, operator, CFG["woo_write"]))
            ids.append(cur.lastrowid)
    _worker_wake.set()
    bump()
    return {"batch": batch, "jobs": ids}


def commit(body, by, operator):
    check_can_write()
    kind = body.get("kind")
    lines = body.get("lines") or []
    if kind not in STOCK_KINDS:
        raise ApiError(400, "Unknown change type")
    if not isinstance(lines, list) or not lines:
        raise ApiError(400, "Nothing to change")
    if len(lines) > 500:
        raise ApiError(400, "Up to 500 lines at a time")
    note = str(body.get("note") or "").strip()[:500]
    reason = ""
    if kind == "adjust":
        direction = body.get("dir")
        if direction not in ("down", "up"):
            raise ApiError(400, "Pick Decrease or Increase")
        allowed = CFG["down_reasons"] if direction == "down" else CFG["up_reasons"]
        reason = str(body.get("reason") or "")
        if reason not in allowed:
            raise ApiError(400, "Pick a reason")
        if reason.lower() == "other" and not note:
            raise ApiError(400, "Add a note saying why (the reason is Other)")
    elif kind == "receive":
        reason = "Received"
    else:
        reason = "Count"
    specs, seen = [], set()
    for ln in lines:
        r = _item(ln.get("id"))
        if r["id"] in seen:
            raise ApiError(400, f"{label_name(r)} is on the list twice")
        seen.add(r["id"])
        if r["scope"] != "shop":
            raise ApiError(409, f"{label_name(r)} can't be changed: {r['why'] or r['scope']}")
        if is_kit(r["id"]):
            raise ApiError(409, f"{label_name(r)} is a kit: its stock comes from its parts (receive, count or adjust those)")
        if kind == "receive":
            specs.append({"item": r, "delta": _int(ln.get("qty"), 1, 100000, "Quantity")})
        elif kind == "adjust":
            n = _int(ln.get("qty"), 1, 100000, "Quantity")
            specs.append({"item": r, "delta": -n if body["dir"] == "down" else n})
        else:
            specs.append({"item": r, "target": _int(ln.get("counted"), 0, 1000000, "Counted"),
                          "expected": _int(ln.get("expected"), -1000000, 1000000, "Expected stock")})
    return queue_jobs(kind, specs, by, operator, reason, note)


def link_barcode(body, by, operator):
    check_can_write()
    r = _item(body.get("id"))
    code = re.sub(r"[\s-]", "", str(body.get("code") or ""))
    if not gtin_ok(code):
        raise ApiError(400, "Only UPC, EAN or GTIN numbers (8, 12, 13 or 14 digits) can be saved as a product's barcode")
    if r["scope"] not in ("shop", "readonly"):
        raise ApiError(409, f"{label_name(r)} can't be changed: {r['why'] or r['scope']}")
    other = q1("SELECT * FROM items WHERE gkey=? AND id!=?", gkey(code), r["id"]) or \
        q1("SELECT i.* FROM alt_barcodes a JOIN items i ON i.id=a.item_id WHERE a.gkey=? AND a.item_id!=?", gkey(code), r["id"])
    if other:
        raise ApiError(409, f"Barcode {code} already belongs to {label_name(other)}"
                            + (f" (SKU {other['sku']})" if other["sku"] else ""))
    if r["gtin"] and gkey(r["gtin"]) != gkey(code) and not body.get("replace"):
        raise ApiError(409, f"{label_name(r)} already has barcode {r['gtin']}", {"needsReplace": True, "current": r["gtin"]})
    res = queue_jobs("gtin", [{"item": r, "value": code, "replace": bool(body.get("replace"))}], by, operator,
                     "Barcode linked", str(body.get("note") or "")[:500])
    res["checkDigit"] = gtin_check_digit_ok(code)
    return res


def next_skus(n):
    """The next n free SKUs like WW-000123: after the highest in use anywhere in the store, and after any already
    handed out (so a failed or dry-run write never gives the same SKU twice)."""
    pre, digits = CFG["sku_prefix"], CFG["sku_digits"]
    top = int(meta_get("sku_next", 1) or 1) - 1
    for r in q("SELECT skey FROM items WHERE skey LIKE ?", pre + "%"):
        m = re.fullmatch(re.escape(pre) + r"(\d+)", r["skey"] or "")
        if m:
            top = max(top, int(m.group(1)))
    out = [f"{pre}{str(top + 1 + i).zfill(digits)}" for i in range(n)]
    meta_set("sku_next", top + 1 + n)
    return out


def generate_skus(body, by, operator):
    check_can_write()
    if feed_prefix_of(CFG["sku_prefix"] + "0"):
        raise ApiError(409, f"SKU_PREFIX {CFG['sku_prefix']} starts like a feed prefix: change it in stock.env")
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids or len(ids) > 500:
        raise ApiError(400, "Pick 1 to 500 items")
    rows = [_item(i) for i in ids]
    for r in rows:
        if r["sku"]:
            raise ApiError(409, f"{label_name(r)} already has SKU {r['sku']}")
        if r["scope"] not in ("shop", "readonly"):
            raise ApiError(409, f"{label_name(r)} can't be changed: {r['why'] or r['scope']}")
    skus = next_skus(len(rows))
    return queue_jobs("sku", [{"item": r, "value": s} for r, s in zip(rows, skus)], by, operator, "SKU generated")


def retry_job(jid):
    j = q1("SELECT * FROM jobs WHERE id=?", jid)
    if not j or j["status"] != "failed":
        raise ApiError(400, "Only failed changes can be tried again")
    with DB_LOCK:
        conn.execute("UPDATE jobs SET status='pending', next_at=0, attempts=0, error='' WHERE id=?", (jid,))
    _worker_wake.set()
    bump()


def cancel_job(jid, by=""):
    with DB_LOCK:
        n = conn.execute("UPDATE jobs SET status='cancelled', error=?, done_at=? WHERE id=? AND status='pending'",
                         (f"Cancelled by {by}" if by else "Cancelled", now_iso(), jid)).rowcount
    if not n:
        raise ApiError(400, "Only waiting changes can be cancelled")
    bump()


# ---------------------------------------------------------------- returns: scan the order's packing slip, then what came back
# Wojtek Stock puts returned items back in stock itself, and leaves a note on the order saying to refund it without
# ticking "Restock refunded items". It reads the order's notes to see what WooCommerce already restocked (a refund with
# that box ticked, a cancelled order), so nothing is restocked twice; and keeps watching orders it restocked, to flag a
# refund that restocks them again later.
RETURN_NOTE_MARK = "Returned via Wojtek Stock"
_ITEM_NOTE = re.compile(r"Item #(\d+) stock increased from (-?\d+) to (-?\d+)", re.I)
_LEVELS_NOTE = re.compile(r"\(([^()]+)\)\s*(-?\d+)\s*(?:→|->)\s*(-?\d+)")


def order_code_norm(v):
    """A scanned order number: '#12345', 'SS-12345', ' 12345 ' -> ('SS-12345' as typed, '12345' its trailing digits)."""
    s = re.sub(r"\s+", "", str(v or "")).upper().lstrip("#")
    m = re.search(r"(\d+)$", s)
    return s, (m.group(1) if m else "")


def number_matches(order_number, code):
    """Is this the order whose number was scanned? Its number exactly, or the scan's trailing digits when ShipStation
    put a prefix in front (SS-12345 for order 12345)."""
    n = re.sub(r"\s+", "", str(order_number or "")).upper().lstrip("#")
    s, digits = order_code_norm(code)
    if not n or not s:
        return False
    return n == s or bool(digits and n.isdigit() and n.lstrip("0") == digits.lstrip("0"))


def find_order(code):
    """The order with this number. Order numbers usually equal WooCommerce's internal id, but a numbering plugin can
    change that, so an order fetched by id only counts if its number matches too."""
    s, digits = order_code_norm(code)
    if not s or len(s) > 40:
        raise ApiError(400, "That isn't an order number")
    if digits:
        try:
            o, _ = woo("GET", f"/orders/{int(digits)}")
            if isinstance(o, dict) and number_matches(o.get("number"), code):
                return o
        except WooError as e:
            if e.status != 404:
                raise
    found, _ = woo("GET", "/orders", {"search": digits or s, "per_page": 20})
    for o in found or []:
        if isinstance(o, dict) and number_matches(o.get("number"), code):
            return o
    raise ApiError(404, f"No WooCommerce order has number {s}")


def woo_restocks(notes, by_sku):
    """What WooCommerce itself put back in stock for an order, from its notes: a refund with "Restock refunded items"
    ticked ("Item #123 stock increased from 4 to 5."), or a cancelled order ("Stock levels increased: Mug (WW-MUG) 4→5").
    {item_id: [(qty, note_id, note_date, text)]}"""
    out = {}
    for n in notes or []:
        text = html.unescape(re.sub(r"<[^>]+>", " ", str(n.get("note") or "")))
        if not text or text.startswith(RETURN_NOTE_MARK) or "increased" not in text.lower():
            continue
        nid, at = int(n.get("id") or 0), n.get("date_created_gmt") or n.get("date_created") or ""
        for m in _ITEM_NOTE.finditer(text):
            qty = int(m.group(3)) - int(m.group(2))
            if qty > 0:
                out.setdefault(int(m.group(1)), []).append((qty, nid, at, text))
        if re.search(r"stock levels increased", text, re.I):
            for m in _LEVELS_NOTE.finditer(text):
                ref, qty = m.group(1).strip(), int(m.group(3)) - int(m.group(2))
                iid = int(ref[1:]) if re.fullmatch(r"#\d+", ref) else by_sku.get(skey(ref))
                if iid and qty > 0:
                    out.setdefault(iid, []).append((qty, nid, at, text))
    return out


def order_view(o):
    """An order's items, and how many of each can still come back."""
    oid = int(o["id"])
    notes, _ = woo("GET", f"/orders/{oid}/notes")
    refunds, _ = woo("GET", f"/orders/{oid}/refunds", {"per_page": 100})
    lines = {}
    for li in o.get("line_items") or []:
        pid, vid = int(li.get("product_id") or 0), int(li.get("variation_id") or 0)
        iid = vid or pid
        if not iid:
            continue   # a product deleted since the sale
        e = lines.setdefault(iid, {"item": iid, "parent": pid if vid else 0, "name": li.get("name") or "", "sku": li.get("sku") or "",
                                   "sold": 0, "refunded": 0})
        e["sold"] += int(li.get("quantity") or 0)
    for rf in refunds or []:
        for li in rf.get("line_items") or []:
            iid = int(li.get("variation_id") or 0) or int(li.get("product_id") or 0)
            if iid in lines:
                lines[iid]["refunded"] += abs(int(li.get("quantity") or 0))
    by_sku = {skey(e["sku"]): iid for iid, e in lines.items() if e["sku"]}
    restocked = woo_restocks(notes, by_sku)
    prior = q("SELECT item_id, restock, SUM(qty) n FROM jobs WHERE kind='return' AND order_id=? "
              "AND status IN ('pending','working','done','logged') GROUP BY item_id, restock", oid)
    for iid, e in lines.items():
        e["wooRestocked"] = min(e["sold"], sum(x[0] for x in restocked.get(iid, [])))
        e["stockRestocked"] = sum(r["n"] or 0 for r in prior if r["item_id"] == iid and r["restock"])
        e["logged"] = sum(r["n"] or 0 for r in prior if r["item_id"] == iid and not r["restock"])
        e["returnable"] = max(0, e["sold"] - e["wooRestocked"] - e["stockRestocked"] - e["logged"])
        local = q1("SELECT * FROM items WHERE id=?", iid)
        if local:
            e.update(name=label_name(local), sku=local["sku"] or e["sku"], gtin=local["gtin"] or "", stock=local["stock"],
                     scope=local["scope"], why=local["why"] or "", canRestock=local["scope"] == "shop")
        else:
            p = feed_prefix_of(e["sku"])
            e.update(gtin="", stock=None, scope="feed" if p else "unknown", canRestock=False,
                     why=f"Distributor feed product (SKU starts with {p})" if p else "Not in the local copy (a gun, a deleted product, or not synced yet)")
    b = o.get("billing") or {}
    last = (b.get("last_name") or "").strip()
    return {"id": oid, "number": str(o.get("number") or oid), "status": o.get("status") or "",
            "date": o.get("date_created_gmt") or o.get("date_created") or "",
            "customer": ((b.get("first_name") or "").strip() + (f" {last[:1]}." if last else "")).strip(),
            "lines": list(lines.values()), "refunds": len(refunds or [])}


def commit_return(body, by, operator):
    check_can_write()
    oid = _int(body.get("order"), 1, 10 ** 12, "Order")
    try:
        o, _ = woo("GET", f"/orders/{oid}")
    except WooError as e:
        if e.status == 404:
            raise ApiError(404, "That order no longer exists in WooCommerce")
        raise
    view = order_view(o)   # checked again, live: a refund may have restocked something since the scan
    num, lines = view["number"], {e["item"]: e for e in view["lines"]}
    want = body.get("lines") or []
    if not isinstance(want, list) or not want:
        raise ApiError(400, "Scan the items that came back first")
    if len(want) > 200:
        raise ApiError(400, "Up to 200 lines at a time")
    note = str(body.get("note") or "").strip()[:500]
    rows, seen = [], set()
    for ln in want:
        iid = _int(ln.get("item"), 1, 10 ** 12, "Item")
        e = lines.get(iid)
        if not e:
            raise ApiError(400, f"Item {iid} isn't on order #{num}")
        if iid in seen:
            raise ApiError(400, f"{e['name']} is on the list twice")
        seen.add(iid)
        qty = _int(ln.get("qty"), 1, 100000, "Quantity")
        if qty > e["returnable"]:
            done = e["sold"] - e["returnable"]
            raise ApiError(409, f"Only {e['returnable']} of {e['name']} can still come back on order #{num} "
                                f"({e['sold']} sold, {done} already back)")
        restock = bool(ln.get("restock"))
        if restock and not e["canRestock"]:
            raise ApiError(409, f"{e['name']} can't be put back in stock here: {e['why']}")
        rows.append((e, qty, restock))
    batch, at, ids = uuid.uuid4().hex[:10], now_iso(), []
    kit_rows = [(e, qty) for e, qty, restock in rows if restock and is_kit(e["item"])]
    rows = [(e, qty, restock) for e, qty, restock in rows if not (restock and is_kit(e["item"]))]
    with Tx() as c:
        for e, qty in kit_rows:   # a kit coming back: its parts go back in stock, and it's recorded as returned
            cur = c.execute("""INSERT INTO jobs(batch,at,kind,item_id,parent_id,name,sku,qty,restock,order_id,order_number,reason,note,by,
                               operator,status,mode,done_at) VALUES(?,?,'return',?,?,?,?,?,1,?,?,?,?,?,?,'done',?,?)""",
                            (batch, at, e["item"], e["parent"], e["name"], e["sku"], qty, oid, num, "Customer return (a kit: its parts restocked)",
                             note, by, operator, CFG["woo_write"], at))
            ids.append(cur.lastrowid)
            for p in kit_parts(e["item"]):
                if p["scope"] == "shop":
                    cur = c.execute("""INSERT INTO jobs(batch,at,kind,item_id,parent_id,name,sku,delta,reason,note,by,operator,status,mode,order_id,order_number)
                                       VALUES(?,?,'kit',?,?,?,?,?,?,?,?,?,'pending',?,?,?)""",
                                    (batch, at, p["part_id"], (q1("SELECT parent_id FROM items WHERE id=?", p["part_id"]) or {"parent_id": 0})["parent_id"] or 0,
                                     label_name(p), p["sku"], qty * p["qty"], f"Kit returned: {e['name']} (order {num})", note, by, operator,
                                     CFG["woo_write"], oid, num))
                    ids.append(cur.lastrowid)
            c.execute("INSERT INTO kit_sales(order_id,kit_id,taken,returned) VALUES(?,?,0,?) ON CONFLICT(order_id,kit_id) DO UPDATE SET "
                      "returned=returned+excluded.returned, taken=MAX(0, taken-excluded.returned)", (oid, e["item"], qty))
        for e, qty, restock in rows:
            cur = c.execute("""INSERT INTO jobs(batch,at,kind,item_id,parent_id,name,sku,delta,qty,restock,order_id,order_number,
                               reason,note,by,operator,status,mode,done_at,before,after) VALUES(?,?,'return',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (batch, at, e["item"], e["parent"], e["name"], e["sku"], qty if restock else None, qty, 1 if restock else 0,
                             oid, num, "Customer return" if restock else "Customer return (damaged, not restocked)", note, by, operator,
                             "pending" if restock else "logged", CFG["woo_write"], None if restock else at,
                             None, None))
            ids.append(cur.lastrowid)
        cur = c.execute("""INSERT INTO jobs(batch,at,kind,item_id,parent_id,name,sku,order_id,order_number,value,reason,by,operator,
                           status,mode) VALUES(?,?,'note',0,0,?,'',?,?,?,'Order note',?,?,'pending',?)""",
                        (batch, at, f"Order #{num}", oid, num, note, by, operator, CFG["woo_write"]))
        ids.append(cur.lastrowid)
    _worker_wake.set()
    bump()
    return {"batch": batch, "jobs": ids, "order": num}


def process_order_note(job, mode):
    """The note on the order, written once its returns have gone through, saying what actually happened."""
    sib = q("SELECT * FROM jobs WHERE batch=? AND kind='return' ORDER BY id", job["batch"])
    if any(r["status"] in ("pending", "working") for r in sib):
        with DB_LOCK:
            conn.execute("UPDATE jobs SET status='pending', next_at=?, attempts=attempts-1 WHERE id=?", (time.time() + 2, job["id"]))
        return None
    who = re.sub(r"\s*\(.*\)$", "", job["by"] or "") or "someone"
    out, any_back = [], False
    for r in sib:
        what = f"{r['qty']} × {r['name']}" + (f" ({r['sku']})" if r["sku"] else "")
        if not r["restock"]:
            out.append(f"{what}: damaged, not restocked")
        elif r["status"] == "done":
            any_back = True
            out.append(f"{what}: restocked" + (f" ({r['before']} to {r['after']})" if r["before"] is not None else " (as its parts)"))
        elif r["status"] == "dryrun":
            out.append(f"{what}: would be restocked (dry run)")
        else:
            out.append(f"{what}: NOT restocked ({r['error'] or r['status']})")
    text = (f"{RETURN_NOTE_MARK} by {who}:\n" + "\n".join("- " + x for x in out)
            + (f"\nNote: {job['value']}" if job["value"] else "")
            + ("\nWhen refunding, leave \"Restock refunded items\" unticked: Wojtek Stock already put the stock back."
               if any_back else "\nWhen refunding, leave \"Restock refunded items\" unticked: these items aren't going back in stock."))
    if mode == "dryrun":
        return job_finish(job["id"], "dryrun", "", text, "", mode)
    try:
        res, _ = woo("POST", f"/orders/{job['order_id']}/notes", None, {"note": text, "customer_note": False}, tries=3)
    except WooError as e:
        if e.status == 404:
            return job_finish(job["id"], "refused", error="The order no longer exists", mode=mode)
        raise
    return job_finish(job["id"], "done", "", text, "", mode)


def check_double_restock(oid):
    """After Wojtek Stock restocked items from this order, did WooCommerce restock them again (a refund with
    "Restock refunded items" ticked)? Those go on the Setup tab's list."""
    done = q("SELECT item_id, MIN(done_at) first, MAX(order_number) num, MAX(name) name, MAX(sku) sku FROM jobs "
             "WHERE kind='return' AND restock=1 AND status='done' AND order_id=? GROUP BY item_id", oid)
    if not done:
        return 0
    notes, _ = woo("GET", f"/orders/{oid}/notes")
    by_sku = {skey(r["sku"]): r["item_id"] for r in done if r["sku"]}
    restocks = woo_restocks(notes, by_sku)
    n = 0
    for r in done:
        first = parse_iso(r["first"])
        for qty, nid, at, text in restocks.get(r["item_id"], []):
            when_ = parse_iso(at)
            if first and when_ and when_ >= first - timedelta(seconds=5):
                with DB_LOCK:
                    n += conn.execute("INSERT OR IGNORE INTO restock_watch(at,order_id,order_number,item_id,name,qty,note_id,note) "
                                      "VALUES(?,?,?,?,?,?,?,?)", (now_iso(), oid, r["num"], r["item_id"], r["name"], qty, nid, text[:300])).rowcount
    if n:
        log.warning("Order %s: WooCommerce restocked %d item(s) Wojtek Stock had already restocked", oid, n)
        bump()
    return n


# ---------------------------------------------------------------- purchasing: vendors, purchase orders, receiving, costs
# Parts only: a PO can only hold in-shop products whose stock Wojtek Stock manages (never feed products or guns).
# Receiving a PO adds stock through the same outbox as everything else (read live, change, read back, log), and records
# what each unit cost, landed: its price plus its share of the receipt's freight and fees.
PO_STATUSES = ("draft", "sent", "partial", "received", "closed", "cancelled")


def _money(v, what, lo=0.0, hi=1_000_000.0):
    try:
        x = round(float(v), 4)
    except (TypeError, ValueError):
        raise ApiError(400, f"{what} should be an amount, such as 12.50")
    if not lo <= x <= hi:
        raise ApiError(400, f"{what} should be between {lo:g} and {hi:,.0f}")
    return x


def _text(v, most=200):
    return re.sub(r"\s+", " ", str(v or "")).strip()[:most]


def vendor_row(r):
    d = dict(r)
    d["openPos"] = q1("SELECT COUNT(*) n FROM pos WHERE vendor_id=? AND status IN ('draft','sent','partial')", r["id"])["n"]
    d["items"] = q1("SELECT COUNT(*) n FROM vendor_items WHERE vendor_id=?", r["id"])["n"]
    return d


def list_vendors():
    return [vendor_row(r) for r in q("SELECT * FROM vendors WHERE active=1 ORDER BY name COLLATE NOCASE")]


def save_vendor(b, by=""):
    name = _text(b.get("name"), 80)
    if not name:
        raise ApiError(400, "Give the vendor a name")
    email = _text(b.get("email"), 120)
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ApiError(400, "That email address doesn't look right")
    prefix = skey(b.get("feed_prefix"))[:12]
    lead = b.get("lead_days")
    lead = _int(lead, 0, 365, "Lead time (days)") if str(lead or "").strip() else None
    auto = 1 if b.get("auto_send") else 0
    if auto and not email:
        raise ApiError(400, "Sending POs automatically needs the vendor's email address")
    vals = (name, _text(b.get("account"), 60), _text(b.get("contact"), 80), email, _text(b.get("phone"), 40),
            _text(b.get("terms"), 60), lead, prefix, _text(b.get("notes"), 500), auto)
    vid = b.get("id")
    other = q1("SELECT id FROM vendors WHERE name=? COLLATE NOCASE AND active=1 AND id!=?", name, int(vid or 0))
    if other:
        raise ApiError(409, f"There's already a vendor called {name}")
    with DB_LOCK:
        if vid:
            if not q1("SELECT 1 FROM vendors WHERE id=?", int(vid)):
                raise ApiError(404, "No such vendor")
            conn.execute("UPDATE vendors SET name=?, account=?, contact=?, email=?, phone=?, terms=?, lead_days=?, feed_prefix=?, "
                         "notes=?, auto_send=? WHERE id=?", vals + (int(vid),))
        else:
            vid = conn.execute("INSERT INTO vendors(name,account,contact,email,phone,terms,lead_days,feed_prefix,notes,auto_send,"
                               "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", vals + (now_iso(),)).lastrowid
    note_change("vendor", "saved", name, by=by)
    bump()
    return vendor_row(q1("SELECT * FROM vendors WHERE id=?", int(vid)))


def remove_vendor(vid, by=""):
    v = q1("SELECT * FROM vendors WHERE id=? AND active=1", vid)
    if not v:
        raise ApiError(404, "No such vendor")
    if q1("SELECT 1 FROM pos WHERE vendor_id=? AND status IN ('draft','sent','partial')", vid):
        raise ApiError(409, f"{v['name']} has open purchase orders: close or cancel them first")
    with DB_LOCK:
        conn.execute("UPDATE vendors SET active=0 WHERE id=?", (vid,))
    note_change("vendor", "removed", v["name"], by=by)
    bump()


def vendor_for_feed(sku):
    """The vendor whose feed prefix this SKU starts with, if one is set."""
    s = skey(sku)
    best = None
    for v in q("SELECT * FROM vendors WHERE active=1 AND feed_prefix!=''"):
        if s.startswith(v["feed_prefix"]) and (not best or len(v["feed_prefix"]) > len(best["feed_prefix"])):
            best = v
    return best


def remember_vendor_item(vendor_id, item_id, vendor_sku=None, cost=None):
    old = q1("SELECT * FROM vendor_items WHERE vendor_id=? AND item_id=?", vendor_id, item_id)
    vs = vendor_sku if vendor_sku is not None else (old["vendor_sku"] if old else "")
    c = cost if cost is not None else (old["last_cost"] if old else None)
    with DB_LOCK:
        conn.execute("INSERT OR REPLACE INTO vendor_items(vendor_id,item_id,vendor_sku,last_cost,updated_at) VALUES(?,?,?,?,?)",
                     (vendor_id, item_id, vs or "", c, now_iso()))


def line_progress(line_id):
    """(received, on its way): received = changes WooCommerce confirmed; on its way = still in the outbox."""
    r = q1("SELECT COALESCE(SUM(CASE WHEN j.status='done' THEN rl.qty END),0) got, "
           "COALESCE(SUM(CASE WHEN j.status IN ('pending','working') THEN rl.qty END),0) going "
           "FROM receipt_lines rl JOIN jobs j ON j.id=rl.job_id WHERE rl.po_line_id=?", line_id)
    return r["got"], r["going"]


def po_out(p, full=True):
    v = q1("SELECT * FROM vendors WHERE id=?", p["vendor_id"])
    d = dict(p)
    d["vendor"] = v["name"] if v else "(removed vendor)"
    lines = q("SELECT * FROM po_lines WHERE po_id=? ORDER BY id", p["id"])
    out, total, open_units = [], 0.0, 0
    for ln in lines:
        got, going = line_progress(ln["id"])
        left = max(0, ln["qty"] - got - going - ln["cancelled"])
        it = q1("SELECT stock, scope, gtin FROM items WHERE id=?", ln["item_id"])
        e = dict(ln, received=got, receiving=going, open=left, stock=it["stock"] if it else None,
                 scope=it["scope"] if it else "gone", gtin=it["gtin"] if it else "", billed=po_line_billing(ln["id"]))
        total += (ln["qty"] - ln["cancelled"]) * (ln["cost"] or 0)
        open_units += left
        out.append(e)
    d.update(total=round(total, 2), open=open_units, units=sum(x["qty"] - x["cancelled"] for x in out), lineCount=len(out))
    if full:
        d["lines"] = out
        d["vendorInfo"] = dict(v) if v else None
        d["bills"] = [{"id": x["id"], "number": x["number"], "total": x["total"], "void": x["void"]}
                      for x in q("SELECT DISTINCT b.* FROM bills b JOIN bill_lines bl ON bl.bill_id=b.id WHERE bl.po_id=? ORDER BY b.id", p["id"])]
        d["receipts"] = []
        for rc in q("SELECT * FROM receipts WHERE po_id=? ORDER BY id", p["id"]):
            rls = [dict(x, status=(q1("SELECT status FROM jobs WHERE id=?", x["job_id"]) or {"status": "?"})["status"],
                        name=(q1("SELECT name FROM po_lines WHERE id=?", x["po_line_id"]) or {"name": ""})["name"])
                   for x in q("SELECT * FROM receipt_lines WHERE receipt_id=? ORDER BY id", rc["id"])]
            d["receipts"].append(dict(rc, lines=rls))
    return d


def refresh_po_status(po_id):
    """Sent POs move to partly received, received, or back, from what's actually been received."""
    p = q1("SELECT * FROM pos WHERE id=?", po_id)
    if not p or p["status"] in ("draft", "cancelled", "closed"):
        return
    d = po_out(p, full=False)
    lines = q("SELECT * FROM po_lines WHERE po_id=?", po_id)
    got_any = any(line_progress(x["id"])[0] for x in lines)
    status = "received" if d["open"] == 0 and not any(line_progress(x["id"])[1] for x in lines) and got_any else \
        "partial" if got_any else "sent"
    if status != p["status"]:
        with DB_LOCK:
            conn.execute("UPDATE pos SET status=?, updated_at=?, closed_at=? WHERE id=?",
                         (status, now_iso(), now_iso() if status == "received" else None, po_id))
        bump()


def list_pos(status="", vendor=0):
    where, args = ["1=1"], []
    if status == "open":
        where.append("status IN ('draft','sent','partial')")
    elif status in PO_STATUSES:
        where.append("status=?")
        args.append(status)
    if vendor:
        where.append("vendor_id=?")
        args.append(vendor)
    return [po_out(p, full=False) for p in q(f"SELECT * FROM pos WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 500", *args)]


def get_po(pid=0, number=""):
    if number:
        n = skey(number)
        m = re.fullmatch(r"PO-?(\d+)", n)
        p = q1("SELECT * FROM pos WHERE number=?", f"PO-{int(m.group(1)):04d}" if m else n)
    else:
        p = q1("SELECT * FROM pos WHERE id=?", pid)
    if not p:
        raise ApiError(404, f"No purchase order {number or pid}")
    refresh_po_status(p["id"])
    return po_out(q1("SELECT * FROM pos WHERE id=?", p["id"]))


def _po_item(item_id):
    r = _item(item_id)
    if r["scope"] != "shop":
        raise ApiError(409, f"{label_name(r)} can't go on a purchase order: {r['why'] or r['scope']}")
    return r


def save_po(b, by=""):
    """Create a draft PO, or change one that's still a draft (vendor, lines, notes)."""
    vid = _int(b.get("vendor_id"), 1, 10 ** 9, "Vendor")
    v = q1("SELECT * FROM vendors WHERE id=? AND active=1", vid)
    if not v:
        raise ApiError(400, "Pick a vendor")
    lines = b.get("lines") or []
    if not isinstance(lines, list) or len(lines) > 500:
        raise ApiError(400, "Up to 500 lines on a purchase order")
    clean, seen = [], set()
    for ln in lines:
        r = _po_item(ln.get("item_id"))
        if r["id"] in seen:
            raise ApiError(400, f"{label_name(r)} is on the PO twice")
        seen.add(r["id"])
        clean.append((r, _int(ln.get("qty"), 1, 100000, "Quantity"), _money(ln.get("cost") or 0, "Cost"),
                      _text(ln.get("vendor_sku"), 60)))
    notes = _text(b.get("notes"), 1000)
    pid = b.get("id")
    with Tx() as c:
        if pid:
            p = c.execute("SELECT * FROM pos WHERE id=?", (int(pid),)).fetchone()
            if not p:
                raise ApiError(404, "No such purchase order")
            if p["status"] != "draft":
                raise ApiError(409, f"{p['number']} has been sent: only drafts can be changed")
            c.execute("UPDATE pos SET vendor_id=?, notes=?, updated_at=? WHERE id=?", (vid, notes, now_iso(), p["id"]))
            c.execute("DELETE FROM po_lines WHERE po_id=?", (p["id"],))
            pid = p["id"]
        else:
            n = int(meta_get("po_next", 1) or 1)
            meta_set("po_next", n + 1)
            pid = c.execute("INSERT INTO pos(number,vendor_id,status,notes,created_at,created_by,updated_at) VALUES(?,?,?,?,?,?,?)",
                            (f"PO-{n:04d}", vid, "draft", notes, now_iso(), by, now_iso())).lastrowid
        for r, qty, cost, vsku in clean:
            c.execute("INSERT INTO po_lines(po_id,item_id,name,sku,vendor_sku,qty,cost) VALUES(?,?,?,?,?,?,?)",
                      (pid, r["id"], label_name(r), r["sku"], vsku, qty, cost))
    for r, qty, cost, vsku in clean:
        remember_vendor_item(vid, r["id"], vsku, cost or None)
    bump()
    return get_po(pid)


def po_action(b, by=""):
    p = q1("SELECT * FROM pos WHERE id=?", _int(b.get("id"), 1, 10 ** 9, "PO"))
    if not p:
        raise ApiError(404, "No such purchase order")
    act = b.get("action")
    now = now_iso()
    if act == "send":
        if p["status"] != "draft":
            raise ApiError(409, f"{p['number']} isn't a draft")
        if not q1("SELECT 1 FROM po_lines WHERE po_id=?", p["id"]):
            raise ApiError(400, "Add something to order first")
        with DB_LOCK:
            conn.execute("UPDATE pos SET status='sent', sent_at=?, updated_at=? WHERE id=?", (now, now, p["id"]))
    elif act == "unsend":
        if p["status"] != "sent" or q1("SELECT 1 FROM receipts WHERE po_id=?", p["id"]):
            raise ApiError(409, "Only a sent PO with nothing received can go back to draft")
        with DB_LOCK:
            conn.execute("UPDATE pos SET status='draft', sent_at=NULL, updated_at=? WHERE id=?", (now, p["id"]))
    elif act == "cancel":
        if p["status"] not in ("draft", "sent") or q1("SELECT 1 FROM receipts WHERE po_id=?", p["id"]):
            raise ApiError(409, "A PO with something received can't be cancelled: close what's left instead")
        with DB_LOCK:
            conn.execute("UPDATE pos SET status='cancelled', closed_at=?, updated_at=? WHERE id=?", (now, now, p["id"]))
    elif act == "close":   # the vendor won't send the rest: cancel what's still open, keep what came
        if p["status"] not in ("sent", "partial"):
            raise ApiError(409, "Only a sent or partly received PO can be closed")
        if any(line_progress(x["id"])[1] for x in q("SELECT id FROM po_lines WHERE po_id=?", p["id"])):
            raise ApiError(409, "Some received items are still being saved to WooCommerce: wait a moment")
        with Tx() as c:
            for ln in c.execute("SELECT * FROM po_lines WHERE po_id=?", (p["id"],)).fetchall():
                got, _ = line_progress(ln["id"])
                c.execute("UPDATE po_lines SET cancelled=? WHERE id=?", (max(0, ln["qty"] - got), ln["id"]))
            c.execute("UPDATE pos SET status='closed', closed_at=?, updated_at=? WHERE id=?", (now, now, p["id"]))
    else:
        raise ApiError(400, "Unknown action")
    note_change("po", act, p["number"], by=by)
    bump()
    return get_po(p["id"])


def allocate_landed(lines, extra_cost):
    """Spread freight and fees over the received lines by their value (by quantity when nothing has a price).
    lines: [(qty, unit_cost)] -> landed unit cost for each."""
    value = sum(qn * c for qn, c in lines)
    units = sum(qn for qn, _ in lines)
    out = []
    for qn, c in lines:
        share = (qn * c / value) if value > 0 else (qn / units if units else 0)
        out.append(round(c + (extra_cost * share / qn if qn else 0), 4))
    return out


def receive_po(b, by, operator):
    """Receive some or all of a PO: stock goes up through the outbox, and each unit's landed cost is recorded."""
    check_can_write()
    p = q1("SELECT * FROM pos WHERE id=?", _int(b.get("id"), 1, 10 ** 9, "PO"))
    if not p:
        raise ApiError(404, "No such purchase order")
    if p["status"] not in ("sent", "partial"):
        raise ApiError(409, f"{p['number']} is {p['status']}: only a sent PO can be received" +
                       (" (mark it sent first)" if p["status"] == "draft" else ""))
    want = b.get("lines") or []
    extras = b.get("extras") or []
    if not isinstance(want, list) or not isinstance(extras, list) or not (want or extras) or len(want) + len(extras) > 500:
        raise ApiError(400, "Scan what arrived first")
    freight, fees = _money(b.get("freight") or 0, "Freight"), _money(b.get("fees") or 0, "Fees")
    rows, seen = [], set()   # (po_line or None, item row, qty, unit cost)
    for ln in want:
        line = q1("SELECT * FROM po_lines WHERE id=? AND po_id=?", _int(ln.get("line_id"), 1, 10 ** 12, "Line"), p["id"])
        if not line:
            raise ApiError(400, f"That line isn't on {p['number']}")
        if line["id"] in seen:
            raise ApiError(400, f"{line['name']} is listed twice")
        seen.add(line["id"])
        qty = _int(ln.get("qty"), 1, 100000, "Quantity")
        got, going = line_progress(line["id"])
        left = line["qty"] - got - going - line["cancelled"]
        if qty > left:
            raise ApiError(409, f"Only {max(0, left)} of {line['name']} are still due on {p['number']} "
                                f"({line['qty']} ordered, {got + going} received)")
        rows.append((line, _po_item(line["item_id"]), qty, line["cost"] or 0.0))
    for ex in extras:   # arrived but wasn't ordered: added to the PO as an extra line
        r = _po_item(ex.get("item_id"))
        rows.append((None, r, _int(ex.get("qty"), 1, 100000, "Quantity"), _money(ex.get("cost") or 0, "Cost")))
    landed = allocate_landed([(qn, c) for _, _, qn, c in rows], freight + fees)
    batch, at = uuid.uuid4().hex[:10], now_iso()
    with Tx() as c:
        rid = c.execute("INSERT INTO receipts(po_id,batch,at,by,freight,fees,invoice,note,mode) VALUES(?,?,?,?,?,?,?,?,?)",
                        (p["id"], batch, at, by, freight, fees, _text(b.get("invoice"), 60), _text(b.get("note"), 500),
                         CFG["woo_write"])).lastrowid
        for (line, r, qty, cost), lu in zip(rows, landed):
            if line is None:
                lid = c.execute("INSERT INTO po_lines(po_id,item_id,name,sku,vendor_sku,qty,cost,extra) VALUES(?,?,?,?,?,?,?,1)",
                                (p["id"], r["id"], label_name(r), r["sku"], "", qty, cost)).lastrowid
            else:
                lid = line["id"]
            jid = c.execute("""INSERT INTO jobs(batch,at,kind,item_id,parent_id,name,sku,delta,reason,note,by,operator,status,mode,
                               po_id,po_number) VALUES(?,?,'receive',?,?,?,?,?,?,?,?,?,'pending',?,?,?)""",
                            (batch, at, r["id"], r["parent_id"] or 0, label_name(r), r["sku"], qty, f"Received on {p['number']}",
                             _text(b.get("invoice"), 60) and f"Invoice {_text(b.get('invoice'), 60)}" or "", by, operator,
                             CFG["woo_write"], p["id"], p["number"])).lastrowid
            c.execute("INSERT INTO receipt_lines(receipt_id,po_line_id,item_id,qty,unit_cost,landed_unit,job_id) VALUES(?,?,?,?,?,?,?)",
                      (rid, lid, r["id"], qty, cost, lu, jid))
    for (line, r, qty, cost), _ in zip(rows, landed):
        remember_vendor_item(p["vendor_id"], r["id"], None, cost or None)
    _worker_wake.set()
    bump()
    return {"batch": batch, "receipt": rid, "po": p["number"]}


def item_costs(item_id):
    """Last and average landed cost (from receipts WooCommerce confirmed), vendors, and what's on order."""
    rows = q("SELECT rl.qty, rl.landed_unit, rl.unit_cost, r.at FROM receipt_lines rl JOIN receipts r ON r.id=rl.receipt_id "
             "JOIN jobs j ON j.id=rl.job_id WHERE rl.item_id=? AND j.status='done' ORDER BY rl.id", item_id)
    units = sum(r["qty"] for r in rows)
    avg = round(sum(r["qty"] * r["landed_unit"] for r in rows) / units, 4) if units else None
    last = rows[-1] if rows else None
    vendors = [dict(v) for v in q("SELECT vi.vendor_id, v.name, vi.vendor_sku, vi.last_cost FROM vendor_items vi "
                                  "JOIN vendors v ON v.id=vi.vendor_id WHERE vi.item_id=? AND v.active=1 ORDER BY v.name", item_id)]
    on_order = 0
    for ln in q("SELECT pl.* FROM po_lines pl JOIN pos p ON p.id=pl.po_id WHERE pl.item_id=? AND p.status IN ('sent','partial')", item_id):
        got, going = line_progress(ln["id"])
        on_order += max(0, ln["qty"] - got - going - ln["cancelled"])
    return {"lastCost": last["landed_unit"] if last else None, "lastPrice": last["unit_cost"] if last else None,
            "lastAt": last["at"] if last else None, "avgCost": avg, "received": units, "vendors": vendors, "onOrder": on_order}


# ---------------------------------------------------------------- QR codes and PDFs on the server (purchase orders sent by email)
# The page's own QR encoder, ported: byte mode, error correction M, versions 1-6, up to 106 bytes.
_QR_EXP, _QR_LOG = [0] * 512, [0] * 256
_x = 1
for _i in range(255):
    _QR_EXP[_i] = _x
    _QR_LOG[_x] = _i
    _x <<= 1
    if _x & 256:
        _x ^= 0x11D
for _i in range(255, 512):
    _QR_EXP[_i] = _QR_EXP[_i - 255]
_QR_SPEC = {1: (1, 16, 10, [], 14), 2: (1, 28, 16, [6, 18], 26), 3: (1, 44, 26, [6, 22], 42),
            4: (2, 32, 18, [6, 26], 62), 5: (2, 43, 24, [6, 30], 84), 6: (4, 27, 16, [6, 34], 106)}


def _qr_mul(a, b):
    return _QR_EXP[_QR_LOG[a] + _QR_LOG[b]] if a and b else 0


def _qr_ec(data, n):
    g = [1]
    for i in range(n):
        ng = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            ng[j] ^= c
            ng[j + 1] ^= _qr_mul(c, _QR_EXP[i])
        g = ng
    r = list(data) + [0] * n
    for i in range(len(data)):
        c = r[i]
        if c:
            for j in range(len(g)):
                r[i + j] ^= _qr_mul(g[j], c)
    return r[len(data):]


def qr_encode(text):
    """A square list of rows of booleans (True = dark), without the quiet zone."""
    data_bytes = str(text).encode("utf-8")
    ver = next((v for v in sorted(_QR_SPEC) if _QR_SPEC[v][4] >= len(data_bytes)), None)
    if not ver:
        raise ValueError("Too long for a QR code (106 characters at most)")
    nb, dpb, epb, align, _ = _QR_SPEC[ver]
    cap = nb * dpb
    bits = []

    def put(v, n):
        for i in range(n - 1, -1, -1):
            bits.append((v >> i) & 1)
    put(4, 4)
    put(len(data_bytes), 8)
    for b in data_bytes:
        put(b, 8)
    for _ in range(4):
        if len(bits) < cap * 8:
            bits.append(0)
    while len(bits) % 8:
        bits.append(0)
    data = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    p = 0
    while len(data) < cap:
        data.append(0x11 if p % 2 else 0xEC)
        p += 1
    blocks = [data[b * dpb:(b + 1) * dpb] for b in range(nb)]
    ecs = [_qr_ec(d, epb) for d in blocks]
    seq = [d[i] for i in range(dpb) for d in blocks] + [e[i] for i in range(epb) for e in ecs]
    size = 17 + 4 * ver
    mod = [[False] * size for _ in range(size)]
    fn = [[False] * size for _ in range(size)]

    def setm(r, c, v):
        mod[r][c] = v
        fn[r][c] = True

    def finder(r0, c0):
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = r0 + r, c0 + c
                if rr < 0 or cc < 0 or rr >= size or cc >= size:
                    continue
                ring = max(abs(r - 3), abs(c - 3))
                setm(rr, cc, 0 <= r <= 6 and 0 <= c <= 6 and ring != 2)
    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)
    for i in range(8, size - 8):
        setm(6, i, i % 2 == 0)
        setm(i, 6, i % 2 == 0)
    if align:
        a = align[1]
        for r in range(-2, 3):
            for c in range(-2, 3):
                setm(a + r, a + c, max(abs(r), abs(c)) != 1)
    setm(size - 8, 8, True)
    for i in range(9):
        fn[8][i] = fn[i][8] = True
    for i in range(8):
        fn[8][size - 1 - i] = fn[size - 1 - i][8] = True
    allbits = [(cw >> i) & 1 for cw in seq for i in range(7, -1, -1)]
    k, up, col = 0, True, size - 1
    while col > 0:
        if col == 6:
            col -= 1
        for n in range(size):
            r = size - 1 - n if up else n
            for c in (col, col - 1):
                if not fn[r][c]:
                    mod[r][c] = bool(allbits[k]) if k < len(allbits) else False
                    k += 1
        up = not up
        col -= 2
    masks = [lambda r, c: (r + c) % 2 == 0, lambda r, c: r % 2 == 0, lambda r, c: c % 3 == 0,
             lambda r, c: (r + c) % 3 == 0, lambda r, c: (r // 2 + c // 3) % 2 == 0,
             lambda r, c: (r * c) % 2 + (r * c) % 3 == 0, lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
             lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0]

    def format_bits(m):
        d = (0 << 3) | m
        v = d << 10
        for i in range(14, 9, -1):
            if (v >> i) & 1:
                v ^= 0x537 << (i - 10)
        return ((d << 10) | v) ^ 0x5412

    def with_mask(m):
        g = [[v if fn[r][c] else (v != masks[m](r, c)) for c, v in enumerate(row)] for r, row in enumerate(mod)]
        f = format_bits(m)
        bit = lambda i: ((f >> i) & 1) == 1
        for i in range(6):
            g[8][i] = bit(14 - i)
        g[8][7], g[8][8], g[7][8] = bit(8), bit(7), bit(6)
        for i in range(9, 15):
            g[14 - i][8] = bit(14 - i)
        for i in range(8):
            g[size - 1 - i][8] = bit(14 - i)
        for i in range(7, 15):   # the second copy along the top right: 8 cells (size-8 .. size-1)
            g[8][size - 15 + i] = bit(14 - i)
        g[size - 8][8] = True
        return g

    def penalty(g):
        p = 0
        for pass_ in range(2):
            for i in range(size):
                run = 1
                for j in range(1, size):
                    a = g[j][i] if pass_ else g[i][j]
                    b = g[j - 1][i] if pass_ else g[i][j - 1]
                    if a == b:
                        run += 1
                        if run == 5:
                            p += 3
                        elif run > 5:
                            p += 1
                    else:
                        run = 1
        for r in range(size - 1):
            for c in range(size - 1):
                v = g[r][c]
                if v == g[r][c + 1] == g[r + 1][c] == g[r + 1][c + 1]:
                    p += 3
        pat = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
        pats = (pat, pat[::-1])
        for i in range(size):
            for j in range(size - 10):
                for pt in pats:
                    if all(int(g[i][j + k_]) == b for k_, b in enumerate(pt)):
                        p += 40
                    if all(int(g[j + k_][i]) == b for k_, b in enumerate(pt)):
                        p += 40
        dark = sum(v for row in g for v in row)
        return p + int(abs(dark * 20 / (size * size) - 10)) * 10

    best = None
    for m in range(8):
        g = with_mask(m)
        s = penalty(g)
        if best is None or s < best[0]:
            best = (s, g)
    return best[1]


# Helvetica (regular) widths, characters 32-126; Helvetica-Bold is _HB_W above; Courier is 600 throughout
_H_W = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556, 556,
        556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833,
        722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556,
        556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334,
        260, 334, 584]
_PDF_SUBS = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-", "\u2026": "...",
             "\u00d7": "x", "\u2022": "*"}


def pdf_str(s):
    out = []
    for ch in str(s):
        ch = _PDF_SUBS.get(ch, ch)
        for c in ch:
            o = ord(c)
            if c in "()\\":
                out.append("\\" + c)
            elif 32 <= o < 127:
                out.append(c)
            elif 160 <= o < 256:
                out.append("\\%03o" % o)
            else:
                out.append("?")
    return "".join(out)


def text_w(s, font, size):
    """Width in mm. font: B (Helvetica-Bold), R (Helvetica), M (Courier)."""
    tbl = _HB_W if font == "B" else _H_W if font == "R" else None
    w = 0
    for ch in str(s):
        ch = _PDF_SUBS.get(ch, ch)[:1] or " "
        o = ord(ch)
        w += (tbl[o - 32] if 32 <= o <= 126 else 556) if tbl else 600
    return w * size / 1000 * 25.4 / 72


def wrap_mm(s, font, size, width):
    lines, cur = [], ""
    for w in str(s or "").split():
        while text_w(w, font, size) > width and len(w) > 1:
            k = len(w)
            while k > 1 and text_w(w[:k], font, size) > width:
                k -= 1
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(w[:k])
            w = w[k:]
        t = f"{cur} {w}" if cur else w
        if text_w(t, font, size) <= width:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def fit_mm(s, font, size, width):
    s = str(s or "")
    while len(s) > 1 and text_w(s, font, size) > width:
        s = s[:-1]
    return s


class PdfPage:
    FONTS = {"B": "F1", "R": "F2", "M": "F3"}

    def __init__(self, w, h):
        self.w, self.h, self.ops = w, h, ["0 g"]

    @staticmethod
    def pt(mm):
        return round(mm * 72 / 25.4, 3)

    def rect(self, x, y, w, h):
        self.ops.append(f"{self.pt(x)} {self.pt(self.h - y - h)} {self.pt(w)} {self.pt(h)} re f")

    def text(self, x, y, s, size, font="R", align="left"):
        w = text_w(s, font, size)
        left = x - w / 2 if align == "center" else x - w if align == "right" else x
        self.ops.append(f"BT /{self.FONTS[font]} {size:.2f} Tf 1 0 0 1 {self.pt(left)} {self.pt(self.h - y)} Tm ({pdf_str(s)}) Tj ET")

    def qr(self, s, x, y, side):
        g = qr_encode(s)
        n = len(g)
        m = side / (n + 2)
        for r, row in enumerate(g):
            c = 0
            while c < n:
                if not row[c]:
                    c += 1
                    continue
                e = c
                while e < n and row[e]:
                    e += 1
                self.rect(x + (c + 1) * m, y + (r + 1) * m, (e - c) * m + 0.01, m + 0.01)
                c = e


def build_pdf(pages):
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None,
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>"]
    kids = []
    for p in pages:
        ops = "\n".join(p.ops) + "\n"
        objs.append(f"<< /Length {len(ops)} >>\nstream\n{ops}endstream")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PdfPage.pt(p.w)} {PdfPage.pt(p.h)}] "
                    f"/Resources << /Font << /F1 3 0 R /F2 4 0 R /F3 5 0 R >> >> /Contents {len(objs)} 0 R >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offs = "%PDF-1.4\n", []
    for i, ob in enumerate(objs):
        offs.append(len(out))
        out += f"{i + 1} 0 obj\n{ob}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{n:010d} 00000 n \n" for n in offs)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode("latin-1")


# ---------------------------------------------------------------- vendor invoices (bills you pay), payments and credits
# An invoice is entered against one or more of the vendor's POs (or none): its lines start from what was ordered and
# received, and it's checked three ways (ordered, received, billed). Saved with "use for costs", its prices and its
# freight and fees become the landed cost of what it bills. Payments (one check can pay several invoices) and vendor
# credits are applied to invoices; what's left is what you owe. Money: the shop password, never a station terminal.
BILL_FILES = os.path.join(os.path.dirname(os.path.abspath(CFG["db"])), "bills")
PAY_METHODS = ("Check", "ACH / bank transfer", "Card", "Cash", "Other")


def _r2(x):
    return round(float(x or 0) + 0.0, 2)


def _day(v, what):
    v = str(v or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        raise ApiError(400, f"{what} should be a date")
    try:
        datetime.strptime(v, "%Y-%m-%d")
    except ValueError:
        raise ApiError(400, f"{what} isn't a real date")
    return v


def terms_days(terms):
    """Net 30 -> 30, Due on receipt / COD -> 0; anything else (or nothing): 30."""
    t = str(terms or "").lower()
    m = re.search(r"net\s*(\d{1,3})", t) or re.search(r"(\d{1,3})\s*days?", t)
    if m:
        return int(m.group(1))
    if re.search(r"receipt|cod|c\.o\.d|prepaid|due now", t):
        return 0
    return 30


def today_local():
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")


def bill_paid(bid):
    return _r2(q1("SELECT COALESCE(SUM(amount),0) n FROM applications WHERE bill_id=?", bid)["n"])


def credit_used(cid):
    return _r2(q1("SELECT COALESCE(SUM(amount),0) n FROM applications WHERE credit_id=?", cid)["n"])


def bill_state(b):
    """open / partial / paid / void, how much is owed (or, for a credit, still to use), and overdue / due soon."""
    if b["void"]:
        return {"status": "void", "owed": 0.0, "paid": 0.0, "flag": ""}
    if b["kind"] == "credit":
        used = credit_used(b["id"])
        left = _r2(b["total"] - used)
        return {"status": "used" if left <= 0.004 else "available", "owed": 0.0, "paid": used, "left": left, "flag": ""}
    paid = bill_paid(b["id"])
    owed = _r2(b["total"] - paid)
    status = "paid" if owed <= 0.004 else "partial" if paid > 0.004 else "open"
    flag = ""
    if status != "paid" and b["due"]:
        t = today_local()
        if b["due"] < t:
            flag = "overdue"
        elif b["due"] <= (datetime.strptime(t, "%Y-%m-%d") + timedelta(days=7)).strftime("%Y-%m-%d"):
            flag = "due soon"
    return {"status": status, "owed": max(0.0, owed), "paid": paid, "flag": flag}


def po_line_billing(line_id, not_bill=None):
    """How much of a PO line has been billed (on invoices that aren't void), leaving one invoice out if asked."""
    r = q1("SELECT COALESCE(SUM(bl.qty),0) n FROM bill_lines bl JOIN bills b ON b.id=bl.bill_id WHERE bl.po_line_id=? "
           "AND b.void=0 AND b.kind='bill'" + (" AND b.id!=?" if not_bill else ""), *([line_id, not_bill] if not_bill else [line_id]))
    return float(r["n"] or 0)


def bill_match(b, lines):
    """The three-way match: for each line on a PO, what was ordered, received, billed (on every invoice), at what price."""
    out, issues = [], 0
    for ln in lines:
        e = dict(ln)
        notes = []
        if ln["po_line_id"]:
            pl = q1("SELECT pl.*, p.number FROM po_lines pl JOIN pos p ON p.id=pl.po_id WHERE pl.id=?", ln["po_line_id"])
            if pl:
                got, going = line_progress(pl["id"])
                ordered = pl["qty"] - pl["cancelled"]
                billed = po_line_billing(pl["id"])
                e.update(po_number=pl["number"], ordered=ordered, received=got, billedAll=billed, poCost=pl["cost"] or 0)
                if not b["void"] and b["kind"] == "bill":
                    if billed > got + 0.0001:
                        notes.append(f"Billed {billed:g} but {got:g} received")
                    if billed > ordered + 0.0001:
                        notes.append(f"Billed {billed:g} but {ordered:g} ordered")
                    if (pl["cost"] or 0) > 0 and abs((ln["price"] or 0) - pl["cost"]) > 0.005:
                        notes.append(f"Price ${ln['price'] or 0:,.2f}, PO says ${pl['cost']:,.2f}")
        else:
            e["po_number"] = ""
        e["issues"] = notes
        issues += len(notes)
        out.append(e)
    return out, issues


def bill_out(b, full=True):
    v = q1("SELECT name, terms FROM vendors WHERE id=?", b["vendor_id"])
    d = dict(b)
    d["vendor"] = v["name"] if v else "(removed vendor)"
    d.update(bill_state(b))
    lines = q("SELECT * FROM bill_lines WHERE bill_id=? ORDER BY id", b["id"])
    d["pos"] = sorted({x["number"] for x in q("SELECT DISTINCT p.number FROM bill_lines bl JOIN pos p ON p.id=bl.po_id WHERE bl.bill_id=?", b["id"])})
    match, issues = bill_match(b, lines)
    d["issues"] = issues
    if full:
        d["lines"] = match
        d["applications"] = [dict(a, payment=dict(q1("SELECT date, method, reference, amount FROM payments WHERE id=?", a["payment_id"]) or {})
                                  if a["payment_id"] else None,
                                  credit=dict(q1("SELECT number, date FROM bills WHERE id=?", a["credit_id"]) or {}) if a["credit_id"] else None)
                             for a in q("SELECT * FROM applications WHERE bill_id=? OR credit_id=? ORDER BY id", b["id"], b["id"])]
        d["hasFile"] = bool(b["file"])
    return d


def list_bills(status="", vendor=0):
    rows = q("SELECT * FROM bills" + (" WHERE vendor_id=?" if vendor else "") + " ORDER BY COALESCE(due, date) DESC, id DESC LIMIT 1000",
             *([vendor] if vendor else []))
    out = []
    for b in rows:
        d = bill_out(b, full=False)
        if status == "open" and not (d["status"] in ("open", "partial") or d["status"] == "available"):
            continue
        if status == "overdue" and d["flag"] != "overdue":
            continue
        if status in ("paid", "void") and d["status"] != status:
            continue
        if status == "credits" and b["kind"] != "credit":
            continue
        out.append(d)
    return out


def payables():
    """What's owed: overdue, due within a week, later; and by vendor."""
    tot = {"overdue": 0.0, "soon": 0.0, "later": 0.0, "credits": 0.0, "count": 0}
    vend = {}
    for b in q("SELECT * FROM bills WHERE void=0"):
        st = bill_state(b)
        name = (q1("SELECT name FROM vendors WHERE id=?", b["vendor_id"]) or {"name": "?"})["name"]
        e = vend.setdefault(b["vendor_id"], {"vendor_id": b["vendor_id"], "vendor": name, "owed": 0.0, "credits": 0.0, "overdue": 0.0})
        if b["kind"] == "credit":
            tot["credits"] += st.get("left", 0)
            e["credits"] += st.get("left", 0)
            continue
        if st["owed"] <= 0.004:
            continue
        tot["count"] += 1
        key = "overdue" if st["flag"] == "overdue" else "soon" if st["flag"] == "due soon" else "later"
        tot[key] += st["owed"]
        e["owed"] += st["owed"]
        if st["flag"] == "overdue":
            e["overdue"] += st["owed"]
    for k in ("overdue", "soon", "later", "credits"):
        tot[k] = _r2(tot[k])
    return {"totals": tot, "vendors": sorted([dict(v, owed=_r2(v["owed"]), credits=_r2(v["credits"]), overdue=_r2(v["overdue"]))
                                              for v in vend.values() if v["owed"] > 0.004 or v["credits"] > 0.004],
                                             key=lambda v: -v["owed"])}


def bill_prefill(vendor_id, po_ids, not_bill=None):
    """Lines for a new invoice from these POs: each line received but not yet billed, at the PO's price."""
    out = []
    for pid in po_ids:
        p = q1("SELECT * FROM pos WHERE id=? AND vendor_id=?", pid, vendor_id)
        if not p:
            raise ApiError(400, f"PO {pid} isn't from this vendor")
        for pl in q("SELECT * FROM po_lines WHERE po_id=? ORDER BY id", pid):
            got, going = line_progress(pl["id"])
            billed = po_line_billing(pl["id"], not_bill)
            ordered = pl["qty"] - pl["cancelled"]
            out.append({"po_line_id": pl["id"], "po_id": pid, "po_number": p["number"], "item_id": pl["item_id"],
                        "description": pl["name"] + (f" ({pl['vendor_sku']})" if pl["vendor_sku"] else ""), "sku": pl["sku"],
                        "ordered": ordered, "received": got, "billed": billed,
                        "qty": max(0, got - billed) if got else max(0, ordered - billed), "price": pl["cost"] or 0})
    return out


def _check_file(f):
    """An attached invoice copy, checked before anything is saved: (data, extension, name), or None."""
    if not f:
        return None
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(f.get("name") or "invoice"))[:80] or "invoice"
    ext = os.path.splitext(name)[1].lower()
    if ext not in (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".heic", ".gif"):
        raise ApiError(400, "Attach a PDF or a photo (PDF, JPG, PNG, WEBP, HEIC)")
    try:
        data = base64.b64decode(str(f.get("data") or ""), validate=True)
    except (ValueError, TypeError):
        raise ApiError(400, "The attached file didn't come through")
    if not data or len(data) > 12_000_000:
        raise ApiError(400, "The attached file should be under 12 MB")
    return data, ext, name


def _save_file(bid, checked):
    if not checked:
        return None, None
    data, ext, name = checked
    os.makedirs(BILL_FILES, exist_ok=True)
    stored = f"{bid}-{uuid.uuid4().hex[:6]}{ext}"
    with open(os.path.join(BILL_FILES, stored), "wb") as fh:
        fh.write(data)
    return stored, name


def apply_bill_costs(bid):
    """The invoice's prices, freight and fees become the landed cost of the parts it bills (their receipt lines)."""
    b = q1("SELECT * FROM bills WHERE id=?", bid)
    if not b or b["void"] or b["kind"] != "bill" or not b["apply_costs"]:
        return 0
    lines = [l for l in q("SELECT * FROM bill_lines WHERE bill_id=? AND po_line_id IS NOT NULL AND qty>0", bid)]
    if not lines:
        return 0
    extra = (b["freight"] or 0) + (b["fees"] or 0)
    landed = allocate_landed([(l["qty"], l["price"] or 0) for l in lines], extra)
    n = 0
    with Tx() as c:
        for l, lu in zip(lines, landed):
            n += c.execute("UPDATE receipt_lines SET unit_cost=?, landed_unit=? WHERE po_line_id=?",
                           (l["price"] or 0, lu, l["po_line_id"])).rowcount
    v = b["vendor_id"]
    for l in lines:
        if l["item_id"] and (l["price"] or 0) > 0:
            remember_vendor_item(v, l["item_id"], None, l["price"])
    return n


def save_bill(b, by=""):
    vid = _int(b.get("vendor_id"), 1, 10 ** 9, "Vendor")
    v = q1("SELECT * FROM vendors WHERE id=?", vid)
    if not v:
        raise ApiError(400, "Pick a vendor")
    kind = "credit" if b.get("kind") == "credit" else "bill"
    number = _text(b.get("number"), 60)
    if not number:
        raise ApiError(400, "Give the vendor's invoice number" if kind == "bill" else "Give the credit's number")
    date = _day(b.get("date"), "Invoice date")
    terms = _text(b.get("terms"), 40) if b.get("terms") is not None else (v["terms"] or "")
    due = _day(b.get("due"), "Due date") if b.get("due") else \
        (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=terms_days(terms))).strftime("%Y-%m-%d")
    freight, fees, tax = (_money(b.get(k) or 0, k.capitalize()) for k in ("freight", "fees", "tax"))
    raw = b.get("lines") or []
    if not isinstance(raw, list) or len(raw) > 500:
        raise ApiError(400, "Up to 500 lines")
    bid = b.get("id")
    lines, seen = [], set()
    for ln in raw:
        plid = ln.get("po_line_id")
        po_id, item_id = None, ln.get("item_id")
        if plid:
            pl = q1("SELECT pl.*, p.vendor_id FROM po_lines pl JOIN pos p ON p.id=pl.po_id WHERE pl.id=?", int(plid))
            if not pl or pl["vendor_id"] != vid:
                raise ApiError(400, "A line is from another vendor's PO")
            if pl["id"] in seen:
                raise ApiError(400, f"{pl['name']} is on the invoice twice")
            seen.add(pl["id"])
            po_id, item_id = pl["po_id"], pl["item_id"]
        try:
            qty = round(float(ln.get("qty") or 0), 3)
        except (TypeError, ValueError):
            raise ApiError(400, "A quantity isn't a number")
        if not 0 <= qty <= 100000:
            raise ApiError(400, "A quantity is out of range")
        price = _money(ln.get("price") or 0, "A price", -1_000_000)
        desc = _text(ln.get("description"), 200)
        if not plid and not desc:
            raise ApiError(400, "A line without a PO needs a description")
        if qty == 0 and plid:
            continue   # a PO line not on this invoice
        lines.append((plid and int(plid), po_id, item_id, desc, qty, price, _r2(qty * price)))
    if not lines and kind == "bill" and not (freight or fees or tax):
        raise ApiError(400, "The invoice has nothing on it")
    attached = _check_file(b.get("file"))   # a refused attachment stops the whole save
    subtotal = _r2(sum(l[6] for l in lines))
    total = _r2(subtotal + freight + fees + tax)
    if kind == "credit" and total <= 0:
        raise ApiError(400, "A credit's amount should be more than 0")
    now = now_iso()
    with Tx() as c:
        if bid:
            old = c.execute("SELECT * FROM bills WHERE id=?", (int(bid),)).fetchone()
            if not old:
                raise ApiError(404, "No such invoice")
            if old["void"]:
                raise ApiError(409, "That invoice is void")
            applied = c.execute("SELECT COALESCE(SUM(amount),0) n FROM applications WHERE bill_id=? OR credit_id=?", (old["id"], old["id"])).fetchone()["n"]
            if applied and total + 0.004 < applied:
                raise ApiError(409, f"${applied:,.2f} has already been paid or applied against it: its total can't go below that")
            c.execute("UPDATE bills SET vendor_id=?, kind=?, number=?, date=?, due=?, terms=?, subtotal=?, freight=?, fees=?, tax=?, total=?, "
                      "notes=?, apply_costs=?, updated_at=? WHERE id=?",
                      (vid, kind, number, date, due, terms, subtotal, freight, fees, tax, total, _text(b.get("notes"), 1000),
                       0 if b.get("apply_costs") is False else 1, now, old["id"]))
            c.execute("DELETE FROM bill_lines WHERE bill_id=?", (old["id"],))
            bid = old["id"]
        else:
            dup = c.execute("SELECT id FROM bills WHERE vendor_id=? AND number=? AND kind=? AND void=0", (vid, number, kind)).fetchone()
            if dup:
                raise ApiError(409, f"{v['name']} invoice {number} is already entered")
            bid = c.execute("INSERT INTO bills(vendor_id,kind,number,date,due,terms,subtotal,freight,fees,tax,total,notes,apply_costs,created_at,"
                            "by,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (vid, kind, number, date, due, terms, subtotal, freight, fees, tax, total, _text(b.get("notes"), 1000),
                             0 if b.get("apply_costs") is False else 1, now, by, now)).lastrowid
        for plid, po_id, item_id, desc, qty, price, amt in lines:
            c.execute("INSERT INTO bill_lines(bill_id,po_line_id,po_id,item_id,description,qty,price,amount) VALUES(?,?,?,?,?,?,?,?)",
                      (bid, plid, po_id, item_id, desc, qty, price, amt))
    stored, name = _save_file(bid, attached)
    if stored:
        with DB_LOCK:
            conn.execute("UPDATE bills SET file=?, file_name=? WHERE id=?", (stored, name, bid))
    if kind == "bill":
        apply_bill_costs(bid)
    note_change("bill", "saved", f"{v['name']} {kind} {number}", f"${total:,.2f}", by)
    bump()
    return bill_out(q1("SELECT * FROM bills WHERE id=?", bid))


def void_bill(bid, by=""):
    b = q1("SELECT * FROM bills WHERE id=?", bid)
    if not b:
        raise ApiError(404, "No such invoice")
    if q1("SELECT 1 FROM applications WHERE bill_id=? OR credit_id=?", bid, bid):
        raise ApiError(409, "Payments or credits are applied to it: void those first")
    with DB_LOCK:
        conn.execute("UPDATE bills SET void=1, updated_at=? WHERE id=?", (now_iso(), bid))
    note_change("bill", "voided", f"{b['kind']} {b['number']}", by=by)
    bump()
    return bill_out(q1("SELECT * FROM bills WHERE id=?", bid))


def record_payment(b, by=""):
    vid = _int(b.get("vendor_id"), 1, 10 ** 9, "Vendor")
    v = q1("SELECT * FROM vendors WHERE id=?", vid)
    if not v:
        raise ApiError(400, "Pick a vendor")
    amount = _money(b.get("amount"), "Amount")
    if amount <= 0:
        raise ApiError(400, "The amount should be more than 0")
    method = b.get("method") if b.get("method") in PAY_METHODS else "Other"
    date = _day(b.get("date"), "Payment date")
    allocs = b.get("allocations") or []
    if not isinstance(allocs, list) or not allocs:
        raise ApiError(400, "Pick the invoices it pays")
    total, rows = 0.0, []
    for a in allocs:
        bl = q1("SELECT * FROM bills WHERE id=?", _int(a.get("bill_id"), 1, 10 ** 12, "Invoice"))
        if not bl or bl["vendor_id"] != vid or bl["kind"] != "bill" or bl["void"]:
            raise ApiError(400, "An invoice isn't this vendor's, or is void")
        amt = _money(a.get("amount"), "An amount")
        if amt <= 0:
            continue
        owed = bill_state(bl)["owed"]
        if amt > owed + 0.004:
            raise ApiError(409, f"Invoice {bl['number']} only has ${owed:,.2f} left to pay")
        total += amt
        rows.append((bl["id"], amt))
    if abs(_r2(total) - amount) > 0.004:
        raise ApiError(400, f"The amounts on the invoices (${total:,.2f}) don't add up to the payment (${amount:,.2f})")
    now = now_iso()
    with Tx() as c:
        pid = c.execute("INSERT INTO payments(vendor_id,date,amount,method,reference,notes,created_at,by) VALUES(?,?,?,?,?,?,?,?)",
                        (vid, date, amount, method, _text(b.get("reference"), 60), _text(b.get("notes"), 500), now, by)).lastrowid
        for bl_id, amt in rows:
            c.execute("INSERT INTO applications(bill_id,payment_id,amount,at,by) VALUES(?,?,?,?,?)", (bl_id, pid, amt, now, by))
    note_change("payment", "recorded", f"{v['name']} ${amount:,.2f}", f"{method} {_text(b.get('reference'), 60)}".strip(), by)
    bump()
    return {"id": pid}


def void_payment(pid, by=""):
    p = q1("SELECT * FROM payments WHERE id=? AND void=0", pid)
    if not p:
        raise ApiError(404, "No such payment")
    with DB_LOCK:
        conn.execute("DELETE FROM applications WHERE payment_id=?", (pid,))
        conn.execute("UPDATE payments SET void=1 WHERE id=?", (pid,))
    note_change("payment", "voided", f"${p['amount']:,.2f} {p['method']} {p['reference'] or ''}".strip(), by=by)
    bump()


def apply_credit(b, by=""):
    cr = q1("SELECT * FROM bills WHERE id=? AND kind='credit' AND void=0", _int(b.get("credit_id"), 1, 10 ** 12, "Credit"))
    bl = q1("SELECT * FROM bills WHERE id=? AND kind='bill' AND void=0", _int(b.get("bill_id"), 1, 10 ** 12, "Invoice"))
    if not cr or not bl or cr["vendor_id"] != bl["vendor_id"]:
        raise ApiError(400, "Pick a credit and an invoice from the same vendor")
    amt = _money(b.get("amount"), "Amount")
    left, owed = bill_state(cr)["left"], bill_state(bl)["owed"]
    if amt <= 0 or amt > left + 0.004 or amt > owed + 0.004:
        raise ApiError(409, f"Up to ${min(left, owed):,.2f} can be applied (the credit has ${left:,.2f}, the invoice owes ${owed:,.2f})")
    with DB_LOCK:
        conn.execute("INSERT INTO applications(bill_id,credit_id,amount,at,by) VALUES(?,?,?,?,?)", (bl["id"], cr["id"], amt, now_iso(), by))
    note_change("credit", "applied", f"credit {cr['number']} -> invoice {bl['number']}", f"${amt:,.2f}", by)
    bump()
    return bill_out(bl)


def list_payments(vendor=0):
    out = []
    for p in q("SELECT * FROM payments" + (" WHERE vendor_id=?" if vendor else "") + " ORDER BY date DESC, id DESC LIMIT 500",
               *([vendor] if vendor else [])):
        d = dict(p)
        d["vendor"] = (q1("SELECT name FROM vendors WHERE id=?", p["vendor_id"]) or {"name": "?"})["name"]
        d["bills"] = [dict(x) for x in q("SELECT b.id, b.number, a.amount FROM applications a JOIN bills b ON b.id=a.bill_id WHERE a.payment_id=?", p["id"])]
        out.append(d)
    return out


def export_bills():
    rows = [["Vendor", "Kind", "Invoice #", "Invoice date", "Due", "Terms", "POs", "Subtotal", "Freight", "Fees", "Tax", "Total",
             "Paid or applied", "Still owed", "Status", "Differences", "Notes", "Entered by"]]
    for b in q("SELECT * FROM bills ORDER BY date, id"):
        d = bill_out(b, full=False)
        rows.append([d["vendor"], b["kind"], b["number"], b["date"], b["due"], b["terms"], " ".join(d["pos"]), b["subtotal"], b["freight"],
                     b["fees"], b["tax"], b["total"], d["paid"], d["owed"], d["status"] + (f" ({d['flag']})" if d["flag"] else ""),
                     d["issues"], b["notes"], b["by"]])
    return csv_bytes(rows)


def export_payments():
    rows = [["Date", "Vendor", "Amount", "Method", "Check # / reference", "Invoices paid", "Notes", "Recorded by", "Void"]]
    for p in list_payments():
        rows.append([p["date"], p["vendor"], p["amount"], p["method"], p["reference"],
                     "; ".join(f"{x['number']} ${x['amount']:,.2f}" for x in p["bills"]), p["notes"], p["by"], "void" if p["void"] else ""])
    return csv_bytes(rows)


# ---------------------------------------------------------------- reports: inventory value, sales, margins, purchasing, snapshots
# The shop password only (costs and money). A part's cost is its average landed cost from receipts WooCommerce
# confirmed; with none yet, its vendor's last price; with neither, it's listed as having no cost (and counts as 0).
def cost_table():
    """{item_id: (cost, where it came from)} for every in-shop part."""
    out = {}
    for r in q("SELECT rl.item_id, SUM(rl.qty * rl.landed_unit) v, SUM(rl.qty) n FROM receipt_lines rl JOIN jobs j ON j.id=rl.job_id "
               "WHERE j.status='done' GROUP BY rl.item_id"):
        if r["n"]:
            out[r["item_id"]] = (round(r["v"] / r["n"], 4), "landed")
    for r in q("SELECT item_id, last_cost FROM vendor_items WHERE last_cost IS NOT NULL AND last_cost>0 ORDER BY updated_at"):
        if r["item_id"] not in out:
            out[r["item_id"]] = (r["last_cost"], "vendor price")
    for k in q("SELECT item_id FROM kits"):   # a kit costs what its parts cost (if every part's cost is known)
        parts = q("SELECT part_id, qty FROM kit_parts WHERE kit_id=?", k["item_id"])
        if parts and all(p["part_id"] in out for p in parts):
            out[k["item_id"]] = (round(sum(out[p["part_id"]][0] * p["qty"] for p in parts), 4), "its parts")
    return out


def item_vendor_names():
    names = {v["id"]: v["name"] for v in q("SELECT id, name FROM vendors")}
    out = {}
    for r in q("SELECT item_id, vendor_id FROM vendor_items ORDER BY updated_at"):
        out[r["item_id"]] = names.get(r["vendor_id"], "")
    for r in q("SELECT item_id, vendor_id FROM reorder WHERE vendor_id IS NOT NULL"):
        out[r["item_id"]] = names.get(r["vendor_id"], out.get(r["item_id"], ""))
    return out


def shop_items_full():
    """In-shop parts with their category names (a variation's are its parent's) and price."""
    names = cat_names()
    pcats = {r["id"]: r["cats"] for r in q("SELECT id, cats FROM items WHERE type='variable'")}
    out = []
    for r in q("SELECT * FROM items WHERE scope='shop' ORDER BY name COLLATE NOCASE, attrs"):
        cats = json.loads((pcats.get(r["parent_id"]) if r["parent_id"] else r["cats"]) or "[]")
        out.append({"id": r["id"], "name": label_name(r), "sku": r["sku"] or "", "stock": r["stock"] or 0,
                    "price": float(r["price"]) if str(r["price"] or "").replace(".", "", 1).isdigit() else None,
                    "category": ", ".join(names[c] for c in cats if c in names) or "(no category)"})
    return out


def report_inventory():
    costs, vend = cost_table(), item_vendor_names()
    items, cats, vends = [], {}, {}
    total = units = 0.0
    no_cost = 0
    kit_ids = {r["item_id"] for r in q("SELECT item_id FROM kits")}
    for it in shop_items_full():
        if it["id"] in kit_ids:
            continue   # a kit's stock is its parts' stock: counted once, as the parts
        c, src = costs.get(it["id"], (None, ""))
        stock = max(0, it["stock"])
        value = round(stock * (c or 0), 2)
        if c is None and stock > 0:
            no_cost += 1
        e = dict(it, cost=c, costFrom=src, value=value, vendor=vend.get(it["id"], ""))
        items.append(e)
        total += value
        units += stock
        for key, bucket in ((it["category"], cats), (e["vendor"] or "(no vendor)", vends)):
            b = bucket.setdefault(key, {"name": key, "items": 0, "units": 0, "value": 0.0})
            b["items"] += 1
            b["units"] += stock
            b["value"] += value
    fix = lambda d: sorted([dict(v, value=round(v["value"], 2)) for v in d.values()], key=lambda v: -v["value"])
    items.sort(key=lambda x: -x["value"])
    return {"total": round(total, 2), "units": units, "items": items, "noCost": no_cost, "byCategory": fix(cats), "byVendor": fix(vends)}


def _period(qs):
    def day(k):
        v = (qs.get(k) or [""])[0]
        return v if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) else ""
    days = qint(qs, "days", 90, 1, 3660)
    end = day("to") or today_local_day()
    start = day("from") or (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    if start > end:
        raise ApiError(400, "The period's start is after its end")
    return start, end


def report_sales(start, end):
    costs = cost_table()
    shop = {it["id"]: it for it in shop_items_full()}
    hist = meta_get("sales_history", {}) or {}
    rows = q("SELECT item_id, SUM(qty) qty, SUM(total) total, COUNT(DISTINCT order_id) orders FROM sales WHERE day BETWEEN ? AND ? "
             "GROUP BY item_id", start, end)
    chans = q("SELECT channel, SUM(qty) qty, SUM(total) total, COUNT(DISTINCT order_id) orders FROM sales s WHERE day BETWEEN ? AND ? "
              "AND item_id IN (SELECT id FROM items WHERE scope='shop') GROUP BY channel ORDER BY total DESC", start, end)
    items, revenue, cogs, units, costed, uncosted = [], 0.0, 0.0, 0.0, 0.0, 0
    for r in rows:
        it = shop.get(r["item_id"])
        if not it:
            continue   # feed products and guns sell too, but these are the in-shop reports
        c = costs.get(r["item_id"], (None, ""))[0]
        cg = round(r["qty"] * c, 2) if c is not None else None
        items.append({"id": it["id"], "name": it["name"], "sku": it["sku"], "category": it["category"], "qty": r["qty"],
                      "revenue": round(r["total"], 2), "cogs": cg, "profit": round(r["total"] - cg, 2) if cg is not None else None,
                      "stock": it["stock"], "orders": r["orders"]})
        revenue += r["total"]
        units += r["qty"]
        if cg is None:
            uncosted += 1   # no cost known: left out of cost and profit, rather than counted as free
        else:
            cogs += cg
            costed += r["total"]
    items.sort(key=lambda x: -x["revenue"])
    sold = {r["item_id"] for r in rows}
    slow = [dict(it, value=round(max(0, it["stock"]) * (costs.get(it["id"], (0, ""))[0] or 0), 2))
            for it in shop.values() if it["stock"] > 0 and it["id"] not in sold]
    slow.sort(key=lambda x: -x["value"])
    last = {r["item_id"]: r["d"] for r in q("SELECT item_id, MAX(day) d FROM sales GROUP BY item_id")}
    cutoff = (datetime.strptime(today_local_day(), "%Y-%m-%d") - timedelta(days=180)).strftime("%Y-%m-%d")
    dead = [dict(it, lastSold=last.get(it["id"]) or "", value=round(max(0, it["stock"]) * (costs.get(it["id"], (0, ""))[0] or 0), 2))
            for it in shop.values() if it["stock"] > 0 and (last.get(it["id"]) or "") < cutoff]
    dead.sort(key=lambda x: -x["value"])
    return {"from": start, "to": end, "units": units, "revenue": round(revenue, 2), "cogs": round(cogs, 2),
            "profit": round(costed - cogs, 2), "costedRevenue": round(costed, 2), "uncosted": uncosted, "items": items, "channels": [dict(c, total=round(c["total"], 2)) for c in chans],
            "slow": slow, "dead": dead, "deadSince": cutoff, "history": hist}


def report_margins(threshold=20.0):
    costs = cost_table()
    out = []
    for it in shop_items_full():
        c, src = costs.get(it["id"], (None, ""))
        if it["price"] is None or c is None:
            m = None
        else:
            m = round((it["price"] - c) / it["price"] * 100, 1) if it["price"] > 0 else None
        flag = "loss" if m is not None and m < 0 else "thin" if m is not None and m < threshold else ""
        out.append(dict(it, cost=c, costFrom=src, margin=m, profit=round(it["price"] - c, 2) if m is not None else None, flag=flag))
    out.sort(key=lambda x: (x["margin"] is None, x["margin"] if x["margin"] is not None else 0))
    return {"threshold": threshold, "items": out, "loss": sum(1 for x in out if x["flag"] == "loss"),
            "thin": sum(1 for x in out if x["flag"] == "thin"), "unknown": sum(1 for x in out if x["margin"] is None)}


def report_purchasing(months=12):
    months = max(1, min(36, int(months)))
    now = datetime.now(LOCAL_TZ)
    keys = []
    y, m = now.year, now.month
    for _ in range(months):
        keys.append(f"{y:04d}-{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    keys.reverse()
    names = {v["id"]: v["name"] for v in q("SELECT id, name FROM vendors")}
    billed = {}
    for b in q("SELECT vendor_id, substr(date,1,7) mo, SUM(CASE WHEN kind='credit' THEN -total ELSE total END) t FROM bills "
               "WHERE void=0 GROUP BY vendor_id, mo"):
        if b["mo"] in keys:
            billed.setdefault(names.get(b["vendor_id"], "?"), {})[b["mo"]] = round(b["t"], 2)
    received = {k: 0.0 for k in keys}
    for r in q("SELECT r.at, rl.qty, rl.landed_unit FROM receipt_lines rl JOIN receipts r ON r.id=rl.receipt_id JOIN jobs j ON j.id=rl.job_id "
               "WHERE j.status='done'"):
        d = parse_iso(r["at"])
        k = d.astimezone(LOCAL_TZ).strftime("%Y-%m") if d else ""
        if k in received:
            received[k] += r["qty"] * (r["landed_unit"] or 0)
    aging = {}
    today = today_local_day()
    for b in q("SELECT * FROM bills WHERE void=0 AND kind='bill'"):
        st = bill_state(b)
        if st["owed"] <= 0.004:
            continue
        late = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(b["due"] or b["date"], "%Y-%m-%d")).days
        bucket = "current" if late <= 0 else "1-30" if late <= 30 else "31-60" if late <= 60 else "61-90" if late <= 90 else "90+"
        a = aging.setdefault(names.get(b["vendor_id"], "?"), {"current": 0.0, "1-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0})
        a[bucket] = round(a[bucket] + st["owed"], 2)
    return {"months": keys, "billed": [{"vendor": v, "months": m_, "total": round(sum(m_.values()), 2)} for v, m_ in sorted(billed.items())],
            "received": [round(received[k], 2) for k in keys],
            "aging": [dict(a, vendor=v, total=round(sum(a.values()), 2)) for v, a in sorted(aging.items(), key=lambda x: -sum(x[1].values()))]}


def take_snapshot(day=None, kind="manual", by="Wojtek Stock"):
    """The inventory's value at a moment, kept: month-ends and year-ends for your taxes, or whenever you ask."""
    inv = report_inventory()
    day = day or today_local_day()
    with Tx() as c:
        sid = c.execute("INSERT INTO snapshots(day,kind,at,by,items,units,value,no_cost) VALUES(?,?,?,?,?,?,?,?)",
                        (day, kind, now_iso(), by, len(inv["items"]), inv["units"], inv["total"], inv["noCost"])).lastrowid
        c.executemany("INSERT INTO snapshot_items(snapshot_id,item_id,name,sku,category,vendor,stock,cost,value) VALUES(?,?,?,?,?,?,?,?,?)",
                      [(sid, x["id"], x["name"], x["sku"], x["category"], x["vendor"], max(0, x["stock"]), x["cost"], x["value"]) for x in inv["items"]])
    log.info("Inventory snapshot (%s, %s): $%.2f", kind, day, inv["total"])
    bump()
    return sid


def snapshot_due(now):
    """It's the last day of the month, late: time for the month-end (or year-end) snapshot, if not taken yet."""
    tomorrow = (now + timedelta(days=1)).date()
    if tomorrow.day != 1 or (now.hour, now.minute) < (23, 45):
        return None
    day = now.strftime("%Y-%m-%d")
    if q1("SELECT 1 FROM snapshots WHERE day=? AND kind IN ('month-end','year-end')", day):
        return None
    return "year-end" if now.month == 12 else "month-end"


def reports_loop():
    """Month-end and year-end inventory snapshots, taken by themselves just before midnight; and the sales history,
    read once."""
    time.sleep(30)
    while True:
        try:
            if setup_confirmed() and WOO_READY and not (meta_get("sales_history", {}) or {}).get("loaded") \
                    and not (meta_get("sales_history", {}) or {}).get("running") and not (meta_get("sales_history", {}) or {}).get("error"):
                load_sales_history()
            now = datetime.now(LOCAL_TZ)
            kind = snapshot_due(now)
            if kind and setup_confirmed():
                take_snapshot(now.strftime("%Y-%m-%d"), kind)
        except (ApiError, WooError) as e:
            log.warning("Reports: %s", e)
        except Exception as e:
            log.warning("Reports loop: %s", e)
        time.sleep(60)


def export_report(kind, qs):
    if kind == "inventory":
        r = report_inventory()
        rows = [["Part", "SKU", "Category", "Vendor", "In stock", "Cost each", "Cost from", "Value"]]
        rows += [[x["name"], x["sku"], x["category"], x["vendor"], x["stock"], x["cost"] if x["cost"] is not None else "", x["costFrom"], x["value"]] for x in r["items"]]
        rows += [[], ["Total", "", "", "", r["units"], "", "", r["total"]]]
        return csv_bytes(rows)
    if kind == "sales":
        start, end = _period(qs)
        r = report_sales(start, end)
        rows = [[f"Sales {start} to {end}"], ["Part", "SKU", "Category", "Sold", "Orders", "Revenue", "Cost of what sold", "Profit", "In stock now"]]
        rows += [[x["name"], x["sku"], x["category"], x["qty"], x["orders"], x["revenue"], x["cogs"] if x["cogs"] is not None else "",
                  x["profit"] if x["profit"] is not None else "", x["stock"]] for x in r["items"]]
        rows += [[], ["Total", "", "", r["units"], "", r["revenue"], r["cogs"], r["profit"], ""]]
        return csv_bytes(rows)
    if kind == "margins":
        r = report_margins()
        rows = [["Part", "SKU", "Category", "Price", "Cost", "Cost from", "Profit each", "Margin %", "Flag"]]
        rows += [[x["name"], x["sku"], x["category"], x["price"] if x["price"] is not None else "", x["cost"] if x["cost"] is not None else "",
                  x["costFrom"], x["profit"] if x["profit"] is not None else "", x["margin"] if x["margin"] is not None else "", x["flag"]] for x in r["items"]]
        return csv_bytes(rows)
    if kind == "snapshot":
        s = q1("SELECT * FROM snapshots WHERE id=?", qint(qs, "id", 0))
        if not s:
            raise ApiError(404, "No such snapshot")
        rows = [[f"Inventory on {s['day']} ({s['kind']}), taken {local_time(s['at'])}"],
                ["Part", "SKU", "Category", "Vendor", "In stock", "Cost each", "Value"]]
        rows += [[x["name"], x["sku"], x["category"], x["vendor"], x["stock"], x["cost"] if x["cost"] is not None else "", x["value"]]
                 for x in q("SELECT * FROM snapshot_items WHERE snapshot_id=? ORDER BY value DESC", s["id"])]
        rows += [[], ["Total", "", "", "", s["units"], "", s["value"]]]
        return csv_bytes(rows)
    raise ApiError(404, "No such report")



# ---------------------------------------------------------------- the morning email, and alerts when something breaks
def morning_to():
    return CFG["morning_to"] or CFG["shop_email"] or CFG["po_cc"]


def alert_to():
    return CFG["alert_to"] or morning_to()


def watch_printer():
    """Every few minutes: how's the Zebra? Remembered with since when, for alerts and the morning email."""
    if not printer_addr():
        return
    last = meta_get("printer_state", {}) or {}
    if last.get("checked") and time.time() - last["checked"] < 300:
        return
    st = printer_status()
    since = last.get("since") if last.get("state") == st["state"] else now_iso()
    meta_set("printer_state", {"state": st["state"], "msg": st["msg"], "since": since, "checked": time.time()})


def attention_items(for_alert=False):
    """What needs a person, right now: (key, one line). The same list the morning email and the alerts use; an
    unreachable printer (often just switched off) is only in the morning email, never an alert."""
    out = []
    ps = meta_get("printer_state", {}) or {}
    since = parse_iso(ps.get("since"))
    mins = (datetime.now(timezone.utc) - since).total_seconds() / 60 if since else 0
    if ps.get("state") in ("paused", "paper", "head", "ribbon") and mins >= 15:
        out.append(("printer", f"Label printer: {ps['msg']} (for {int(mins)} minutes)"))
    elif ps.get("state") == "unreachable" and mins >= 30 and not for_alert:
        out.append(("printer-off", "Label printer: can't be reached (switched off, or a network change?)"))
    sy = meta_get("sync", {}) or {}
    last_ok = parse_iso(sy.get("last_ok"))
    if sy.get("error") and (not last_ok or (datetime.now(timezone.utc) - last_ok).total_seconds() > 1800):
        out.append(("sync", f"Syncing with WooCommerce has been failing: {sy['error']}"))
    p = writes_paused()
    if p:
        out.append(("paused", f"Changes to WooCommerce are paused: {p.get('reason', '')}"))
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    failed = q1("SELECT COUNT(*) n FROM jobs WHERE status='failed' AND done_at>=?", since)["n"]
    if failed:
        out.append(("failed", f"{failed} change{'s' if failed != 1 else ''} to WooCommerce failed in the last day (Log tab)"))
    d = dbx_conf()
    if d.get("refresh_token"):
        ok = parse_iso(d.get("last_ok"))
        if d.get("last_error"):
            out.append(("backup", f"The last Dropbox backup failed: {d['last_error']}"))
        elif not ok or (datetime.now(timezone.utc) - ok).days >= 2:
            out.append(("backup", "No Dropbox backup in two days"))
    g = meta_get("gb", {}) or {}
    if gb_ready() and g.get("error"):
        out.append(("gunbroker", f"GunBroker sync problem: {g['error']}"))
    n = q1("SELECT COUNT(*) n FROM gb_listings WHERE error!=''")["n"] + q1("SELECT COUNT(*) n FROM gb_orders WHERE status IN ('failed','cancelled after import')")["n"]
    if n:
        out.append(("gb-attention", f"GunBroker: {n} listing{'s' if n != 1 else ''} or sale{'s' if n != 1 else ''} flagged (GunBroker tab)"))
    w = q1("SELECT COUNT(*) n FROM restock_watch")["n"]
    if w:
        out.append(("double-restock", f"{w} possible double restock{'s' if w != 1 else ''} (Setup tab)"))
    return out


def morning_report(day=None):
    """(subject, text) of the morning email."""
    now = datetime.now(LOCAL_TZ)
    yday = day or (now - timedelta(days=1)).strftime("%Y-%m-%d")
    L = []
    money_ = lambda x: f"${x:,.2f}"
    sr = report_sales(yday, yday)
    L.append(f"YESTERDAY ({datetime.strptime(yday, '%Y-%m-%d'):%a %b %-d})")
    if sr["items"]:
        L.append(f"  Sold {sr['units']:g} unit{'s' if sr['units'] != 1 else ''} of in-shop parts, {money_(sr['revenue'])}: " +
                 ", ".join(f"{c['channel']} {money_(c['total'])}" for c in sr["channels"]))
        for x in sr["items"][:5]:
            L.append(f"    {x['qty']:g} x {x['name']} ({x['sku']})  {money_(x['revenue'])}")
    else:
        L.append("  No in-shop parts sold.")
    ro = [r for r in reorder_status() if r["need"]]
    drafts = q("SELECT p.number, v.name, v.auto_send, (SELECT COUNT(*) FROM po_lines WHERE po_id=p.id) n FROM pos p JOIN vendors v ON v.id=p.vendor_id "
               "WHERE p.status='draft' AND p.auto=1")
    L += ["", "NEEDS ORDERING"]
    if ro:
        L += [f"  {r['name']} ({r['sku']}): {r['stock']} in stock, reorder at {r['min']}" + (f", from {r['vendor']}" if r["vendor"] else ", NO VENDOR SET") for r in ro[:15]]
        if len(ro) > 15:
            L.append(f"  ...and {len(ro) - 15} more")
    for dft in drafts:
        if dft["n"]:
            L.append(f"  Automatic draft {dft['number']} to {dft['name']}: {dft['n']} part{'s' if dft['n'] != 1 else ''}" +
                     (f", emailed by itself at {CFG['auto_po_time']}" if dft["auto_send"] else ", waiting for you to send it"))
    if not ro and not any(d_["n"] for d_ in drafts):
        L.append("  Nothing: every part with a reorder point has enough coming.")
    L += ["", "COMING IN"]
    pos = q("SELECT p.*, v.name vendor, v.lead_days FROM pos p JOIN vendors v ON v.id=p.vendor_id WHERE p.status IN ('sent','partial') ORDER BY p.sent_at")
    for p in pos:
        d_ = po_out(p, full=False)
        sent = parse_iso(p["sent_at"])
        days = (datetime.now(timezone.utc) - sent).days if sent else 0
        late = p["lead_days"] is not None and days > (p["lead_days"] or 0) + 2
        ago = "today" if days == 0 else "yesterday" if days == 1 else f"{days} days ago"
        L.append(f"  {p['number']} from {p['vendor']}: {d_['open']} unit{'s' if d_['open'] != 1 else ''} still due, sent {ago}"
                 + (f"  LATE (usually {p['lead_days']} days)" if late else ""))
    if not pos:
        L.append("  No open purchase orders.")
    pb = payables()
    t = pb["totals"]
    L += ["", "BILLS"]
    if t["count"]:
        L.append(f"  Overdue {money_(t['overdue'])} · due within a week {money_(t['soon'])} · later {money_(t['later'])}")
        for b in q("SELECT * FROM bills WHERE void=0 AND kind='bill' ORDER BY due"):
            st = bill_state(b)
            if st["owed"] > 0.004 and st["flag"]:
                v = (q1("SELECT name FROM vendors WHERE id=?", b["vendor_id"]) or {"name": "?"})["name"]
                L.append(f"    {v} invoice {b['number']}: {money_(st['owed'])} {st['flag']} (due {b['due']})")
    else:
        L.append("  Nothing owed.")
    outs = q("SELECT name, attrs, sku FROM items WHERE scope='shop' AND stock<=0 ORDER BY name COLLATE NOCASE")
    L += ["", f"OUT OF STOCK ({len(outs)})"]
    L += [f"  {o['name']}{' (' + o['attrs'] + ')' if o['attrs'] else ''} ({o['sku']})" for o in outs[:20]] or ["  None."]
    if len(outs) > 20:
        L.append(f"  ...and {len(outs) - 20} more (Products tab: Out of stock)")
    waiting = q("SELECT * FROM rmas WHERE status='shipped' ORDER BY shipped_at")
    if waiting:
        L += ["", "RETURNS TO VENDORS, WAITING ON A CREDIT"]
        for r_ in waiting:
            d_ = rma_out(r_, full=False)
            sent = parse_iso(r_["shipped_at"])
            L.append(f"  {r_['number']} to {d_['vendor']}: ${d_['expected']:,.2f} expected, shipped {(datetime.now(timezone.utc) - sent).days if sent else 0} days ago")
    od = len(overdue_counts(90))
    if od:
        L += ["", f"COUNTING: {od} in-stock part{'s' if od != 1 else ''} not counted in 90 days (Products tab: Not counted in 90 days)"]
    att = attention_items()
    drift = q1("SELECT COUNT(*) n FROM drift WHERE at>=?", (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds"))["n"]
    if drift:
        att.append(("drift", f"Stock changed outside sales on {drift} part{'s' if drift != 1 else ''} yesterday (Setup tab)"))
    L += ["", "NEEDS ATTENTION"] + ([f"  ! {t_}" for _, t_ in att] or ["  All clear."])
    L += ["", f"Wojtek Stock {VERSION}"]
    subject = f"Wojtek Stock: {now:%a %b %-d}" + (f", {len(att)} thing{'s' if len(att) != 1 else ''} need{'' if len(att) != 1 else 's'} attention" if att else "")
    return subject, "\n".join(L)


def send_morning(by="Wojtek Stock"):
    to = morning_to()
    if not to:
        raise ApiError(400, "No address for the morning email: set MORNING_EMAIL_TO (or SHOP_EMAIL) in stock.env")
    subject, text = morning_report()
    send_email(to, subject, text)
    meta_set("morning_last", {"at": now_iso(), "to": to, "by": by})
    log.info("Morning email sent to %s", to)
    bump()
    return {"to": to, "subject": subject}


def check_alerts():
    """Email when something breaks (once, not every minute), and again when it's back to normal."""
    if not CFG["alerts_on"] or not email_ready() or not alert_to():
        return
    now = {k: t for k, t in attention_items(for_alert=True) if k not in ("double-restock", "gb-attention")}   # those wait for the morning
    active = meta_get("alerts_active", {}) or {}
    new = {k: t for k, t in now.items() if k not in active}
    cleared = [k for k in active if k not in now]
    try:
        if new:
            send_email(alert_to(), f"Wojtek Stock needs attention: {list(new.values())[0][:80]}",
                       "\n\n".join(new.values()) + f"\n\nOpen Wojtek Stock to look: the banner at the top says more.\n\nWojtek Stock {VERSION}")
        if cleared:
            send_email(alert_to(), "Wojtek Stock: back to normal",
                       "These are fine again:\n\n" + "\n".join(f"- {active[k]}" for k in cleared) + f"\n\nWojtek Stock {VERSION}")
    except ApiError as e:
        log.warning("Couldn't send an alert email: %s", e.msg)
        return
    if new or cleared:
        meta_set("alerts_active", {k: now[k] for k in now})
        log.info("Alerts: %d new, %d cleared", len(new), len(cleared))


def notify_loop():
    every = max(1, int(env("NOTIFY_CHECK_SECONDS", "60") or 60))
    time.sleep(min(20, every * 5))
    while True:
        try:
            if setup_confirmed():
                watch_printer()
                check_alerts()
                mt = CFG["morning_time"]
                now = datetime.now(LOCAL_TZ)
                if mt != "off" and email_ready() and morning_to():
                    h, _, m = mt.partition(":")
                    if (now.hour, now.minute) >= (int(h or 7), int(m or 0)) and meta_get("morning_day") != now.strftime("%Y-%m-%d"):
                        meta_set("morning_day", now.strftime("%Y-%m-%d"))
                        send_morning()
        except Exception as e:
            log.warning("Notifications: %s", e)
        time.sleep(every)



# ---------------------------------------------------------------- bins (where parts live) and counting by location
def bin_code(v):
    """A bin's code, as typed or scanned: A-03, a03 or BIN-A-03 all mean A-03."""
    c = skey(v)
    if c.startswith("BIN-"):
        c = c[4:]
    c = re.sub(r"\s+", "-", c)
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9._/-]{0,19}", c):
        raise ApiError(400, "A bin code is letters and digits (and - _ . /), up to 20, such as A-03")
    return c


def last_counts():
    return {r["item_id"]: r["at"] for r in q("SELECT item_id, MAX(at) at FROM item_counts GROUP BY item_id")}


def item_bin_codes():
    out = {}
    for r in q("SELECT ib.item_id, b.code FROM item_bins ib JOIN bins b ON b.id=ib.bin_id ORDER BY ib.item_id, ib.pos, b.code"):
        out.setdefault(r["item_id"], []).append(r["code"])
    return out


def list_bins():
    lc = last_counts()
    out = []
    for b in q("SELECT * FROM bins ORDER BY code"):
        items = q("SELECT i.id, i.stock FROM item_bins ib JOIN items i ON i.id=ib.item_id WHERE ib.bin_id=? AND i.scope IN ('shop','readonly')", b["id"])
        counts = [lc.get(i["id"]) for i in items]
        out.append(dict(b, parts=len(items), units=sum(max(0, i["stock"] or 0) for i in items),
                        oldest=None if not items else (min(c for c in counts if c) if all(counts) else "never")))
    return out


def save_bin(b, by=""):
    code = bin_code(b.get("code"))
    name, notes = _text(b.get("name"), 80), _text(b.get("notes"), 300)
    bid = b.get("id")
    other = q1("SELECT id FROM bins WHERE code=? AND id!=?", code, int(bid or 0))
    if other:
        raise ApiError(409, f"There's already a bin {code}")
    with DB_LOCK:
        if bid:
            conn.execute("UPDATE bins SET code=?, name=?, notes=? WHERE id=?", (code, name, notes, int(bid)))
        else:
            bid = conn.execute("INSERT INTO bins(code,name,notes,created_at) VALUES(?,?,?,?)", (code, name, notes, now_iso())).lastrowid
    note_change("bin", "saved", code, name, by)
    bump(items=True)
    return dict(q1("SELECT * FROM bins WHERE id=?", int(bid)))


def remove_bin(bid, by=""):
    b = q1("SELECT * FROM bins WHERE id=?", bid)
    if not b:
        raise ApiError(404, "No such bin")
    n = q1("SELECT COUNT(*) n FROM item_bins WHERE bin_id=?", bid)["n"]
    with DB_LOCK:
        conn.execute("DELETE FROM item_bins WHERE bin_id=?", (bid,))
        conn.execute("DELETE FROM bins WHERE id=?", (bid,))
    note_change("bin", "removed", b["code"], f"{n} part(s) unassigned", by)
    bump(items=True)


def get_bin(code):
    b = q1("SELECT * FROM bins WHERE code=?", bin_code(code))
    if not b:
        raise ApiError(404, f"No bin {bin_code(code)}: add it on the Products tab (Bins)")
    lc = last_counts()
    items = [dict(item_out(r), lastCount=lc.get(r["id"]), main=r["pos"] == 0) for r in
             q("SELECT i.*, ib.pos FROM item_bins ib JOIN items i ON i.id=ib.item_id WHERE ib.bin_id=? AND i.scope IN ('shop','readonly') "
               "ORDER BY i.name COLLATE NOCASE, i.attrs", b["id"])]
    return dict(b, items=items)


def set_item_bins(item_id, codes, by=""):
    r = _item(item_id)
    if r["scope"] not in ("shop", "readonly"):
        raise ApiError(409, f"{label_name(r)} isn't an in-shop part")
    ids = []
    for c in codes or []:
        b = q1("SELECT * FROM bins WHERE code=?", bin_code(c))
        if not b:
            raise ApiError(404, f"No bin {bin_code(c)}: add it first")
        if b["id"] not in ids:
            ids.append(b["id"])
    with Tx() as c_:
        c_.execute("DELETE FROM item_bins WHERE item_id=?", (r["id"],))
        c_.executemany("INSERT INTO item_bins(item_id,bin_id,pos) VALUES(?,?,?)", [(r["id"], b_, i) for i, b_ in enumerate(ids)])
    note_change("bin", "assigned", label_name(r), ", ".join(codes or []) or "(none)", by)
    bump(items=True)
    return item_bin_codes().get(r["id"], [])


def add_to_bin(item_id, bin_id):
    """Put a part in a bin if it isn't already (its main bin if it has none)."""
    if q1("SELECT 1 FROM item_bins WHERE item_id=? AND bin_id=?", item_id, bin_id):
        return False
    n = q1("SELECT COUNT(*) n FROM item_bins WHERE item_id=?", item_id)["n"]
    with DB_LOCK:
        conn.execute("INSERT INTO item_bins(item_id,bin_id,pos) VALUES(?,?,?)", (item_id, bin_id, n))
    return True


def active_full_count():
    return q1("SELECT * FROM full_counts WHERE status='open' ORDER BY id DESC LIMIT 1")


def count_done(b, by, operator):
    """A count finished: every part counted is recorded as counted (even if it matched); the ticked differences go to
    WooCommerce (only if stock hasn't moved since the review); counted parts can be put in the bin being counted."""
    check_can_write()
    lines = b.get("lines") or []
    if not isinstance(lines, list) or not lines or len(lines) > 1000:
        raise ApiError(400, "Nothing counted")
    bin_row = None
    if b.get("bin_id"):
        bin_row = q1("SELECT * FROM bins WHERE id=?", _int(b.get("bin_id"), 1, 10 ** 9, "Bin"))
        if not bin_row:
            raise ApiError(404, "No such bin")
    fc = active_full_count()
    specs, recs, seen = [], [], set()
    for ln in lines:
        r = _item(ln.get("id"))
        if r["id"] in seen:
            raise ApiError(400, f"{label_name(r)} is on the count twice")
        seen.add(r["id"])
        counted = _int(ln.get("counted"), 0, 1000000, "Counted")
        expected = _int(ln.get("expected"), -1000000, 1000000, "Expected stock") if ln.get("expected") is not None else None
        recs.append((r["id"], now_iso(), by, counted, expected, bin_row["id"] if bin_row else None, fc["id"] if fc else None))
        if ln.get("apply"):
            if r["scope"] != "shop":
                raise ApiError(409, f"{label_name(r)} can't be changed: {r['why'] or r['scope']}")
            if expected is None:
                raise ApiError(400, "A change needs the stock seen at the review")
            if counted != expected:
                specs.append({"item": r, "target": counted, "expected": expected})
    with DB_LOCK:
        conn.executemany("INSERT INTO item_counts(item_id,at,by,counted,expected,bin_id,full_id) VALUES(?,?,?,?,?,?,?)", recs)
    assigned = 0
    if bin_row and b.get("assign", True):
        for rid, *_ in recs:
            if b.get("assign_ids") is None or rid in (b.get("assign_ids") or []):
                assigned += add_to_bin(rid, bin_row["id"])
    res = queue_jobs("count", specs, by, operator, "Count" + (f" (bin {bin_row['code']})" if bin_row else ""), "") if specs else {"batch": None, "jobs": []}
    bump(items=True)
    return dict(res, recorded=len(recs), assigned=assigned)


def overdue_counts(days=90):
    lc = last_counts()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat(timespec="seconds")
    bins = item_bin_codes()
    out = [dict(item_out(r), lastCount=lc.get(r["id"]), bins=bins.get(r["id"], []))
           for r in q("SELECT * FROM items WHERE scope='shop' AND COALESCE(stock,0)!=0 ORDER BY name COLLATE NOCASE, attrs")
           if (lc.get(r["id"]) or "") < cutoff]
    return out


def full_count_status():
    fc = active_full_count()
    last = q1("SELECT * FROM full_counts WHERE status='finished' ORDER BY id DESC LIMIT 1")
    out = {"active": dict(fc) if fc else None, "last": dict(last) if last else None}
    if fc:
        counted = {r["item_id"] for r in q("SELECT DISTINCT item_id FROM item_counts WHERE full_id=?", fc["id"])}
        shop = q("SELECT * FROM items WHERE scope='shop'")
        bins = item_bin_codes()
        missing = [dict(item_out(r), bins=bins.get(r["id"], [])) for r in shop if r["id"] not in counted and (r["stock"] or 0) != 0]
        out.update(counted=len(counted), total=len(shop), missing=sorted(missing, key=lambda x: (x["bins"][:1] or ["~"], x["name"])))
    return out


def full_count_start(by=""):
    if active_full_count():
        raise ApiError(409, "A full count is already going")
    with DB_LOCK:
        conn.execute("INSERT INTO full_counts(started_at,by,status) VALUES(?,?,'open')", (now_iso(), by))
    note_change("count", "full count started", "", by=by)
    bump()
    return full_count_status()


def full_count_finish(b, by, operator):
    """End the full count: parts in stock that nobody counted can be set to 0 (ticked ones only)."""
    fc = active_full_count()
    if not fc:
        raise ApiError(409, "No full count is going")
    zero = [int(x) for x in (b.get("zero_ids") or []) if str(x).isdigit()]
    res = {"batch": None}
    if zero:
        check_can_write()
        specs = []
        for i in zero:
            r = _item(i)
            if r["scope"] == "shop" and (r["stock"] or 0) != 0:
                specs.append({"item": r, "target": 0, "expected": r["stock"]})
        if specs:
            res = queue_jobs("count", specs, by, operator, "Full count: not found", "Set to 0 at the end of the full count")
            with DB_LOCK:
                conn.executemany("INSERT INTO item_counts(item_id,at,by,counted,expected,bin_id,full_id) VALUES(?,?,?,?,?,?,?)",
                                 [(sp["item"]["id"], now_iso(), by, 0, sp["expected"], None, fc["id"]) for sp in specs])
    with DB_LOCK:
        conn.execute("UPDATE full_counts SET status='finished', finished_at=?, finished_by=? WHERE id=?", (now_iso(), by, fc["id"]))
    note_change("count", "full count finished", "", f"{len(zero)} part(s) set to 0", by)
    bump(items=True)
    return dict(res, zeroed=len(zero))



# ---------------------------------------------------------------- returns to vendors (RMAs)
# Parts going back to a vendor: from stock (taken out of WooCommerce when the return ships) or from the damaged pile
# (customer returns logged as damaged, adjustments made as Damaged or Returned to vendor: already out of stock). It ships
# with the vendor's RA number, a credit is expected at what you paid, and the vendor's credit, when it comes, becomes a
# credit on their account (vendor invoices) to apply to a bill.
RMA_DONE = ("closed", "cancelled")


def rma_cost(vendor_id, item_id):
    vi = q1("SELECT last_cost FROM vendor_items WHERE vendor_id=? AND item_id=?", vendor_id, item_id)
    if vi and vi["last_cost"]:
        return vi["last_cost"]
    c = cost_table().get(item_id)
    return c[0] if c else 0.0


def damaged_pile(vendor_id=None):
    """Parts already out of stock and waiting to go back: customer returns logged as damaged, adjustments made as
    Damaged or Returned to vendor, not on a return yet. With a vendor: theirs first (by who supplies the part)."""
    used = {r["source_job_id"] for r in q("SELECT rl.source_job_id FROM rma_lines rl JOIN rmas r ON r.id=rl.rma_id "
                                         "WHERE rl.source_job_id IS NOT NULL AND r.status!='cancelled'")}
    vend = item_vendor_names()
    vid_names = {v["id"]: v["name"] for v in q("SELECT id, name FROM vendors")}
    out = []
    for j in q("SELECT * FROM jobs WHERE ((kind='return' AND restock=0 AND status='logged') OR "
               "(kind='adjust' AND delta<0 AND status IN ('done','dryrun') AND reason IN ('Damaged','Returned to vendor'))) "
               "AND at>=? ORDER BY id DESC", (datetime.now(timezone.utc) - timedelta(days=365)).isoformat(timespec="seconds")):
        if j["id"] in used:
            continue
        it = q1("SELECT scope FROM items WHERE id=?", j["item_id"])
        if not it or it["scope"] not in ("shop", "readonly"):
            continue   # feed products and guns don't go back through here
        qty = j["qty"] if j["kind"] == "return" else -int(j["delta"] or 0)
        out.append({"job_id": j["id"], "item_id": j["item_id"], "name": j["name"], "sku": j["sku"], "qty": qty, "at": j["at"],
                    "reason": "Customer return, damaged" + (f" (order {j['order_number']})" if j["order_number"] else "") if j["kind"] == "return"
                    else j["reason"] + (f": {j['note']}" if j["note"] else ""), "vendor": vend.get(j["item_id"], "")})
    if vendor_id:
        name = vid_names.get(vendor_id, "")
        out.sort(key=lambda x: (x["vendor"] != name, x["at"]))
    return out


def rma_out(r, full=True):
    d = dict(r)
    v = q1("SELECT * FROM vendors WHERE id=?", r["vendor_id"])
    d["vendor"] = v["name"] if v else "(removed vendor)"
    lines = [dict(x) for x in q("SELECT * FROM rma_lines WHERE rma_id=? ORDER BY id", r["id"])]
    d["expected"] = round(sum((x["qty"] or 0) * (x["cost"] or 0) for x in lines), 2)
    d["units"] = sum(x["qty"] or 0 for x in lines)
    cr = q1("SELECT * FROM bills WHERE id=?", r["credit_bill_id"]) if r["credit_bill_id"] else None
    d["credit"] = {"id": cr["id"], "number": cr["number"], "total": cr["total"], "date": cr["date"]} if cr else None
    d["short"] = round(d["expected"] - cr["total"], 2) if cr and cr["total"] + 0.004 < d["expected"] else 0
    if full:
        for x in lines:
            x["jobStatus"] = (q1("SELECT status, error FROM jobs WHERE id=?", x["job_id"]) or {"status": None})["status"] if x["job_id"] else None
            it = q1("SELECT stock FROM items WHERE id=?", x["item_id"])
            x["stock"] = it["stock"] if it else None
        d["lines"] = lines
        d["vendorInfo"] = dict(v) if v else None
    return d


def list_rmas(status=""):
    where = "status NOT IN ('closed','cancelled')" if status == "open" else "status=?" if status else "1=1"
    return [rma_out(r, full=False) for r in q(f"SELECT * FROM rmas WHERE {where} ORDER BY id DESC LIMIT 500", *([status] if status and status != "open" else []))]


def save_rma(b, by=""):
    vid = _int(b.get("vendor_id"), 1, 10 ** 9, "Vendor")
    if not q1("SELECT 1 FROM vendors WHERE id=?", vid):
        raise ApiError(400, "Pick a vendor")
    rid = b.get("id")
    raw = b.get("lines") or []
    if not isinstance(raw, list) or len(raw) > 300:
        raise ApiError(400, "Up to 300 lines")
    lines = []
    for ln in raw:
        r = _item(ln.get("item_id"))
        if r["scope"] not in ("shop", "readonly"):
            raise ApiError(409, f"{label_name(r)} isn't an in-shop part")
        qty = _int(ln.get("qty"), 1, 100000, "Quantity")
        src = int(ln["source_job_id"]) if str(ln.get("source_job_id") or "").isdigit() else None
        remove = 0 if src else (1 if ln.get("remove", True) else 0)
        if remove and r["scope"] != "shop":
            raise ApiError(409, f"{label_name(r)}'s stock isn't tracked: untick \"take out of stock\" for it")
        lines.append((r, qty, _money(ln.get("cost") if ln.get("cost") not in (None, "") else rma_cost(vid, r["id"]), "Credit each"),
                      _text(ln.get("reason"), 120), remove, src))
    now = now_iso()
    with Tx() as c:
        if rid:
            old = c.execute("SELECT * FROM rmas WHERE id=?", (int(rid),)).fetchone()
            if not old:
                raise ApiError(404, "No such return")
            if old["status"] != "open":
                raise ApiError(409, f"{old['number']} has shipped: its parts can't change")
            c.execute("UPDATE rmas SET vendor_id=?, vendor_rma=?, reason=?, notes=?, updated_at=? WHERE id=?",
                      (vid, _text(b.get("vendor_rma"), 60), _text(b.get("reason"), 200), _text(b.get("notes"), 1000), now, old["id"]))
            c.execute("DELETE FROM rma_lines WHERE rma_id=?", (old["id"],))
            rid = old["id"]
        else:
            n = int(meta_get("rma_next", 1) or 1)
            meta_set("rma_next", n + 1)
            rid = c.execute("INSERT INTO rmas(number,vendor_id,status,vendor_rma,reason,notes,created_at,by,updated_at) VALUES(?,?,'open',?,?,?,?,?,?)",
                            (f"RMA-{n:04d}", vid, _text(b.get("vendor_rma"), 60), _text(b.get("reason"), 200), _text(b.get("notes"), 1000),
                             now, by, now)).lastrowid
        for r, qty, cost, reason, remove, src in lines:
            c.execute("INSERT INTO rma_lines(rma_id,item_id,name,sku,qty,cost,reason,remove,source_job_id) VALUES(?,?,?,?,?,?,?,?,?)",
                      (rid, r["id"], label_name(r), r["sku"], qty, cost, reason, remove, src))
    bump()
    return rma_out(q1("SELECT * FROM rmas WHERE id=?", rid))


def ship_rma(b, by, operator):
    r = q1("SELECT * FROM rmas WHERE id=?", _int(b.get("id"), 1, 10 ** 9, "Return"))
    if not r:
        raise ApiError(404, "No such return")
    if r["status"] != "open":
        raise ApiError(409, f"{r['number']} has already shipped")
    lines = q("SELECT * FROM rma_lines WHERE rma_id=?", r["id"])
    if not lines:
        raise ApiError(400, "Add the parts going back first")
    take = [l for l in lines if l["remove"]]
    if take:
        check_can_write()
    specs = []
    for l in take:
        it = _item(l["item_id"])
        if it["scope"] != "shop":
            raise ApiError(409, f"{label_name(it)} can't be changed: {it['why']}")
        specs.append({"item": it, "delta": -int(l["qty"])})
    res = queue_jobs("adjust", specs, by, operator, f"Returned to vendor ({r['number']})", r["vendor_rma"] and f"Vendor RA {r['vendor_rma']}" or "") if specs else {"jobs": []}
    with DB_LOCK:
        for l, jid in zip(take, res["jobs"]):
            conn.execute("UPDATE rma_lines SET job_id=? WHERE id=?", (jid, l["id"]))
        conn.execute("UPDATE rmas SET status='shipped', shipped_at=?, ship_date=?, carrier=?, tracking=?, updated_at=? WHERE id=?",
                     (now_iso(), _day(b.get("date") or today_local_day(), "Ship date"), _text(b.get("carrier"), 40), _text(b.get("tracking"), 80),
                      now_iso(), r["id"]))
    note_change("rma", "shipped", r["number"], f"{len(lines)} line(s)", by)
    bump()
    return dict(rma_out(q1("SELECT * FROM rmas WHERE id=?", r["id"])), batch=res.get("batch"))


def credit_rma(b, by=""):
    """The vendor's credit for a return: a credit on their account (vendor invoices) to apply to a bill."""
    r = q1("SELECT * FROM rmas WHERE id=?", _int(b.get("id"), 1, 10 ** 9, "Return"))
    if not r:
        raise ApiError(404, "No such return")
    if r["status"] != "shipped":
        raise ApiError(409, "A credit is recorded once the return has shipped" if r["status"] == "open" else f"{r['number']} is {r['status']}")
    amount = _money(b.get("amount"), "Credit amount")
    if amount <= 0:
        raise ApiError(400, "The credit should be more than 0 (to close a return with no credit, use Close)")
    cr = save_bill({"vendor_id": r["vendor_id"], "kind": "credit", "number": _text(b.get("number"), 60) or f"Credit for {r['number']}",
                    "date": b.get("date") or today_local_day(),
                    "lines": [{"description": f"Credit for {r['number']}" + (f" (vendor RA {r['vendor_rma']})" if r["vendor_rma"] else ""),
                               "qty": 1, "price": amount}], "notes": f"From return {r['number']}"}, by)
    with DB_LOCK:
        conn.execute("UPDATE rmas SET status='credited', credit_bill_id=?, updated_at=? WHERE id=?", (cr["id"], now_iso(), r["id"]))
    note_change("rma", "credited", r["number"], f"${amount:,.2f}", by)
    bump()
    return rma_out(q1("SELECT * FROM rmas WHERE id=?", r["id"]))


def close_rma(b, by=""):
    r = q1("SELECT * FROM rmas WHERE id=?", _int(b.get("id"), 1, 10 ** 9, "Return"))
    if not r:
        raise ApiError(404, "No such return")
    act = b.get("action")
    if act == "cancel":
        if r["status"] != "open":
            raise ApiError(409, "Only a return that hasn't shipped can be cancelled")
        status = "cancelled"
    elif act == "close":
        if r["status"] not in ("shipped", "credited"):
            raise ApiError(409, "Ship it first (or cancel it)")
        status = "closed"
    else:
        raise ApiError(400, "Unknown action")
    with DB_LOCK:
        conn.execute("UPDATE rmas SET status=?, closed_at=?, close_note=?, updated_at=? WHERE id=?",
                     (status, now_iso(), _text(b.get("note"), 300), now_iso(), r["id"]))
    note_change("rma", status, r["number"], _text(b.get("note"), 300), by)
    bump()
    return rma_out(q1("SELECT * FROM rmas WHERE id=?", r["id"]))


def rma_pdf(r):
    """The return form: letterhead, vendor, their RA number, the parts going back, and the credit expected."""
    W, H, M = 215.9, 279.4, 16
    pg = PdfPage(W, H)
    y = M
    pg.text(M, y + 6, CFG["shop_name"], 16, "B")
    yy = y + 12
    addr = [x.strip() for x in CFG["shop_address"].split("|") if x.strip()]
    for t in addr + [" · ".join(x for x in (CFG["shop_phone"], CFG["shop_email"]) if x)]:
        if t:
            pg.text(M, yy, t, 9, "R")
            yy += 4.4
    pg.text(W - M - 34, y + 6, "RETURN TO VENDOR", 13, "B", "right")
    pg.text(W - M - 34, y + 13, r["number"], 12, "B", "right")
    if r["vendor_rma"]:
        pg.text(W - M - 34, y + 19, f"Your RA # {r['vendor_rma']}", 10, "B", "right")
    pg.qr(r["number"], W - M - 30, y - 1, 30)
    y = max(yy, y + 31) + 4
    pg.rect(M, y, W - 2 * M, 0.25)
    y += 6
    v = r.get("vendorInfo") or {}
    pg.text(M, y, "TO", 8, "B")
    y += 5
    for i, t in enumerate(x for x in (v.get("name"), v.get("contact"), v.get("email"), v.get("phone"),
                                      ("Account # " + v["account"]) if v.get("account") else "") if x):
        pg.text(M, y, t, 10, "B" if i == 0 else "R")
        y += 4.8
    if r["reason"]:
        y += 2
        for t in wrap_mm("Reason: " + r["reason"], "R", 10, W - 2 * M):
            pg.text(M, y, t, 10, "R")
            y += 4.8
    y += 5
    cols = [(M, "Our SKU"), (M + 34, "Part"), (W - M - 60, "Qty"), (W - M - 28, "Credit each"), (W - M, "Total")]
    for i, (x, t) in enumerate(cols):
        pg.text(x, y, t, 8, "B", "right" if i >= 2 else "left")
    y += 2
    pg.rect(M, y, W - 2 * M, 0.25)
    y += 5
    for l in r["lines"]:
        desc = wrap_mm(l["name"] + (f" ({l['reason']})" if l["reason"] else ""), "R", 9, 90)
        pg.text(M, y, fit_mm(l["sku"] or "", "M", 8.5, 32), 8.5, "M")
        for i, dl in enumerate(desc):
            pg.text(M + 34, y + i * 4.2, dl, 9, "R")
        pg.text(W - M - 60, y, str(l["qty"]), 9.5, "B", "right")
        pg.text(W - M - 28, y, f"${l['cost'] or 0:,.2f}", 9, "R", "right")
        pg.text(W - M, y, f"${(l['cost'] or 0) * l['qty']:,.2f}", 9, "R", "right")
        y += len(desc) * 4.2 + 2
        pg.rect(M, y - 1.2, W - 2 * M, 0.25)
        y += 3.5
    pg.text(W - M - 28, y + 2, "Credit expected", 10, "B", "right")
    pg.text(W - M, y + 2, f"${r['expected']:,.2f}", 11, "B", "right")
    if r["notes"]:
        y += 12
        for t in wrap_mm(r["notes"], "R", 9.5, W - 2 * M):
            pg.text(M, y, t, 9.5, "R")
            y += 4.6
    pg.text(M, H - 14, f"Please put {r['number']}" + (f" and RA # {r['vendor_rma']}" if r["vendor_rma"] else "") + " on the credit.", 9, "R")
    return build_pdf([pg])


def email_rma(rid, by=""):
    r = q1("SELECT * FROM rmas WHERE id=?", rid)
    if not r:
        raise ApiError(404, "No such return")
    v = q1("SELECT * FROM vendors WHERE id=?", r["vendor_id"])
    if not v or not v["email"]:
        raise ApiError(400, "The vendor has no email address")
    d = rma_out(r)
    lines = [f"{l['qty']} x {l['name']} ({l['sku']})" + (f": {l['reason']}" if l["reason"] else "") for l in d["lines"]]
    text = "\n".join([f"Hello{(' ' + v['contact'].split()[0]) if v['contact'] else ''},", "",
                      f"We're returning these parts ({d['number']}" + (f", your RA # {d['vendor_rma']}" if d["vendor_rma"] else "") + "):", ""] + lines +
                     ["", f"Credit expected: ${d['expected']:,.2f}"] + ([f"", f"Reason: {d['reason']}"] if d["reason"] else []) +
                     ["", "The return form is attached." + ("" if d["vendor_rma"] else " Please send an RA number if you need one."), "", "Thank you,", CFG["shop_name"]])
    send_email(v["email"], f"Return {d['number']} from {CFG['shop_name']}", text, [(f"{d['number']}.pdf", rma_pdf(d))], CFG["po_cc"] or None)
    with DB_LOCK:
        conn.execute("UPDATE rmas SET emailed_at=? WHERE id=?", (now_iso(), rid))
    note_change("rma", "emailed", d["number"], v["email"], by)
    bump()
    return rma_out(q1("SELECT * FROM rmas WHERE id=?", rid))



# ---------------------------------------------------------------- kits: combos and packs made from other parts
# A kit is a WooCommerce product (a combo: rod + oil + patches; or a pack: a "case of 10" made of 10 boxes) whose
# stock is how many can be made from its parts. Selling one (an order that's processing, on hold, completed, or later
# refunded) takes its parts out once per order; a cancelled order puts them back; a return in Return mode restocks the
# parts. Kits aren't received, counted or adjusted themselves: their parts are.
KIT_SOLD = ("processing", "on-hold", "completed", "refunded")


def is_kit(item_id):
    return bool(q1("SELECT 1 FROM kits WHERE item_id=?", item_id))


def kit_parts(kit_id):
    return [dict(r) for r in q("SELECT kp.part_id, kp.qty, i.name, i.attrs, i.sku, i.stock, i.scope FROM kit_parts kp "
                               "LEFT JOIN items i ON i.id=kp.part_id WHERE kp.kit_id=? ORDER BY kp.rowid", kit_id)]


def kit_available(kit_id):
    parts = kit_parts(kit_id)
    if not parts:
        return None
    return max(0, min(((p["stock"] or 0) // p["qty"]) if p["qty"] else 0 for p in parts))


def list_kits():
    out = []
    for k in q("SELECT k.*, i.name, i.attrs, i.sku, i.stock FROM kits k JOIN items i ON i.id=k.item_id ORDER BY i.name COLLATE NOCASE"):
        out.append(dict(k, label=label_name(k), parts=kit_parts(k["item_id"]), available=kit_available(k["item_id"])))
    return out


def save_kit(b, by=""):
    k = _item(b.get("item_id"))
    if k["scope"] != "shop":
        raise ApiError(409, f"{label_name(k)} can't be a kit: {k['why'] or 'its stock must be tracked in WooCommerce'}")
    raw = b.get("parts") or []
    if not isinstance(raw, list) or not raw or len(raw) > 50:
        raise ApiError(400, "A kit needs 1 to 50 parts")
    parts, seen = [], set()
    for x in raw:
        p = _item(x.get("part_id"))
        if p["id"] == k["id"]:
            raise ApiError(400, "A kit can't contain itself")
        if p["scope"] != "shop":
            raise ApiError(409, f"{label_name(p)} can't be a part: {p['why'] or p['scope']}")
        if is_kit(p["id"]):
            raise ApiError(409, f"{label_name(p)} is a kit itself: list its parts instead")
        if p["id"] in seen:
            raise ApiError(400, f"{label_name(p)} is listed twice")
        seen.add(p["id"])
        parts.append((p["id"], _int(x.get("qty"), 1, 10000, "Quantity")))
    if q1("SELECT 1 FROM kit_parts WHERE part_id=?", k["id"]):
        raise ApiError(409, f"{label_name(k)} is a part in another kit, so it can't be a kit too")
    new = not is_kit(k["id"])
    with Tx() as c:
        if new:
            c.execute("INSERT INTO kits(item_id,created_at,by) VALUES(?,?,?)", (k["id"], now_iso(), by))
        c.execute("DELETE FROM kit_parts WHERE kit_id=?", (k["id"],))
        c.executemany("INSERT INTO kit_parts(kit_id,part_id,qty) VALUES(?,?,?)", [(k["id"], p, n) for p, n in parts])
    note_change("kit", "saved", label_name(k), ", ".join(f"{n} x {p}" for p, n in parts), by)
    kits_refresh([k["id"]], by)
    bump(items=True)
    return [x for x in list_kits() if x["item_id"] == k["id"]][0]


def remove_kit(item_id, by=""):
    k = q1("SELECT * FROM kits WHERE item_id=?", item_id)
    if not k:
        raise ApiError(404, "That isn't a kit")
    with DB_LOCK:
        conn.execute("DELETE FROM kit_parts WHERE kit_id=?", (item_id,))
        conn.execute("DELETE FROM kits WHERE item_id=?", (item_id,))
    note_change("kit", "removed", label_name(_item(item_id)), "its stock is its own again", by)
    bump(items=True)


def kits_refresh(kit_ids=None, by="Wojtek Stock (kits)"):
    """Bring kits' WooCommerce stock to what their parts can make (only where it differs, and not twice at once)."""
    if not setup_confirmed():
        return 0
    ids = kit_ids if kit_ids is not None else [r["item_id"] for r in q("SELECT item_id FROM kits")]
    specs = []
    for kid in ids:
        it = q1("SELECT * FROM items WHERE id=?", kid)
        av = kit_available(kid)
        if not it or it["scope"] != "shop" or av is None or av == it["stock"]:
            continue
        if q1("SELECT 1 FROM jobs WHERE kind='kitset' AND item_id=? AND status IN ('pending','working')", kid):
            continue
        specs.append({"item": it})
    if specs:
        queue_jobs("kitset", specs, by, "", "Kit: set to what its parts can make")
    return len(specs)


def kits_of_part(part_id):
    return [r["kit_id"] for r in q("SELECT kit_id FROM kit_parts WHERE part_id=?", part_id)]


def process_kit_sales(orders):
    """Kits on these orders: take their parts out once per order (or put them back if the order was cancelled)."""
    kits = {r["item_id"]: r["created_at"] for r in q("SELECT * FROM kits")}
    if not kits:
        return 0
    n = 0
    for o in orders:
        oid = int(o.get("id") or 0)
        sold = {}
        for li in o.get("line_items") or []:
            iid = int(li.get("variation_id") or 0) or int(li.get("product_id") or 0)
            if iid in kits:
                sold[iid] = sold.get(iid, 0) + float(li.get("quantity") or 0)
        made = o.get("date_created_gmt") or ""
        for kid, qty in sold.items():
            if made and made + "+00:00" < kits[kid] and not q1("SELECT 1 FROM kit_sales WHERE order_id=? AND kit_id=?", oid, kid):
                continue   # sold before it was a kit: its parts were never part of it
            ks = q1("SELECT * FROM kit_sales WHERE order_id=? AND kit_id=?", oid, kid) or {"taken": 0, "returned": 0}
            target = max(0.0, (qty if o.get("status") in KIT_SOLD else 0) - (ks["returned"] or 0))
            diff = target - (ks["taken"] or 0)
            if abs(diff) < 1e-9:
                continue
            kit = q1("SELECT * FROM items WHERE id=?", kid)
            specs = [{"item": _item(p["part_id"]), "delta": int(round(-diff * p["qty"]))} for p in kit_parts(kid) if p["scope"] == "shop"]
            if specs:
                queue_jobs("kit", specs, "Wojtek Stock (kits)", "",
                           (f"Kit sold: {label_name(kit)}" if diff > 0 else f"Kit order cancelled: {label_name(kit)}") + f" (order {o.get('number') or oid})")
            with DB_LOCK:
                conn.execute("INSERT INTO kit_sales(order_id,kit_id,taken,returned) VALUES(?,?,?,?) "
                             "ON CONFLICT(order_id,kit_id) DO UPDATE SET taken=excluded.taken", (oid, kid, target, ks["returned"] or 0))
            n += 1
    return n


# ---------------------------------------------------------------- reorder points: parts that need ordering, drafted automatically
def on_order_qty(item_id):
    """Units coming: still due on sent or partly received POs, plus what's on draft POs (so a part is never drafted twice)."""
    n = 0
    for ln in q("SELECT pl.*, p.status FROM po_lines pl JOIN pos p ON p.id=pl.po_id WHERE pl.item_id=? "
                "AND p.status IN ('draft','sent','partial')", item_id):
        if ln["status"] == "draft":
            n += ln["qty"]
        else:
            got, going = line_progress(ln["id"])
            n += max(0, ln["qty"] - got - going - ln["cancelled"])
    return n


def reorder_vendor(item_id, preferred):
    """The vendor to order a part from: the one set for it, else the one it was last bought from or priced by."""
    if preferred and q1("SELECT 1 FROM vendors WHERE id=? AND active=1", preferred):
        return preferred
    r = q1("SELECT vi.vendor_id FROM vendor_items vi JOIN vendors v ON v.id=vi.vendor_id WHERE vi.item_id=? AND v.active=1 "
           "ORDER BY vi.updated_at DESC LIMIT 1", item_id)
    return r["vendor_id"] if r else None


def reorder_status():
    """Every part with a reorder point: stock, what's coming, and whether it needs ordering (and how many)."""
    out = []
    names = {v["id"]: v["name"] for v in q("SELECT id, name FROM vendors WHERE active=1")}
    for r in q("SELECT ro.*, i.name, i.attrs, i.sku, i.stock, i.scope FROM reorder ro JOIN items i ON i.id=ro.item_id "
               "ORDER BY i.name COLLATE NOCASE, i.attrs"):
        coming = on_order_qty(r["item_id"])
        stock = r["stock"] or 0
        need = max(0, (r["max_qty"] or 0) - (stock + coming)) if stock + coming <= (r["min_qty"] or 0) else 0
        vid = reorder_vendor(r["item_id"], r["vendor_id"])
        out.append({"item_id": r["item_id"], "name": r["name"] + (f" ({r['attrs']})" if r["attrs"] else ""), "sku": r["sku"],
                    "stock": r["stock"], "min": r["min_qty"], "max": r["max_qty"], "coming": coming, "need": need,
                    "vendor_id": vid, "vendor": names.get(vid, ""), "setVendor": r["vendor_id"], "scope": r["scope"]})
    return out


def set_reorder(rows, by=""):
    """[{item_id, min, max, vendor_id}]: min blank (or max 0) clears the part's reorder point."""
    if not isinstance(rows, list) or not rows or len(rows) > 2000:
        raise ApiError(400, "Nothing to save")
    done = 0
    for x in rows:
        r = _po_item(x.get("item_id"))
        if str(x.get("min", "")).strip() == "" or str(x.get("max", "")).strip() in ("", "0"):
            with DB_LOCK:
                conn.execute("DELETE FROM reorder WHERE item_id=?", (r["id"],))
            done += 1
            continue
        lo = _int(x.get("min"), 0, 100000, "Reorder point")
        hi = _int(x.get("max"), 1, 100000, "Order up to")
        if hi <= lo:
            raise ApiError(400, f"{label_name(r)}: the order-up-to level should be more than the reorder point")
        vid = x.get("vendor_id") or None
        if vid and not q1("SELECT 1 FROM vendors WHERE id=? AND active=1", int(vid)):
            raise ApiError(400, "No such vendor")
        with DB_LOCK:
            conn.execute("INSERT OR REPLACE INTO reorder(item_id,min_qty,max_qty,vendor_id,updated_at,by) VALUES(?,?,?,?,?,?)",
                         (r["id"], lo, hi, int(vid) if vid else None, now_iso(), by))
        done += 1
    note_change("reorder", "saved", f"{done} part{'s' if done != 1 else ''}", by=by)
    bump()
    return {"saved": done}


REORDER_LOCK = threading.Lock()


def reorder_run(by="Wojtek Stock (reorder points)"):
    """Parts at or below their reorder point go on their vendor's automatic draft PO, up to their order-up-to level.
    One automatic draft per vendor collects them until it's sent. Never twice: what's on any open PO counts as coming."""
    with REORDER_LOCK:
        added, nov = {}, []
        for r in reorder_status():
            if not r["need"] or r["scope"] != "shop":
                continue
            if not r["vendor_id"]:
                nov.append(r["name"])
                continue
            vid = r["vendor_id"]
            po = q1("SELECT * FROM pos WHERE vendor_id=? AND status='draft' AND auto=1 ORDER BY id LIMIT 1", vid)
            if not po:
                n = int(meta_get("po_next", 1) or 1)
                meta_set("po_next", n + 1)
                with DB_LOCK:
                    pid = conn.execute("INSERT INTO pos(number,vendor_id,status,notes,created_at,created_by,updated_at,auto) "
                                       "VALUES(?,?,'draft','',?,?,?,1)", (f"PO-{n:04d}", vid, now_iso(), by, now_iso())).lastrowid
                po = q1("SELECT * FROM pos WHERE id=?", pid)
            vi = q1("SELECT * FROM vendor_items WHERE vendor_id=? AND item_id=?", vid, r["item_id"])
            it = _item(r["item_id"])
            with DB_LOCK:
                conn.execute("INSERT INTO po_lines(po_id,item_id,name,sku,vendor_sku,qty,cost) VALUES(?,?,?,?,?,?,?)",
                             (po["id"], it["id"], label_name(it), it["sku"], vi["vendor_sku"] if vi else "", r["need"],
                              (vi["last_cost"] if vi else None) or 0))
                conn.execute("UPDATE pos SET updated_at=? WHERE id=?", (now_iso(), po["id"]))
            added.setdefault(po["number"], 0)
            added[po["number"]] += 1
        if added:
            log.info("Reorder points: drafted %s", ", ".join(f"{k} (+{v})" for k, v in added.items()))
            bump()
        return {"drafted": added, "noVendor": nov}


def reorder_suggest(days=90):
    """Reorder levels from the last `days` of sales: enough to cover the vendor's lead time plus a week, then a month."""
    days = max(14, min(365, int(days)))
    after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    sold = {}
    for o in woo_pages("/orders", {"after": after, "dates_are_gmt": "true", "_fields": "id,status,line_items"}):
        if o.get("status") in ("cancelled", "failed", "refunded", "trash", "checkout-draft", "pending"):
            continue
        for li in o.get("line_items") or []:
            iid = int(li.get("variation_id") or 0) or int(li.get("product_id") or 0)
            sold[iid] = sold.get(iid, 0) + int(li.get("quantity") or 0)
    out = []
    for iid, n in sold.items():
        r = q1("SELECT * FROM items WHERE id=? AND scope='shop'", iid)
        if not r or n <= 0:
            continue
        vid = reorder_vendor(iid, (q1("SELECT vendor_id FROM reorder WHERE item_id=?", iid) or {"vendor_id": None})["vendor_id"])
        lead = (q1("SELECT lead_days FROM vendors WHERE id=?", vid) or {"lead_days": None})["lead_days"] if vid else None
        lead = lead if lead is not None else 7
        daily = n / days
        lo = max(1, -(-daily * (lead + 7) // 1))
        hi = max(lo + 1, lo + -(-daily * 30 // 1))
        cur = q1("SELECT * FROM reorder WHERE item_id=?", iid)
        out.append({"item_id": iid, "name": label_name(r), "sku": r["sku"], "stock": r["stock"], "sold": n, "days": days,
                    "min": int(lo), "max": int(hi), "lead": lead, "curMin": cur["min_qty"] if cur else None,
                    "curMax": cur["max_qty"] if cur else None})
    out.sort(key=lambda x: -x["sold"])
    return {"rows": out, "days": days}


# ---------------------------------------------------------------- purchase orders as PDFs, and by email
def po_pdf(po):
    """The PO as a PDF: letterhead, vendor, ship-to, lines, total, notes, and a QR code of its number for receiving."""
    v = po.get("vendorInfo") or {"name": po.get("vendor", "")}
    lines = [{"name": l["name"], "sku": l["sku"] or "", "vendor_sku": l["vendor_sku"] or "", "qty": l["qty"] - l["cancelled"],
              "cost": l["cost"] or 0} for l in po["lines"]]
    shop_addr = [x.strip() for x in CFG["shop_address"].split("|") if x.strip()]
    W, H, M = 215.9, 279.4, 16
    pages = []
    cols = [(M, "Their part #", None), (M + 32, "Our SKU", None), (M + 64, "Description", None), (W - M - 50, "Qty", "right"),
            (W - M - 22, "Unit", "right"), (W - M, "Total", "right")]
    money_s = lambda x: f"${x:,.2f}"
    st = {}

    def head():
        pg = PdfPage(W, H)
        pages.append(pg)
        y = M
        pg.text(M, y + 6, CFG["shop_name"], 16, "B")
        yy = y + 12
        for t in shop_addr + [" · ".join(x for x in (CFG["shop_phone"], CFG["shop_email"]) if x)]:
            if t:
                pg.text(M, yy, t, 9, "R")
                yy += 4.4
        pg.text(W - M - 34, y + 6, "PURCHASE ORDER", 13, "B", "right")
        pg.text(W - M - 34, y + 13, po["number"], 12, "B", "right")
        d = parse_iso(po.get("sent_at")) or datetime.now(timezone.utc)
        pg.text(W - M - 34, y + 19, d.astimezone(LOCAL_TZ).strftime("%-m/%-d/%Y"), 9, "R", "right")
        pg.qr(po["number"], W - M - 30, y - 1, 30)
        y = max(yy, y + 31) + 4
        pg.rect(M, y, W - 2 * M, 0.25)
        y += 6
        pg.text(M, y, "VENDOR", 8, "B")
        pg.text(W / 2 + 4, y, "SHIP TO", 8, "B")
        y += 5
        vl = [x for x in (v.get("name"), v.get("contact"), v.get("email"), v.get("phone"),
                          ("Account # " + v["account"]) if v.get("account") else "") if x]
        sl = [x for x in [CFG["shop_name"]] + shop_addr if x]
        for i in range(max(len(vl), len(sl))):
            if i < len(vl):
                pg.text(M, y, vl[i], 10, "R" if i else "B")
            if i < len(sl):
                pg.text(W / 2 + 4, y, sl[i], 10, "R" if i else "B")
            y += 4.8
        y += 4
        for x, t, al in cols:
            pg.text(x, y, t, 8, "B", al or "left")
        y += 2
        pg.rect(M, y, W - 2 * M, 0.25)
        st.update(pg=pg, y=y + 5)
    head()
    total = 0.0
    for l in lines:
        desc = wrap_mm(l["name"], "R", 9, 72)
        need = len(desc) * 4.2 + 2
        if st["y"] + need > H - 40:
            st["pg"].text(W - M, H - 10, "Continued on the next page", 8, "R", "right")
            head()
        pg, y = st["pg"], st["y"]
        pg.text(cols[0][0], y, fit_mm(l["vendor_sku"], "M", 8.5, 30), 8.5, "M")
        pg.text(cols[1][0], y, fit_mm(l["sku"], "M", 8.5, 30), 8.5, "M")
        for i, dl in enumerate(desc):
            pg.text(cols[2][0], y + i * 4.2, dl, 9, "R")
        pg.text(cols[3][0], y, str(l["qty"]), 9.5, "B", "right")
        pg.text(cols[4][0], y, money_s(l["cost"]) if l["cost"] else "-", 9, "R", "right")   # no price known: the vendor's
        pg.text(cols[5][0], y, money_s(l["qty"] * l["cost"]) if l["cost"] else "-", 9, "R", "right")
        total += l["qty"] * l["cost"]
        y += need
        pg.rect(M, y - 1.2, W - 2 * M, 0.25)
        st["y"] = y + 3.5
    pg, y = st["pg"], st["y"]
    pg.text(cols[4][0], y + 2, "Total", 10, "B", "right")
    pg.text(cols[5][0], y + 2, money_s(total), 11, "B", "right")
    if any(not l["cost"] for l in lines):
        y += 6
        pg.text(W - M, y + 2, "Lines without a price: please confirm your price.", 8.5, "R", "right")
    y += 12
    if po.get("notes"):
        pg.text(M, y, "Notes", 9, "B")
        y += 5
        for t in wrap_mm(po["notes"], "R", 9.5, W - 2 * M):
            pg.text(M, y, t, 9.5, "R")
            y += 4.6
    pg.text(M, H - 14, f"Please put {po['number']} on your invoice and packing list.", 9, "R")
    return build_pdf(pages)


def email_ready():
    return bool(CFG["smtp_host"] and CFG["smtp_user"] and CFG["smtp_token"] and CFG["smtp_from"])


def send_email(to, subject, text, attachments=(), cc=None):
    """Through your mail provider's SMTP (Proton Mail: an SMTP token). attachments: [(filename, bytes)] PDFs."""
    import smtplib
    import ssl
    from email.message import EmailMessage
    from email.utils import formataddr, make_msgid
    if CFG["safe_copy"]:
        raise ApiError(409, "This is a test copy restored from a backup: it doesn't send email")
    if not email_ready():
        raise ApiError(400, "Email isn't set up: fill in SMTP_HOST, SMTP_USER, SMTP_TOKEN and SMTP_FROM in stock.env")
    msg = EmailMessage()
    msg["From"] = formataddr((CFG["shop_name"], CFG["smtp_from"]))
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if CFG["shop_email"] and CFG["shop_email"].lower() != CFG["smtp_from"].lower():
        msg["Reply-To"] = CFG["shop_email"]
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=CFG["smtp_from"].split("@")[-1])
    msg.set_content(text)
    for name, data in attachments:
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    ctx = ssl.create_default_context()
    try:
        if CFG["smtp_port"] == 465:
            srv = smtplib.SMTP_SSL(CFG["smtp_host"], 465, context=ctx, timeout=30)
        else:
            srv = smtplib.SMTP(CFG["smtp_host"], CFG["smtp_port"], timeout=30)
            srv.ehlo()
            if srv.has_extn("starttls"):
                srv.starttls(context=ctx)
                srv.ehlo()
        with srv:
            srv.login(CFG["smtp_user"], CFG["smtp_token"])
            srv.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise ApiError(502, "The mail server refused the SMTP username or token (check SMTP_USER and SMTP_TOKEN)")
    except (smtplib.SMTPException, OSError) as e:
        raise ApiError(502, f"Couldn't send the email: {e}")


def po_email_text(po):
    lines = [f"{l['qty'] - l['cancelled']} x {l['name']}" + (f"  (your # {l['vendor_sku']})" if l["vendor_sku"] else "") +
             (f"  at ${l['cost']:,.2f}" if l["cost"] else "  (please confirm your price)") for l in po["lines"] if l["qty"] - l["cancelled"] > 0]
    v = po.get("vendorInfo") or {}
    return "\n".join([f"Hello{(' ' + v['contact'].split()[0]) if v.get('contact') else ''},", "",
                      f"Please find purchase order {po['number']} from {CFG['shop_name']} attached"
                      + (f" (account {v['account']})" if v.get("account") else "") + ".", ""] + lines +
                     [f"", f"Total: ${po['total']:,.2f}"] + ([f"", f"Notes: {po['notes']}"] if po.get("notes") else []) +
                     ["", f"Please put {po['number']} on your invoice and packing list, and reply to confirm.", "", "Thank you,",
                      CFG["shop_name"]] + [x for x in (CFG["shop_phone"], CFG["shop_email"]) if x])


def email_po(pid, by=""):
    """Email a PO to its vendor, with the PDF attached. A draft is marked sent once the email has gone."""
    p = q1("SELECT * FROM pos WHERE id=?", pid)
    if not p:
        raise ApiError(404, "No such purchase order")
    if p["status"] not in ("draft", "sent", "partial"):
        raise ApiError(409, f"{p['number']} is {p['status']}")
    v = q1("SELECT * FROM vendors WHERE id=?", p["vendor_id"])
    if not v or not v["email"]:
        raise ApiError(400, f"{v['name'] if v else 'The vendor'} has no email address: add it on the vendor first")
    if not q1("SELECT 1 FROM po_lines WHERE po_id=?", pid):
        raise ApiError(400, "There's nothing on it to order")
    po = get_po(pid)
    try:
        send_email(v["email"], f"Purchase order {po['number']} from {CFG['shop_name']}", po_email_text(po),
                   [(f"{po['number']}.pdf", po_pdf(po))], CFG["po_cc"] or None)
    except ApiError as e:
        with DB_LOCK:
            conn.execute("UPDATE pos SET email_error=?, updated_at=? WHERE id=?", (e.msg, now_iso(), pid))
        bump()
        raise
    now = now_iso()
    with DB_LOCK:
        conn.execute("UPDATE pos SET emailed_at=?, emailed_to=?, email_error='', updated_at=? WHERE id=?", (now, v["email"], now, pid))
        if p["status"] == "draft":
            conn.execute("UPDATE pos SET status='sent', sent_at=? WHERE id=?", (now, pid))
    note_change("po", "emailed", p["number"], f"to {v['email']}", by)
    log.info("Emailed %s to %s (%s)", p["number"], v["email"], by)
    bump()
    return get_po(pid)


def purchasing_loop():
    """Each day at AUTO_PO_TIME: check reorder points, then email the automatic drafts of vendors set to send by
    themselves. (Reorder points are also checked after every sync; only the sending waits for this time.)"""
    every = max(1, int(env("PURCHASING_CHECK_SECONDS", "60") or 60))   # tests check more often
    while True:
        time.sleep(every)
        try:
            now = datetime.now(LOCAL_TZ)
            if CFG["auto_po_time"] == "off":
                continue
            h, _, m = CFG["auto_po_time"].partition(":")
            if (now.hour, now.minute) < (int(h or 15), int(m or 0)) or meta_get("auto_po_day") == now.strftime("%Y-%m-%d"):
                continue
            meta_set("auto_po_day", now.strftime("%Y-%m-%d"))
            if not setup_confirmed():
                continue
            reorder_run()
            if not email_ready():
                continue
            for p in q("SELECT p.id, p.number FROM pos p JOIN vendors v ON v.id=p.vendor_id WHERE p.status='draft' AND p.auto=1 "
                       "AND v.auto_send=1 AND v.active=1 AND v.email!='' ORDER BY p.id"):
                try:
                    email_po(p["id"], "Wojtek Stock (sent automatically)")
                except ApiError as e:
                    log.warning("Couldn't email %s automatically: %s", p["number"], e.msg)
        except Exception as e:
            log.warning("Purchasing check: %s", e)


# ---------------------------------------------------------------- GunBroker: listings linked by SKU, quantities kept in step, sales brought in
# Your GunBroker fixed-price listings are matched to in-shop products by SKU (feed products and guns never). Their
# quantity follows WooCommerce stock (less GB_BUFFER held back). Each GunBroker sale becomes a WooCommerce order
# (GB_CREATE_ORDERS), and stock is lowered exactly once: if WooCommerce didn't lower it when the order was created,
# Wojtek Stock does. GB_SYNC: off, dryrun (work it all out and show it, change nothing) or on.
GB_LOCK = threading.Lock()
_gb_token = {"v": None}
_gb_last = [0.0]


def gb_ready():
    return bool(CFG["gb_devkey"] and CFG["gb_username"] and CFG["gb_password"]) and CFG["gb_sync"] in ("dryrun", "on")


def gb_base():
    return CFG["gb_api"] or ("https://api.sandbox.gunbroker.com/v1" if CFG["gb_sandbox"] else "https://api.gunbroker.com/v1")


def gk(d, *names, default=None):
    """A field from a GunBroker response, whatever its capitalisation (accessToken / AccessToken)."""
    if not isinstance(d, dict):
        return default
    low = {k.lower(): v for k, v in d.items()}
    for n in names:
        if n.lower() in low:
            return low[n.lower()]
    return default


class GbError(Exception):
    def __init__(self, msg, status=0):
        super().__init__(msg)
        self.status = status


def _gb_once(method, path, params=None, body=None, token=True):
    url = gb_base() + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"X-DevKey": CFG["gb_devkey"], "Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": f"WojtekStock/{VERSION}"}
    if token and _gb_token["v"]:
        headers["X-AccessToken"] = _gb_token["v"]
    data = json.dumps(body).encode() if body is not None else None
    wait = 0.5 - (time.time() - _gb_last[0])   # gently: GunBroker is a shared, live website
    if wait > 0:
        time.sleep(wait)
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raw = e.read()[:2000]
        try:
            j = json.loads(raw)
            msg = gk(j, "userMessage", "developerMessage", "message", default="") or ""
        except ValueError:
            msg = ""
        raise GbError(msg or f"GunBroker said {e.code}", e.code)
    except (urllib.error.URLError, OSError) as e:
        raise GbError(f"Can't reach GunBroker ({getattr(e, 'reason', e)})")
    finally:
        _gb_last[0] = time.time()
    try:
        return json.loads(raw or b"null")
    except ValueError:
        raise GbError("GunBroker answered with a web page instead of data")


def gb_login():
    r = _gb_once("POST", "/Users/AccessToken", body={"Username": CFG["gb_username"], "Password": CFG["gb_password"]}, token=False)
    t = gk(r, "accessToken")
    if not t:
        raise GbError("GunBroker didn't give an access token (check GB_USERNAME and GB_PASSWORD)")
    _gb_token["v"] = t
    return t


def gb(method, path, params=None, body=None):
    if not _gb_token["v"]:
        gb_login()
    try:
        return _gb_once(method, path, params, body)
    except GbError as e:
        if e.status == 401:   # the token ran out: log in again, once
            gb_login()
            return _gb_once(method, path, params, body)
        raise


def gb_list(path, params):
    """Every page of a GunBroker list (300 at a time)."""
    out, page = [], 1
    while page <= 50:
        r = gb("GET", path, dict(params, PageSize=300, PageIndex=page))
        rows = gk(r, "results", default=[]) or []
        out += rows
        if len(rows) < 300:
            break
        page += 1
    return out


def gb_status(**kw):
    st = meta_get("gb", {}) or {}
    st.update(kw)
    meta_set("gb", st)
    bump()


def gb_link(sku):
    """The in-shop product a GunBroker listing's SKU belongs to, or why there isn't one."""
    k = skey(sku)
    if not k:
        return None, "No SKU on the listing"
    rows = q("SELECT * FROM items WHERE skey=?", k)
    shop = [r for r in rows if r["scope"] == "shop"]
    if len(shop) == 1:
        return shop[0], ""
    if len(shop) > 1:
        return None, "More than one in-shop product has this SKU"
    if rows:
        return None, rows[0]["why"] or "Not an in-shop product"
    if feed_prefix_of(k):
        return None, "A distributor feed SKU: never synced"
    return None, "No in-shop product has this SKU"


def gb_refresh_listings():
    rows = gb_list("/ItemsSelling", {})
    at = now_iso()
    seen = set()
    with Tx() as c:
        for x in rows:
            gid = int(gk(x, "itemID") or 0)
            if not gid:
                continue
            seen.add(gid)
            sku = gk(x, "sku", default="") or ""
            item, why = gb_link(sku)
            fixed = 1 if gk(x, "isFixedPrice") else 0
            if item and not fixed:
                why = "An auction: quantity isn't synced"
            c.execute("""INSERT INTO gb_listings(gb_id,title,sku,qty,fixed,price,item_id,why,seen_at) VALUES(?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(gb_id) DO UPDATE SET title=excluded.title, sku=excluded.sku, qty=excluded.qty,
                         fixed=excluded.fixed, price=excluded.price, item_id=excluded.item_id, why=excluded.why, seen_at=excluded.seen_at""",
                      (gid, gk(x, "title", default="") or "", sku, int(gk(x, "quantity") or 0), fixed,
                       float(gk(x, "fixedPrice", "buyNowPrice", default=0) or 0), item["id"] if item else None, why, at))
        if seen:
            c.execute(f"DELETE FROM gb_listings WHERE gb_id NOT IN ({','.join('?' * len(seen))})", tuple(seen))
        else:
            c.execute("DELETE FROM gb_listings")
    return len(rows)


def gb_push_quantities():
    """Set each linked fixed-price listing to WooCommerce stock less the buffer, where it differs."""
    rows = q("SELECT * FROM gb_listings WHERE item_id IS NOT NULL AND fixed=1")
    if not rows:
        return 0
    live_many([r["item_id"] for r in rows])   # current stock, straight from WooCommerce
    n = 0
    for r in rows:
        it = q1("SELECT * FROM items WHERE id=?", r["item_id"])
        if not it or it["scope"] != "shop" or it["stock"] is None:
            continue
        want = max(0, it["stock"] - CFG["gb_buffer"])
        with DB_LOCK:
            conn.execute("UPDATE gb_listings SET want=? WHERE gb_id=?", (want, r["gb_id"]))
        if want == r["qty"]:
            if r["error"]:
                with DB_LOCK:
                    conn.execute("UPDATE gb_listings SET error='' WHERE gb_id=?", (r["gb_id"],))
            continue
        if CFG["gb_sync"] != "on":
            continue
        try:
            gb("PUT", f"/Items/{r['gb_id']}", body={"Quantity": want})
            with DB_LOCK:
                conn.execute("UPDATE gb_listings SET qty=?, pushed_qty=?, pushed_at=?, error='' WHERE gb_id=?",
                             (want, want, now_iso(), r["gb_id"]))
            n += 1
            log.info("GunBroker listing %s: quantity %s -> %s", r["gb_id"], r["qty"], want)
        except GbError as e:
            msg = (f"Sold out here, but GunBroker wouldn't set the listing to 0 ({e}): end it on GunBroker" if want == 0
                   else f"GunBroker refused the new quantity: {e}")
            with DB_LOCK:
                conn.execute("UPDATE gb_listings SET error=? WHERE gb_id=?", (msg, r["gb_id"]))
    bump()
    return n


def gb_import_orders(since):
    """GunBroker orders changed since `since` (UTC datetime): each new, not-cancelled one with linked items becomes a
    WooCommerce order (and lowers stock once). Never twice: every order is remembered."""
    params = {"OrdersModifiedSinceDate": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "TimeFrame": 8}
    done = 0
    for o in gb_list("/OrdersSold", params):
        oid = int(gk(o, "orderID") or 0)
        if not oid:
            continue
        known = q1("SELECT * FROM gb_orders WHERE gb_order_id=?", oid)
        cancelled = bool(gk(o, "orderCancelled"))
        if known:
            if cancelled and known["status"] == "imported":
                with DB_LOCK:
                    conn.execute("UPDATE gb_orders SET status='cancelled after import', detail=? WHERE gb_order_id=?",
                                 ("Cancelled on GunBroker after it was brought in: check the WooCommerce order and the stock", oid))
                bump()
            continue
        if cancelled:
            continue
        gb_import_one(o)
        done += 1
    return done


def _gb_record(oid, o, status, detail, items, woo_id=None, woo_number=""):
    buyer = gk(o, "shipToName", "billToName", default="") or (gk(gk(o, "buyer", default={}) or {}, "username", default="") or "")
    with DB_LOCK:
        conn.execute("INSERT OR REPLACE INTO gb_orders(gb_order_id,at,buyer,total,status,woo_order_id,woo_number,detail,items,"
                     "imported_at,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (oid, gk(o, "orderDateUTC", default="") or "", buyer, float(gk(o, "totalPrice", default=0) or 0), status,
                      woo_id, woo_number, detail, json.dumps(items), now_iso(), CFG["gb_sync"]))
    bump()


def gb_import_one(o):
    oid = int(gk(o, "orderID"))
    lines, skipped = [], []
    for li in gk(o, "orderItemsCollection", default=[]) or []:
        gid = int(gk(li, "itemID") or 0)
        lst = q1("SELECT item_id FROM gb_listings WHERE gb_id=?", gid)
        item = q1("SELECT * FROM items WHERE id=? AND scope='shop'", lst["item_id"]) if lst and lst["item_id"] else None
        item = item or gb_link(gk(li, "sku", default=""))[0]
        qty = int(gk(li, "quantity") or 1)
        if not item:
            skipped.append(f"{gk(li, 'title', default='')} ({gk(li, 'sku', default='') or 'no SKU'})")
            continue
        lines.append({"item": item["id"], "parent": item["parent_id"] or 0, "name": label_name(item), "sku": item["sku"],
                      "qty": qty, "price": float(gk(li, "itemPrice", default=0) or 0)})
    if not lines:
        return _gb_record(oid, o, "skipped", "Nothing on it is a linked in-shop product" +
                          (f": {', '.join(skipped)}" if skipped else ""), [])
    if CFG["gb_sync"] != "on" or CFG["woo_write"] != "on" or not setup_confirmed():
        why = "GB_SYNC isn't on" if CFG["gb_sync"] != "on" else "WooCommerce writes aren't on" if CFG["woo_write"] != "on" else "Setup isn't confirmed"
        return _gb_record(oid, o, "dry run", f"Would bring in {sum(l['qty'] for l in lines)} unit(s) ({why})", lines)
    before = {}
    for l in lines:
        row, _ = live_one(l["item"], l["parent"])
        before[l["item"]] = row["stock"]
    woo_id, woo_number = None, ""
    if CFG["gb_create_orders"]:
        name = (gk(o, "shipToName", default="") or "").split(" ", 1)
        addr = lambda pre: {"first_name": name[0] if name else "", "last_name": name[1] if len(name) > 1 else "",
                            "address_1": gk(o, pre + "Address1", default="") or "", "address_2": gk(o, pre + "Address2", default="") or "",
                            "city": gk(o, pre + "City", default="") or "", "state": gk(o, pre + "State", default="") or "",
                            "postcode": gk(o, pre + "PostalCode", default="") or "", "country": gk(o, pre + "CountryCode", default="") or "US",
                            "phone": gk(o, pre + "Phone", default="") or ""}
        body = {"status": "processing", "set_paid": bool(gk(o, "paymentReceived")), "payment_method": "gunbroker",
                "payment_method_title": "GunBroker", "billing": addr("billTo") if gk(o, "billToAddress1") else addr("shipTo"),
                "shipping": addr("shipTo"),   # no email address: WooCommerce sends the buyer nothing
                "customer_note": f"GunBroker order {oid}",
                "line_items": [dict({"product_id": l["parent"] or l["item"], "quantity": l["qty"],
                                     "subtotal": f"{l['price'] * l['qty']:.2f}", "total": f"{l['price'] * l['qty']:.2f}"},
                                    **({"variation_id": l["item"]} if l["parent"] else {})) for l in lines],
                "shipping_lines": [{"method_id": "flat_rate", "method_title": "GunBroker shipping",
                                    "total": f"{float(gk(o, 'shipCost', default=0) or 0):.2f}"}],
                "meta_data": [{"key": "_gunbroker_order_id", "value": str(oid)}]}
        try:
            created, _ = woo("POST", "/orders", None, body, tries=2)
            woo_id, woo_number = int(created["id"]), str(created.get("number") or created["id"])
        except WooError as e:
            return _gb_record(oid, o, "failed", f"Couldn't create the WooCommerce order: {e}", lines)
    lowered, by_us = [], []
    for l in lines:   # did WooCommerce lower the stock with the order? If not (or no order), Wojtek Stock does
        row, _ = live_one(l["item"], l["parent"])
        if woo_id and row["stock"] is not None and before[l["item"]] is not None and row["stock"] <= before[l["item"]] - l["qty"]:
            lowered.append(l)
        else:
            by_us.append(l)
    if by_us:
        queue_jobs("sale", [{"item": _item(l["item"]), "delta": -l["qty"]} for l in by_us],
                   "GunBroker", "", f"Sold on GunBroker (order {oid})", f"WooCommerce order {woo_number}" if woo_number else "")
    if woo_id:
        with DB_LOCK:
            conn.execute("INSERT OR REPLACE INTO order_touch(item_id,order_id,at) VALUES " +
                         ",".join("(?,?,?)" for _ in lines), sum(([l["item"], woo_id, now_iso()] for l in lines), []))
    detail = (f"WooCommerce order {woo_number}" if woo_id else "Stock lowered (no WooCommerce order: GB_CREATE_ORDERS is off)") + \
             (f"; stock lowered by WooCommerce" if lowered and not by_us else f"; stock lowered by Wojtek Stock" if by_us else "") + \
             (f"; not linked: {', '.join(skipped)}" if skipped else "")
    log.info("GunBroker order %s brought in: %s", oid, detail)
    return _gb_record(oid, o, "imported", detail, lines, woo_id, woo_number)


def gb_sync(reason="scheduled"):
    if not gb_ready():
        raise ApiError(400, "GunBroker isn't set up: GB_DEVKEY, GB_USERNAME, GB_PASSWORD and GB_SYNC in stock.env")
    if not GB_LOCK.acquire(blocking=False):
        raise ApiError(409, "A GunBroker sync is already running")
    try:
        started = datetime.now(timezone.utc)
        last = parse_iso(meta_get("gb_orders_since")) or started - timedelta(days=2)
        gb_status(running=True, error="")
        n_list = gb_refresh_listings()
        n_orders = gb_import_orders(last - timedelta(minutes=10)) if setup_confirmed() else 0
        n_push = gb_push_quantities() if setup_confirmed() else 0
        meta_set("gb_orders_since", started.isoformat(timespec="seconds"))
        gb_status(running=False, last_ok=now_iso(), listings=n_list, error="")
        return {"listings": n_list, "orders": n_orders, "pushed": n_push}
    except (GbError, WooError) as e:
        gb_status(running=False, error=str(e), last_try=now_iso())
        log.warning("GunBroker sync: %s", e)
        raise ApiError(502, f"GunBroker sync: {e}")
    finally:
        GB_LOCK.release()


def gunbroker_loop():
    time.sleep(10)
    while True:
        try:
            if gb_ready() and setup_confirmed():
                gb_sync()
        except ApiError:
            pass
        except Exception as e:
            log.warning("GunBroker loop: %s", e)
            gb_status(running=False, error=f"Unexpected: {e}")
        for _ in range(int(CFG["gb_minutes"] * 60)):
            if meta_get("gb_now"):
                meta_set("gb_now", False)
                break
            time.sleep(1)


def gb_info():
    st = meta_get("gb", {}) or {}
    return {"ready": gb_ready(), "mode": CFG["gb_sync"], "sandbox": CFG["gb_sandbox"], "buffer": CFG["gb_buffer"],
            "minutes": CFG["gb_minutes"], "createOrders": CFG["gb_create_orders"], "user": CFG["gb_username"],
            "status": st,
            "listings": [dict(r, item_name=(label_name(_item(r["item_id"])) if r["item_id"] and q1("SELECT 1 FROM items WHERE id=?", r["item_id"]) else ""),
                              stock=(q1("SELECT stock FROM items WHERE id=?", r["item_id"]) or {"stock": None})["stock"] if r["item_id"] else None)
                         for r in q("SELECT * FROM gb_listings ORDER BY (item_id IS NULL), title COLLATE NOCASE")],
            "orders": [dict(r, items=json.loads(r["items"] or "[]")) for r in q("SELECT * FROM gb_orders ORDER BY gb_order_id DESC LIMIT 100")]}


# ---------------------------------------------------------------- distributor feed listings: copy, or link to a part you have
def feed_search(text):
    words = [w for w in skey(text).split() if w]
    if not words:
        return []
    where = " AND ".join(["(UPPER(name) LIKE ? OR skey LIKE ? OR gkey LIKE ?)"] * len(words))
    args = []
    for w in words:
        args += [f"%{w}%", f"%{w}%", f"%{gkey(w) or w}%"]
    out = []
    for r in q(f"SELECT * FROM items WHERE scope='feed' AND type='simple' AND {where} ORDER BY name COLLATE NOCASE LIMIT 40", *args):
        d = item_out(r)
        link = q1("SELECT f.*, i.name iname, i.sku isku FROM feed_links f LEFT JOIN items i ON i.id=f.item_id WHERE f.feed_id=?", r["id"])
        d["linked"] = {"item": link["item_id"], "name": link["iname"], "sku": link["isku"], "how": link["how"]} if link else None
        v = vendor_for_feed(r["sku"])
        d["vendor"] = v["name"] if v else ""
        out.append(d)
    return out


def _feed_item(fid):
    r = _item(fid)
    if r["scope"] != "feed":
        raise ApiError(409, f"{label_name(r)} isn't a distributor feed listing")
    if r["type"] != "simple":
        raise ApiError(409, "Only simple feed listings can be copied (not variable products)")
    return r


def add_alt_barcode(code, item_id, source):
    g = gkey(code)
    if not g:
        return False
    with DB_LOCK:
        conn.execute("INSERT OR REPLACE INTO alt_barcodes(gkey,code,item_id,source,at) VALUES(?,?,?,?,?)",
                     (g, re.sub(r"[\s-]", "", code), item_id, source, now_iso()))
    bump(items=True)
    return True


def link_feed(b, by, operator):
    """This feed listing and a part you already have are the same thing: remembered for ordering, and the listing's
    UPC scans to your part at the station."""
    f = _feed_item(_int(b.get("feed_id"), 1, 10 ** 12, "Feed listing"))
    r = _po_item(b.get("item_id"))
    with DB_LOCK:
        conn.execute("INSERT OR REPLACE INTO feed_links(feed_id,item_id,how,at,by) VALUES(?,?,?,?,?)",
                     (f["id"], r["id"], "linked", now_iso(), by))
    v = vendor_for_feed(f["sku"])
    if v:
        remember_vendor_item(v["id"], r["id"], f["sku"])
    alt = bool(f["gtin"]) and gkey(f["gtin"]) != gkey(r["gtin"]) and add_alt_barcode(f["gtin"], r["id"], f"feed {f['sku']}")
    note_change("feed", "linked", f"{f['sku']} -> {r['sku'] or r['id']}", label_name(r), by)
    bump(items=True)
    return {"ok": True, "vendor": v["name"] if v else "", "altBarcode": f["gtin"] if alt else ""}


def copy_feed(b, by, operator):
    """A new in-shop WooCommerce product, copied from a feed listing: name, descriptions, images (the same media, not
    copies), categories, price and weight. Its own SKU (generated, or one you give), stock tracked from 0. The listing
    itself is never changed. WooCommerce allows a UPC on one product only, so the listing's UPC stays on the listing and
    Wojtek Stock keeps it as an extra barcode of the new product: the station finds it, the register may not."""
    check_can_write()
    f = _feed_item(_int(b.get("feed_id"), 1, 10 ** 12, "Feed listing"))
    if q1("SELECT 1 FROM feed_links WHERE feed_id=?", f["id"]):
        raise ApiError(409, "This listing is already copied or linked")
    if set(json.loads(f["cats"] or "[]")) & gun_categories():
        raise ApiError(409, "That's in a gun category: purchasing is for parts only")
    mode = b.get("sku_mode")
    if mode == "custom":
        sku = skey(b.get("sku"))
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9._/\-]{1,39}", sku):
            raise ApiError(400, "A SKU is letters, digits and - _ . / (2 to 40)")
        if q1("SELECT 1 FROM items WHERE skey=?", sku):
            raise ApiError(409, f"SKU {sku} is already used")
    elif mode == "generate":
        sku = ""
    else:
        raise ApiError(400, "Pick a SKU: generated or your own")
    if feed_prefix_of(sku or CFG["sku_prefix"] + "0"):
        raise ApiError(409, "That SKU starts like a distributor feed: it would be treated as a feed product")
    status = b.get("status") if b.get("status") in ("draft", "publish", "private") else "draft"
    name = _text(b.get("name"), 200) or f["name"]
    live, _ = woo("GET", f"/products/{f['id']}")
    if feed_prefix_of(live.get("sku")) == "":
        raise ApiError(409, "That listing's SKU no longer starts with a feed prefix: it isn't a feed listing now")
    if mode == "generate":
        sku = next_skus(1)[0]
    body = {"name": name, "type": "simple", "status": status, "sku": sku, "manage_stock": True, "stock_quantity": 0,
            "regular_price": str(b.get("price") or live.get("regular_price") or ""),
            "description": live.get("description") or "", "short_description": live.get("short_description") or "",
            "categories": [{"id": c["id"]} for c in (live.get("categories") or []) if isinstance(c, dict) and c.get("id")],
            "images": [{"id": i["id"]} for i in (live.get("images") or []) if isinstance(i, dict) and i.get("id")],
            "weight": live.get("weight") or "", "dimensions": live.get("dimensions") or {}}
    if CFG["woo_write"] != "on":
        return {"dryRun": True, "would": {k: body[k] for k in ("name", "sku", "status", "regular_price")},
                "message": "Dry run: nothing was created in WooCommerce"}
    created, _ = woo("POST", "/products", None, body, tries=2)
    nid = int(created["id"])
    refresh_products([nid], now_iso())
    rescope()
    with DB_LOCK:
        conn.execute("INSERT OR REPLACE INTO feed_links(feed_id,item_id,how,at,by) VALUES(?,?,?,?,?)", (f["id"], nid, "copied", now_iso(), by))
        conn.execute("""INSERT INTO jobs(batch,at,kind,item_id,parent_id,name,sku,value,reason,note,by,operator,status,mode,done_at,
                        before,after) VALUES(?,?,'create',?,0,?,?,?,?,?,?,?,'done',?,?,'',?)""",
                     (uuid.uuid4().hex[:10], now_iso(), nid, name, sku, sku, f"Copied from feed listing {f['sku']}", "", by, operator,
                      "on", now_iso(), f"new product #{nid}"))
    v = vendor_for_feed(f["sku"])
    if v:
        remember_vendor_item(v["id"], nid, f["sku"])
    alt = bool(f["gtin"]) and add_alt_barcode(f["gtin"], nid, f"feed {f['sku']}")
    note_change("feed", "copied", f"{f['sku']} -> {sku}", name, by)
    bump(items=True)
    return {"item": item_out(_item(nid)), "vendor": v["name"] if v else "", "altBarcode": f["gtin"] if alt else ""}


# ---------------------------------------------------------------- what the station sees
def item_out(r, history=False):
    d = {"id": r["id"], "parent": r["parent_id"] or 0, "name": r["name"], "attrs": r["attrs"] or "", "sku": r["sku"] or "",
         "gtin": r["gtin"] or "", "stock": r["stock"], "scope": r["scope"], "why": r["why"] or "",
         "price": r["price"] or "", "status": r["status"] or "", "seen": r["seen_at"]}
    if history:
        d["history"] = [dict(x) for x in q("SELECT id, at, kind, delta, target, value, qty, restock, order_number, reason, note, "
                                         "by, status, before, after, error FROM jobs WHERE item_id=? AND kind!='note' "
                                         "ORDER BY id DESC LIMIT 15", r["id"])]
        d["labels"] = q1("SELECT COUNT(*) n, MAX(at) last FROM labels WHERE item_id=?", r["id"])["n"]
        d["costs"] = item_costs(r["id"])
        d["bins"] = item_bin_codes().get(r["id"], [])
        d["kit"] = {"parts": kit_parts(r["id"]), "available": kit_available(r["id"])} if is_kit(r["id"]) else None
        d["inKits"] = [dict(x) for x in q("SELECT k.item_id, i.name, i.attrs, kp.qty FROM kit_parts kp JOIN kits k ON k.item_id=kp.kit_id "
                                         "JOIN items i ON i.id=k.item_id WHERE kp.part_id=?", r["id"])]
        d["counts"] = [dict(x) for x in q("SELECT at, by, counted, expected FROM item_counts WHERE item_id=? ORDER BY id DESC LIMIT 5", r["id"])]
        ro = q1("SELECT * FROM reorder WHERE item_id=?", r["id"])
        if ro:
            vid = reorder_vendor(r["id"], ro["vendor_id"])
            d["reorder"] = {"min": ro["min_qty"], "max": ro["max_qty"], "vendor": (q1("SELECT name FROM vendors WHERE id=?", vid) or {"name": ""})["name"] if vid else ""}
        else:
            d["reorder"] = None
        d["altBarcodes"] = [x["code"] for x in q("SELECT code FROM alt_barcodes WHERE item_id=?", r["id"])]
    return d


def station_items():
    """Everything the station can scan: in-shop items (and read-only ones, for Lookup), with their category names
    (a variation's are its parent's) and price, for the Products tab."""
    lab = {r["item_id"]: r["n"] for r in q("SELECT item_id, SUM(copies) n FROM labels GROUP BY item_id")}
    names = cat_names()
    rows = q("SELECT * FROM items WHERE scope IN ('shop','readonly') ORDER BY name COLLATE NOCASE, attrs")
    pcats = {r["id"]: r["cats"] for r in q("SELECT id, cats FROM items WHERE type='variable'")}
    alts = {}
    for a in q("SELECT code, item_id FROM alt_barcodes"):
        alts.setdefault(a["item_id"], []).append(a["code"])
    binmap, lc = item_bin_codes(), last_counts()
    kitset = {r["item_id"] for r in q("SELECT item_id FROM kits")}
    out = []
    for r in rows:
        cats = json.loads((pcats.get(r["parent_id"]) if r["parent_id"] else r["cats"]) or "[]")
        out.append([r["id"], r["parent_id"] or 0, r["name"], r["attrs"] or "", r["sku"] or "", r["gtin"] or "", r["stock"],
                    r["scope"], r["why"] or "", lab.get(r["id"], 0), [names[c] for c in cats if c in names], r["price"] or "",
                    alts.get(r["id"], []), binmap.get(r["id"], []), lc.get(r["id"]), r["id"] in kitset])
    return out


def resolve_code(code):
    """Every item (any scope) with this SKU or barcode, so the station can say why a code can't be used."""
    code = str(code or "").strip()
    rows = []
    g = gkey(code)
    if g:
        rows += q("SELECT i.* FROM alt_barcodes a JOIN items i ON i.id=a.item_id WHERE a.gkey=?", g)
        rows += q("SELECT * FROM items WHERE gkey=?", g)
    rows += q("SELECT * FROM items WHERE skey=?", skey(code))
    seen, out = set(), []
    for r in rows:
        if r["id"] not in seen:
            seen.add(r["id"])
            out.append(item_out(r))
    return out


def setup_info():
    prefixes = feed_prefixes()
    groups = {}
    for r in q("SELECT skey FROM items WHERE type!='variation' AND skey!=''"):
        g = prefix_group(r["skey"])
        e = groups.setdefault(g, {"prefix": g, "count": 0, "samples": []})
        e["count"] += 1
        if len(e["samples"]) < 3:
            e["samples"].append(r["skey"])
    gl = sorted(groups.values(), key=lambda e: (-e["count"], e["prefix"]))
    for e in gl:
        e["feed"] = e["prefix"] in prefixes
        e["covered"] = any(e["prefix"].startswith(p) for p in prefixes)
    env_cats = _env_gun_cat_ids()
    ui_cats = {int(x) for x in (meta_get("gun_categories_ui", []) or []) if str(x).isdigit()}
    counts = {}
    for r in q("SELECT cats FROM items WHERE type!='variation'"):
        for c in json.loads(r["cats"] or "[]"):
            counts[c] = counts.get(c, 0) + 1
    cats = [{"id": r["id"], "name": r["name"], "slug": r["slug"], "count": counts.get(r["id"], 0),
             "gun": r["id"] in ui_cats or r["id"] in env_cats, "env": r["id"] in env_cats}
            for r in q("SELECT * FROM cats ORDER BY name COLLATE NOCASE")]
    scopes = {r["scope"] or "unsorted": r["n"] for r in q("SELECT scope, COUNT(*) n FROM items GROUP BY scope")}
    shop = q1("SELECT COUNT(*) n, SUM(CASE WHEN gtin!='' THEN 1 ELSE 0 END) g, SUM(CASE WHEN sku='' THEN 1 ELSE 0 END) ns "
              "FROM items WHERE scope IN ('shop','readonly')")
    return {"confirmed": setup_confirmed(), "confirmedAt": meta_get("setup_confirmed_at"),
            "prefixes": prefixes, "envPrefixes": CFG["feed_prefixes"], "uiPrefixes": meta_get("feed_prefixes_ui", []) or [],
            "groups": gl[:400], "groupCount": len(gl), "cats": cats, "scopes": scopes,
            "shop": {"items": shop["n"] or 0, "withGtin": shop["g"] or 0, "noSku": shop["ns"] or 0},
            "drift": [dict(r) for r in q("SELECT * FROM drift ORDER BY id DESC LIMIT 100")],
            "watch": [dict(r) for r in q("SELECT * FROM restock_watch ORDER BY id DESC LIMIT 100")],
            "varsLoaded": q1("SELECT COUNT(*) n FROM items WHERE type='variation'")["n"],
            "http": CFG["woo_url"].startswith("http://")}


def save_setup(body, by=""):
    raw = body.get("prefixes")
    if not isinstance(raw, list):
        raise ApiError(400, "prefixes should be a list")
    prefixes = []
    for p in raw:
        p = skey(p)
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_.\-]{0,11}", p):
            raise ApiError(400, f"{p or 'An empty prefix'} isn't a valid prefix (letters, digits, - _ . up to 12)")
        prefixes.append(p)
    cats = body.get("guns")
    if not isinstance(cats, list) or not all(str(c).isdigit() for c in cats):
        raise ApiError(400, "guns should be a list of category ids")
    old_p, old_c = meta_get("feed_prefixes_ui", []) or [], meta_get("gun_categories_ui", []) or []
    meta_set("feed_prefixes_ui", sorted(set(prefixes)))
    meta_set("gun_categories_ui", sorted({int(c) for c in cats}))
    was = setup_confirmed()
    if body.get("confirm"):
        if not feed_prefixes():
            raise ApiError(400, "Tick at least one feed prefix before confirming")
        meta_set("setup_confirmed", True)
        meta_set("setup_confirmed_at", now_iso())
    note_change("setup", "saved", "Feed prefixes and gun categories",
                f"prefixes {','.join(old_p) or '-'} -> {','.join(sorted(set(prefixes))) or '-'}; "
                f"gun categories {old_c} -> {sorted({int(c) for c in cats})}" + ("; confirmed" if body.get("confirm") else ""), by)
    n = rescope()
    if setup_confirmed() and not was:
        _sync_now["full"] = True   # now the variations of in-shop products can be read
    bump(items=True)
    return {"rescoped": n, "info": setup_info()}


# ---------------------------------------------------------------- labels on a network Zebra printer (ZPL)
def _zpl_text(t):
    return re.sub(r"[\^~]", "-", re.sub(r"\s+", " ", str(t or ""))).strip()


def printer_addr():
    a = CFG["label_printer"].strip()
    if not a:
        return None
    if re.search(r":\d+$", a):
        host, _, port = a.rpartition(":")
        return host, int(port)
    return a, 9100


def printer_status(timeout=3.0):
    """Ask the Zebra how it is (~HS, its host status): reachable, paused, out of labels, head open, ribbon out.
    A printer that's reachable but doesn't answer the question is treated as fine (status unknown)."""
    addr = printer_addr()
    if not addr:
        return {"state": "none", "msg": "No label printer is set up (LABEL_PRINTER in stock.env)"}
    out = {"state": "ok", "msg": "Ready", "addr": f"{addr[0]}:{addr[1]}", "at": now_iso()}
    try:
        with socket.create_connection(addr, timeout=timeout) as sk:
            sk.sendall(b"~HS")
            sk.settimeout(timeout)
            data = b""
            try:
                while data.count(b"\x03") < 3:
                    chunk = sk.recv(1024)
                    if not chunk:
                        break
                    data += chunk
            except socket.timeout:
                pass
    except OSError as e:
        return dict(out, state="unreachable", msg=f"Can't reach the label printer at {addr[0]}:{addr[1]} ({getattr(e, 'strerror', None) or e}). "
                                                  "Check that it's on, and that this server can reach its network (a firewall rule between "
                                                  "networks, or the printer's gateway setting).")
    parts = [p.decode("ascii", "replace").strip() for p in re.findall(rb"\x02([^\x03]*)\x03", data)]
    if len(parts) < 2:
        return dict(out, state="ok", msg="Reachable (it didn't report its status)", unknown=True)
    a, b_ = parts[0].split(","), parts[1].split(",")
    flag = lambda lst, i: len(lst) > i and lst[i].strip() == "1"
    problems = []
    if flag(b_, 2):
        problems.append(("head", "Its print head is open: close it"))
    if flag(a, 1):
        problems.append(("paper", "It's out of labels (or the labels aren't seated): load labels"))
    if flag(b_, 3) and flag(b_, 4):
        problems.append(("ribbon", "It's out of ribbon"))
    if flag(a, 2):
        problems.append(("paused", "It's paused: press its Pause button"))
    if problems:
        return dict(out, state=problems[0][0], msg="The Zebra isn't ready. " + "; ".join(m for _, m in problems) + ".")
    return out


def send_to_printer(zpl):
    addr = printer_addr()
    if not addr:
        raise ApiError(400, "No label printer is set up: put its network address in stock.env (LABEL_PRINTER)")
    st = printer_status()
    meta_set("printer_state", {"state": st["state"], "msg": st["msg"], "since": now_iso() if st["state"] != ((meta_get("printer_state", {}) or {}).get("state")) else (meta_get("printer_state", {}) or {}).get("since", now_iso())})
    if st["state"] == "unreachable":
        raise ApiError(502, st["msg"])
    if st["state"] not in ("ok", "none"):
        raise ApiError(409, st["msg"] + " Then print again.")
    try:
        with socket.create_connection(addr, timeout=6) as s:
            s.sendall(zpl.encode("utf-8"))
    except OSError as e:
        raise ApiError(502, f"Couldn't reach the label printer at {addr[0]}:{addr[1]} ({e}). Check that it's on, and that "
                            "this server can reach its network (the Zebra is on a different network from the server).")


def zpl_svg(zpl):
    """A picture of a label drawn from the very ZPL the printer gets (an idea from the Locator), turned as it's read:
    text where the printer puts it, sized by the Zebra font's widths, the QR code module by module, boxes. It can't
    drift from what prints, and works with no printer connected."""
    from xml.sax.saxutils import escape as xesc
    W = int(re.search(r"\^PW(\d+)", zpl).group(1))
    H = int(re.search(r"\^LL(\d+)", zpl).group(1))
    out = []
    for f in re.findall(r"\^FO[^\^]*(?:\^(?!FS)[^\^]*)*\^FS", zpl):
        x, y = map(int, re.search(r"\^FO(\d+),(\d+)", f).groups())
        gb = re.search(r"\^GB(\d+),(\d+),(\d+)", f)
        if gb:
            bw, bh, t = map(int, gb.groups())
            out.append(f'<rect x="{x}" y="{y}" width="{bw}" height="{bh}" fill="none" stroke="#000" stroke-width="{t}"/>')
            continue
        dm = re.search(r"\^FD(.*?)\^FS", f)
        if not dm:
            continue
        data = dm.group(1)
        qm = re.search(r"\^BQN,2,(\d+)", f)
        if qm:
            mag = int(qm.group(1))
            g = qr_encode(data[3:] if data.startswith("MA,") else data)
            out.append("".join(f'<rect x="{x + c * mag}" y="{y + r * mag}" width="{mag}" height="{mag}"/>'
                               for r, row in enumerate(g) for c, v in enumerate(row) if v))
            continue
        a = re.search(r"\^A0([NR]),(\d+),(\d+)", f)
        fb = re.search(r"\^FB(\d+),(\d+),(\d+),([LCR])", f)
        if not (a and fb):
            continue
        h, wd = int(a.group(2)), int(a.group(3))
        bw, sp, al = int(fb.group(1)), int(fb.group(3)), fb.group(4)
        lines = data.split(ZPL_NEWLINE)
        anchor, tx = {"L": ("start", 0), "C": ("middle", bw / 2), "R": ("end", bw)}[al]
        spans = "".join(
            f'<text x="{tx:.0f}" y="{i * (h + sp) + h * 0.8:.0f}" font-size="{h * 0.98:.0f}" text-anchor="{anchor}" font-weight="700" '
            f'textLength="{min(bw, _zw(t, h, wd / h)):.0f}" lengthAdjust="spacingAndGlyphs">{xesc(t)}</text>'
            for i, t in enumerate(lines) if t)
        box = len(lines) * h + (len(lines) - 1) * sp
        rot = f"translate({x + box},{y}) rotate(90)" if a.group(1) == "R" else f"translate({x},{y})"
        out.append(f'<g transform="{rot}">{spans}</g>')
    sideways = zpl.count("^A0R") > zpl.count("^A0N")
    body = (f'<g transform="translate(0,{W}) rotate(-90)">' if sideways else "<g>") + "".join(out) + "</g>"
    vw, vh = (H, W) if sideways else (W, H)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="-6 -6 {vw + 12} {vh + 12}" width="{vw + 12}" height="{vh + 12}" '
            f'shape-rendering="crispEdges" '
            f'font-family="\'Arial Narrow\', \'Helvetica Neue\', Helvetica, Arial, sans-serif"><rect x="-6" y="-6" width="{vw + 12}" '
            f'height="{vh + 12}" rx="14" fill="#fff" stroke="#999"/>{body}</svg>').encode()


def zebra_preview(qs):
    """The Zebra's version of a product's label (or a sample), at a size: the SVG drawn from its ZPL."""
    def num(k, d):
        try:
            return float((qs.get(k) or [d])[0])
        except (TypeError, ValueError):
            raise ApiError(400, "The label size should be in inches")
    w_in, l_in, sideways = num("w", CFG["label_w_in"]), num("h", CFG["label_l_in"]), (qs.get("sideways") or ["0"])[0] == "1"
    head = l_in if sideways else w_in
    if not (0.5 <= min(w_in, l_in) and max(w_in, l_in) <= 8 and 0.75 <= head <= 4.1):
        raise ApiError(400, "That label size doesn't fit a label printer")
    iid = qint(qs, "id", 0)
    r = _item(iid) if iid else {"sku": "HORN-9MM-115", "name": "Hornady Critical Defense 9mm Luger 115 gr FTX", "attrs": "",
                                 "gtin": "090255000001"}
    if not r["sku"]:
        raise ApiError(400, "That product has no SKU")
    item = {"sku": r["sku"], "name": r["name"], "attrs": r["attrs"] or "", "gtin": r["gtin"] or "",
            "bin": (item_bin_codes().get(iid) or ["A-03"] if not iid else item_bin_codes().get(iid) or [""])[0]}
    return zpl_svg(zpl_label(item, w_in, l_in, (qs.get("name") or ["1"])[0] != "0", (qs.get("upc") or ["0"])[0] == "1", sideways,
                             (qs.get("bin") or ["0"])[0] == "1"))


def print_test_label(body, by=""):
    """A test label at the chosen size: the label's edges, a QR code, the size and the time. Sent means the printer
    took it; if nothing comes out, the printer is paused, out of labels, or its head is open."""
    size = body.get("size") or {}
    try:
        w_in, l_in = float(size.get("w") or CFG["label_w_in"]), float(size.get("h") or CFG["label_l_in"])
    except (TypeError, ValueError, AttributeError):
        raise ApiError(400, "The label size should be in inches")
    sideways = bool(size.get("sideways"))
    head = l_in if sideways else w_in
    if not (0.5 <= min(w_in, l_in) and max(w_in, l_in) <= 8 and 0.75 <= head <= 4.1):
        raise ApiError(400, "That label size doesn't fit a label printer")
    r = {"sku": "TEST-LABEL", "name": f"Wojtek Stock test label: {w_in:g} x {l_in:g} in"
                                      f"{' (printed sideways)' if sideways else ''}, {datetime.now(LOCAL_TZ):%-m/%-d %-I:%M %p}",
         "attrs": "", "gtin": ""}
    zpl = zpl_label(r, w_in, l_in, True, False, sideways)
    dpi = CFG["label_dpi"]
    W, H = (int(round(l_in * dpi)), int(round(w_in * dpi))) if sideways else (int(round(w_in * dpi)), int(round(l_in * dpi)))
    zpl = zpl.replace("^XZ", f"^FO2,2^GB{W - 4},{H - 4},3^FS^XZ")   # the label's edges: is it lined up?
    send_to_printer(zpl)
    note_change("label", "test", f"{w_in:g} x {l_in:g} in", by=by)
    return {"ok": True, "printer": ":".join(map(str, printer_addr()))}


ZPL_NEWLINE = "\\&"   # a line break inside a ^FB text block (backslash, ampersand)


def _sku_split(code):
    """A long SKU on two lines, broken at the hyphen (or other separator) nearest its middle: HORN-9MM / -115."""
    cuts = [i + 1 for i, ch in enumerate(code[:-1]) if ch in "-_./ "]
    if not cuts:
        return [code[:len(code) // 2], code[len(code) // 2:]]
    i = min(cuts, key=lambda c: abs(c - len(code) / 2))
    return [code[:i], code[i:]]


# Helvetica-Bold widths (per 1000 em, characters 32-126): Zebra font 0 is a condensed bold sans, a little narrower, so
# measuring with these (and a margin) keeps planned line breaks from overflowing on the printer
_HB_W = [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556, 556,
         556, 556, 556, 333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833,
         722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556, 333, 556, 611, 556, 611,
         556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611, 611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389,
         280, 389, 584]


def _zw(text, h, wf=1.0):
    """Estimated width in dots of text in Zebra font 0, h dots tall, at width factor wf."""
    return sum(_HB_W[ord(c) - 32] if 32 <= ord(c) <= 126 else 600 for c in str(text)) / 1000 * h * wf * 0.92


def _zwrap(text, h, wf, width):
    """Every word on as many lines as it takes (a word wider than the line is broken)."""
    lines, cur = [], ""
    for w in str(text or "").split():
        while _zw(w, h, wf) > width and len(w) > 1:
            k = len(w)
            while k > 1 and _zw(w[:k], h, wf) > width:
                k -= 1
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(w[:k])
            w = w[k:]
        t = f"{cur} {w}" if cur else w
        if _zw(t, h, wf) <= width:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def zpl_label(r, w_in=None, l_in=None, show_name=True, show_upc=False, sideways=False, show_bin=False):
    """A product label: a QR code of the SKU, the SKU in large letters, then the whole name, the variation and, if
    asked, the UPC, sized to fit (the name shrinks and wraps before the SKU does; it's only cut, ending in ..., if
    it can't fit even small). w_in x l_in is the label as it reads. Upright labels have the QR on top; wide ones have
    it on the left. sideways: a wide label (3 x 2 in) on stock that feeds narrow side first (2 in across the print
    head, like the Locator's 2 x 3 tags), so everything is turned 90 degrees."""
    dpi = CFG["label_dpi"]
    RW = int(round((w_in or CFG["label_w_in"]) * dpi))   # as it reads: across
    RH = int(round((l_in or CFG["label_l_in"]) * dpi))   # as it reads: down
    m = int(dpi * 0.08)
    code = r["sku"]
    n = 21 if len(code) <= 14 else 25 if len(code) <= 26 else 29
    pt = lambda p: p / 72 * dpi   # points to dots

    def sku_fit(width, room, most):
        """(lines, letter height) for the SKU: one line, or two when that makes the letters bigger."""
        fit = lambda t, k: int(min(most, width / max(1, _zw(t, 1)), room / (k * 1.12)))
        one = fit(code, 1)
        parts = _sku_split(code) if len(code) > 8 else None
        two = min(fit(parts[0], 2), fit(parts[1], 2)) if parts else 0
        return ([code], one) if one >= two * 0.8 else (parts, two)

    def small_block(h, width):
        """[(lines, letter height)] for the name, the variation and the UPC at size h, and their height."""
        blk = []
        if show_name:
            blk.append((_zwrap(r["name"], h, 0.85, width), h))
            if r["attrs"]:
                blk.append((_zwrap(r["attrs"], int(h * 0.92), 0.85, width), int(h * 0.92)))
        if show_upc and r["gtin"]:
            blk.append(([f"UPC {r['gtin']}"], int(h * 0.92)))
        if show_bin and r.get("bin"):
            blk.append(([f"Bin {r['bin']}"], int(h * 0.92)))
        blk = [(ls, hh) for ls, hh in blk if ls]
        return blk, sum(len(ls) * int(hh * 1.18) for ls, hh in blk)

    def fit_text(width, avail, pref, low, sku_max, sku_min):
        sku_min = min(sku_min, sku_fit(width, 10 ** 6, sku_max)[1])
        h = pref
        while True:
            blk, bh = small_block(int(h), width)
            gap = int(dpi * 0.06) if blk else 0
            lines, sh = sku_fit(width, max(1, avail - bh - gap), sku_max)
            if sh >= sku_min - 1 or h <= low:
                break
            h -= max(1, pref * 0.03)
        if sh < sku_min - 1:   # can't fit even small: keep the SKU at its floor and cut the name's last lines
            lines, sh = sku_fit(width, 10 ** 6, sku_min)
            while bh + gap + int(len(lines) * sh * 1.12) > avail and blk and sum(len(ls) for ls, _ in blk) > 1:
                ls, hh = blk[-1]
                if len(ls) > 1:
                    ls = ls[:-1]
                    last = ls[-1]
                    while len(last) > 1 and _zw(last + "...", hh, 0.85) > width:
                        last = last[:-1]
                    ls[-1] = last.rstrip() + "..."
                    blk[-1] = (ls, hh)
                else:
                    blk.pop()
                bh = sum(len(x) * int(y * 1.18) for x, y in blk)
        return lines, max(int(sh), int(dpi * 0.08)), blk, bh, gap

    ops = []   # ("qr", X, Y, mag, size) and ("text", X, Y, height, width factor, box width, lines, align), as read
    def place_text(x0, y, width, lines, sh, blk, gap, align):
        ops.append(("text", x0, y, sh, 1.0, width, lines, align))
        y += int(len(lines) * sh * 1.12) + gap
        for ls, hh in blk:
            ops.append(("text", x0, y, hh, 0.85, width, ls, align))
            y += len(ls) * int(hh * 1.18)

    if RH > RW * 1.2:   # upright: QR on top
        side = min(RW - 2 * m, int(RH * 0.52))
        mag = max(2, min(10, side // n))
        qs = mag * n
        ops.append(("qr", (RW - qs) // 2, m, mag, qs))
        top = m + qs + int(dpi * 0.1)
        avail = RH - m - top
        lines, sh, blk, bh, gap = fit_text(RW - 2 * m, avail, pt(9), pt(5.5), pt(46), pt(24 if RH > dpi * 2.4 else 12))
        total = int(len(lines) * sh * 1.12) + gap + bh
        place_text(m, top + max(0, (avail - total) // 2), RW - 2 * m, lines, sh, blk, gap, "C")
    else:               # wide: QR on the left, the text beside it (centred on a label 1.5 in tall or more)
        big = RH >= dpi * 1.5
        side = min(RH - 2 * m, int(RW * (0.32 if big else 0.45)))
        mag = max(2, min(10, side // n))
        qs = mag * n
        ops.append(("qr", m, max(0, (RH - qs) // 2), mag, qs))
        x0 = m + qs + int(dpi * (0.12 if big else 0.08))
        tw = RW - x0 - m
        avail = RH - 2 * m
        if big:
            lines, sh, blk, bh, gap = fit_text(tw, avail, pt(11.5), pt(6), pt(40), pt(28))
        else:
            lines, sh, blk, bh, gap = fit_text(tw, avail, pt(7.5), pt(5), pt(30), pt(13))
        total = int(len(lines) * sh * 1.12) + gap + bh
        place_text(x0, max(m, (RH - total) // 2), tw, lines, sh, blk, gap, "C" if big else "L")

    W, H = (RH, RW) if sideways else (RW, RH)   # sideways: across the print head is the reading height
    out = ["^XA^CI28", f"^PW{W}^LL{H}^LH0,0"]
    for op in ops:
        if op[0] == "qr":
            _, X, Y, mag, qs = op
            fo = f"^FO{W - Y - qs},{X}" if sideways else f"^FO{X},{Y}"
            out.append(f"{fo}^BQN,2,{mag}^FDMA,{_zpl_text(code)}^FS")
            continue
        _, X, Y, h, wf, bw, lines, align = op
        sp = int(h * (0.12 if wf == 1.0 else 0.18))   # extra dots between lines
        box = len(lines) * h + (len(lines) - 1) * sp
        fd = ZPL_NEWLINE.join(_zpl_text(x) for x in lines)
        if sideways:   # rotated 90 degrees clockwise: a box at (X, Y) as read, box tall, sits at x = W - Y - box, y = X
            out.append(f"^FO{W - Y - box},{X}^A0R,{h},{int(h * wf)}^FB{bw},{len(lines)},{sp},{align},0^FD{fd}^FS")
        else:
            out.append(f"^FO{X},{Y}^A0N,{h},{int(h * wf)}^FB{bw},{len(lines)},{sp},{align},0^FD{fd}^FS")
    out.append("^XZ")
    return "".join(out)


def print_labels(body, by=""):
    lines = body.get("lines") or []
    if not isinstance(lines, list) or not lines:
        raise ApiError(400, "No labels to print")
    size = body.get("size") or {}
    try:
        w_in, l_in = float(size.get("w") or CFG["label_w_in"]), float(size.get("h") or CFG["label_l_in"])
    except (TypeError, ValueError, AttributeError):
        raise ApiError(400, "The label size should be in inches")
    head = l_in if size.get("sideways") else w_in   # what goes across the print head
    if not (0.5 <= min(w_in, l_in) and max(w_in, l_in) <= 8 and 0.75 <= head <= 4.1):
        raise ApiError(400, "That label size doesn't fit a label printer (up to 4 in across)")
    jobs, total = [], 0
    for ln in lines:
        r = _item(ln.get("id"))
        if not r["sku"]:
            raise ApiError(400, f"{label_name(r)} has no SKU: generate one first")
        copies = _int(ln.get("copies", 1), 1, 500, "Copies")
        total += copies
        jobs.append((r, copies))
    if total > 1000:
        raise ApiError(400, "Up to 1,000 labels at a time")
    show_name, show_upc, show_bin = body.get("name", True) is not False, bool(body.get("upc")), bool(body.get("bin"))
    sideways = bool(size.get("sideways"))
    bins = item_bin_codes()
    zpl = "".join(zpl_label({"sku": r["sku"], "name": r["name"], "attrs": r["attrs"] or "", "gtin": r["gtin"] or "",
                             "bin": (bins.get(r["id"]) or [""])[0]}, w_in, l_in, show_name, show_upc, sideways, show_bin) * c
                  for r, c in jobs)
    send_to_printer(zpl)
    record_labels([(r["id"], c) for r, c in jobs], by, "zebra")
    return {"printed": total}


def print_bin_labels(body, by=""):
    """Bin labels on the Zebra: a QR code of BIN-code (scan it at the station), the code large, the bin's name."""
    codes = body.get("codes") or []
    if not isinstance(codes, list) or not codes or len(codes) > 300:
        raise ApiError(400, "Pick 1 to 300 bins")
    size = body.get("size") or {}
    try:
        w_in, l_in = float(size.get("w") or CFG["label_w_in"]), float(size.get("h") or CFG["label_l_in"])
    except (TypeError, ValueError, AttributeError):
        raise ApiError(400, "The label size should be in inches")
    sideways = bool(size.get("sideways"))
    zpl = []
    for c in codes:
        b = q1("SELECT * FROM bins WHERE code=?", bin_code(c))
        if not b:
            raise ApiError(404, f"No bin {bin_code(c)}")
        zpl.append(zpl_label({"sku": "BIN-" + b["code"], "name": b["name"] or f"Bin {b['code']}", "attrs": "", "gtin": ""}, w_in, l_in, True, False, sideways))
    send_to_printer("".join(zpl))
    note_change("label", "bins printed", f"{len(codes)} bin label(s)", by=by)
    return {"printed": len(codes)}


def record_labels(pairs, by, via):
    at = now_iso()
    with DB_LOCK:
        conn.executemany("INSERT INTO labels(at,item_id,copies,by,via) VALUES(?,?,?,?,?)",
                         [(at, int(i), int(c), by, via) for i, c in pairs])
    bump(items=True)


# ---------------------------------------------------------------- exports
def _csv_safe(v):
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def csv_bytes(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    for r in rows:
        w.writerow([_csv_safe(c) for c in r])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def local_time(iso):
    d = parse_iso(iso)
    return d.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S") if d else ""


def export_log(where="1=1", args=()):
    rows = [["When (shop time)", "Change", "Item", "SKU", "WooCommerce id", "Change by", "Set to", "Before", "After",
             "Reason", "Note", "Who", "Result", "Message", "Mode", "Order", "Returned"]]
    for r in q(f"SELECT * FROM jobs WHERE {where} ORDER BY id DESC LIMIT 100000", *args):
        change = (f"{r['delta']:+d}" if r["delta"] is not None else "")
        if r["kind"] == "note":
            change = ""
        rows.append([local_time(r["at"]), r["kind"], r["name"], r["sku"], r["item_id"], change,
                     r["target"] if r["target"] is not None else (r["value"] or ""), r["before"], r["after"],
                     r["reason"], r["note"], r["by"], r["status"], r["error"], r["mode"], r["order_number"] or "",
                     r["qty"] if r["kind"] == "return" else ""])
    return csv_bytes(rows)


def export_items():
    rows = [["WooCommerce id", "Parent id", "Name", "Variation", "SKU", "Barcode (GTIN/UPC)", "Stock", "Scope", "Why",
             "Status", "Price", "Last read (shop time)"]]
    for r in q("SELECT * FROM items WHERE scope IN ('shop','readonly','gun') ORDER BY name COLLATE NOCASE, attrs"):
        rows.append([r["id"], r["parent_id"] or "", r["name"], r["attrs"], r["sku"], r["gtin"], r["stock"], r["scope"],
                     r["why"], r["status"], r["price"], local_time(r["seen_at"])])
    return csv_bytes(rows)


def export_people():
    rows = [["Name", "Badge", "Active", "Added (UTC)"]]
    for r in q("SELECT * FROM people ORDER BY active DESC, name COLLATE NOCASE"):
        rows.append([r["name"], "USR-" + r["code"], "yes" if r["active"] else "removed", r["created_at"] or ""])
    return csv_bytes(rows)


def export_changes():
    rows = [["When (shop time)", "Kind", "Action", "Subject", "Detail", "By"]]
    for r in q("SELECT * FROM changes ORDER BY id DESC"):
        rows.append([local_time(r["at"]), r["kind"], r["action"], r["subject"], r["detail"], r["by"]])
    return csv_bytes(rows)


# ---------------------------------------------------------------- Dropbox backups (adapted from the Locator)
DBX_OAUTH = env("DROPBOX_OAUTH_URL", "https://api.dropboxapi.com/oauth2/token")
DBX_API = env("DROPBOX_API_URL", "https://api.dropboxapi.com/2")
DBX_CONTENT = env("DROPBOX_CONTENT_URL", "https://content.dropboxapi.com/2")
_dbx_token = {"v": None, "exp": 0}
BACKUP_LOCK = threading.Lock()


class DbxError(Exception):
    pass


def dbx_conf():
    return meta_get("dropbox", {}) or {}


def dbx_public():
    d = dbx_conf()
    out = {k: d.get(k) for k in ("account", "folder", "time", "keep_days", "last_ok", "last_try", "last_error", "last_folder")}
    out["connected"] = bool(d.get("refresh_token"))
    return out


def _dbx_http(url, data, headers, timeout=60):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read()[:600].decode(errors="replace")
        try:
            err = json.loads(body)
            body = err.get("error_summary") or err.get("error_description") or body
        except ValueError:
            pass
        raise DbxError(f"Dropbox said {e.code}: {body}".strip())
    except (urllib.error.URLError, OSError) as e:
        raise DbxError(f"Can't reach Dropbox: {e}")


def dbx_token():
    if _dbx_token["v"] and time.time() < _dbx_token["exp"]:
        return _dbx_token["v"]
    d = dbx_conf()
    if not d.get("refresh_token"):
        raise DbxError("Dropbox isn't connected")
    r = _dbx_http(DBX_OAUTH, urllib.parse.urlencode({
        "grant_type": "refresh_token", "refresh_token": d["refresh_token"],
        "client_id": d["app_key"], "client_secret": d["app_secret"]}).encode(),
        {"Content-Type": "application/x-www-form-urlencoded"})
    _dbx_token.update(v=r["access_token"], exp=time.time() + int(r.get("expires_in", 14400)) - 300)
    return _dbx_token["v"]


def dbx_rpc(path, args=None):
    return _dbx_http(DBX_API + path, json.dumps(args).encode() if args is not None else b"null",
                     {"Authorization": "Bearer " + dbx_token(), "Content-Type": "application/json"})


def dbx_upload(path, data):
    return _dbx_http(DBX_CONTENT + "/files/upload", data, {
        "Authorization": "Bearer " + dbx_token(), "Content-Type": "application/octet-stream",
        "Dropbox-API-Arg": json.dumps({"path": path, "mode": "overwrite", "mute": True})}, timeout=300)


def dbx_folder(raw):
    f = "/" + "/".join(p for p in str(raw or "").replace("\\", "/").split("/") if p.strip())
    return f if f != "/" else "/Wojtek Stock backups"


def dbx_connect(app_key, app_secret, code, by=""):
    app_key, app_secret, code = (str(x or "").strip() for x in (app_key, app_secret, code))
    if not (app_key and app_secret and code):
        raise ApiError(400, "Fill in the app key, app secret and authorization code")
    try:
        r = _dbx_http(DBX_OAUTH, urllib.parse.urlencode({"grant_type": "authorization_code", "code": code,
                                                         "client_id": app_key, "client_secret": app_secret}).encode(),
                      {"Content-Type": "application/x-www-form-urlencoded"})
    except DbxError as e:
        raise ApiError(502, str(e))
    if not r.get("refresh_token"):
        raise ApiError(502, "Dropbox didn't return a long-term token. Get a fresh code and try again.")
    d = dbx_conf()
    d.update(app_key=app_key, app_secret=app_secret, refresh_token=r["refresh_token"], last_error="")
    d.setdefault("folder", "/Wojtek Stock backups")
    d.setdefault("time", "23:45")
    d.setdefault("keep_days", 90)
    meta_set("dropbox", d)
    _dbx_token.update(v=r.get("access_token"), exp=time.time() + int(r.get("expires_in", 14400)) - 300)
    try:
        who = dbx_rpc("/users/get_current_account")
        d["account"] = f"{who.get('name', {}).get('display_name', '')} ({who.get('email', '')})".strip()
    except DbxError:
        d["account"] = "connected"
    meta_set("dropbox", d)
    note_change("setting", "connected", "Dropbox backups", d["account"], by)
    return dbx_public()


def db_snapshot():
    """A gzipped copy of the database without the Dropbox credentials."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "stock.db")
        dst = sqlite3.connect(path)
        with DB_LOCK:
            conn.backup(dst)
        dst.execute("DELETE FROM meta WHERE k='dropbox'")
        dst.commit()
        dst.execute("VACUUM")
        dst.close()
        with open(path, "rb") as f:
            return gzip.compress(f.read(), 6)


def run_backup(reason="scheduled"):
    if CFG["safe_copy"]:
        raise ApiError(409, "This is a test copy restored from a backup: it doesn't make backups")
    if not BACKUP_LOCK.acquire(blocking=False):
        raise ApiError(409, "A backup is already running")
    try:
        d = dbx_conf()
        d["last_try"] = now_iso()
        meta_set("dropbox", d)
        day = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
        base = dbx_folder(d.get("folder"))
        folder = f"{base}/{day}"
        files = {"change-log.csv": export_log(), "items.csv": export_items(), "people.csv": export_people(),
                 "settings-changes.csv": export_changes(), "stock-db.sqlite.gz": db_snapshot()}
        try:
            for name, data in files.items():
                dbx_upload(f"{folder}/{name}", data)
            done = set(meta_get("bill_files_backed", []) or [])
            if os.path.isdir(BILL_FILES):
                for fn in sorted(os.listdir(BILL_FILES)):
                    if fn not in done:
                        with open(os.path.join(BILL_FILES, fn), "rb") as fh:
                            dbx_upload(f"{base}/invoice-copies/{fn}", fh.read())
                        done.add(fn)
                meta_set("bill_files_backed", sorted(done))
            pruned = dbx_prune(base, int(d.get("keep_days") or 90))
        except DbxError as e:
            d = dbx_conf()
            d["last_error"] = str(e)
            meta_set("dropbox", d)
            log.warning("Dropbox backup failed: %s", e)
            raise ApiError(502, str(e))
        d = dbx_conf()
        d.update(last_ok=now_iso(), last_error="", last_folder=folder)
        meta_set("dropbox", d)
        log.info("Backed up %d files to Dropbox %s (%s)%s", len(files), folder, reason,
                 f"; removed {pruned} old day(s)" if pruned else "")
        bump()
        return dbx_public()
    finally:
        BACKUP_LOCK.release()


def dbx_prune(folder, keep_days):
    if keep_days <= 0:
        return 0
    try:
        listing = dbx_rpc("/files/list_folder", {"path": folder})
    except DbxError as e:
        if "not_found" in str(e):
            return 0
        raise
    cutoff = datetime.now(LOCAL_TZ).date().toordinal() - keep_days
    n = 0
    for e in listing.get("entries", []):
        name = e.get("name", "")
        if e.get(".tag") == "folder" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", name):
            try:
                if datetime.strptime(name, "%Y-%m-%d").date().toordinal() < cutoff:
                    dbx_rpc("/files/delete_v2", {"path": f"{folder}/{name}"})
                    n += 1
            except (ValueError, DbxError) as err:
                log.info("Couldn't remove old backup %s: %s", name, err)
    return n


def backup_loop():
    while True:
        time.sleep(60)
        try:
            d = dbx_conf()
            if not d.get("refresh_token"):
                continue
            now = datetime.now(LOCAL_TZ)
            h, _, m = str(d.get("time") or "23:45").partition(":")
            due = (now.hour, now.minute) >= (int(h or 23), int(m or 0))
            last = parse_iso(d.get("last_ok"))
            done_today = last and last.astimezone(LOCAL_TZ).date() == now.date()
            tried = parse_iso(d.get("last_try"))
            recent_try = tried and (datetime.now(timezone.utc) - tried).total_seconds() < 1800
            if due and not done_today and not (d.get("last_error") and recent_try):
                run_backup()
        except Exception as e:
            log.warning("Backup check: %s", e)


def woo_test():
    t0 = time.time()
    data, hdrs = woo("GET", "/products", {"per_page": 1, "_fields": "id"})
    h = {k.lower(): v for k, v in hdrs.items()}
    out = {"ok": True, "products": int(h.get("x-wp-total", "0") or 0), "auth": _woo_auth["mode"],
           "ms": int((time.time() - t0) * 1000)}
    try:
        st, _ = woo("GET", "/system_status", {"_fields": "environment"}, tries=1)
        out["wcVersion"] = ((st or {}).get("environment") or {}).get("version", "")
    except WooError:
        out["wcVersion"] = ""
    return out


# ---------------------------------------------------------------- HTTP
def qint(qs, name, default, lo=None, hi=None):
    try:
        v = int((qs.get(name) or [default])[0])
    except (TypeError, ValueError):
        v = default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


class Handler(BaseHTTPRequestHandler):
    server_version = f"WojtekStock/{VERSION}"

    def log_message(self, fmt, *args):
        if not (self.path.startswith("/api/state") or self.path.startswith("/api/batch") or self.path == "/healthz"):
            log.info("%s %s", self.address_string(), fmt % args)

    def cookie(self, name):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def authorized(self):
        """self.auth: open (no password set), password, or terminal (with self.terminal)."""
        self.auth, self.terminal = None, None
        if not CFG["password"]:
            self.auth = "open"
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                u, _, p = base64.b64decode(h[6:]).decode().partition(":")
                if hmac.compare_digest(u, CFG["user"]) and hmac.compare_digest(p, CFG["password"]):
                    self.auth = "password"
                    return True
            except Exception:
                pass
        term = terminal_for(self.cookie(TERMINAL_COOKIE))
        if term:
            self.auth, self.terminal = "terminal", term
            note_seen(term, self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip())
            return True
        return False

    def manager(self):
        """The manager signed in with their PIN on this station, if any."""
        if not hasattr(self, "_mgr"):
            self._mgr = manager_for(self.headers.get("X-Stock-Manager", ""))
        return self._mgr

    def can_manage(self):
        return self.auth in ("open", "password") or bool(self.manager())

    def need_manage(self, what="That"):
        if not self.can_manage():
            raise ApiError(403, f"{what} is for managers: scan a manager's badge and enter the PIN (or use the shop password)")

    def operator(self):
        m = self.manager()
        if m:   # a manager's own session: their name on everything, whatever the badge header says
            return {"code": m["code"], "name": m["name"]}
        return operator_for(self.headers.get("X-Stock-Operator", ""))

    def actor(self, op=None):
        op = op if op is not None else self.operator()
        where = (f"on {self.terminal['name']}" if self.terminal else "shop password" if self.auth == "password" else "")
        name = op["name"] if op else ""
        return f"{name} ({where})" if name and where else name or where or "unknown"

    def who_for_write(self):
        op = self.operator()
        if not op and meta_get("require_badge", True) and q1("SELECT 1 FROM people WHERE active=1"):
            raise ApiError(403, "Scan your badge first")
        return self.actor(op), (op["code"] if op else "")

    def send(self, status, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Bad request length")
        if n > 20_000_000:
            raise ApiError(413, "That request is too large")
        if not n:
            return {}
        try:
            b = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "That request wasn't valid JSON")
        if not isinstance(b, dict):
            raise ApiError(400, "That request should be a JSON object")
        return b

    def csv(self, name, data):
        return self.send(200, data, "text/csv; charset=utf-8",
                         {"Content-Disposition": f'attachment; filename="{name}"'})

    def do_GET(self):
        self.handle_any("GET")

    def do_HEAD(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def handle_any(self, method):
        try:
            u = urllib.parse.urlparse(self.path)
            path, qs = u.path, urllib.parse.parse_qs(u.query)
            if method == "GET" and path == "/healthz":
                return self.send(200, b"ok", "text/plain")
            tm = re.fullmatch(r"/terminal/([A-Za-z0-9_-]+)", path)
            if method == "GET" and tm:
                if not terminal_for(tm.group(1)):
                    return self.send(403, "<!doctype html><meta name=viewport content='width=device-width'>"
                                          "<body style='font:18px system-ui;padding:40px'><h1>This terminal link doesn't work</h1>"
                                          "<p>It was revoked or mistyped. Add the terminal again on the People tab and use "
                                          "the new link.</p>", "text/html; charset=utf-8")
                return self.send(302, b"", "text/plain", {
                    "Location": "/",
                    "Set-Cookie": f"{TERMINAL_COOKIE}={tm.group(1)}; Path=/; Max-Age=315360000; HttpOnly; SameSite=Lax"})
            if not self.authorized():
                return self.send(401, {"error": "Sign in required"}, extra={"WWW-Authenticate": 'Basic realm="Wojtek Stock"'})
            if method == "POST" and self.headers.get("X-Requested-With") != "stock":
                raise ApiError(403, "Requests must come from the Wojtek Stock page")   # no cross-site form posts
            if method == "GET":
                return self.get(path, qs)
            return self.post(path, self.body())
        except ApiError as e:
            return self.send(e.status, dict({"error": e.msg}, **e.extra))
        except WooError as e:
            return self.send(502, {"error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            return None
        except Exception as e:
            log.exception("Request %s failed: %s", self.path, e)
            return self.send(500, {"error": f"Something went wrong on the server: {e}"})

    # ------------------------------------------------------------ reads
    def get(self, path, qs):
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/state":
            if (qs.get("v") or [""])[0] == state_version():
                return self.send(200, {"same": True})
            return self.send(200, self.state())
        if path == "/api/items":
            if (qs.get("v") or [""])[0] == items_version():
                return self.send(200, {"same": True})
            return self.send(200, {"v": items_version(), "rows": station_items()})
        if path == "/api/resolve":
            return self.send(200, {"matches": resolve_code((qs.get("code") or [""])[0])})
        if path == "/api/item":
            r = _item(qint(qs, "id", 0))
            if (qs.get("live") or ["0"])[0] == "1" and r["scope"] in ("shop", "readonly"):
                try:
                    live_one(r["id"], r["parent_id"])
                    rescope()
                    r = _item(r["id"])
                except WooError as e:
                    out = item_out(r, True)
                    out["liveError"] = str(e)
                    return self.send(200, out)
            out = item_out(r, True)
            out["live"] = (qs.get("live") or ["0"])[0] == "1"
            return self.send(200, out)
        if path == "/api/batch":
            b = (qs.get("id") or [""])[0]
            rows = [dict(r) for r in q("SELECT id, kind, item_id, name, sku, delta, target, value, qty, restock, order_number, "
                                       "status, before, after, error, attempts FROM jobs WHERE batch=? ORDER BY id", b)]
            return self.send(200, {"jobs": rows, "paused": writes_paused()})
        if path == "/api/log":
            where, args = ["1=1"], []
            before = qint(qs, "before", 0)
            if before:
                where.append("id<?")
                args.append(before)
            for key, col in (("kind", "kind"), ("status", "status")):
                v = (qs.get(key) or [""])[0]
                if v:
                    where.append(f"{col}=?")
                    args.append(v)
            text = (qs.get("q") or [""])[0].strip()
            if text:
                where.append("(name LIKE ? OR sku LIKE ? OR by LIKE ? OR reason LIKE ? OR note LIKE ? OR order_number=?)")
                args += [f"%{text}%"] * 5 + [text.lstrip("#")]
            day = (qs.get("day") or [""])[0]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)
                where.append("at>=? AND at<?")
                args += [start.isoformat(timespec="seconds"), (start + timedelta(days=1)).isoformat(timespec="seconds")]
            limit = qint(qs, "limit", 200, 1, 1000)
            rows = [dict(r) for r in q(f"SELECT * FROM jobs WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?", *args, limit)]
            return self.send(200, {"rows": rows, "more": len(rows) == limit})
        if path == "/api/log.csv":
            return self.csv(f"wojtek-stock-log-{datetime.now(LOCAL_TZ):%Y-%m-%d}.csv", export_log())
        if path == "/api/items.csv":
            return self.csv(f"wojtek-stock-items-{datetime.now(LOCAL_TZ):%Y-%m-%d}.csv", export_items())
        if path == "/api/people.csv":
            return self.csv("wojtek-stock-people.csv", export_people())
        if path == "/api/setup":
            return self.send(200, setup_info())
        if path == "/api/vendors":
            return self.send(200, {"vendors": list_vendors()})
        if path == "/api/pos":
            return self.send(200, {"pos": list_pos((qs.get("status") or [""])[0], qint(qs, "vendor", 0))})
        if path == "/api/po":
            return self.send(200, {"po": get_po(qint(qs, "id", 0), (qs.get("number") or [""])[0])})
        if path == "/api/po/pdf":
            po = get_po(qint(qs, "id", 0))
            return self.send(200, po_pdf(po), "application/pdf", {"Content-Disposition": f'attachment; filename="{po["number"]}.pdf"'})
        if path == "/api/gb":
            return self.send(200, gb_info())
        if path == "/api/reorder":
            return self.send(200, {"rows": reorder_status()})
        if path == "/api/feed/search":
            return self.send(200, {"rows": feed_search((qs.get("q") or [""])[0])})
        if path == "/api/vendor/items":
            vid = qint(qs, "id", 0)
            return self.send(200, {"rows": [dict(r) for r in q(
                "SELECT vi.*, i.name, i.attrs, i.sku, i.stock, i.scope FROM vendor_items vi JOIN items i ON i.id=vi.item_id "
                "WHERE vi.vendor_id=? AND i.scope='shop' ORDER BY i.name COLLATE NOCASE", vid)]})
        if path == "/api/bins":
            return self.send(200, {"bins": list_bins()})
        if path == "/api/kits":
            return self.send(200, {"kits": list_kits()})
        if path == "/api/bin":
            return self.send(200, {"bin": get_bin((qs.get("code") or [""])[0])})
        if path == "/api/counts/overdue":
            return self.send(200, {"rows": overdue_counts(qint(qs, "days", 90, 1, 3650))})
        if path == "/api/fullcount":
            return self.send(200, full_count_status())
        if path == "/api/rmas":
            return self.send(200, {"rmas": list_rmas((qs.get("status") or [""])[0])})
        if path == "/api/rma":
            r = q1("SELECT * FROM rmas WHERE id=?", qint(qs, "id", 0))
            if not r:
                raise ApiError(404, "No such return")
            return self.send(200, {"rma": rma_out(r)})
        if path == "/api/rma/pdf":
            r = q1("SELECT * FROM rmas WHERE id=?", qint(qs, "id", 0))
            if not r:
                raise ApiError(404, "No such return")
            d = rma_out(r)
            return self.send(200, rma_pdf(d), "application/pdf", {"Content-Disposition": f'attachment; filename="{d["number"]}.pdf"'})
        if path == "/api/rma/damaged":
            return self.send(200, {"rows": damaged_pile(qint(qs, "vendor", 0) or None)})
        if path == "/api/order":
            return self.send(200, {"order": order_view(find_order((qs.get("code") or [""])[0]))})
        if path == "/api/morning/preview":
            self.need_manage("The morning email")
            subject, text = morning_report()
            return self.send(200, {"subject": subject, "text": text, "to": morning_to(), "time": CFG["morning_time"],
                                   "last": meta_get("morning_last"), "alerts": meta_get("alerts_active", {}) or {},
                                   "alertTo": alert_to(), "alertsOn": CFG["alerts_on"]})
        if path.startswith("/api/report") or path in ("/api/snapshots",):
            self.need_manage("Reports")
            if path == "/api/report/inventory":
                return self.send(200, report_inventory())
            if path == "/api/report/sales":
                return self.send(200, report_sales(*_period(qs)))
            if path == "/api/report/margins":
                try:
                    th = float((qs.get("threshold") or ["20"])[0])
                except ValueError:
                    th = 20.0
                return self.send(200, report_margins(th))
            if path == "/api/report/purchasing":
                return self.send(200, report_purchasing(qint(qs, "months", 12)))
            if path == "/api/snapshots":
                return self.send(200, {"snapshots": [dict(r) for r in q("SELECT * FROM snapshots ORDER BY day DESC, id DESC")]})
            m = re.fullmatch(r"/api/report/(inventory|sales|margins|snapshot)\.csv", path)
            if m:
                return self.csv(f"wojtek-stock-{m.group(1)}-{datetime.now(LOCAL_TZ):%Y-%m-%d}.csv", export_report(m.group(1), qs))
            raise ApiError(404, "No such report")
        if path in ("/api/bills", "/api/bill", "/api/bill/prefill", "/api/bill/file", "/api/payables", "/api/payments",
                    "/api/bills.csv", "/api/payments.csv"):
            self.need_manage("Vendor invoices and payments")
            if path == "/api/bills":
                return self.send(200, {"bills": list_bills((qs.get("status") or [""])[0], qint(qs, "vendor", 0))})
            if path == "/api/bill":
                b = q1("SELECT * FROM bills WHERE id=?", qint(qs, "id", 0))
                if not b:
                    raise ApiError(404, "No such invoice")
                return self.send(200, {"bill": bill_out(b)})
            if path == "/api/bill/prefill":
                ids = [int(x) for x in (qs.get("pos") or [""])[0].split(",") if x.strip().isdigit()][:50]
                return self.send(200, {"lines": bill_prefill(qint(qs, "vendor", 0), ids, qint(qs, "bill", 0) or None)})
            if path == "/api/bill/file":
                b = q1("SELECT * FROM bills WHERE id=?", qint(qs, "id", 0))
                if not b or not b["file"] or not os.path.exists(os.path.join(BILL_FILES, b["file"])):
                    raise ApiError(404, "No copy of that invoice is attached")
                ext = os.path.splitext(b["file"])[1].lower()
                ctype = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                         ".webp": "image/webp", ".gif": "image/gif", ".heic": "image/heic"}.get(ext, "application/octet-stream")
                with open(os.path.join(BILL_FILES, b["file"]), "rb") as fh:
                    return self.send(200, fh.read(), ctype, {"Content-Disposition": f'inline; filename="{b["file_name"] or b["file"]}"'})
            if path == "/api/payables":
                return self.send(200, payables())
            if path == "/api/payments":
                return self.send(200, {"payments": list_payments(qint(qs, "vendor", 0))})
            if path == "/api/bills.csv":
                return self.csv(f"wojtek-stock-vendor-invoices-{datetime.now(LOCAL_TZ):%Y-%m-%d}.csv", export_bills())
            return self.csv(f"wojtek-stock-payments-{datetime.now(LOCAL_TZ):%Y-%m-%d}.csv", export_payments())
        if path == "/api/printer":
            return self.send(200, printer_status())
        if path == "/api/labels/zebra-preview":
            return self.send(200, zebra_preview(qs), "image/svg+xml")
        if path == "/api/nosku":
            return self.send(200, {"rows": [item_out(r) for r in q("SELECT * FROM items WHERE scope IN ('shop','readonly') "
                                                                  "AND sku='' ORDER BY name COLLATE NOCASE, attrs")]})
        raise ApiError(404, "Not found")

    def state(self):
        s = meta_get("sync", {}) or {}
        out = {
            "version": VERSION, "v": state_version(), "itemsV": items_version(),
            "woo": {"ready": WOO_READY, "url": CFG["woo_url"], "write": CFG["woo_write"]},
            "setup": {"confirmed": setup_confirmed(), "prefixes": feed_prefixes()},
            "sync": {k: s.get(k) for k in ("running", "error", "last_ok", "last_full", "last_quick", "products", "variations", "full_secs")},
            "progress": prog_public(),
            "syncMinutes": CFG["sync_minutes"],
            "paused": writes_paused(), "pending": jobs_pending(),
            "people": [dict(r, hasPin=bool(r["pin_hash"]), pin_hash=None) for r in q("SELECT code, name, role, pin_hash FROM people WHERE active=1 ORDER BY name COLLATE NOCASE")],
            "requireBadge": bool(meta_get("require_badge", True)),
            "reasons": {"down": CFG["down_reasons"], "up": CFG["up_reasons"]},
            "label": {"printer": bool(printer_addr()), "w": CFG["label_w_in"], "h": CFG["label_l_in"], "dpi": CFG["label_dpi"]},
            "skuPrefix": CFG["sku_prefix"],
            "shop": {"name": CFG["shop_name"], "address": [x.strip() for x in CFG["shop_address"].split("|") if x.strip()],
                     "phone": CFG["shop_phone"], "email": CFG["shop_email"]},
            "openPos": q1("SELECT COUNT(*) n FROM pos WHERE status IN ('sent','partial')")["n"],
            "email": {"ready": email_ready(), "from": CFG["smtp_from"], "host": CFG["smtp_host"], "cc": CFG["po_cc"]},
            "autoPoTime": CFG["auto_po_time"],
            "testCopy": CFG["safe_copy"],
            "gb": {"ready": gb_ready(), "mode": CFG["gb_sync"], "error": (meta_get("gb", {}) or {}).get("error", ""),
                   "attention": q1("SELECT COUNT(*) n FROM gb_listings WHERE error!=''")["n"] +
                                q1("SELECT COUNT(*) n FROM gb_orders WHERE status IN ('failed','cancelled after import')")["n"]},
            "autoDrafts": q1("SELECT COUNT(*) n FROM pos WHERE status='draft' AND auto=1 AND EXISTS(SELECT 1 FROM po_lines WHERE po_id=pos.id)")["n"],
            "backup": dbx_public(),
            "access": {"terminal": self.terminal["name"] if self.terminal else "", "canManage": self.can_manage(),
                       "manager": (self.manager() or {}).get("name", ""), "password": self.auth in ("open", "password")},
            "watchCount": q1("SELECT COUNT(*) n FROM restock_watch")["n"],
            "fullCount": (lambda f: {"since": f["started_at"], "by": f["by"]} if f else None)(active_full_count()),
            "driftCount": q1("SELECT COUNT(*) n FROM drift WHERE at>=?",
                             (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds"))["n"],
        }
        if self.can_manage():
            pt = payables()["totals"]
            out["payables"] = {"overdue": pt["overdue"], "soon": pt["soon"], "count": pt["count"]}
            out["terminals"] = [dict(r) for r in q("SELECT id, name, created_at, last_seen, last_ip FROM terminals "
                                                   "WHERE active=1 ORDER BY created_at")]
        return out

    # ------------------------------------------------------------ changes
    def post(self, path, b):
        if path == "/api/commit":
            by, op = self.who_for_write()
            return self.send(200, commit(b, by, op))
        if path == "/api/link":
            by, op = self.who_for_write()
            return self.send(200, link_barcode(b, by, op))
        if path == "/api/sku":
            by, op = self.who_for_write()
            return self.send(200, generate_skus(b, by, op))
        if path == "/api/vendors":
            self.need_manage("Purchasing")
            by, _ = self.who_for_write()
            return self.send(200, save_vendor(b, by))
        if path == "/api/vendors/remove":
            self.need_manage("Purchasing")
            by, _ = self.who_for_write()
            remove_vendor(_int(b.get("id"), 1, 10 ** 9, "Vendor"), by)
            return self.send(200, {"ok": True})
        if path == "/api/po":
            self.need_manage("Purchasing")
            by, _ = self.who_for_write()
            return self.send(200, {"po": save_po(b, by)})
        if path == "/api/po/action":
            self.need_manage("Purchasing")
            by, _ = self.who_for_write()
            return self.send(200, {"po": po_action(b, by)})
        if path == "/api/morning/send":
            self.need_manage("Sending the morning email")
            return self.send(200, send_morning(self.actor()))
        if path in ("/api/snapshot/take", "/api/sales/load"):
            self.need_manage("Reports")
            if path == "/api/snapshot/take":
                return self.send(200, {"id": take_snapshot(None, "manual", self.actor())})
            if (meta_get("sales_history", {}) or {}).get("running"):
                raise ApiError(409, "Sales history is already being read")
            days = _int(b.get("days") or 400, 30, 3660, "Days")
            who = self.actor()

            def run():
                try:
                    load_sales_history(days, who)
                except (ApiError, WooError) as e:
                    log.warning("Sales history: %s", e)
            threading.Thread(target=run, daemon=True).start()
            return self.send(200, {"ok": True})
        if path in ("/api/bill", "/api/bill/void", "/api/payment", "/api/payment/void", "/api/credit/apply"):
            self.need_manage("Vendor invoices and payments")
            by = self.actor()
            if path == "/api/bill":
                return self.send(200, {"bill": save_bill(b, by)})
            if path == "/api/bill/void":
                return self.send(200, {"bill": void_bill(_int(b.get("id"), 1, 10 ** 12, "Invoice"), by)})
            if path == "/api/payment":
                return self.send(200, record_payment(b, by))
            if path == "/api/payment/void":
                void_payment(_int(b.get("id"), 1, 10 ** 12, "Payment"), by)
                return self.send(200, {"ok": True})
            return self.send(200, {"bill": apply_credit(b, by)})
        if path == "/api/po/email":
            self.need_manage("Purchasing")
            by, _ = self.who_for_write()
            return self.send(200, {"po": email_po(_int(b.get("id"), 1, 10 ** 9, "PO"), by)})
        if path == "/api/gb/sync":
            self.need_manage("A GunBroker sync")
            return self.send(200, gb_sync("by hand"))
        if path == "/api/reorder/set":
            self.need_manage("Purchasing")
            by, _ = self.who_for_write()
            return self.send(200, set_reorder(b.get("rows"), by))
        if path == "/api/reorder/run":
            self.need_manage("Purchasing")
            check_can_write()
            return self.send(200, reorder_run(f"{self.actor()} (reorder points)"))
        if path == "/api/reorder/suggest":
            return self.send(200, reorder_suggest(b.get("days") or 90))
        if path == "/api/email/test":
            self.need_manage("Sending a test email")
            to = _text(b.get("to"), 120) or CFG["shop_email"] or CFG["smtp_from"]
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to or ""):
                raise ApiError(400, "Give an address to send the test to")
            send_email(to, f"Wojtek Stock test email from {CFG['shop_name']}",
                       "This is a test from Wojtek Stock. If you're reading it, purchase orders can be emailed to vendors.")
            return self.send(200, {"ok": True, "to": to})
        if path == "/api/po/receive":
            by, op = self.who_for_write()
            return self.send(200, receive_po(b, by, op))
        if path == "/api/feed/copy":
            self.need_manage("Purchasing")
            by, op = self.who_for_write()
            return self.send(200, copy_feed(b, by, op))
        if path == "/api/feed/link":
            self.need_manage("Purchasing")
            by, op = self.who_for_write()
            return self.send(200, link_feed(b, by, op))
        if path == "/api/bins":
            by, _ = self.who_for_write()
            return self.send(200, {"bin": save_bin(b, by)})
        if path == "/api/kits":
            self.need_manage("Setting up kits")
            return self.send(200, {"kit": save_kit(b, self.actor())})
        if path == "/api/kits/remove":
            self.need_manage("Setting up kits")
            remove_kit(_int(b.get("item_id"), 1, 10 ** 12, "Kit"), self.actor())
            return self.send(200, {"ok": True})
        if path == "/api/bins/remove":
            self.need_manage("Removing a bin")
            remove_bin(_int(b.get("id"), 1, 10 ** 9, "Bin"), self.actor())
            return self.send(200, {"ok": True})
        if path == "/api/item/bins":
            by, _ = self.who_for_write()
            codes = b.get("bins") or []
            if not isinstance(codes, list) or len(codes) > 10:
                raise ApiError(400, "Up to 10 bins")
            return self.send(200, {"bins": set_item_bins(_int(b.get("item_id"), 1, 10 ** 12, "Item"), codes, by)})
        if path == "/api/count/done":
            by, op = self.who_for_write()
            return self.send(200, count_done(b, by, op))
        if path == "/api/fullcount/start":
            self.need_manage("Starting a full count")
            return self.send(200, full_count_start(self.actor()))
        if path == "/api/fullcount/finish":
            self.need_manage("Finishing a full count")
            by, op = self.who_for_write()
            return self.send(200, full_count_finish(b, by, op))
        if path in ("/api/rma", "/api/rma/ship", "/api/rma/close", "/api/rma/email"):
            self.need_manage("Returns to vendors")
            by, op = self.who_for_write()
            if path == "/api/rma":
                return self.send(200, {"rma": save_rma(b, by)})
            if path == "/api/rma/ship":
                return self.send(200, {"rma": ship_rma(b, by, op)})
            if path == "/api/rma/email":
                return self.send(200, {"rma": email_rma(_int(b.get("id"), 1, 10 ** 9, "Return"), by)})
            return self.send(200, {"rma": close_rma(b, by)})
        if path == "/api/rma/credit":
            self.need_manage("Recording a vendor's credit")
            return self.send(200, {"rma": credit_rma(b, self.actor())})
        if path == "/api/return":
            by, op = self.who_for_write()
            return self.send(200, commit_return(b, by, op))
        if path == "/api/watch/clear":
            self.need_manage("Clearing the list")
            with DB_LOCK:
                conn.execute("DELETE FROM restock_watch")
            bump()
            return self.send(200, {"ok": True})
        if path == "/api/live":
            ids = b.get("ids") or []
            if not isinstance(ids, list) or not all(str(i).isdigit() for i in ids):
                raise ApiError(400, "ids should be a list of item ids")
            return self.send(200, {"items": live_many(ids)})
        if path == "/api/labels/print":
            return self.send(200, print_labels(b, self.actor()))
        if path == "/api/labels/bins":
            return self.send(200, print_bin_labels(b, self.actor()))
        if path == "/api/labels/test":
            return self.send(200, print_test_label(b, self.actor()))
        if path == "/api/labels/record":
            pairs = [(int(x.get("id")), _int(x.get("copies", 1), 1, 500, "Copies")) for x in (b.get("lines") or [])
                     if str(x.get("id", "")).isdigit()]
            if pairs:
                record_labels(pairs, self.actor(), "pdf")
            return self.send(200, {"ok": True})
        if path == "/api/sync":
            _sync_now["full" if b.get("full") else "inc"] = True
            return self.send(200, {"ok": True})
        if path == "/api/jobs/retry":
            retry_job(_int(b.get("id"), 1, 10 ** 12, "id"))
            return self.send(200, {"ok": True})
        if path == "/api/jobs/cancel":
            cancel_job(_int(b.get("id"), 1, 10 ** 12, "id"), self.actor())
            return self.send(200, {"ok": True})
        if path == "/api/drift/clear":
            self.need_manage("Clearing the list")
            with DB_LOCK:
                conn.execute("DELETE FROM drift")
            bump()
            return self.send(200, {"ok": True})
        # everything below changes settings: the shop password, not a station
        if path == "/api/writes/resume":
            self.need_manage("Resuming writes")
            meta_set("writes_paused", None)
            note_change("setting", "resumed", "WooCommerce writes", by=self.actor())
            _worker_wake.set()
            bump()
            return self.send(200, {"ok": True})
        if path == "/api/setup":
            self.need_manage("Changing Setup")
            return self.send(200, save_setup(b, self.actor()))
        if path == "/api/woo/test":
            self.need_manage("Testing the connection")
            return self.send(200, woo_test())
        if path == "/api/manager/login":
            return self.send(200, manager_login(b.get("code"), b.get("pin"), self.terminal["name"] if self.terminal else "shop password"))
        if path == "/api/manager/logout":
            tok = self.headers.get("X-Stock-Manager", "")
            if tok:
                with DB_LOCK:
                    conn.execute("DELETE FROM manager_sessions WHERE token_hash=?", (token_hash(tok),))
            return self.send(200, {"ok": True})
        if path == "/api/people/role":
            self.need_manage("Changing roles")
            set_role(b.get("code"), b.get("role"), self.actor())
            return self.send(200, {"ok": True})
        if path == "/api/people/pin":
            self.need_manage("Setting PINs")
            set_pin(b.get("code"), b.get("pin"), self.actor())
            return self.send(200, {"ok": True})
        if path == "/api/people":
            self.need_manage("Adding people")
            return self.send(200, add_person(b.get("name"), b.get("code"), self.actor()))
        if path == "/api/people/remove":
            self.need_manage("Removing people")
            remove_person(b.get("code"), self.actor())
            return self.send(200, {"ok": True})
        if path == "/api/people/import":
            self.need_manage("Importing people")
            return self.send(200, import_people(b.get("csv"), self.actor()))
        if path == "/api/settings":
            self.need_manage("Changing settings")
            if "requireBadge" in b:
                meta_set("require_badge", bool(b["requireBadge"]))
                note_change("setting", "changed", "Require a badge", "on" if b["requireBadge"] else "off", self.actor())
            bump()
            return self.send(200, {"ok": True})
        if path == "/api/terminals":
            self.need_manage("Adding terminals")
            return self.send(200, add_terminal(b.get("name"), self.actor()))
        if path == "/api/terminals/revoke":
            self.need_manage("Revoking terminals")
            revoke_terminal(str(b.get("id") or ""), self.actor())
            return self.send(200, {"ok": True})
        if path == "/api/backup/connect":
            self.need_manage("Connecting Dropbox")
            return self.send(200, dbx_connect(b.get("app_key"), b.get("app_secret"), b.get("code"), self.actor()))
        if path == "/api/backup/settings":
            self.need_manage("Changing backups")
            d = dbx_conf()
            if "folder" in b:
                d["folder"] = dbx_folder(b["folder"])
            if "time" in b:
                if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", str(b["time"])):
                    raise ApiError(400, "Time should look like 23:45")
                d["time"] = str(b["time"])
            if "keep_days" in b:
                d["keep_days"] = _int(b["keep_days"], 0, 3650, "Days to keep")
            meta_set("dropbox", d)
            bump()
            return self.send(200, dbx_public())
        if path == "/api/backup/run":
            self.need_manage("Running a backup")
            return self.send(200, run_backup("by hand"))
        if path == "/api/backup/disconnect":
            self.need_manage("Disconnecting Dropbox")
            meta_set("dropbox", {})
            _dbx_token.update(v=None, exp=0)
            note_change("setting", "disconnected", "Dropbox backups", by=self.actor())
            bump()
            return self.send(200, dbx_public())
        raise ApiError(404, "Not found")


def main():
    # the password first (as in the Locator): with none set, start nothing (no sync, no writes, no backups)
    if not CFG["password"]:
        log.error("STOCK_PASSWORD isn't set (in stock.env): refusing to start, rather than run with no password")
        raise SystemExit(2)
    rescope()
    if not WOO_READY:
        log.warning("WooCommerce isn't set up yet: fill in WOO_URL, WOO_KEY and WOO_SECRET in stock.env")
    log.info("WooCommerce writes: %s. Feed prefixes: %s. Setup %s.", CFG["woo_write"],
             ", ".join(feed_prefixes()) or "none yet", "confirmed" if setup_confirmed() else "not confirmed yet")
    for fn in (sync_loop, worker_loop, backup_loop, purchasing_loop, gunbroker_loop, reports_loop, notify_loop):
        threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
    srv = ThreadingHTTPServer((CFG["host"], CFG["port"]), Handler)
    srv.daemon_threads = True
    log.info("Wojtek Stock %s on http://%s:%d", VERSION, CFG["host"], CFG["port"])
    srv.serve_forever()


if __name__ == "__main__":
    main()
