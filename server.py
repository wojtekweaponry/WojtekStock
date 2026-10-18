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

VERSION = "v8"
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
}
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
                                            "_fields": "id,line_items,date_modified_gmt"}))
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
                if want_full:
                    _sync_now["full"] = False
                    full_sync("requested" if last_full else "first")
                elif _sync_now["inc"] or not last_sync or (now - last_sync).total_seconds() >= CFG["sync_minutes"] * 60:
                    _sync_now["inc"] = False
                    inc_sync()
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
STOCK_KINDS = ("receive", "count", "adjust", "return")


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
    if kind in ("receive", "adjust", "return"):
        before = item["stock"]
        after = before + int(job["delta"])
        if after < 0:
            return job_finish(job["id"], "refused", before, after,
                              f"Would leave stock below zero (WooCommerce has {before})", mode)
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
    other = q1("SELECT * FROM items WHERE gkey=? AND id!=?", gkey(code), r["id"])
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
    with Tx() as c:
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
            out.append(f"{what}: restocked ({r['before']} to {r['after']})")
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
    return d


def station_items():
    """Everything the station can scan: in-shop items (and read-only ones, for Lookup), with their category names
    (a variation's are its parent's) and price, for the Products tab."""
    lab = {r["item_id"]: r["n"] for r in q("SELECT item_id, SUM(copies) n FROM labels GROUP BY item_id")}
    names = cat_names()
    rows = q("SELECT * FROM items WHERE scope IN ('shop','readonly') ORDER BY name COLLATE NOCASE, attrs")
    pcats = {r["id"]: r["cats"] for r in q("SELECT id, cats FROM items WHERE type='variable'")}
    out = []
    for r in rows:
        cats = json.loads((pcats.get(r["parent_id"]) if r["parent_id"] else r["cats"]) or "[]")
        out.append([r["id"], r["parent_id"] or 0, r["name"], r["attrs"] or "", r["sku"] or "", r["gtin"] or "", r["stock"],
                    r["scope"], r["why"] or "", lab.get(r["id"], 0), [names[c] for c in cats if c in names], r["price"] or ""])
    return out


def resolve_code(code):
    """Every item (any scope) with this SKU or barcode, so the station can say why a code can't be used."""
    code = str(code or "").strip()
    rows = []
    g = gkey(code)
    if g:
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


def send_to_printer(zpl):
    addr = printer_addr()
    if not addr:
        raise ApiError(400, "No label printer is set up: put its network address in stock.env (LABEL_PRINTER)")
    try:
        with socket.create_connection(addr, timeout=6) as s:
            s.sendall(zpl.encode("utf-8"))
    except OSError as e:
        raise ApiError(502, f"Couldn't reach the label printer at {addr[0]}:{addr[1]} ({e}). Is it on and on the network?")


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


def zpl_label(r, w_in=None, l_in=None, show_name=True, show_upc=False, sideways=False):
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
    show_name, show_upc = body.get("name", True) is not False, bool(body.get("upc"))
    sideways = bool(size.get("sideways"))
    zpl = "".join(zpl_label(r, w_in, l_in, show_name, show_upc, sideways) * c for r, c in jobs)
    send_to_printer(zpl)
    record_labels([(r["id"], c) for r, c in jobs], by, "zebra")
    return {"printed": total}


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

    def can_manage(self):
        return self.auth in ("open", "password")

    def need_manage(self, what="That"):
        if not self.can_manage():
            raise ApiError(403, f"{what} needs the shop password (not a station terminal)")

    def operator(self):
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
        if n > 5_000_000:
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
        if path == "/api/order":
            return self.send(200, {"order": order_view(find_order((qs.get("code") or [""])[0]))})
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
            "people": [dict(r) for r in q("SELECT code, name FROM people WHERE active=1 ORDER BY name COLLATE NOCASE")],
            "requireBadge": bool(meta_get("require_badge", True)),
            "reasons": {"down": CFG["down_reasons"], "up": CFG["up_reasons"]},
            "label": {"printer": bool(printer_addr()), "w": CFG["label_w_in"], "h": CFG["label_l_in"], "dpi": CFG["label_dpi"]},
            "skuPrefix": CFG["sku_prefix"],
            "backup": dbx_public(),
            "access": {"terminal": self.terminal["name"] if self.terminal else "", "canManage": self.can_manage()},
            "watchCount": q1("SELECT COUNT(*) n FROM restock_watch")["n"],
            "driftCount": q1("SELECT COUNT(*) n FROM drift WHERE at>=?",
                             (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds"))["n"],
        }
        if self.can_manage():
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
    for fn in (sync_loop, worker_loop, backup_loop):
        threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
    srv = ThreadingHTTPServer((CFG["host"], CFG["port"]), Handler)
    srv.daemon_threads = True
    log.info("Wojtek Stock %s on http://%s:%d", VERSION, CFG["host"], CFG["port"])
    srv.serve_forever()


if __name__ == "__main__":
    main()
