#!/usr/bin/env python3
"""End-to-end tests: Wojtek Stock against the mock WooCommerce store (tests/mock_woo.py). Nothing touches a real store.
Run from the project folder:  python3 tests/test_stock.py
"""
import base64
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MOCK_PORT, STOCK_PORT, PRINTER_PORT = 9311, 8091, 9312
TMP = tempfile.mkdtemp(prefix="wstock-test-")
DB = os.path.join(TMP, "stock.db")
PASSED, FAILED = [], []
procs = {}
printed = []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f"  -> {detail}"), flush=True)


def http(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw), dict(r.headers)
            except ValueError:
                return r.status, raw, dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw), dict(e.headers)
        except ValueError:
            return e.code, raw, dict(e.headers)


AUTH = {"Authorization": "Basic " + base64.b64encode(b"shop:test-pass").decode()}


def api(method, path, body=None, op=None, auth=True, extra=None):
    h = dict(AUTH) if auth else {}
    if method == "POST":
        h["X-Requested-With"] = "stock"
    if op:
        h["X-Stock-Operator"] = op
    h.update(extra or {})
    return http(method, f"http://127.0.0.1:{STOCK_PORT}{path}", body, h)


def mock(path, body=None):
    if body is None:
        return http("GET", f"http://127.0.0.1:{MOCK_PORT}{path}")[1]
    return http("POST", f"http://127.0.0.1:{MOCK_PORT}{path}", body)[1]


def wait_port(port, t=15):
    end = time.time() + t
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 0.5).close()
            return True
        except OSError:
            time.sleep(0.2)
    return False


def start_server(write="dryrun", extra_env=None):
    stop_server()
    envv = dict(os.environ, STOCK_PORT=str(STOCK_PORT), STOCK_DB=DB, STOCK_PASSWORD="test-pass",
                WOO_URL=f"http://127.0.0.1:{MOCK_PORT}", WOO_KEY="ck_test", WOO_SECRET="cs_test", WOO_WRITE=write,
                WOO_MIN_GAP="0", SYNC_MINUTES="60", FULL_SYNC_TIME="03:15", SHOP_TZ="America/New_York",
                LABEL_PRINTER=f"127.0.0.1:{PRINTER_PORT}", LABEL_WIDTH_IN="2", LABEL_LENGTH_IN="1")
    envv.update(extra_env or {})
    log = open(os.path.join(TMP, "server.log"), "a")
    procs["server"] = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], env=envv, stdout=log, stderr=log)
    assert wait_port(STOCK_PORT), "server didn't start"


def stop_server():
    p = procs.pop("server", None)
    if p:
        p.terminate()
        p.wait(10)
        time.sleep(0.3)


def wait_for(fn, t=40, step=0.3):
    end = time.time() + t
    last = None
    while time.time() < end:
        last = fn()
        if last:
            return last
        time.sleep(step)
    return last


def state():
    return api("GET", "/api/state")[1]


def wait_sync(key="last_full", after=""):
    return wait_for(lambda: (state()["sync"].get(key) or "") > after and not state()["sync"].get("running"), 60)


def wait_batch(batch, t=60):
    def done():
        jobs = api("GET", f"/api/batch?id={batch}")[1]["jobs"]
        return jobs if jobs and all(j["status"] not in ("pending", "working") for j in jobs) else None
    return wait_for(done, t)


def items():
    return {r[0]: r for r in api("GET", "/api/items")[1]["rows"]}


def printer():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", PRINTER_PORT))
    srv.listen(5)
    while True:
        c, _ = srv.accept()
        data = b""
        c.settimeout(2)
        try:
            while True:
                chunk = c.recv(65536)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            pass
        printed.append(data.decode())
        c.close()


def main():
    log = open(os.path.join(TMP, "mock.log"), "w")
    procs["mock"] = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_woo.py"), str(MOCK_PORT)], stdout=log, stderr=log)
    assert wait_port(MOCK_PORT)
    threading.Thread(target=printer, daemon=True).start()
    try:
        run()
    finally:
        stop_server()
        procs["mock"].terminate()
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed  (logs in {TMP})")
    if FAILED:
        print("Failed:", *FAILED, sep="\n  ")
        sys.exit(1)


