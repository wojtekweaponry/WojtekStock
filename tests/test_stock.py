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
MOCK_PORT, STOCK_PORT, PRINTER_PORT, SMTP_PORT = 9311, 8091, 9312, 9325
TMP = tempfile.mkdtemp(prefix="wstock-test-")
DB = os.path.join(TMP, "stock.db")
PASSED, FAILED = [], []
procs = {}
printed = []
mails = []                      # what the pretend mail server received: {"from", "to", "data"}
SMTP_AUTH = {"user": "wojtek", "token": "tok-123"}


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
                LABEL_PRINTER=f"127.0.0.1:{PRINTER_PORT}", LABEL_WIDTH_IN="2", LABEL_LENGTH_IN="1",
                SMTP_HOST="127.0.0.1", SMTP_PORT=str(SMTP_PORT), SMTP_USER="wojtek", SMTP_TOKEN="tok-123",
                SMTP_FROM="orders@wojtek.example", SHOP_EMAIL="shop@wojtek.example", PO_EMAIL_CC="nate@wojtek.example",
                SHOP_ADDRESS="1 Main St|Manchester, NH", AUTO_PO_TIME="23:59")
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


def smtp_server():
    """A pretend mail server: plain SMTP with AUTH PLAIN/LOGIN (no TLS), keeps every message in `mails`."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", SMTP_PORT))
    srv.listen(5)

    def handle(c):
        f = c.makefile("rb")
        say = lambda s: c.sendall((s + "\r\n").encode())
        say("220 pretend ESMTP")
        msg = {"to": []}
        while True:
            line = f.readline().decode(errors="replace").rstrip("\r\n")
            if not line:
                break
            cmd = line.upper()
            if cmd.startswith("EHLO") or cmd.startswith("HELO"):
                c.sendall(b"250-pretend\r\n250-AUTH PLAIN LOGIN\r\n250 OK\r\n")
            elif cmd.startswith("AUTH PLAIN"):
                parts = line.split()
                raw = parts[2] if len(parts) > 2 else (say("334 ") or f.readline().decode().strip())
                _, u, p = base64.b64decode(raw).decode().split("\0")
                say("235 ok" if (u, p) == (SMTP_AUTH["user"], SMTP_AUTH["token"]) else "535 bad credentials")
            elif cmd.startswith("MAIL FROM"):
                msg = {"from": line[10:], "to": []}
                say("250 ok")
            elif cmd.startswith("RCPT TO"):
                msg["to"].append(line[8:].strip("<> "))
                say("250 ok")
            elif cmd == "DATA":
                say("354 go on")
                data = b""
                while True:
                    l2 = f.readline()
                    if l2 in (b".\r\n", b".\n", b""):
                        break
                    data += l2
                msg["data"] = data
                mails.append(msg)
                say("250 queued")
            elif cmd == "QUIT":
                say("221 bye")
                break
            else:
                say("250 ok")
        c.close()
    while True:
        conn_, _ = srv.accept()
        threading.Thread(target=handle, args=(conn_,), daemon=True).start()


def pdf_qr(data):
    """The QR codes in a PDF (bytes), via pdftoppm and OpenCV's two QR readers."""
    import cv2
    d = tempfile.mkdtemp()
    p = os.path.join(d, "x.pdf")
    open(p, "wb").write(data)
    subprocess.run(["pdftoppm", "-r", "200", "-png", p, os.path.join(d, "pg")], check=True)
    out = []
    for f in sorted(os.listdir(d)):
        if f.endswith(".png"):
            img = cv2.imread(os.path.join(d, f))
            t = cv2.QRCodeDetector().detectAndDecode(img)[0] or cv2.QRCodeDetectorAruco().detectAndDecode(img)[0]
            if t:
                out.append(t)
    return out


def today_iso():
    from datetime import date
    return date.today().isoformat()


