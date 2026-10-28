#!/usr/bin/env python3
"""A small stand-in for the WooCommerce REST API (wc/v3), for testing Wojtek Stock without touching the real store.

Seeded with distributor feed products (IRV-, ORI, CSSI-), in-shop products, guns, variable products and orders.
Extra /mock/... endpoints let tests simulate sales, admin edits, outages and a read-only key.
Run: python3 mock_woo.py PORT
"""
import base64
import json
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

KEY, SECRET = "ck_test", "cs_test"
LOCK = threading.RLock()
CLOCK = [datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)]


SEEDING = [True]


def tick():
    """A clock that only moves forward: each change is a second later than the last, and never behind real time.
    Seeded products are dated in the past."""
    if SEEDING[0]:
        return "2026-10-01T12:00:00"
    CLOCK[0] = max(CLOCK[0] + timedelta(seconds=1), datetime.now(timezone.utc))
    return CLOCK[0].strftime("%Y-%m-%dT%H:%M:%S")


CATS = {10: ("Ammunition", "ammunition"), 11: ("Accessories", "accessories"), 12: ("Apparel", "apparel"),
        50: ("Handguns", "handguns"), 51: ("Rifles", "rifles"), 99: ("Distributor", "distributor")}
P, V, ORDERS = {}, {}, []
NOTES, REFUNDS = {}, {}
NEXT = {"order": 5000, "line": 70000, "note": 300000, "refund": 80000, "product": 3000}
FLAGS = {"fail": [], "readonly": False, "query_only": False, "truncate": 0, "requests": 0, "delay": 0, "order_reduces": True}
# a pretend GunBroker, under /gb/v1
GB = {"listings": {}, "orders": [], "tokens": set(), "puts": [], "next_order": 7700000, "logins": 0}


def gb_seed():
    for gid, sku, qty, fixed, title in ((900001, "ACC-011", 5, True, "Shop accessory 11 on GunBroker"),
                                        (900002, "WW-TEE-M", 2, True, "Wojtek T-shirt M"),
                                        (900003, "IRV-10002", 3, True, "A feed item someone listed"),
                                        (900004, "PA195S201", 1, False, "Glock 19 auction"),
                                        (900005, "NOPE-1", 4, True, "Something not in the shop")):
        GB["listings"][gid] = {"itemID": gid, "sku": sku, "quantity": qty, "isFixedPrice": fixed, "title": title,
                               "fixedPrice": 24.5 if fixed else 0, "buyNowPrice": 0}


gb_seed()


def cat(i):
    return {"id": i, "name": CATS[i][0], "slug": CATS[i][1]}


def add(pid, name, sku, stock=5, gtin="", typ="simple", cats=(11,), manage=True, status="publish"):
    P[pid] = {"id": pid, "name": name, "type": typ, "status": status, "sku": sku, "global_unique_id": gtin,
              "stock_quantity": stock if manage else None, "manage_stock": manage,
              "categories": [cat(c) for c in cats], "parent_id": 0, "date_modified_gmt": tick(), "price": "19.99",
              "regular_price": "19.99", "description": f"<p>{name}, the long description.</p>", "short_description": "",
              "images": [{"id": 500 + pid % 400, "src": f"https://example.com/img/{pid}.jpg"}], "weight": "0.5",
              "dimensions": {"length": "1", "width": "1", "height": "1"}, "variations": []}


def addvar(pid, vid, opt, sku, stock=3, gtin="", manage=True):
    V.setdefault(pid, {})[vid] = {"id": vid, "sku": sku, "global_unique_id": gtin,
                                  "stock_quantity": stock if manage is True else None, "manage_stock": manage,
                                  "status": "publish", "attributes": [{"id": 1, "name": "Size", "option": opt}],
                                  "date_modified_gmt": tick(), "price": "24.99", "name": P[pid]["name"] + " - " + opt}
    P[pid]["variations"].append(vid)