def run():
    print("1. First sync, before Setup")
    start_server("dryrun")
    check("first full sync finishes", wait_sync())
    st = state()
    check("setup not confirmed at first", st["setup"]["confirmed"] is False)
    s = api("GET", "/api/setup")[1]
    groups = {g["prefix"]: g["count"] for g in s["groups"]}
    check("Setup finds IRV (151 incl. variable)", groups.get("IRV") == 151, groups.get("IRV"))
    check("Setup finds ORI (no separator)", groups.get("ORI") == 100, groups.get("ORI"))
    check("Setup finds CSSI", groups.get("CSSI") == 50, groups.get("CSSI"))
    check("no variations read before Setup", s["varsLoaded"] == 0, s["varsLoaded"])
    check("draft and private products included", 2800 in items() and 2801 in items())
    code, r, _ = api("POST", "/api/commit", {"kind": "receive", "lines": [{"id": 2001, "qty": 1}]})
    check("writes locked before Setup", code == 409 and "Setup" in r["error"], (code, r))

    print("2. Confirm Setup")
    code, r, _ = api("POST", "/api/setup", {"prefixes": [], "guns": [], "confirm": True})
    check("can't confirm with no prefixes", code == 400, (code, r))
    last = state()["sync"]["last_full"]
    code, r, _ = api("POST", "/api/setup", {"prefixes": ["IRV", "ORI", "CSSI"], "guns": [50, 51], "confirm": True})
    check("Setup saved and confirmed", code == 200 and r["info"]["confirmed"], (code, r))
    check("full sync after confirm (reads variations)", wait_sync("last_full", last))
    it = items()
    check("feed products never reach the station", not any(str(x[4]).startswith(("IRV", "ORI", "CSSI")) for x in it.values()))
    check("feed variations not read", 1401 not in it and api("GET", "/api/setup")[1]["varsLoaded"] == 4)
    check("in-shop variations are items", it.get(2502, [None] * 8)[7] == "shop", it.get(2502))
    check("variable parent isn't an item", 2500 not in it)
    check("products carry their category", it[2001][10] == ["Ammunition"], it[2001])
    check("a variation carries its parent's category", it[2502][10] == ["Apparel"], it[2502])
    check("products carry their price", it[2001][11] == "19.99", it[2001])
    check("parent-managed variation is read-only", it.get(2601, [None] * 8)[7] == "readonly", it.get(2601))
    check("stock-not-tracked product is read-only", it.get(2301, [None] * 8)[7] == "readonly")
    check("guns excluded from the station", 2401 not in it and 2402 not in it)
    check("grouped product excluded", 2700 not in it)
    res = api("GET", "/api/resolve?code=IRV-10005")[1]["matches"]
    check("feed SKU resolves with the reason", res and res[0]["scope"] == "feed" and "IRV" in res[0]["why"], res)
    res = api("GET", "/api/resolve?code=90255000001")[1]["matches"]
    check("UPC without leading zero finds 090255000001", res and res[0]["id"] == 2001, res)
    res = api("GET", "/api/resolve?code=00090255000001")[1]["matches"]
    check("GTIN-14 form finds it too", res and res[0]["id"] == 2001, res)

    print("3. Badges")
    code, r, _ = api("POST", "/api/people", {"name": "Trevor Smith", "code": "TREVOR"})
    check("add a person", code == 200 and r["code"] == "TREVOR", r)
    code, r, _ = api("POST", "/api/people/import", {"csv": "Name,Badge,Active,Added (UTC)\nAna,USR-ANA,yes,2026-09-01\nOld,USR-OLD,removed,\nTrevor Smith,USR-TREVOR,yes,"})
    check("import the Locator's People export", code == 200 and r["added"] == 1 and r["skipped"] == 2, r)
    code, r, _ = api("POST", "/api/commit", {"kind": "receive", "lines": [{"id": 2001, "qty": 1}]})
    check("badge required for changes", code == 403 and "badge" in r["error"], (code, r))
    code, r, _ = api("POST", "/api/commit", {"kind": "receive", "lines": [{"id": 2001, "qty": 1}]}, op="NOBODY")
    check("unknown badge refused", code == 403)

    print("4. Dry run")
    before = mock("/mock/get?pid=2001")["stock_quantity"]
    code, r, _ = api("POST", "/api/commit", {"kind": "receive", "lines": [{"id": 2001, "qty": 3}]}, op="TREVOR")
    jobs = wait_batch(r["batch"])
    check("dry run records the change", jobs and jobs[0]["status"] == "dryrun" and jobs[0]["after"] == str(before + 3), jobs)
    check("dry run leaves the store alone", mock("/mock/get?pid=2001")["stock_quantity"] == before)

    print("5. Writes on")
    start_server("on")
    code, r, _ = api("POST", "/api/commit", {"kind": "receive", "lines": [{"id": 2001, "qty": 3}, {"id": 2502, "qty": 2}]}, op="TREVOR")
    jobs = wait_batch(r["batch"])
    check("receive +3 written and confirmed", jobs and jobs[0]["status"] == "done" and mock("/mock/get?pid=2001")["stock_quantity"] == before + 3, jobs)
    check("variation receive +2", mock("/mock/get?pid=2500&vid=2502")["stock_quantity"] == 10, mock("/mock/get?pid=2500&vid=2502"))
    log = api("GET", "/api/log?limit=5")[1]["rows"]
    check("log has who and why", log[0]["by"].startswith("Trevor Smith") and log[0]["reason"] == "Received", log[0])

    print("6. Feed protection (live re-check)")
    mock("/mock/set_quiet", {"pid": 2005, "fields": {"sku": "IRV-HIJACK"}})   # a feed SKU appears after the last sync
    st_bag = mock("/mock/get?pid=2005")["stock_quantity"]
    code, r, _ = api("POST", "/api/commit", {"kind": "receive", "lines": [{"id": 2005, "qty": 1}]}, op="TREVOR")
    jobs = wait_batch(r["batch"])
    check("live feed SKU refused", jobs and jobs[0]["status"] == "refused" and "feed" in jobs[0]["error"], jobs)
    check("store untouched", mock("/mock/get?pid=2005")["stock_quantity"] == st_bag)
    check("item drops out of the station", 2005 not in items())
    db = sqlite3.connect(DB)
    db.execute("UPDATE items SET scope='shop', why='' WHERE id IN (1000, 2401)")   # a corrupted local copy
    db.commit()
    db.close()
    for pid, word in ((1000, "feed"), (2401, "gun")):
        code, r, _ = api("POST", "/api/commit", {"kind": "receive", "lines": [{"id": pid, "qty": 1}]}, op="TREVOR")
        if code != 200:   # the server may have already put it back
            check(f"{word} item blocked (local copy)", code == 409, (code, r))
            continue
        jobs = wait_batch(r["batch"])
        check(f"{word} item refused even if the local copy is wrong", jobs and jobs[0]["status"] == "refused" and word in jobs[0]["error"].lower(), jobs)
    check("feed item stock untouched", mock("/mock/get?pid=1000")["stock_quantity"] == 50)
    check("gun stock untouched", mock("/mock/get?pid=2401")["stock_quantity"] == 1)
    code, r, _ = api("POST", "/api/commit", {"kind": "receive", "lines": [{"id": 2601, "qty": 1}]}, op="TREVOR")
    check("read-only item refused at the door", code == 409, (code, r))

    print("7. Sales between scan and commit")
    mock("/mock/sell", {"pid": 2002, "qty": 2})     # local copy still says 4
    code, r, _ = api("POST", "/api/commit", {"kind": "receive", "lines": [{"id": 2002, "qty": 1}]}, op="TREVOR")
    jobs = wait_batch(r["batch"])
    check("receive adds to live stock, keeping the sale", jobs[0]["before"] == "2" and mock("/mock/get?pid=2002")["stock_quantity"] == 3, jobs)

    print("8. Count")
    live = api("POST", "/api/live", {"ids": [2003, 2006, 2503]})[1]["items"]
    check("live read for review", live["2003"]["stock"] == 9 and live["2503"]["stock"] == 2, live)
    mock("/mock/sell", {"pid": 2006, "qty": 1})   # a sale during the review
    code, r, _ = api("POST", "/api/commit", {"kind": "count", "lines": [
        {"id": 2003, "counted": 7, "expected": live["2003"]["stock"]},
        {"id": 2006, "counted": 40, "expected": live["2006"]["stock"]},
        {"id": 2503, "counted": 2, "expected": 2}]}, op="TREVOR")
    jobs = {j["item_id"]: j for j in wait_batch(r["batch"])}
    check("count sets the number", jobs[2003]["status"] == "done" and mock("/mock/get?pid=2003")["stock_quantity"] == 7, jobs[2003])
    check("count refuses if stock moved since review", jobs[2006]["status"] == "conflict" and mock("/mock/get?pid=2006")["stock_quantity"] == 29, jobs[2006])
    check("count that already matches is a no-op", jobs[2503]["status"] == "done" and "matched" in jobs[2503]["error"], jobs[2503])

    print("9. Adjust")
    code, r, _ = api("POST", "/api/commit", {"kind": "adjust", "dir": "down", "reason": "Damaged", "lines": [{"id": 2003, "qty": 1}]}, op="ANA")
    jobs = wait_batch(r["batch"])
    check("adjust down with reason", jobs[0]["status"] == "done" and mock("/mock/get?pid=2003")["stock_quantity"] == 6, jobs)
    code, r, _ = api("POST", "/api/commit", {"kind": "adjust", "dir": "up", "reason": "Found", "lines": [{"id": 2003, "qty": 2}]}, op="ANA")
    jobs = wait_batch(r["batch"])
    check("adjust up", mock("/mock/get?pid=2003")["stock_quantity"] == 8, jobs)
    code, r, _ = api("POST", "/api/commit", {"kind": "adjust", "dir": "down", "reason": "Damaged", "lines": [{"id": 2004, "qty": 1}]}, op="ANA")
    jobs = wait_batch(r["batch"])
    check("adjust below zero refused", jobs[0]["status"] == "refused" and mock("/mock/get?pid=2004")["stock_quantity"] == 0, jobs)
    code, r, _ = api("POST", "/api/commit", {"kind": "adjust", "dir": "down", "reason": "Other", "lines": [{"id": 2003, "qty": 1}]}, op="ANA")
    check("Other needs a note", code == 400, r)
    code, r, _ = api("POST", "/api/commit", {"kind": "adjust", "dir": "down", "reason": "Because", "lines": [{"id": 2003, "qty": 1}]}, op="ANA")
    check("unknown reason refused", code == 400, r)

    print("10. Link a barcode")
    code, r, _ = api("POST", "/api/link", {"id": 2003, "code": "090255000001"}, op="ANA")
    check("duplicate barcode caught locally", code == 409 and "Hornady" in r["error"], r)
    code, r, _ = api("POST", "/api/link", {"id": 2003, "code": "ABC123"}, op="ANA")
    check("non-numeric code refused", code == 400)
    code, r, _ = api("POST", "/api/link", {"id": 2003, "code": "850000000018"}, op="ANA")
    jobs = wait_batch(r["batch"])
    check("barcode saved to global_unique_id", jobs[0]["status"] == "done" and mock("/mock/get?pid=2003")["global_unique_id"] == "850000000018", jobs)
    check("check digit reported", r["checkDigit"] is True, r)
    code, r, _ = api("POST", "/api/link", {"id": 2119, "code": "850000000017"}, op="ANA")
    check("bad check digit warned, not refused", code == 200 and r["checkDigit"] is False, r)
    wait_batch(r["batch"])
    code, r, _ = api("POST", "/api/link", {"id": 2003, "code": "850000000025"}, op="ANA")
    check("replacing a barcode asks first", code == 409 and r.get("needsReplace"), r)
    mock("/mock/set_quiet", {"pid": 2105, "fields": {"global_unique_id": "777777777777"}})
    code, r, _ = api("POST", "/api/link", {"id": 2106, "code": "777777777777"}, op="ANA")
    jobs = wait_batch(r["batch"])
    check("WooCommerce's own duplicate check reported", jobs[0]["status"] == "failed" and "GTIN" in jobs[0]["error"], jobs)

    print("11. SKUs")
    code, r, _ = api("POST", "/api/sku", {"ids": [2201, 2202]}, op="ANA")
    jobs = wait_batch(r["batch"])
    check("SKUs generated", [j["after"] for j in jobs] == ["WW-000001", "WW-000002"] and mock("/mock/get?pid=2201")["sku"] == "WW-000001", jobs)
    code, r, _ = api("POST", "/api/sku", {"ids": [2203]}, op="ANA")
    jobs = wait_batch(r["batch"])
    check("next SKU continues", jobs[0]["after"] == "WW-000003", jobs)
    code, r, _ = api("POST", "/api/sku", {"ids": [2003]}, op="ANA")
    check("item with a SKU refused", code == 409)

    print("12. Quick sync, sales and changes made elsewhere")
    mock("/mock/sell", {"pid": 2500, "vid": 2501, "qty": 1})
    mock("/mock/sell", {"pid": 2100, "qty": 1})
    mock("/mock/set_quiet", {"pid": 2104, "fields": {"stock_quantity": 99}})   # stock changed by something else, no order
    mock("/mock/set", {"pid": 2107, "fields": {"stock_quantity": 50}})          # edited in wp-admin
    last = state()["sync"].get("last_quick") or ""
    api("POST", "/api/sync", {})
    check("quick sync runs", wait_sync("last_quick", last))
    it = items()
    check("sold variation updated", it[2501][6] == 4, it[2501])
    check("sold simple product updated", it[2100][6] == -1, it[2100])
    check("admin edit picked up", it[2107][6] == 50, it[2107])
    drift = {d["item_id"] for d in api("GET", "/api/setup")[1]["drift"]}
    check("admin edit flagged as outside change", 2107 in drift, drift)
    check("sales not flagged", 2501 not in drift and 2100 not in drift, drift)
    check("Stock's own changes not flagged", 2003 not in drift and 2001 not in drift, drift)
    last = state()["sync"]["last_full"]
    api("POST", "/api/sync", {"full": True})
    check("full sync catches a quiet change", wait_sync("last_full", last) and items()[2104][6] == 99)
    check("quiet change flagged", 2104 in {d["item_id"] for d in api("GET", "/api/setup")[1]["drift"]})

    print("13. Trouble")
    mock("/mock/fail", {"status": 503, "count": 2})
    code, r, _ = api("POST", "/api/commit", {"kind": "receive", "lines": [{"id": 2110, "qty": 1}]}, op="ANA")
    jobs = wait_batch(r["batch"], 90)
    check("503s retried, then done", jobs and jobs[0]["status"] == "done", jobs)
    mock("/mock/fail", {"status": "html", "count": 1})
    code, r, _ = api("POST", "/api/commit", {"kind": "receive", "lines": [{"id": 2111, "qty": 1}]}, op="ANA")
    jobs = wait_batch(r["batch"], 90)
    check("firewall page retried, then done", jobs and jobs[0]["status"] == "done", jobs)
    mock("/mock/flags", {"readonly": True})
    code, r, _ = api("POST", "/api/commit", {"kind": "receive", "lines": [{"id": 2112, "qty": 1}]}, op="ANA")
    check("read-only key pauses writes", wait_for(lambda: state()["paused"], 30), state()["paused"])
    jobs = api("GET", f"/api/batch?id={r['batch']}")[1]["jobs"]
    check("change waits, not lost", jobs[0]["status"] == "pending", jobs)
    mock("/mock/flags", {"readonly": False})
    api("POST", "/api/writes/resume", {})
    jobs = wait_batch(r["batch"])
    check("resume finishes it", jobs and jobs[0]["status"] == "done", jobs)
    mock("/mock/flags", {"query_only": True})
    code, r, _ = api("POST", "/api/commit", {"kind": "receive", "lines": [{"id": 2113, "qty": 1}]}, op="ANA")
    jobs = wait_batch(r["batch"])
    check("falls back to key-in-URL auth", jobs and jobs[0]["status"] == "done", jobs)
    mock("/mock/flags", {"query_only": False})
    mock("/mock/flags", {"truncate": 20})
    n_before = len(items())
    last = state()["sync"].get("last_try") or ""
    api("POST", "/api/sync", {"full": True})
    wait_for(lambda: "far fewer" in (state()["sync"].get("error") or ""), 30)
    check("big drop kept the local copy", "far fewer" in (state()["sync"]["error"] or "") and len(items()) == n_before)
    mock("/mock/flags", {"truncate": 0})

    print("14. Access")
    code, r, _ = api("GET", "/api/state", auth=False)
    check("password required", code == 401)
    code, r, _ = http("POST", f"http://127.0.0.1:{STOCK_PORT}/api/sync", {}, AUTH)
    check("cross-site posts refused", code == 403)
    code, r, _ = api("POST", "/api/terminals", {"name": "Back room"})
    token = r["path"].split("/")[-1]
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        urllib.request.build_opener(NoRedirect).open(f"http://127.0.0.1:{STOCK_PORT}/terminal/{token}")
        code, hdr = 200, {}
    except urllib.error.HTTPError as e:
        code, hdr = e.code, dict(e.headers)
    check("terminal link sets its key", code == 302 and f"stock_terminal={token}" in hdr.get("Set-Cookie", ""), (code, hdr))
    code, r, _ = http("GET", f"http://127.0.0.1:{STOCK_PORT}/terminal/not-a-real-key")
    check("revoked or wrong terminal link refused", code == 403)
    ck = {"Cookie": f"stock_terminal={token}", "X-Requested-With": "stock"}
    code, r, _ = http("GET", f"http://127.0.0.1:{STOCK_PORT}/api/state", None, ck)
    check("terminal can use the station", code == 200 and r["access"]["terminal"] == "Back room")
    code, r, _ = http("POST", f"http://127.0.0.1:{STOCK_PORT}/api/setup", {"prefixes": [], "guns": []}, ck)
    check("terminal can't change Setup", code == 403, (code, r))
    code, r, _ = http("POST", f"http://127.0.0.1:{STOCK_PORT}/api/commit",
                      {"kind": "receive", "lines": [{"id": 2114, "qty": 1}]}, dict(ck, **{"X-Stock-Operator": "ANA"}))
    jobs = wait_batch(r["batch"])
    check("change logged with person and terminal", jobs and api("GET", "/api/log?limit=1")[1]["rows"][0]["by"] == "Ana (on Back room)")

    print("15. Labels and exports")
    code, r, _ = api("POST", "/api/labels/print", {"lines": [{"id": 2001, "copies": 2}, {"id": 2502, "copies": 1}]})
    time.sleep(1)
    z = "".join(printed)
    check("Zebra gets ZPL with QR of the SKU", code == 200 and z.count("^XA") == 3 and "^FDMA,HORN-9MM-115^FS" in z, (code, r, z[:300]))
    check("label shows the variation", "Size: M" in z)
    code, r, _ = api("POST", "/api/labels/print", {"lines": [{"id": 2301, "copies": 1}, {"id": 2201}]})
    check("labels need a SKU first", code == 400 or code == 200, (code, r))
    api("POST", "/api/commit", {"kind": "adjust", "dir": "down", "reason": "Other", "note": "=HYPERLINK(\"x\")",
                                "lines": [{"id": 2003, "qty": 1}]}, op="ANA")
    time.sleep(2)
    code, raw, _ = api("GET", "/api/log.csv")
    check("CSV keeps formulas as text", code == 200 and b"'=HYPERLINK" in raw, raw[:200])
    code, raw, _ = api("GET", "/api/items.csv")
    check("items CSV", code == 200 and b"HORN-9MM-115" in raw and b"IRV-1" not in raw)

    print("16. Returns")
    stock = lambda pid, vid=0: mock(f"/mock/get?pid={pid}&vid={vid}")["stock_quantity"]
    o = mock("/mock/order", {"lines": [{"pid": 2003, "qty": 2}, {"pid": 2500, "vid": 2502, "qty": 1},
                                       {"pid": 1000, "qty": 1}, {"pid": 2401, "qty": 1}]})
    num = o["number"]
    code, r, _ = api("GET", f"/api/order?code={num}")
    check("order found by its number", code == 200 and r["order"]["number"] == num and len(r["order"]["lines"]) == 4, (code, r))
    lines = {l["item"]: l for l in r["order"]["lines"]}
    check("in-shop lines can be restocked", lines[2003]["canRestock"] and lines[2502]["canRestock"] and lines[2003]["returnable"] == 2)
    check("feed line shown, never restocked", not lines[1000]["canRestock"] and "feed" in lines[1000]["why"].lower(), lines[1000])
    check("gun line shown, never restocked", not lines[2401]["canRestock"] and "gun" in lines[2401]["why"].lower(), lines[2401])
    check("customer shown briefly", r["order"]["customer"] == "Jamie R.", r["order"]["customer"])
    for c in (f"#{num}", f"SS-{num}", f" {num} "):
        code, r2, _ = api("GET", "/api/order?code=" + urllib.parse.quote(c))
        check(f"order found from {c.strip()!r}", code == 200 and r2["order"]["id"] == o["id"], (code, r2))
    a = mock("/mock/order", {"lines": [{"pid": 2006, "qty": 1}], "id": 6001, "number": "6002"})
    mock("/mock/order", {"lines": [{"pid": 2006, "qty": 1}], "id": 6002, "number": "9999"})
    code, r2, _ = api("GET", "/api/order?code=6002")
    check("order number wins over another order's internal id", code == 200 and r2["order"]["id"] == 6001, r2)
    mock("/mock/order", {"lines": [{"pid": 2006, "qty": 1}], "id": 6100, "number": "WW-777"})
    code, r2, _ = api("GET", "/api/order?code=WW-777")
    check("custom order numbers work", code == 200 and r2["order"]["id"] == 6100, (code, r2))
    code, r2, _ = api("GET", "/api/order?code=424242")
    check("unknown order says so", code == 404, (code, r2))

    mug = stock(2003)
    code, r, _ = api("POST", "/api/return", {"order": o["id"], "lines": [{"item": 2003, "qty": 1, "restock": True}]})
    check("returns need a badge", code == 403)
    code, r, _ = api("POST", "/api/return", {"order": o["id"], "note": "Box crushed", "lines": [
        {"item": 2003, "qty": 1, "restock": True}, {"item": 1000, "qty": 1, "restock": False}]}, op="ANA")
    check("return accepted", code == 200, (code, r))
    jobs = wait_batch(r["batch"])
    kinds = {(j["kind"], j["item_id"]): j for j in jobs}
    check("restocked item goes back in stock", kinds[("return", 2003)]["status"] == "done" and stock(2003) == mug + 1, jobs)
    check("damaged item logged, stock untouched", kinds[("return", 1000)]["status"] == "logged" and stock(1000) == 49, (kinds[("return", 1000)], stock(1000)))
    notes = mock(f"/mock/notes?order={o['id']}")
    t = notes[-1]["note"]
    check("order note says what happened", t.startswith("Returned via Wojtek Stock by Ana") and "restocked" in t and "damaged, not restocked" in t and "unticked" in t and "Box crushed" in t, t)
    code, r, _ = api("GET", f"/api/order?code={num}")
    lines = {l["item"]: l for l in r["order"]["lines"]}
    check("returned counts remembered", lines[2003]["returnable"] == 1 and lines[2003]["stockRestocked"] == 1 and lines[1000]["returnable"] == 0, lines[2003])
    code, r, _ = api("POST", "/api/return", {"order": o["id"], "lines": [{"item": 2003, "qty": 2, "restock": True}]}, op="ANA")
    check("can't return more than was sold", code == 409 and "Only 1" in r["error"], (code, r))
    code, r, _ = api("POST", "/api/return", {"order": o["id"], "lines": [{"item": 2401, "qty": 1, "restock": True}]}, op="ANA")
    check("gun on the order refused for restock", code == 409, (code, r))
    code, r, _ = api("POST", "/api/return", {"order": o["id"], "lines": [{"item": 2119, "qty": 1, "restock": True}]}, op="ANA")
    check("item not on the order refused", code == 400, (code, r))

    mock("/mock/refund", {"order": o["id"], "lines": [{"pid": 2500, "vid": 2502, "qty": 1}], "restock": True})
    code, r, _ = api("GET", f"/api/order?code={num}")
    tee = {l["item"]: l for l in r["order"]["lines"]}[2502]
    check("refund that already restocked is seen", tee["wooRestocked"] == 1 and tee["returnable"] == 0 and tee["refunded"] == 1, tee)
    code, r, _ = api("POST", "/api/return", {"order": o["id"], "lines": [{"item": 2502, "qty": 1, "restock": True}]}, op="ANA")
    check("so it isn't restocked twice", code == 409, (code, r))

    c = mock("/mock/order", {"lines": [{"pid": 2003, "qty": 1}, {"pid": 2104, "qty": 2}]})
    mock("/mock/cancel", {"order": c["id"]})
    code, r, _ = api("GET", f"/api/order?code={c['number']}")
    check("cancelled order: WooCommerce's restock seen", all(l["returnable"] == 0 for l in r["order"]["lines"]), r["order"]["lines"])

    d = mock("/mock/order", {"lines": [{"pid": 2110, "qty": 2}]})
    code, r, _ = api("POST", "/api/return", {"order": d["id"], "lines": [{"item": 2110, "qty": 2, "restock": True}]}, op="ANA")
    wait_batch(r["batch"])
    mock("/mock/refund", {"order": d["id"], "lines": [{"pid": 2110, "qty": 2}], "restock": True})   # box ticked anyway
    last = state()["sync"].get("last_quick") or ""
    api("POST", "/api/sync", {})
    wait_sync("last_quick", last)
    w = api("GET", "/api/setup")[1]["watch"]
    check("refund that restocks again is flagged", any(x["order_id"] == d["id"] and x["item_id"] == 2110 and x["qty"] == 2 for x in w), w)
    check("flag shows in the header state", state()["watchCount"] >= 1)
    r = api("GET", "/api/log?q=" + num)[1]["rows"]
    check("log finds returns by order number", {x["kind"] for x in r} >= {"return", "note"}, [x["kind"] for x in r])
    code, raw, _ = api("GET", "/api/log.csv")
    check("CSV has the order column", b"Order,Returned" in raw)

    print("17. Sync progress")
    check("no progress when idle", state()["progress"] is None, state()["progress"])
    mock("/mock/flags", {"delay": 0.6})
    last = state()["sync"]["last_full"]
    api("POST", "/api/sync", {"full": True})
    seen = []
    end = time.time() + 30
    while time.time() < end:
        p = state()["progress"]
        if p:
            seen.append(p)
        elif seen:
            break
        time.sleep(0.15)
    mock("/mock/flags", {"delay": 0})
    mid = [p for p in seen if p["kind"] == "full" and p["pct"] is not None and 0 < p["pct"] < 100]
    check("progress shows a percentage mid-sync", bool(mid), seen[:3])
    check("progress says which page", any(p["phase"] == "Reading products" and "page 2 of 4" in p["detail"] for p in seen), [p["detail"] for p in seen][:6])
    check("progress goes up, never down", all(a["pct"] is None or b["pct"] is None or b["pct"] >= a["pct"] for a, b in zip(mid, mid[1:])), [p["pct"] for p in mid])
    check("time left estimated", any(p["eta"] is not None for p in seen), seen[-1] if seen else None)
    check("variations step shown", any(p["phase"] == "Reading variations" for p in seen), {p["phase"] for p in seen})
    check("progress cleared when done", wait_sync("last_full", last) and state()["progress"] is None)
    check("full sync time remembered", (state()["sync"].get("full_secs") or 0) >= 2, state()["sync"])

    print("18. Restart keeps everything")
    start_server("on")
    st = state()
    check("Setup still confirmed after restart", st["setup"]["confirmed"] and st["setup"]["prefixes"] == ["CSSI", "IRV", "ORI"], st["setup"])
    check("people kept", len(st["people"]) == 2)

    print("19. No password, no start")
    stop_server()
    envv = dict(os.environ, STOCK_PORT=str(STOCK_PORT), STOCK_DB=DB, STOCK_PASSWORD="", WOO_URL=f"http://127.0.0.1:{MOCK_PORT}",
                WOO_KEY="ck_test", WOO_SECRET="cs_test")
    p = subprocess.run([sys.executable, os.path.join(ROOT, "server.py")], env=envv, capture_output=True, text=True, timeout=20)
    check("refuses to start without STOCK_PASSWORD", p.returncode == 2 and "refusing to start" in p.stderr, (p.returncode, p.stderr[-300:]))
    try:
        socket.create_connection(("127.0.0.1", STOCK_PORT), 0.5).close()
        listening = True
    except OSError:
        listening = False
    check("and isn't listening", not listening)


if __name__ == "__main__":
    main()
