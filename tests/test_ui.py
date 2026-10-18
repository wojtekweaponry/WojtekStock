#!/usr/bin/env python3
"""Browser test: drives the station page in headless Chromium the way a USB scanner does (types, then Enter),
against the mock store. Decodes the QR codes in the label and badge PDFs. Needs playwright, opencv and pdftoppm.
Run from the project folder:  python3 tests/test_ui.py [screenshot-folder]
"""
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_stock as T  # noqa: E402  (reuses its servers and helpers)
from playwright.sync_api import sync_playwright  # noqa: E402

SHOTS = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="wstock-shots-")
os.makedirs(SHOTS, exist_ok=True)


def decode_pdf_qrs(path):
    import cv2
    out = []
    prefix = path[:-4]
    subprocess.run(["pdftoppm", "-r", "300", "-png", path, prefix], check=True)
    det = cv2.QRCodeDetector()
    for f in sorted(x for x in os.listdir(os.path.dirname(path)) if x.startswith(os.path.basename(prefix)) and x.endswith(".png")):
        img = cv2.imread(os.path.join(os.path.dirname(path), f))
        ok, texts, _, _ = det.detectAndDecodeMulti(img)
        if ok:
            out += [t for t in texts if t]
    return out


def S_tab(page):
    return page.evaluate("[...document.querySelectorAll('nav button')].find(b => b.getAttribute('aria-selected') === 'true').dataset.tab")