def seed():
    i = 1000
    for n in range(150):                      # IRV-12345 style
        add(i, f"Irvine feed item {n}", f"IRV-{10000 + n}", 50, gtin=f"8{n:011d}" if n < 40 else "", cats=(99,)); i += 1
    for n in range(100):                      # ORI12345, no separator
        add(i, f"Orion feed item {n}", f"ORI{20000 + n}", 20, cats=(99, 10)); i += 1
    for n in range(50):                       # CSSI-77 style
        add(i, f"CSSI feed item {n}", f"CSSI-{n}", 7, cats=(99,)); i += 1
    add(1400, "Feed polo", "IRV-POLO", None, typ="variable", cats=(99, 12), manage=False)
    addvar(1400, 1401, "M", "IRV-POLO-M", 99)
    addvar(1400, 1402, "L", "IRV-POLO-L", 99)
    # in-shop products
    shop = [(2001, "Hornady 9mm 115gr FTX", "HORN-9MM-115", 12, "090255000001"),
            (2002, "SIG P365 10rd magazine", "SIG-P365-MAG10", 4, "798681000002"),
            (2003, "Wojtek coffee mug", "WW-MUG", 9, ""),
            (2004, "Gun oil 4oz", "LUBE-4OZ", 0, "0012345678905"),
            (2005, "Range bag", "BAG-RANGE", 2, ""),
            (2006, "Ear plugs 10 pack", "EAR-10", 30, "036000291452")]
    for pid, name, sku, st, g in shop:
        add(pid, name, sku, st, g, cats=(10,) if "9mm" in name else (11,))
    for n in range(20):
        add(2100 + n, f"Shop accessory {n}", f"ACC-{n:03d}", n % 7, cats=(11,))
    add(2950, "Magpul PMAG 30 AR/M4 GEN M3 Window 5.56x45 NATO / .223 Remington 30-Round Polymer Magazine, Black",
        "MAG570-BLK", 6, "873750008036")
    add(2201, "Wojtek patch (no SKU)", "", 15)
    add(2202, "Cleaning rod (no SKU)", "", 3)
    add(2203, "Target pasters (no SKU)", "", 40)
    add(2301, "Consignment knife, stock not tracked", "KNIFE-CONS", None, manage=False)
    add(2401, "Glock 19 Gen5", "PA195S201", 1, "764503000001", cats=(50,))
    add(2402, "Henry Big Boy", "H012", 2, "619835000002", cats=(51,))
    add(2500, "Wojtek T-shirt", "WW-TEE", None, typ="variable", cats=(12,), manage=False)
    addvar(2500, 2501, "S", "WW-TEE-S", 5, "123456789012")
    addvar(2500, 2502, "M", "WW-TEE-M", 8)
    addvar(2500, 2503, "L", "WW-TEE-L", 2)
    add(2600, "Holster", "HOL-1", 6, typ="variable", cats=(11,), manage=True)
    addvar(2600, 2601, "Right hand", "HOL-1-R", None, manage="parent")
    add(2700, "Gift box set", "GIFT-SET", None, typ="grouped", manage=False)
    add(2800, "Draft item", "DRAFT-1", 1, status="draft")
    add(2801, "Private in-store item", "PRIV-1", 3, status="private")


seed()
SEEDING[0] = False


def gtin_taken(code, not_id):
    if not code:
        return False
    k = code.lstrip("0")
    for x in P.values():
        if x["id"] != not_id and x["global_unique_id"] and x["global_unique_id"].lstrip("0") == k:
            return True
    for vs in V.values():
        for x in vs.values():
            if x["id"] != not_id and x["global_unique_id"] and x["global_unique_id"].lstrip("0") == k:
                return True
    return False


def sku_taken(sku, not_id):
    if not sku:
        return False
    for x in list(P.values()) + [v for vs in V.values() for v in vs.values()]:
        if x["id"] != not_id and x["sku"] == sku:
            return True
    return False