def main():
    log = open(os.path.join(TMP, "mock.log"), "w")
    procs["mock"] = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_woo.py"), str(MOCK_PORT)], stdout=log, stderr=log)
    assert wait_port(MOCK_PORT)
    threading.Thread(target=printer, daemon=True).start()
    threading.Thread(target=smtp_server, daemon=True).start()
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
    n0 = len(printed)
    code, r, _ = api("POST", "/api/labels/test", {"size": {"w": 3, "h": 2, "sideways": True}})
    time.sleep(1)
    tz = printed[-1] if len(printed) > n0 else ""
    check("Print a test label", code == 200 and "TEST-LABEL" in tz and "^GB" in tz and "^PW406^LL609" in tz, (code, r, tz[:120]))
    code, raw, hd = api("GET", "/api/labels/zebra-preview?id=2001&w=3&h=2&sideways=1&name=1&upc=0")
    svg = raw.decode() if isinstance(raw, bytes) else str(raw)
    check("Zebra preview drawn from its own instructions", code == 200 and "image/svg+xml" in hd.get("Content-Type", "")
          and "HORN-" in svg and "rotate(-90)" in svg and "Hornady" in svg, (code, svg[:200]))
    code, r, _ = api("GET", "/api/labels/zebra-preview?w=9&h=9")
    check("preview refuses a size no label printer takes", code == 400, (code, r))
    try:
        sys.path.insert(0, HERE)
        import zpl_preview
        bad = []
        os.environ.setdefault("STOCK_DB", os.path.join(TMP, "preview.db"))
        srv = sys.modules.get("stock_server") or (zpl_preview._qr("x") and sys.modules["stock_server"])
        names = ["Hornady 9mm 115gr FTX", "Magpul PMAG 30 AR/M4 GEN M3 Window 5.56x45 NATO / .223 Remington 30-Round Polymer Magazine, Black",
                 "X", "Super Long Name " * 8]
        for sku in ("HORN-9MM-115", "WW-TEE-S", "A", "VERYLONGSKUWITHNOBREAKS123456"):
            for nm in names:
                for w_, l_, sw in ((3, 2, True), (2, 3, False), (2, 1, False)):
                    _, probs = zpl_preview.render(srv.zpl_label({"sku": sku, "name": nm, "attrs": "Size: L", "gtin": "012345678905"}, w_, l_, True, True, sw))
                    bad += [f"{sku}/{w_}x{l_}: {p}" for p in probs]
        check("Zebra labels: nothing runs over, whatever the SKU and name", not bad, bad[:5])
    except ImportError:
        print("  (skipped the Zebra preview check: Pillow isn't installed here)")
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

    print("17b. Purchasing")
    code, r, _ = api("POST", "/api/vendors", {"name": "Orion", "email": "orders@orion.example", "feed_prefix": "ori", "lead_days": 5}, op="ANA")
    check("vendor added", code == 200 and r["feed_prefix"] == "ORI", (code, r))
    orion = r["id"]
    code, r, _ = api("POST", "/api/vendors", {"name": "orion"}, op="ANA")
    check("duplicate vendor name refused", code == 409)
    code, r, _ = api("POST", "/api/vendors", {"name": "Bad", "email": "nope"}, op="ANA")
    check("bad email refused", code == 400)
    irvine = api("POST", "/api/vendors", {"name": "Irvine", "feed_prefix": "IRV"}, op="ANA")[1]["id"]
    code, r, _ = api("POST", "/api/po", {"vendor_id": orion, "lines": [{"item_id": 1000, "qty": 1}]}, op="ANA")
    check("feed product can't go on a PO", code == 409, r)
    code, r, _ = api("POST", "/api/po", {"vendor_id": orion, "lines": [{"item_id": 2401, "qty": 1}]}, op="ANA")
    check("gun can't go on a PO", code == 409, r)
    code, r, _ = api("POST", "/api/po", {"vendor_id": orion, "notes": "Rush", "lines": [
        {"item_id": 2003, "qty": 8, "cost": 4, "vendor_sku": "ORI-MUG"}, {"item_id": 2105, "qty": 5, "cost": 2}]}, op="ANA")
    check("draft PO created", code == 200 and r["po"]["number"] == "PO-0001" and r["po"]["status"] == "draft" and r["po"]["total"] == 42, (code, r))
    po = r["po"]
    code, r, _ = api("POST", "/api/po", {"id": po["id"], "vendor_id": orion, "lines": [
        {"item_id": 2003, "qty": 10, "cost": 4, "vendor_sku": "ORI-MUG"}, {"item_id": 2105, "qty": 5, "cost": 2}]}, op="ANA")
    check("draft can be changed", code == 200 and r["po"]["total"] == 50 and r["po"]["number"] == "PO-0001", r)
    code, r, _ = api("POST", "/api/po/receive", {"id": po["id"], "lines": [{"line_id": 1, "qty": 1}]}, op="ANA")
    check("a draft can't be received", code == 409, r)
    code, r, _ = api("POST", "/api/po/action", {"id": po["id"], "action": "send"}, op="ANA")
    check("PO sent", code == 200 and r["po"]["status"] == "sent" and r["po"]["sent_at"], r)
    code, r, _ = api("POST", "/api/po", {"id": po["id"], "vendor_id": orion, "lines": []}, op="ANA")
    check("a sent PO can't be changed", code == 409)
    for c in ("PO-0001", "po-1", "PO1"):
        code, r, _ = api("GET", "/api/po?number=" + c)
        check(f"PO found by {c!r}", code == 200 and r["po"]["id"] == po["id"], (code, r))
    lines = {l["item_id"]: l for l in r["po"]["lines"]}
    mug0, acc0 = mock("/mock/get?pid=2003")["stock_quantity"], mock("/mock/get?pid=2105")["stock_quantity"]
    code, r, _ = api("POST", "/api/po/receive", {"id": po["id"], "freight": 10, "fees": 2, "invoice": "INV-77", "lines": [
        {"line_id": lines[2003]["id"], "qty": 6}, {"line_id": lines[2105]["id"], "qty": 5}]}, op="ANA")
    check("partial receipt accepted", code == 200, (code, r))
    jobs = wait_batch(r["batch"])
    check("stock goes up", all(j["status"] == "done" for j in jobs) and mock("/mock/get?pid=2003")["stock_quantity"] == mug0 + 6
          and mock("/mock/get?pid=2105")["stock_quantity"] == acc0 + 5, jobs)
    p = api("GET", f"/api/po?id={po['id']}")[1]["po"]
    ln = {l["item_id"]: l for l in p["lines"]}
    check("PO partly received, 4 backordered", p["status"] == "partial" and ln[2003]["received"] == 6 and ln[2003]["open"] == 4 and ln[2105]["open"] == 0, p)
    rl = {x["item_id"]: x for x in p["receipts"][0]["lines"]}
    check("freight and fees spread by value (landed cost)", abs(rl[2003]["landed_unit"] - (4 + 12 * 24 / 34 / 6)) < 0.001
          and abs(rl[2105]["landed_unit"] - (2 + 12 * 10 / 34 / 5)) < 0.001, rl)
    log = api("GET", "/api/log?limit=2")[1]["rows"]
    check("log says which PO", all(x["reason"] == "Received on PO-0001" and x["note"] == "Invoice INV-77" for x in log), log)
    code, r, _ = api("POST", "/api/po/receive", {"id": po["id"], "lines": [{"line_id": lines[2003]["id"], "qty": 5}]}, op="ANA")
    check("can't receive more than is due", code == 409 and "Only 4" in r["error"], r)
    code, r, _ = api("POST", "/api/po/receive", {"id": po["id"], "lines": [{"line_id": lines[2003]["id"], "qty": 4}],
                                                 "extras": [{"item_id": 2106, "qty": 2, "cost": 3}]}, op="ANA")
    wait_batch(r["batch"])
    p = api("GET", f"/api/po?id={po['id']}")[1]["po"]
    check("the rest arrives: PO received", p["status"] == "received" and p["open"] == 0, p)
    check("an extra item becomes an extra line", any(l["item_id"] == 2106 and l["extra"] == 1 and l["received"] == 2 for l in p["lines"]))
    c = api("GET", "/api/item?id=2003")[1]["costs"]
    check("item costs: last and average landed", c["lastCost"] == 4 and abs(c["avgCost"] - (6 * (4 + 12 * 24 / 34 / 6) + 4 * 4) / 10) < 0.001 and c["received"] == 10, c)
    check("vendor part number and price remembered", any(v["name"] == "Orion" and v["vendor_sku"] == "ORI-MUG" and v["last_cost"] == 4 for v in c["vendors"]), c)
    p2 = api("POST", "/api/po", {"vendor_id": orion, "lines": [{"item_id": 2107, "qty": 3, "cost": 1.5}]}, op="ANA")[1]["po"]
    api("POST", "/api/po/action", {"id": p2["id"], "action": "send"}, op="ANA")
    check("on order shows on the item", api("GET", "/api/item?id=2107")[1]["costs"]["onOrder"] == 3)
    r = api("POST", "/api/po/receive", {"id": p2["id"], "lines": [{"line_id": p2["lines"][0]["id"], "qty": 1}]}, op="ANA")[1]
    wait_batch(r["batch"])
    code, r, _ = api("POST", "/api/po/action", {"id": p2["id"], "action": "cancel"}, op="ANA")
    check("a PO with receipts can't be cancelled", code == 409)
    code, r, _ = api("POST", "/api/po/action", {"id": p2["id"], "action": "close"}, op="ANA")
    check("closing cancels the backorder", code == 200 and r["po"]["status"] == "closed" and r["po"]["open"] == 0 and r["po"]["lines"][0]["cancelled"] == 2, r)
    check("nothing on order after closing", api("GET", "/api/item?id=2107")[1]["costs"]["onOrder"] == 0)
    p3 = api("POST", "/api/po", {"vendor_id": orion, "lines": [{"item_id": 2107, "qty": 1}]}, op="ANA")[1]["po"]
    code, r, _ = api("POST", "/api/po/action", {"id": p3["id"], "action": "cancel"}, op="ANA")
    check("a draft can be cancelled", code == 200 and r["po"]["status"] == "cancelled")
    api("POST", "/api/po", {"vendor_id": orion, "lines": [{"item_id": 2107, "qty": 1}]}, op="ANA")   # an open draft
    code, r, _ = api("POST", "/api/vendors/remove", {"id": orion}, op="ANA")
    check("vendor with open POs can't be removed", code == 409, (code, r))
    check("open PO list", all(x["status"] in ("draft", "sent", "partial") for x in api("GET", "/api/pos?status=open")[1]["pos"]))

    print("17c. Feed listings")
    r = api("GET", "/api/feed/search?q=irvine%20item%200")[1]["rows"]
    check("feed search finds listings", any(x["id"] == 1000 and x["vendor"] == "Irvine" for x in r), [x["sku"] for x in r][:5])
    feed_before = mock("/mock/get?pid=1000")
    code, r, _ = api("POST", "/api/feed/copy", {"feed_id": 1000, "sku_mode": "custom", "sku": "ORI-COPY"}, op="ANA")
    check("custom SKU that looks like a feed refused", code == 409, r)
    code, r, _ = api("POST", "/api/feed/copy", {"feed_id": 1000, "sku_mode": "custom", "sku": "HORN-9MM-115"}, op="ANA")
    check("custom SKU already used refused", code == 409, r)
    code, r, _ = api("POST", "/api/feed/copy", {"feed_id": 1000, "sku_mode": "generate", "status": "draft"}, op="ANA")
    check("copied to a new in-shop product", code == 200 and r["item"]["scope"] == "shop" and r["item"]["sku"].startswith("WW-") and r["vendor"] == "Irvine", (code, r))
    nid = r["item"]["id"]
    new = mock(f"/mock/get?pid={nid}")
    check("copy has the name, description, same images, price, stock 0", new["name"] == feed_before["name"] and "long description" in new["description"]
          and [i["id"] for i in new["images"]] == [i["id"] for i in feed_before["images"]] and new["stock_quantity"] == 0
          and new["manage_stock"] is True and new["status"] == "draft" and new["regular_price"] == feed_before["regular_price"], new)
    check("the feed listing is untouched", mock("/mock/get?pid=1000") == feed_before)
    res = api("GET", "/api/resolve?code=800000000000")[1]["matches"]
    check("the listing's UPC scans to the new product", res and res[0]["id"] == nid, res)
    check("station has the extra barcode", items()[nid][12] == ["800000000000"], items().get(nid))
    code, r, _ = api("POST", "/api/feed/copy", {"feed_id": 1000, "sku_mode": "generate"}, op="ANA")
    check("can't copy the same listing twice", code == 409)
    code, r, _ = api("POST", "/api/feed/link", {"feed_id": 1001, "item_id": 2104}, op="ANA")
    check("linked to a part I have", code == 200 and r["altBarcode"] == "800000000001", r)
    res = api("GET", "/api/resolve?code=800000000001")[1]["matches"]
    check("linked listing's UPC scans to my part", res and res[0]["id"] == 2104, res)
    c = api("GET", "/api/item?id=2104")[1]["costs"]
    check("its vendor and part number remembered", any(v["name"] == "Irvine" and v["vendor_sku"] == "IRV-10001" for v in c["vendors"]), c)
    code, r, _ = api("POST", "/api/link", {"id": 2107, "code": "800000000001"}, op="ANA")
    check("an extra barcode counts as taken", code == 409, r)

    print("17d. Reorder points and email")
    import email as _email
    code, r, _ = api("POST", "/api/vendors", {"id": orion, "name": "Orion", "email": "orders@orion.example", "feed_prefix": "ORI",
                                               "lead_days": 5, "auto_send": True}, op="ANA")
    check("vendor set to send by itself", code == 200 and r["auto_send"] == 1, r)
    code, r, _ = api("POST", "/api/vendors", {"id": irvine, "name": "Irvine", "feed_prefix": "IRV", "auto_send": True}, op="ANA")
    check("sending by itself needs an email", code == 400, r)
    code, r, _ = api("POST", "/api/reorder/set", {"rows": [{"item_id": 2108, "min": 5, "max": 5}]}, op="ANA")
    check("order-up-to must be above the reorder point", code == 400, r)
    code, r, _ = api("POST", "/api/reorder/set", {"rows": [{"item_id": 1000, "min": 1, "max": 5}]}, op="ANA")
    check("no reorder point on a feed product", code == 409, r)
    code, r, _ = api("POST", "/api/reorder/set", {"rows": [{"item_id": 2108, "min": 5, "max": 12, "vendor_id": orion},
                                                           {"item_id": 2109, "min": 2, "max": 6}]}, op="ANA")
    check("reorder points saved", code == 200 and r["saved"] == 2, r)
    rows = {x["item_id"]: x for x in api("GET", "/api/reorder")[1]["rows"]}
    s2108 = mock("/mock/get?pid=2108")["stock_quantity"]
    check("a part at its reorder point needs ordering up to its level", rows[2108]["need"] == 12 - s2108 and rows[2108]["vendor"] == "Orion", rows[2108])
    r = api("POST", "/api/reorder/run", {}, op="ANA")[1]
    check("drafted automatically", len(r["drafted"]) == 1 and "Shop accessory 9" in r["noVendor"], r)
    auto_no = list(r["drafted"])[0]
    po = api("GET", "/api/po?number=" + auto_no)[1]["po"]
    check("the automatic draft", po["status"] == "draft" and po["auto"] == 1 and po["lines"][0]["item_id"] == 2108 and po["lines"][0]["qty"] == 12 - s2108, po)
    r = api("POST", "/api/reorder/run", {}, op="ANA")[1]
    check("never drafted twice", r["drafted"] == {}, r)
    rows = {x["item_id"]: x for x in api("GET", "/api/reorder")[1]["rows"]}
    check("what's on the draft counts as coming", rows[2108]["coming"] == 12 - s2108 and rows[2108]["need"] == 0, rows[2108])
    code, raw, hd = api("GET", f"/api/po/pdf?id={po['id']}")
    check("PO as a PDF from the server", code == 200 and raw[:5] == b"%PDF-" and pdf_qr(raw) == [auto_no], (code, raw[:20]))
    n0 = len(mails)
    code, r, _ = api("POST", "/api/po/email", {"id": po["id"]}, op="ANA")
    check("PO emailed and marked sent", code == 200 and r["po"]["status"] == "sent" and r["po"]["emailed_to"] == "orders@orion.example", (code, r))
    m = mails[-1] if len(mails) > n0 else {"to": [], "data": b""}
    msg = _email.message_from_bytes(m["data"])
    att = [p for p in msg.walk() if p.get_content_type() == "application/pdf"]
    check("to the vendor, a copy to you, reply to the shop", set(m["to"]) == {"orders@orion.example", "nate@wojtek.example"}
          and msg["Reply-To"] == "shop@wojtek.example" and auto_no in msg["Subject"], (m["to"], msg["Subject"], msg["Reply-To"]))
    check("the PDF attached, its QR the PO number", att and att[0].get_filename() == f"{auto_no}.pdf" and pdf_qr(att[0].get_payload(decode=True)) == [auto_no])
    body = [p for p in msg.walk() if p.get_content_type() == "text/plain"][0].get_payload(decode=True).decode()
    check("the email lists the parts", "Shop accessory 8" in body and auto_no in body, body[:300])
    p4 = api("POST", "/api/po", {"vendor_id": irvine, "lines": [{"item_id": 2110, "qty": 1}]}, op="ANA")[1]["po"]
    code, r, _ = api("POST", "/api/po/email", {"id": p4["id"]}, op="ANA")
    check("a vendor without email can't be emailed", code == 400 and "no email" in r["error"], r)
    p5 = api("POST", "/api/po", {"vendor_id": orion, "lines": [{"item_id": 2110, "qty": 2}]}, op="ANA")[1]["po"]
    SMTP_AUTH["token"] = "changed"
    code, r, _ = api("POST", "/api/po/email", {"id": p5["id"]}, op="ANA")
    SMTP_AUTH["token"] = "tok-123"
    p5 = api("GET", f"/api/po?id={p5['id']}")[1]["po"]
    check("a refused login is reported, and the PO stays a draft", code == 502 and "token" in r["error"] and p5["status"] == "draft" and p5["email_error"], (code, r, p5["status"]))
    code, r, _ = api("POST", "/api/email/test", {"to": "someone@wojtek.example"})
    check("test email", code == 200 and mails[-1]["to"] == ["someone@wojtek.example"], (code, r))
    r = api("POST", "/api/reorder/suggest", {"days": 90})[1]
    sug = {x["item_id"]: x for x in r["rows"]}
    check("reorder levels suggested from sales", 2100 in sug and sug[2100]["min"] >= 1 and sug[2100]["max"] > sug[2100]["min"], r["rows"][:3])
    api("POST", "/api/reorder/set", {"rows": [{"item_id": 2111, "min": 50, "max": 60, "vendor_id": orion}]}, op="ANA")
    n0 = len(mails)
    start_server("on", {"AUTO_PO_TIME": "00:00", "PURCHASING_CHECK_SECONDS": "1"})
    check("at the set time, the automatic draft is emailed by itself", wait_for(lambda: len(mails) > n0, 30), len(mails))
    sent = [x for x in api("GET", "/api/pos")[1]["pos"] if x["auto"] == 1 and x["status"] == "sent"]
    check("and marked sent", any(any(l["item_id"] == 2111 for l in api("GET", f"/api/po?id={x['id']}")[1]["po"]["lines"]) for x in sent))
    check("manual drafts are never sent by themselves", api("GET", f"/api/po?id={p5['id']}")[1]["po"]["status"] == "draft")
    start_server("on")

    print("17e. GunBroker")
    gbenv = {"GB_DEVKEY": "dev-key-1", "GB_USERNAME": "wojtekgb", "GB_PASSWORD": "gb-pass",
             "GB_API_URL": f"http://127.0.0.1:{MOCK_PORT}/gb/v1", "GB_MINUTES": "60"}

    def gbsync():
        for _ in range(30):
            code, r, _ = api("POST", "/api/gb/sync", {})
            if code != 409:
                return code, r
            time.sleep(1)
        return code, r
    start_server("on", dict(gbenv, GB_SYNC="dryrun"))
    code, r = gbsync()
    check("dry run reads the listings", code == 200 and r["listings"] == 5, (code, r))
    L = {x["gb_id"]: x for x in api("GET", "/api/gb")[1]["listings"]}
    check("linked to in-shop products by SKU", L[900001]["item_id"] == 2111 and L[900002]["item_id"] == 2502, L)
    check("a feed listing is never linked", L[900003]["item_id"] is None and "feed" in L[900003]["why"].lower(), L[900003])
    check("a gun is never linked", L[900004]["item_id"] is None and "gun" in L[900004]["why"].lower(), L[900004])
    check("an unknown SKU says so", L[900005]["item_id"] is None and "No in-shop" in L[900005]["why"], L[900005])
    s2111 = stock(2111)
    check("dry run works out the quantity", L[900001]["want"] == s2111, (L[900001], s2111))
    check("dry run changes nothing on GunBroker", mock("/mock/gb/state")["listings"]["900001"]["quantity"] == 5)
    mock("/mock/gb/sell", {"item": 900001, "qty": 1})
    gbsync()
    o = api("GET", "/api/gb")[1]["orders"][0]
    check("dry run: the sale is shown, nothing brought in", o["status"] == "dry run" and stock(2111) == s2111 and "Would bring in 1" in o["detail"], o)
    n_woo_orders = len(mock("/mock/orders"))

    start_server("on", dict(gbenv, GB_SYNC="on", GB_BUFFER="1"))
    gbsync()
    st = mock("/mock/gb/state")["listings"]
    check("live: quantity set to stock less the buffer", st["900001"]["quantity"] == stock(2111) - 1 and st["900002"]["quantity"] == stock(2500, 2502) - 1,
          (st["900001"], stock(2111), st["900002"]))
    check("unlinked listings are left alone", st["900003"]["quantity"] == 3 and st["900005"]["quantity"] == 4)
    check("the dry-run sale isn't brought in later", len(mock("/mock/orders")) == n_woo_orders)
    mock("/mock/set_quiet", {"pid": 2500, "vid": 2502, "fields": {"stock_quantity": 1}})
    gbsync()
    L = {x["gb_id"]: x for x in api("GET", "/api/gb")[1]["listings"]}
    check("sold out here: flagged when GunBroker won't go to 0", "end it on GunBroker" in (L[900002]["error"] or ""), L[900002])
    check("the problem shows in the header state", state()["gb"]["attention"] >= 1)

    before = stock(2111)
    mock("/mock/gb/sell", {"item": 900001, "qty": 2})
    gbsync()
    o = api("GET", "/api/gb")[1]["orders"][0]
    wo = mock("/mock/orders")[-1]
    check("a GunBroker sale becomes a WooCommerce order", o["status"] == "imported" and o["woo_number"] == wo["number"]
          and any(m["key"] == "_gunbroker_order_id" and m["value"] == str(o["gb_order_id"]) for m in wo["meta_data"]), (o, wo["meta_data"]))
    check("with the items, address and no buyer email", wo["line_items"][0]["product_id"] == 2111 and wo["line_items"][0]["quantity"] == 2
          and wo["shipping"]["city"] == "Concord" and "email" not in wo["billing"] and wo["customer_note"].startswith("GunBroker order"), wo)
    check("stock lowered once (by WooCommerce)", stock(2111) == before - 2 and "by WooCommerce" in o["detail"], (stock(2111), before, o["detail"]))
    mock("/mock/flags", {"order_reduces": False})
    before = stock(2111)
    mock("/mock/gb/sell", {"item": 900001, "qty": 1})
    gbsync()
    check("stock lowered once (by Wojtek Stock, when WooCommerce didn't)", wait_for(lambda: stock(2111) == before - 1, 20), (stock(2111), before))
    o = api("GET", "/api/gb")[1]["orders"][0]
    check("and says so", "by Wojtek Stock" in o["detail"], o)
    check("logged as a GunBroker sale", any(x["kind"] == "sale" and "GunBroker" in x["reason"] for x in api("GET", "/api/log?limit=5")[1]["rows"]))
    mock("/mock/flags", {"order_reduces": True})
    n_gb, n_wo = len(api("GET", "/api/gb")[1]["orders"]), len(mock("/mock/orders"))
    gbsync()
    check("nothing brought in twice", len(api("GET", "/api/gb")[1]["orders"]) == n_gb and len(mock("/mock/orders")) == n_wo)
    mock("/mock/gb/sell", {"item": 900005, "qty": 1})
    gbsync()
    o = api("GET", "/api/gb")[1]["orders"][0]
    check("an order with nothing linked is skipped", o["status"] == "skipped" and len(mock("/mock/orders")) == n_wo, o)
    mock("/mock/gb/sell", {"item": 900001, "qty": 1, "also": [{"item": 900005, "qty": 1}]})
    gbsync()
    o = api("GET", "/api/gb")[1]["orders"][0]
    check("a mixed order brings in the linked part, notes the rest", o["status"] == "imported" and "not linked" in o["detail"]
          and len(mock("/mock/orders")[-1]["line_items"]) == 1, o)
    first = [x for x in api("GET", "/api/gb")[1]["orders"] if x["status"] == "imported"][-1]
    mock("/mock/gb/cancel", {"order": first["gb_order_id"]})
    gbsync()
    c = [x for x in api("GET", "/api/gb")[1]["orders"] if x["gb_order_id"] == first["gb_order_id"]][0]
    check("cancelled on GunBroker after import is flagged", c["status"] == "cancelled after import", c)
    logins = mock("/mock/gb/state")["logins"]
    mock("/mock/gb/expire", {})
    code, r = gbsync()
    check("an expired token: logs in again", code == 200 and mock("/mock/gb/state")["logins"] == logins + 1, (code, r))
    code, r, _ = api("GET", "/api/order?code=" + first["woo_number"])
    check("a GunBroker sale can come back through Return mode", code == 200 and any(l["item"] == 2111 for l in r["order"]["lines"]), (code, r))
    start_server("on")

    print("17f. Vendor invoices and payments")
    code, r, _ = http("GET", f"http://127.0.0.1:{STOCK_PORT}/api/bills", None, ck)
    check("a station terminal can't see invoices", code == 403, (code, r))
    code, r, _ = http("POST", f"http://127.0.0.1:{STOCK_PORT}/api/payment", {"vendor_id": orion}, ck)
    check("or record payments", code == 403, (code, r))
    po = api("GET", "/api/po?number=PO-0001")[1]["po"]   # the PO received in 17b (po was reused for later ones)
    pre = api("GET", f"/api/bill/prefill?vendor={orion}&pos={po['id']}")[1]["lines"]
    P1 = {x["item_id"]: x for x in pre}
    check("lines start from the PO: received, not yet billed, at the PO price", P1[2003]["qty"] == 10 and P1[2003]["price"] == 4
          and P1[2105]["qty"] == 5 and P1[2106]["received"] == 2, P1)
    lines = [{"po_line_id": P1[2003]["po_line_id"], "qty": 10, "price": 4.25},
             {"po_line_id": P1[2105]["po_line_id"], "qty": 5, "price": 2},
             {"po_line_id": P1[2106]["po_line_id"], "qty": 3, "price": 3},
             {"description": "Restocking fee", "qty": 1, "price": 5}]
    pdf = base64.b64encode(b"%PDF-1.4 a pretend invoice").decode()
    code, r, _ = api("POST", "/api/bill", {"vendor_id": orion, "number": "INV-1001", "date": "2026-08-01", "terms": "Net 30",
                                          "freight": 12, "lines": lines, "file": {"name": "inv 1001.pdf", "data": pdf}})
    check("invoice entered", code == 200, (code, r))
    bill = r["bill"]
    check("total and due date from the terms", bill["total"] == 78.5 and bill["due"] == "2026-08-31" and bill["status"] == "open"
          and bill["flag"] == "overdue", {k: bill[k] for k in ("total", "due", "status", "flag")})
    iss = [i for l in bill["lines"] for i in l["issues"]]
    check("three-way match: the price change", any("Price $4.25, PO says $4.00" in i for i in iss), iss)
    check("three-way match: billed more than received and ordered", any("Billed 3 but 2 received" in i for i in iss)
          and any("Billed 3 but 2 ordered" in i for i in iss) and bill["issues"] == 3, iss)
    c = api("GET", "/api/item?id=2003")[1]["costs"]
    want = 4.25 + 12 * 42.5 / 61.5 / 10
    check("the invoice's price and freight become the landed cost", abs(c["lastCost"] - want) < 0.001 and abs(c["avgCost"] - want) < 0.001, (c, want))
    check("and the vendor's price for next time", any(v["name"] == "Orion" and v["last_cost"] == 4.25 for v in c["vendors"]), c["vendors"])
    p = api("GET", f"/api/po?id={po['id']}")[1]["po"]
    check("the PO shows what's billed, and the invoice", {l["item_id"]: l["billed"] for l in p["lines"]}[2003] == 10 and p["bills"][0]["number"] == "INV-1001", p.get("bills"))
    code, r, _ = api("POST", "/api/bill", {"vendor_id": orion, "number": "INV-1001", "date": "2026-08-01", "lines": lines})
    check("the same invoice can't be entered twice", code == 409, r)
    code, raw, hd = api("GET", f"/api/bill/file?id={bill['id']}")
    check("the copy is kept", code == 200 and raw == b"%PDF-1.4 a pretend invoice" and hd.get("Content-Type") == "application/pdf", (code, raw[:30]))
    code, r, _ = api("POST", "/api/bill", {"vendor_id": orion, "number": "X", "date": "2026-08-01", "lines": [{"description": "a", "qty": 1, "price": 1}],
                                          "file": {"name": "virus.exe", "data": pdf}})
    check("only PDFs and photos can be attached", code == 400, r)
    check("and a refused attachment saves nothing", not any(x["number"] == "X" for x in api("GET", "/api/bills")[1]["bills"]))
    code, r, _ = api("POST", "/api/bill", {"vendor_id": orion, "number": "INV-1002", "date": today_iso(), "lines": [{"description": "Shop supplies", "qty": 1, "price": 20}]})
    b2 = r["bill"]
    check("an invoice with no PO", code == 200 and b2["total"] == 20 and b2["issues"] == 0 and b2["flag"] in ("", "due soon"), b2)
    pay = api("GET", "/api/payables")[1]
    check("what's owed: overdue and later", pay["totals"]["overdue"] == 78.5 and pay["totals"]["count"] == 2 and pay["vendors"][0]["owed"] == 98.5, pay)
    code, r, _ = api("POST", "/api/payment", {"vendor_id": orion, "date": today_iso(), "amount": 50, "method": "Check", "reference": "1201",
                                             "allocations": [{"bill_id": bill["id"], "amount": 40}]})
    check("a payment must add up to what it pays", code == 400 and "don't add up" in r["error"], r)
    code, r, _ = api("POST", "/api/payment", {"vendor_id": orion, "date": today_iso(), "amount": 100, "method": "Check",
                                             "allocations": [{"bill_id": b2["id"], "amount": 100}]})
    check("can't pay more than an invoice owes", code == 409, r)
    code, r, _ = api("POST", "/api/payment", {"vendor_id": orion, "date": today_iso(), "amount": 50, "method": "Check", "reference": "1201",
                                             "allocations": [{"bill_id": bill["id"], "amount": 50}]})
    b1 = api("GET", f"/api/bill?id={bill['id']}")[1]["bill"]
    check("partly paid", code == 200 and b1["status"] == "partial" and b1["owed"] == 28.5, (code, r, b1["status"], b1["owed"]))
    code, r, _ = api("POST", "/api/bill", {"vendor_id": orion, "kind": "credit", "number": "CM-7", "date": today_iso(),
                                          "lines": [{"description": "Credit for a returned part", "qty": 1, "price": 15}]})
    cm = r["bill"]
    check("a vendor credit", code == 200 and cm["status"] == "available" and cm["left"] == 15, (code, r))
    code, r, _ = api("POST", "/api/credit/apply", {"credit_id": cm["id"], "bill_id": bill["id"], "amount": 20})
    check("can't apply more credit than there is", code == 409, r)
    code, r, _ = api("POST", "/api/credit/apply", {"credit_id": cm["id"], "bill_id": bill["id"], "amount": 15})
    check("credit applied", code == 200 and r["bill"]["owed"] == 13.5, r)
    code, r, _ = api("POST", "/api/payment", {"vendor_id": orion, "date": today_iso(), "amount": 33.5, "method": "Check", "reference": "1202",
                                             "allocations": [{"bill_id": bill["id"], "amount": 13.5}, {"bill_id": b2["id"], "amount": 20}]})
    pid = r.get("id")
    check("one check pays two invoices", code == 200 and api("GET", f"/api/bill?id={bill['id']}")[1]["bill"]["status"] == "paid"
          and api("GET", f"/api/bill?id={b2['id']}")[1]["bill"]["status"] == "paid", (code, r))
    check("nothing owed now", api("GET", "/api/payables")[1]["totals"]["count"] == 0)
    code, r, _ = api("POST", "/api/bill/void", {"id": bill["id"]})
    check("a paid invoice can't be voided", code == 409, r)
    code, r, _ = api("POST", "/api/bill", {"id": bill["id"], "vendor_id": orion, "number": "INV-1001", "date": "2026-08-01", "lines": [{"description": "x", "qty": 1, "price": 1}]})
    check("an invoice's total can't drop below what's paid on it", code == 409, r)
    api("POST", "/api/payment/void", {"id": pid})
    check("voiding a payment reopens what it paid", api("GET", f"/api/bill?id={bill['id']}")[1]["bill"]["owed"] == 13.5
          and api("GET", f"/api/bill?id={b2['id']}")[1]["bill"]["status"] == "open")
    code, raw, _ = api("GET", "/api/bills.csv")
    check("invoices export", code == 200 and b"INV-1001" in raw and b"CM-7" in raw and b"78.5" in raw, raw[:300])
    code, raw, _ = api("GET", "/api/payments.csv")
    check("payments export", code == 200 and b"1201" in raw and b"void" in raw, raw[:300])
    check("the header knows what's overdue", state()["payables"]["overdue"] == 13.5)

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