def run_ui():
    T.start_server("on")
    T.check("server up and first sync", T.wait_sync())
    T.api("POST", "/api/setup", {"prefixes": ["IRV", "ORI", "CSSI"], "guns": [50, 51], "confirm": True})
    T.wait_for(lambda: T.api("GET", "/api/setup")[1]["varsLoaded"] > 0, 60)
    T.api("POST", "/api/people", {"name": "Ana", "code": "ANA"})
    errors = []
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        ctx = br.new_context(http_credentials={"username": "shop", "password": "test-pass"}, accept_downloads=True,
                             viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.add_init_script("""window.__beeps = []; window.AudioContext = class { constructor(){ this.state='running'; this.currentTime=0; this.destination={}; }
            resume(){ return Promise.resolve(); } createBuffer(){ return {}; } createBufferSource(){ return {connect(){}, start(){}}; }
            createOscillator(){ return {type:'', frequency:{value:0}, connect(g){ return g; }, start(){}, stop(){}}; }
            createGain(){ const g = {gain:{setValueAtTime(){}, exponentialRampToValueAtTime(){}}, connect(){ return g; }}; return g; } };""")
        page.goto(f"http://127.0.0.1:{T.STOCK_PORT}/")
        page.wait_for_selector("#scan")
        page.wait_for_function("() => document.querySelector('#ver').textContent.length > 0")
        page.evaluate("""() => { const b = window.beep; window.beep = k => { window.__beeps.push(k); b(k); }; }""")

        def scan(code):
            page.fill("#scan", code)
            page.press("#scan", "Enter")
            page.wait_for_timeout(250)

        def flash():
            return page.inner_text("#flash")

        def last_beep():
            return page.evaluate("window.__beeps[window.__beeps.length-1]")

        page.screenshot(path=f"{SHOTS}/01-station-empty.png", full_page=True)
        scan("090255000001")
        T.check("badge required before scanning products", "badge" in flash().lower() and last_beep() == "err", flash())
        scan("USR-ANA")
        T.check("badge scan signs in", "Hi, Ana" in flash() and "Ana" in page.inner_text("#opChip"), flash())
        scan("090255000001")
        T.check("scan adds +1 with a blip", "Hornady" in flash() and last_beep() == "ok", flash())
        scan("90255000001")
        T.check("same product again (UPC without its 0) is a double boop", "Already scanned" in flash() and last_beep() == "dup", flash())
        scan("+12")
        qty = page.input_value("#modePanel tr[data-id='2001'] input.qty")
        T.check("+12 after a scan makes that scan 12", qty == "13", qty)
        scan("IRV-10005")
        T.check("feed product refused with a buzz", "distributor feed" in flash() and last_beep() == "err", flash())
        scan("PA195S201")
        T.check("gun refused", "serialized gun" in flash(), flash())
        scan("KNIFE-CONS")
        T.check("read-only product refused", "can't be changed" in flash(), flash())
        scan("WW-TEE-M")
        T.check("variation scans by its SKU", "Size: M" in flash(), flash())
        scan("0001")
        T.check("last digits of a barcode find the product", "Hornady" in flash() and "end of its barcode" in flash(), flash())
        scan("MAG10")
        T.check("last letters of a SKU find the product", "SIG P365" in flash(), flash())
        scan("001")
        page.wait_for_selector(".modal")
        T.check("several matching endings: the station asks which", "ends in 001" in page.inner_text(".modal") and page.locator(".modal input[name=pick]").count() >= 2, page.inner_text(".modal"))
        page.click(".modal input[value='2101']")
        page.click(".modal button[data-v='1']")
        page.wait_for_timeout(300)
        T.check("picked one goes on the list", page.is_visible("#modePanel tr[data-id='2101']"))
        page.click("#modePanel tr[data-id='2101'] [data-rm]")
        for rid in ("2001", "2002"):   # keep the list as the rest of this test expects it
            page.fill(f"#modePanel tr[data-id='{rid}'] input.qty", "13" if rid == "2001" else "0")
        page.click("#modePanel tr[data-id='2002'] [data-rm]")
        page.wait_for_timeout(200)
        scan("850000000031")
        T.check("unknown barcode offers linking", page.is_visible("#linkPanel") and "Unknown barcode" in flash(), flash())
        page.fill("#linkQ", "coffee mug")
        page.wait_for_timeout(200)
        page.screenshot(path=f"{SHOTS}/02-link-barcode.png", full_page=True)
        page.click("#linkRes button[data-id='2003']")
        page.wait_for_timeout(600)
        T.check("linked product goes on the list", page.is_visible("#modePanel tr[data-id='2003']"), page.inner_text("#modePanel"))
        T.check("linked barcode saved in the store", T.wait_for(lambda: T.mock("/mock/get?pid=2003")["global_unique_id"] == "850000000031", 20))
        scan("850000000031")
        T.check("the new barcode scans now", "coffee mug" in flash().lower() and last_beep() == "dup", flash())
        page.screenshot(path=f"{SHOTS}/03-receive-list.png", full_page=True)
        before = T.mock("/mock/get?pid=2001")["stock_quantity"]
        page.click("#listGo")
        T.check("receive finishes", T.wait_for(lambda: "Saved in WooCommerce" in flash(), 30), flash())
        T.check("store stock went up by 13", T.mock("/mock/get?pid=2001")["stock_quantity"] == before + 13)
        T.check("rising two-tone when done", "done" in page.evaluate("window.__beeps"))
        page.screenshot(path=f"{SHOTS}/04-receive-done.png", full_page=True)
        T.check("list empties after it's saved", "Scan products as you unpack" in page.inner_text("#modePanel"))

        scan("CMD-COUNT")
        T.check("command QR switches mode", page.get_attribute(".modes button[data-mode=count]", "aria-pressed") == "true")
        for _ in range(3):
            scan("036000291452")
        scan("ACC-001")
        page.click("#listGo")
        page.wait_for_selector("#revApply")
        page.wait_for_timeout(300)
        page.screenshot(path=f"{SHOTS}/05-count-review.png", full_page=True)
        T.check("review shows the difference", "-27" in page.inner_text("#modePanel") or "−27" in page.inner_text("#modePanel"), page.inner_text("#modePanel"))
        page.click("#revApply")
        T.check("count applied", T.wait_for(lambda: T.mock("/mock/get?pid=2006")["stock_quantity"] == 3, 30), T.mock("/mock/get?pid=2006"))
        T.check("matching line left alone", T.mock("/mock/get?pid=2101")["stock_quantity"] == 1)

        page.click(".modes button[data-mode=adjust]")
        page.select_option("#adjReason", "Used on gunsmithing job")
        page.fill("#adjNote", "Job 4471")
        scan("ACC-003")
        page.screenshot(path=f"{SHOTS}/06-adjust.png", full_page=True)
        st = T.mock("/mock/get?pid=2103")["stock_quantity"]
        page.click("#listGo")
        T.check("adjust down applied", T.wait_for(lambda: T.mock("/mock/get?pid=2103")["stock_quantity"] == st - 1, 30))
        log = T.api("GET", "/api/log?limit=1")[1]["rows"][0]
        T.check("reason and note logged", log["reason"] == "Used on gunsmithing job" and log["note"] == "Job 4471" and log["by"].startswith("Ana"), log)

        o = T.mock("/mock/order", {"lines": [{"pid": 2002, "qty": 2}, {"pid": 2500, "vid": 2503, "qty": 1}, {"pid": 1000, "qty": 1}]})
        sig = T.mock("/mock/get?pid=2002")["stock_quantity"]
        tee = T.mock("/mock/get?pid=2500&vid=2503")["stock_quantity"]
        scan("CMD-RETURN")
        T.check("Return mode from the command sheet", page.get_attribute(".modes button[data-mode=return]", "aria-pressed") == "true")
        scan(o["number"])
        page.wait_for_selector("#retGo")
        T.check("packing slip barcode opens the order", f"Order #{o['number']}" in flash() and "Jamie R." in flash(), flash())
        T.check("feed line can't be restocked", page.is_disabled(f"tr[data-item='1000'] select option[value='1']"))
        scan("798681000002")
        scan("798681000002")
        T.check("second unit is a double boop", last_beep() == "dup" and "2 of 2" in flash(), flash())
        scan("798681000002")
        T.check("a third is refused: only 2 sold", last_beep() == "err" and "Only 2" in flash(), flash())
        scan("HORN-9MM-115")
        T.check("item not on the order refused", "isn't on order" in flash(), flash())
        scan("WW-TEE-L")
        page.select_option("tr[data-item='2503'] select", "0")
        page.fill("#retNote", "Wrong size")
        page.wait_for_timeout(200)
        page.screenshot(path=f"{SHOTS}/06b-return.png", full_page=True)
        page.click("#retGo")
        T.check("return finishes", T.wait_for(lambda: "Saved in WooCommerce" in flash(), 30), flash())
        T.check("restocked items back in stock", T.mock("/mock/get?pid=2002")["stock_quantity"] == sig + 2)
        T.check("damaged item not restocked", T.mock("/mock/get?pid=2500&vid=2503")["stock_quantity"] == tee)
        nt = T.mock(f"/mock/notes?order={o['id']}")[-1]["note"]
        T.check("order note written", "Wrong size" in nt and "damaged, not restocked" in nt and "unticked" in nt, nt)
        page.screenshot(path=f"{SHOTS}/06c-return-done.png", full_page=True)
        scan(o["number"])
        page.wait_for_selector("#modePanel tr[data-item='2002']")
        T.check("reopening the order shows them as back", "All back" in page.inner_text("tr[data-item='2002']"))
        page.click("#retClear")
        T.mock("/mock/refund", {"order": o["id"], "lines": [{"pid": 2002, "qty": 2}], "restock": True})
        T.api("POST", "/api/sync", {})
        T.check("double restock banner appears", T.wait_for(lambda: "possible double restock" in page.inner_text("#banners"), 30), page.inner_text("#banners"))
        scan("CMD-LOOKUP")
        scan("HORN-9MM-115")
        page.wait_for_selector(".bigstock")
        page.wait_for_timeout(500)
        page.screenshot(path=f"{SHOTS}/07-lookup.png", full_page=True)
        T.check("lookup shows live stock and history", page.inner_text(".bigstock").strip() == str(before + 13) and "Received" in page.inner_text("#modePanel"), page.inner_text(".bigstock"))

        page.click("nav button[data-tab=products]")
        page.wait_for_selector("#prodTable tbody tr[data-id]")
        rows = lambda: page.eval_on_selector_all("#prodTable tbody tr[data-id]", "els => els.map(e => +e.dataset.id)")
        T.check("Products tab lists the in-shop products", len(rows()) > 30 and "of" in page.inner_text("#prodSummary"), page.inner_text("#prodSummary"))
        T.check("no feed products listed", not any(1000 <= i < 1500 for i in rows()))

        def psearch(q):
            page.fill("#prodQ", q)
            page.wait_for_timeout(300)
            return rows()
        T.check("search by name", psearch("hornady") == [2001])
        T.check("search by UPC without its leading zero", psearch("90255000001") == [2001])
        T.check("search by SKU", sorted(psearch("WW-TEE")) == [2501, 2502, 2503])
        T.check("search by category word", set(psearch("apparel")) >= {2501, 2502, 2503})
        T.check("words in any order", psearch("magazine sig") == [2002])
        page.fill("#prodQ", "")
        page.select_option("#prodCat", "Ammunition")
        page.wait_for_timeout(200)
        T.check("category picker", rows() == [2001], rows())
        page.select_option("#prodCat", "")
        page.select_option("#prodBar", "noupc")
        page.wait_for_timeout(200)
        T.check("No UPC filter", 2001 not in rows() and 2003 not in rows() and 2501 not in rows() and 2502 in rows(), rows()[:8])
        page.select_option("#prodBar", "")
        page.select_option("#prodSort", "stock:1")
        page.wait_for_timeout(200)
        stocks = page.eval_on_selector_all("#prodTable tbody tr[data-id] td:nth-child(5)", "els => els.map(e => e.textContent.trim())")
        nums = [int(x) for x in stocks if x.lstrip("-").isdigit()]
        T.check("sort by stock, lowest first", nums == sorted(nums) and stocks[-1] == "–", stocks[:5] + stocks[-3:])
        page.select_option("#prodSort", "name:1")
        page.click("#prodSummary")
        page.keyboard.press("/")
        T.check("/ jumps to the search box", page.evaluate("document.activeElement.id") == "prodQ")
        page.keyboard.type("hornady")
        page.wait_for_timeout(300)
        page.screenshot(path=f"{SHOTS}/07b-products-search.png", full_page=True)
        page.fill("#prodQ", "")
        page.wait_for_timeout(300)
        page.screenshot(path=f"{SHOTS}/07c-products.png")
        page.fill("#prodQ", "hornady")
        page.press("#prodQ", "Enter")
        page.wait_for_selector(".bigstock")
        T.check("Enter on a single match opens its Lookup card", S_tab(page) == "station" and "Hornady" in page.inner_text("#modePanel"))
        page.click("nav button[data-tab=products]")
        page.fill("#prodQ", "WW-TEE-S")
        page.wait_for_timeout(300)
        page.click("#prodTable tbody tr[data-id='2501'] td:first-child")
        page.wait_for_selector(".bigstock")
        T.check("clicking a product opens its Lookup card", "Size: S" in page.inner_text("#modePanel"))
        page.click("nav button[data-tab=labels]")
        page.fill("#labSearch", "HORN-9MM-115")
        page.press("#labSearch", "Enter")
        page.fill("#labSearch", "WW-TEE-S")
        page.press("#labSearch", "Enter")
        page.fill("#labSearch", "SIG-P365-MAG10")
        page.press("#labSearch", "Enter")
        page.select_option("#labSize", "printer")
        page.wait_for_timeout(300)
        page.screenshot(path=f"{SHOTS}/08-labels.png", full_page=True)
        with page.expect_download() as dl:
            page.click("#labPdf")
        pdf = os.path.join(SHOTS, "labels.pdf")
        dl.value.save_as(pdf)
        codes = decode_pdf_qrs(pdf)
        T.check("label PDF QR codes decode to the SKUs", sorted(codes) == ["HORN-9MM-115", "SIG-P365-MAG10", "WW-TEE-S"], codes)
        page.select_option("#labSize", "5160")
        page.wait_for_timeout(200)
        page.fill("#labSkip", "4")
        with page.expect_download() as dl:
            page.click("#labPdf")
        pdf2 = os.path.join(SHOTS, "labels-5160.pdf")
        dl.value.save_as(pdf2)
        T.check("Avery sheet labels decode too", sorted(decode_pdf_qrs(pdf2)) == ["HORN-9MM-115", "SIG-P365-MAG10", "WW-TEE-S"])
        T.check("Zebra button off for sheets", page.is_disabled("#labZebra"))
        page.select_option("#labSize", "2x3")
        page.wait_for_timeout(300)
        T.check("preview shows the SKU large", "HORN-" in page.inner_html("#labPreview") and "UPC" not in page.inner_text("#labPreview"))
        page.check("#labUpc")
        page.wait_for_timeout(200)
        T.check("UPC option shows it", "UPC 090255000001" in page.inner_html("#labPreview"))
        page.uncheck("#labUpc")
        page.wait_for_timeout(200)
        page.screenshot(path=f"{SHOTS}/08b-label-design.png", full_page=True)
        with page.expect_download() as dl:
            page.click("#labPdf")
        pdf3 = os.path.join(SHOTS, "labels-2x3.pdf")
        dl.value.save_as(pdf3)
        T.check("2 x 3 label QR codes decode to the SKUs", sorted(decode_pdf_qrs(pdf3)) == ["HORN-9MM-115", "SIG-P365-MAG10", "WW-TEE-S"])
        page.select_option("#labSize", "3x2")
        page.wait_for_timeout(300)
        page.screenshot(path=f"{SHOTS}/08c-label-wide.png", full_page=True)
        with page.expect_download() as dl:
            page.click("#labPdf")
        pdf4 = os.path.join(SHOTS, "labels-3x2.pdf")
        dl.value.save_as(pdf4)
        T.check("3 x 2 wide labels decode to the SKUs", sorted(decode_pdf_qrs(pdf4)) == ["HORN-9MM-115", "SIG-P365-MAG10", "WW-TEE-S"])
        n0 = len(T.printed)
        page.click("#labZebra")
        T.wait_for(lambda: len(T.printed) > n0, 10)
        z = T.printed[-1] if len(T.printed) > n0 else ""
        T.check("Zebra prints wide labels sideways on 2 x 3 stock", "^PW406^LL609" in z and "^A0R" in z and "^A0N" not in z and z.count("^XA") == 3, z[:200])
        page.select_option("#labSize", "2x3")
        page.wait_for_timeout(300)
        n0 = len(T.printed)
        page.click("#labZebra")
        T.wait_for(lambda: len(T.printed) > n0, 10)
        z = T.printed[-1] if len(T.printed) > n0 else ""
        T.check("Zebra prints 2 x 3 upright", "^PW406^LL609" in z and z.count("^XA") == 3 and "^FDMA,WW-TEE-S^FS" in z, z[:200])
        T.check("Zebra: no UPC unless ticked", "UPC" not in z)
        T.check("Zebra: long SKU on two lines", "HORN-\\&9MM-115" in z, z[z.find("HORN"):z.find("HORN") + 40])
        page.click("#labClear")
        page.fill("#labSearch", "MAG570-BLK")
        page.press("#labSearch", "Enter")
        page.select_option("#labSize", "3x2")
        page.wait_for_timeout(300)
        page.screenshot(path=f"{SHOTS}/08d-long-name.png", full_page=True)
        long_name = "Magpul PMAG 30 AR/M4 GEN M3 Window 5.56x45 NATO / .223 Remington 30-Round Polymer Magazine, Black"
        T.check("preview shows the whole long name", page.inner_text("#labPreview").split() == ["MAG570-", "BLK"] + long_name.split() or
                " ".join(page.eval_on_selector_all("#labPreview text", "els => els.map(e => e.textContent)")).replace("MAG570- BLK ", "").replace("MAG570-BLK ", "") == long_name,
                page.eval_on_selector_all("#labPreview text", "els => els.map(e => e.textContent)"))
        for size in ("3x2", "2x3", "2x1"):
            page.select_option("#labSize", size)
            page.wait_for_timeout(200)
            with page.expect_download() as dl:
                page.click("#labPdf")
            p = os.path.join(SHOTS, f"long-{size}.pdf")
            dl.value.save_as(p)
            txt = subprocess.run(["pdftotext", "-raw", p, "-"], capture_output=True, text=True).stdout
            T.check(f"{size} PDF has the whole long name", all(w in txt.split() for w in long_name.split()) and "..." not in txt, txt)
            T.check(f"{size} long-name label still scans", decode_pdf_qrs(p) == ["MAG570-BLK"])
        page.select_option("#labSize", "3x2")
        n0 = len(T.printed)
        page.click("#labZebra")
        T.wait_for(lambda: len(T.printed) > n0, 10)
        z = T.printed[-1] if len(T.printed) > n0 else ""
        import re as _re
        fds = " ".join(x.replace("\\&", " ") for x in _re.findall(r"\^FD(.*?)\^FS", z) if not x.startswith("MA,"))
        T.check("Zebra gets the whole long name", long_name in " ".join(fds.split()), fds)
        T.check("no-SKU products listed", "no SKU" in page.inner_text("#noSkuBody") or "Wojtek patch" in page.inner_text("#noSkuBody"), page.inner_text("#noSkuBody"))

        page.click("nav button[data-tab=people]")
        page.wait_for_timeout(300)
        with page.expect_download() as dl:
            page.click("#badgesPdf")
        bp = os.path.join(SHOTS, "badges.pdf")
        dl.value.save_as(bp)
        T.check("badge QR decodes to USR-ANA", "USR-ANA" in decode_pdf_qrs(bp))
        with page.expect_download() as dl:
            page.click("#cmdPdf")
        cp = os.path.join(SHOTS, "commands.pdf")
        dl.value.save_as(cp)
        T.check("command sheet decodes", set(decode_pdf_qrs(cp)) >= {"CMD-RECEIVE", "CMD-COUNT", "CMD-ADJUST", "CMD-LOOKUP", "CMD-LOGOUT"})
        page.fill("#tName", "Back room")
        page.click("#tAdd")
        page.wait_for_selector("#tLink:not([hidden])")
        page.screenshot(path=f"{SHOTS}/09-people.png", full_page=True)

        page.click("nav button[data-tab=setup]")
        page.wait_for_selector("#prefTable tbody tr")
        page.wait_for_timeout(400)
        page.screenshot(path=f"{SHOTS}/10-setup.png", full_page=True)
        T.check("setup lists the IRV group ticked", page.is_checked("input[data-pref='IRV']"))
        T.mock("/mock/flags", {"delay": 0.8})
        page.click("#syncFull")
        page.wait_for_selector("#syncProg .prog", timeout=10000)
        T.wait_for(lambda: "%" in page.inner_text("#syncChip"), 10)
        T.wait_for(lambda: "left" in page.inner_text("#syncProg"), 15)
        chip = page.inner_text("#syncChip")
        page.screenshot(path=f"{SHOTS}/10b-sync-progress.png")
        T.check("header chip shows the percentage", chip.startswith("Syncing ") and chip.endswith("%"), chip)
        txt = page.inner_text("#syncProg")
        T.check("Setup shows the step and time left", ("page" in txt or "product" in txt) and "left" in txt, txt.replace("\n", " | "))
        T.check("sync buttons wait while it runs", page.is_disabled("#syncFull"))
        T.mock("/mock/flags", {"delay": 0})
        T.check("progress goes away when done", T.wait_for(lambda: not page.query_selector("#syncProg .prog"), 30))
        T.check("last sync time shown", "last full sync took" in page.inner_text("#syncProg").lower(), page.inner_text("#syncProg"))

        page.click("nav button[data-tab=log]")
        page.wait_for_selector("#logTable tbody tr")
        page.wait_for_timeout(300)
        page.screenshot(path=f"{SHOTS}/11-log.png", full_page=True)
        T.check("log lists changes", "Hornady" in page.inner_text("#logTable"))

        page.click("nav button[data-tab=station]")
        for theme in ("dark", "terminal"):
            page.select_option("#theme", theme)
            page.click(".modes button[data-mode=receive]")
            scan("090255000001")
            page.screenshot(path=f"{SHOTS}/12-{theme}.png", full_page=True)
        page.select_option("#theme", "light")
        page.set_viewport_size({"width": 390, "height": 844})
        scan("WW-TEE-L")
        page.screenshot(path=f"{SHOTS}/13-phone.png", full_page=True)
        page.reload()
        page.wait_for_selector("#scan")
        page.wait_for_timeout(800)
        T.check("list and sign-in survive a reload", "Ana" in page.inner_text("#opChip") and page.is_visible("#modePanel tr[data-id='2503']"))
        scan("CMD-LOGOUT")
        T.check("sign out", "Not signed in" in page.inner_text("#opChip"))
        T.check("no page errors", not errors, errors)
        br.close()


if __name__ == "__main__":
    T.main.__globals__["run"] = run_ui
    T.main()
    print("Screenshots in", SHOTS)