def rec_of(pid, vid):
    return V[pid][vid] if vid else P[pid]


def note(oid, text):
    NEXT["note"] += 1
    n = {"id": NEXT["note"], "note": text, "date_created_gmt": tick(), "customer_note": False}
    NOTES.setdefault(oid, []).append(n)
    return n


def make_order(lines, number=None, oid=None, status="processing", reduce=True, via="checkout", day=None):
    NEXT["order"] += 1
    oid = oid or NEXT["order"]
    items, changes = [], []
    for ln in lines:
        pid, vid, qty = int(ln["pid"]), int(ln.get("vid") or 0), int(ln.get("qty", 1))
        rec = rec_of(pid, vid)
        NEXT["line"] += 1
        items.append({"id": NEXT["line"], "product_id": pid, "variation_id": vid, "quantity": qty,
                      "name": P[pid]["name"] + (" - " + rec["attributes"][0]["option"] if vid else ""), "sku": rec["sku"],
                      "total": f"{float(ln.get('price', rec.get('price') or 0)) * qty:.2f}"})
        if reduce and rec["manage_stock"] is True:
            old = rec["stock_quantity"]
            rec["stock_quantity"] -= qty
            changes.append(f"{items[-1]['name']} ({rec['sku'] or '#' + str(rec['id'])}) {old}&rarr;{rec['stock_quantity']}")
    t = tick()
    o = {"id": oid, "number": str(number or oid), "status": status, "date_created_gmt": (day + "T15:00:00") if day else t,
         "date_modified_gmt": t, "created_via": via, "meta_data": [],
         "billing": {"first_name": "Jamie", "last_name": "Rivera"}, "line_items": items}
    ORDERS.append(o)
    if changes:
        note(oid, "Stock levels reduced: " + ", ".join(changes))
    return o


def refund(o, lines, restock):
    NEXT["refund"] += 1
    rl = []
    for ln in lines:
        pid, vid, qty = int(ln["pid"]), int(ln.get("vid") or 0), int(ln.get("qty", 1))
        rl.append({"product_id": pid, "variation_id": vid, "quantity": -qty})
        if restock:
            rec = rec_of(pid, vid)
            old = rec["stock_quantity"]
            rec["stock_quantity"] += qty
            note(o["id"], f"Item #{rec['id']} stock increased from {old} to {rec['stock_quantity']}.")
    REFUNDS.setdefault(o["id"], []).append({"id": NEXT["refund"], "line_items": rl})
    o["date_modified_gmt"] = tick()


def apply_put(rec, body):
    if "global_unique_id" in body:
        g = str(body["global_unique_id"])
        if gtin_taken(g, rec["id"]):
            return 400, {"code": "product_invalid_global_unique_id",
                         "message": "Invalid or duplicated GTIN, UPC, EAN or ISBN.", "data": {"status": 400}}
        rec["global_unique_id"] = g
    if "sku" in body:
        if sku_taken(body["sku"], rec["id"]):
            return 400, {"code": "product_invalid_sku", "message": "Invalid or duplicated SKU.", "data": {"status": 400}}
        rec["sku"] = body["sku"]
    if "stock_quantity" in body and rec["manage_stock"] is True:
        rec["stock_quantity"] = int(body["stock_quantity"])
    rec["date_modified_gmt"] = tick()
    return 200, rec


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj, headers=None):
        b = json.dumps(obj).encode() if not isinstance(obj, bytes) else obj
        self.send_response(code)
        self.send_header("Content-Type", "application/json" if not isinstance(obj, bytes) else "text/html")
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def authed(self, qs):
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic ") and not FLAGS["query_only"]:
            u, _, p = base64.b64decode(h[6:]).decode().partition(":")
            return u == KEY and p == SECRET
        return qs.get("consumer_key", [""])[0] == KEY and qs.get("consumer_secret", [""])[0] == SECRET

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        self.route("GET")

    def do_PUT(self):
        self.route("PUT")

    def do_POST(self):
        self.route("POST")

    def route(self, method):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        path = u.path
        with LOCK:
            if path.startswith("/mock/"):
                return self.mock(path, method, qs)
            if path.startswith("/gb/v1/"):
                return self.gunbroker(path[len("/gb/v1"):], method, qs)
            FLAGS["requests"] += 1
            if FLAGS["fail"]:
                f = FLAGS["fail"][0]
                if not f.get("method") or f["method"] == method:
                    FLAGS["fail"].pop(0)
                    if f["status"] == "html":
                        return self.send(200, b"<html><body>Just a moment...</body></html>")
                    return self.send(f["status"], {"code": "mock_fail", "message": "Mock failure"})
            if not path.startswith("/wp-json/wc/v3"):
                return self.send(404, {"code": "rest_no_route", "message": "No route"})
            if not self.authed(qs):
                return self.send(401, {"code": "woocommerce_rest_cannot_view", "message": "Sorry, you cannot list resources.",
                                       "data": {"status": 401}})
            p = path[len("/wp-json/wc/v3"):]
            if method in ("PUT", "POST") and FLAGS["readonly"]:
                return self.send(401, {"code": "woocommerce_rest_cannot_edit", "message": "Sorry, you are not allowed to edit this resource.",
                                       "data": {"status": 401}})
            return self.api(method, p, qs)

    def listing(self, items, qs):
        per = int(qs.get("per_page", ["10"])[0])
        page = int(qs.get("page", ["1"])[0])
        total = len(items)
        pages = max(1, -(-total // per))
        return self.send(200, items[(page - 1) * per: page * per], {"X-WP-Total": total, "X-WP-TotalPages": pages})

    def api(self, method, p, qs):
        if p == "/system_status":
            return self.send(200, {"environment": {"version": "10.2.1"}})
        if p == "/products" and method == "POST":   # a new product
            b = self.body()
            if b.get("sku") and sku_taken(b["sku"], 0):
                return self.send(400, {"code": "product_invalid_sku", "message": "Invalid or duplicated SKU.", "data": {"status": 400}})
            if b.get("global_unique_id") and gtin_taken(b["global_unique_id"], 0):
                return self.send(400, {"code": "product_invalid_global_unique_id", "message": "Invalid or duplicated GTIN, UPC, EAN or ISBN."})
            NEXT["product"] += 1
            pid = NEXT["product"]
            cats = [c["id"] for c in b.get("categories") or []]
            add(pid, b["name"], b.get("sku") or "", int(b.get("stock_quantity") or 0), b.get("global_unique_id") or "",
                cats=tuple(cats) or (11,), manage=bool(b.get("manage_stock")), status=b.get("status") or "publish")
            P[pid].update({k: b[k] for k in ("description", "short_description", "regular_price", "weight", "dimensions") if k in b})
            P[pid]["images"] = [{"id": i["id"], "src": f"https://example.com/media/{i['id']}.jpg"} for i in b.get("images") or []]
            P[pid]["created_with"] = b
            return self.send(201, P[pid])
        if p == "/products" and method == "GET":
            if FLAGS["delay"]:   # a slow store, so a test can watch the sync's progress
                time.sleep(FLAGS["delay"])
            items = sorted(P.values(), key=lambda x: x["id"])
            if FLAGS["truncate"]:
                items = items[:FLAGS["truncate"]]
            if "include" in qs:
                want = {int(x) for x in qs["include"][0].split(",") if x}
                items = [x for x in items if x["id"] in want]
            if "modified_after" in qs:
                ma = qs["modified_after"][0][:19]
                items = [x for x in items if x["date_modified_gmt"] > ma]
            st = qs.get("status", ["publish"])[0]
            if st != "any":
                items = [x for x in items if x["status"] == st]
            return self.listing(items, qs)
        m = re.fullmatch(r"/products/(\d+)", p)
        if m:
            pid = int(m.group(1))
            if pid not in P:
                return self.send(404, {"code": "woocommerce_rest_product_invalid_id", "message": "Invalid ID."})
            if method == "GET":
                return self.send(200, P[pid])
            if method == "PUT":
                return self.send(*apply_put(P[pid], self.body()))
        m = re.fullmatch(r"/products/(\d+)/variations", p)
        if m:
            pid = int(m.group(1))
            if pid not in P:
                return self.send(404, {"code": "woocommerce_rest_product_invalid_id", "message": "Invalid ID."})
            if FLAGS["delay"]:
                time.sleep(FLAGS["delay"])
            items = sorted(V.get(pid, {}).values(), key=lambda x: x["id"])
            if "include" in qs:
                want = {int(x) for x in qs["include"][0].split(",") if x}
                items = [x for x in items if x["id"] in want]
            return self.listing(items, qs)
        m = re.fullmatch(r"/products/(\d+)/variations/(\d+)", p)
        if m:
            pid, vid = int(m.group(1)), int(m.group(2))
            rec = V.get(pid, {}).get(vid)
            if not rec:
                return self.send(404, {"code": "woocommerce_rest_product_variation_invalid_id", "message": "Invalid ID."})
            if method == "GET":
                return self.send(200, rec)
            if method == "PUT":
                return self.send(*apply_put(rec, self.body()))
        if p == "/orders" and method == "POST":   # a new order (from GunBroker): lowers stock if FLAGS["order_reduces"]
            b = self.body()
            NEXT["order"] += 1
            oid = NEXT["order"]
            items = []
            for li in b.get("line_items") or []:
                pid, vid = int(li["product_id"]), int(li.get("variation_id") or 0)
                NEXT["line"] += 1
                rec = rec_of(pid, vid)
                items.append({"id": NEXT["line"], "product_id": pid, "variation_id": vid, "quantity": int(li["quantity"]),
                              "name": P[pid]["name"], "sku": rec["sku"], "total": li.get("total")})
                if FLAGS["order_reduces"] and rec["manage_stock"] is True:
                    rec["stock_quantity"] -= int(li["quantity"])
            t = tick()
            o = {"id": oid, "number": str(oid), "status": b.get("status", "pending"), "date_created_gmt": t, "date_modified_gmt": t,
                 "created_via": "rest-api",
                 "billing": b.get("billing") or {}, "shipping": b.get("shipping") or {}, "line_items": items,
                 "meta_data": b.get("meta_data") or [], "customer_note": b.get("customer_note", ""), "set_paid": b.get("set_paid")}
            ORDERS.append(o)
            return self.send(201, o)
        if p == "/orders" and method == "GET":
            items = ORDERS
            if "modified_after" in qs:
                ma = qs["modified_after"][0][:19]
                items = [o for o in items if o["date_modified_gmt"] > ma]
            if "after" in qs:
                af = qs["after"][0][:19]
                items = [o for o in items if o["date_created_gmt"] > af]
            if "search" in qs:
                t = qs["search"][0]
                items = [o for o in items if t in str(o["number"]) or t in str(o["id"])]
            return self.listing(items, qs)
        m = re.fullmatch(r"/orders/(\d+)(/notes|/refunds)?", p)
        if m:
            o = next((x for x in ORDERS if x["id"] == int(m.group(1))), None)
            if not o:
                return self.send(404, {"code": "woocommerce_rest_shop_order_invalid_id", "message": "Invalid ID."})
            if not m.group(2) and method == "GET":
                return self.send(200, o)
            if m.group(2) == "/refunds":
                return self.send(200, REFUNDS.get(o["id"], []))
            if m.group(2) == "/notes" and method == "GET":
                return self.send(200, list(reversed(NOTES.get(o["id"], []))))
            if m.group(2) == "/notes" and method == "POST":
                b = self.body()
                n = note(o["id"], b["note"])
                return self.send(201, n)
        return self.send(404, {"code": "rest_no_route", "message": "No route was found."})

    def gunbroker(self, p, method, qs):
        if self.headers.get("X-DevKey") != "dev-key-1":
            return self.send(401, {"userMessage": "Invalid developer key"})
        if p == "/Users/AccessToken" and method == "POST":
            b = self.body()
            if (b.get("Username"), b.get("Password")) != ("wojtekgb", "gb-pass"):
                return self.send(401, {"userMessage": "Invalid username or password"})
            GB["logins"] += 1
            t = f"tok-{GB['logins']}"
            GB["tokens"].add(t)
            return self.send(200, {"accessToken": t})
        if self.headers.get("X-AccessToken") not in GB["tokens"]:
            return self.send(401, {"userMessage": "Access token expired"})
        if p == "/ItemsSelling" and method == "GET":
            rows = sorted(GB["listings"].values(), key=lambda x: x["itemID"])
            return self.send(200, {"count": len(rows), "pageIndex": 1, "pageSize": 300, "results": rows})
        m = re.fullmatch(r"/Items/(\d+)", p)
        if m and method == "PUT":
            b, it = self.body(), GB["listings"].get(int(m.group(1)))
            if not it:
                return self.send(404, {"userMessage": "Item not found"})
            if "Quantity" in b:
                if int(b["Quantity"]) < 1:
                    return self.send(400, {"userMessage": "Quantity must be a positive number"})
                if not it["isFixedPrice"] and int(b["Quantity"]) != 1:
                    return self.send(400, {"userMessage": "Quantity must be 1 for auctions"})
                it["quantity"] = int(b["Quantity"])
            GB["puts"].append((it["itemID"], b))
            return self.send(200, {"userMessage": "Item updated"})
        if p == "/OrdersSold" and method == "GET":
            since = qs.get("OrdersModifiedSinceDate", [""])[0].rstrip("Z")[:19]
            rows = [o for o in GB["orders"] if o["lastModifiedDate"] > since]
            return self.send(200, {"count": len(rows), "results": rows})
        return self.send(404, {"userMessage": "No such route"})

    def mock(self, path, method, qs):
        b = self.body() if method == "POST" else {}
        if path == "/mock/sell":   # a sale: stock goes down and an order appears; the product's modified date doesn't move
            pid, vid = int(b["pid"]), int(b.get("vid") or 0)
            make_order([{"pid": pid, "vid": vid, "qty": int(b.get("qty", 1))}])
            return self.send(200, rec_of(pid, vid))
        if path == "/mock/order":  # {lines:[{pid,vid,qty,price?}], number?, id?, via?, day?, status?}
            return self.send(200, make_order(b["lines"], b.get("number"), b.get("id"), b.get("status", "processing"),
                                             via=b.get("via", "checkout"), day=b.get("day")))
        if path == "/mock/order_status":
            o = next(x for x in ORDERS if x["id"] == int(b["order"]))
            o["status"] = b["status"]
            o["date_modified_gmt"] = tick()
            return self.send(200, o)
        if path == "/mock/refund":  # {order, lines:[{pid,vid,qty}], restock}
            o = next(x for x in ORDERS if x["id"] == int(b["order"]))
            refund(o, b["lines"], bool(b.get("restock")))
            return self.send(200, {"ok": True})
        if path == "/mock/cancel":  # cancelled: WooCommerce puts every line back in stock, with one note
            o = next(x for x in ORDERS if x["id"] == int(b["order"]))
            o["status"] = "cancelled"
            ch = []
            for li in o["line_items"]:
                rec = rec_of(li["product_id"], li["variation_id"])
                if rec["manage_stock"] is True:
                    old = rec["stock_quantity"]
                    rec["stock_quantity"] += li["quantity"]
                    ch.append(f"{li['name']} ({rec['sku'] or '#' + str(rec['id'])}) {old}&rarr;{rec['stock_quantity']}")
            note(o["id"], "Stock levels increased: " + ", ".join(ch))
            o["date_modified_gmt"] = tick()
            return self.send(200, o)
        if path == "/mock/gb/sell":   # a GunBroker buyer purchases: listing quantity drops, an order appears
            it = GB["listings"][int(b["item"])]
            qty = int(b.get("qty", 1))
            it["quantity"] -= qty
            GB["next_order"] += 1
            o = {"orderID": GB["next_order"], "orderDateUTC": tick(), "lastModifiedDate": tick(), "orderCancelled": False,
                 "paymentReceived": True, "totalPrice": it["fixedPrice"] * qty + 9.95, "shipCost": 9.95,
                 "shipToName": "Pat Buyer", "shipToAddress1": "9 Elm St", "shipToCity": "Concord", "shipToState": "NH",
                 "shipToPostalCode": "03301", "shipToCountryCode": "US", "shipToEmail": "pat@buyer.example",
                 "orderItemsCollection": [{"itemID": it["itemID"], "sku": it["sku"], "quantity": qty, "itemPrice": it["fixedPrice"],
                                           "title": it["title"]} for it in [it]] + [
                     {"itemID": int(x["item"]), "sku": GB["listings"][int(x["item"])]["sku"], "quantity": int(x.get("qty", 1)),
                      "itemPrice": 10, "title": GB["listings"][int(x["item"])]["title"]} for x in b.get("also", [])]}
            GB["orders"].append(o)
            return self.send(200, o)
        if path == "/mock/gb/cancel":
            o = next(x for x in GB["orders"] if x["orderID"] == int(b["order"]))
            o["orderCancelled"] = True
            o["lastModifiedDate"] = tick()
            return self.send(200, o)
        if path == "/mock/gb/expire":
            GB["tokens"].clear()
            return self.send(200, {"ok": True})
        if path == "/mock/gb/state":
            return self.send(200, {"listings": GB["listings"], "puts": GB["puts"], "logins": GB["logins"], "orders": len(GB["orders"])})
        if path == "/mock/orders":
            return self.send(200, ORDERS)
        if path == "/mock/notes":
            return self.send(200, NOTES.get(int(qs["order"][0]), []))
        if path == "/mock/set":    # an edit in wp-admin: modified date moves
            pid, vid = int(b["pid"]), int(b.get("vid") or 0)
            rec = V[pid][vid] if vid else P[pid]
            rec.update(b["fields"])
            rec["date_modified_gmt"] = tick()
            return self.send(200, rec)
        if path == "/mock/set_quiet":   # a change that doesn't move the modified date (another plugin writing directly)
            pid, vid = int(b["pid"]), int(b.get("vid") or 0)
            rec = V[pid][vid] if vid else P[pid]
            rec.update(b["fields"])
            return self.send(200, rec)
        if path == "/mock/get":
            pid, vid = int(qs["pid"][0]), int(qs.get("vid", ["0"])[0])
            return self.send(200, V[pid][vid] if vid else P[pid])
        if path == "/mock/fail":
            FLAGS["fail"] += [{"status": b["status"], "method": b.get("method")} for _ in range(int(b.get("count", 1)))]
            return self.send(200, {"ok": True})
        if path == "/mock/flags":
            for k in ("readonly", "query_only", "truncate", "delay", "order_reduces"):
                if k in b:
                    FLAGS[k] = b[k]
            return self.send(200, {k: FLAGS[k] for k in ("readonly", "query_only", "truncate", "requests", "delay")})
        return self.send(404, {"error": "no mock route"})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9311
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"mock WooCommerce on {port}: {len(P)} products", flush=True)
    srv.serve_forever()
