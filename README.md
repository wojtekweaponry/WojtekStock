# Wojtek Stock

Phase 1 of replacing ecomdash: a barcode stock station for Wojtek Weaponry's in-shop WooCommerce products (about
1,000, variations included). Scan to receive, count, adjust, take returns and look up stock; link unknown barcodes;
print QR labels.

WooCommerce stays the single source of truth. Its sales, online and at the register, already lower stock; Wojtek Stock
only changes **stock quantity**, plus the **barcode (GTIN/UPC)** when you link one, the **SKU** when you ask it to
give a product one, and a **note on an order** when you take a return against it. Every change re-reads the product live first, is read back afterwards to confirm it took, and is
logged with who, when and why.

It runs on its own: its own Docker Compose project, container, port (8090), folder (`~/wojtek-stock`) and SQLite
database. It doesn't use or touch the Wojtek Locator, and has no Telegram bot.

## What it never changes

- **Distributor feed products** (about 22,000): any product whose SKU, or whose parent product's SKU, starts with a
  feed prefix (IRV, ORI…). You tick the prefixes on the Setup page, from the SKUs actually in your store.
- **Serialized guns**: anything in the gun categories you tick on the Setup page. Their stock moves through FastBound
  and sales.
- **Products whose stock WooCommerce doesn't track** (stock management off, or a variation managed by its parent):
  shown in Lookup as read-only.

These are checked twice: the station never offers them, and **every write re-reads the product live from WooCommerce
and checks again** right before sending, so a stale local copy (or a SKU changed five minutes ago) can't slip through.
Changes stay locked until you confirm the Setup page.

## Install (about 10 minutes)

**1. Copy the zip to the server.** From the folder where you downloaded it:

Windows PowerShell:
```powershell
scp $HOME\Downloads\wojtek-stock-v1.zip pepe@10.42.10.246:~/
ssh pepe@10.42.10.246
```

Mac Terminal:
```bash
scp ~/Downloads/wojtek-stock-v1.zip pepe@10.42.10.246:~/
ssh pepe@10.42.10.246
```

**2. On the server:**
```bash
cd ~ && unzip -o wojtek-stock-v1.zip
cd ~/wojtek-stock
cp stock.env.example stock.env && chmod 600 stock.env
nano stock.env                                   # password, WOO_URL, WOO_KEY, WOO_SECRET (leave WOO_WRITE=dryrun)
mkdir -p data && sudo chown 10001:10001 data     # the container runs as user 10001
sudo docker compose up -d --build
sudo docker compose logs -f                      # Ctrl+C once it says "Wojtek Stock v1 on http://0.0.0.0:8090"
```

**3. Open `http://10.42.10.246:8090`** and sign in with `STOCK_USER` / `STOCK_PASSWORD`.

The WooCommerce key: WordPress admin → WooCommerce → Settings → Advanced → REST API → **Add key**, user an admin or
shop manager, Permissions **Read/Write**. Copy both keys at once; WooCommerce shows the secret only once. Use your
store's `https://` address: WooCommerce only accepts keys over https.

## First run: Setup, then a dry run

1. **Setup tab → Test connection.** It shows your product count and WooCommerce version.
2. **Wait for the first full sync** (a few minutes for 23,000 products; the header says "Synced …" when done).
   It only reads.
3. **Distributor feed prefixes:** the table groups every SKU in the store by how it starts, largest first. Your feeds
   stand out with thousands of products each. Tick each one. Add any that aren't listed (a two-letter prefix, say)
   with **Add**. A prefix covers everything that starts with it.
4. **Serialized gun categories:** tick your gun categories.
5. **Save** to see the counts update (In-shop, can change should be about 1,000), then **Save and unlock changes**.
   It then reads the variations of your in-shop variable products.
6. **Try it in dry run** (`WOO_WRITE=dryrun`): receive, count and adjust a few things. Each change is checked against
   the live store and logged as **Dry run**, with the before and after it would have written, and nothing is sent.
   Check the **Log** tab.
7. **Go live:** set `WOO_WRITE=on` in `stock.env`, then `sudo docker compose up -d`. The header chip turns from
   "Dry run" to "Live".

## Updating to a new version, and GitHub

Wojtek Stock is kept on a private GitHub repository, the same way as the Wojtek Locator: `deploy.sh` unpacks each new
zip, commits it to git (described by the line in `CHANGES.txt`), labels it with its version, pushes it to GitHub, then
rebuilds and restarts. Your `stock.env` and `data/` never go to GitHub, and the zip never contains them.
One-time setup: `GITHUB.md`.

Windows PowerShell:
```powershell
scp $HOME\Downloads\wojtek-stock-v12.zip pepe@10.42.10.246:~/
ssh pepe@10.42.10.246
```

Mac Terminal:
```bash
scp ~/Downloads/wojtek-stock-v12.zip pepe@10.42.10.246:~/
ssh pepe@10.42.10.246
```

Then on the server:
```bash
~/wojtek-stock/deploy.sh
```

`deploy.sh pull` takes changes merged on GitHub (someone else's pull request). `deploy.sh` refuses a zip while GitHub
has changes the server doesn't, and stops a zip that would undo someone else's change: `GITHUB.md` explains both.
Before GitHub is set up, `deploy.sh` still unpacks and rebuilds; it just doesn't commit anything.

## At the station

**Sign in:** scan your badge (`USR-` plus your code). The Wojtek Locator's badges work here: import its People export
once (People tab). Scan `CMD-LOGOUT` to sign out; it also signs out after 10 idle minutes. Each person's lists are kept
separately, so one person's scans are never credited to another, and a list waits for its owner to sign back in.

**Modes** (buttons, or scan the command sheet from the People tab):

- **Receive:** each scan adds 1. For a case, scan once, then type `+12` and Enter: that scan becomes 12. (Type `+12`
  *before* scanning and the next scan counts 12.) Press **Add to stock** when the box is unpacked.
- **Count:** scan a shelf's worth; repeat scans add 1. The counted numbers are shown, not WooCommerce's, so the count
  isn't biased. **Review** reads every product's stock live and shows the differences; untick any you don't want,
  then **Apply**. If stock changes between the review and applying (a sale at the register), that product is left
  alone and marked: count it again. Products you didn't scan aren't touched.
- **Adjust:** pick **Decrease** or **Increase** and a reason (damaged, used on a gunsmithing job, returned to vendor,
  found, customer return…), add a note if useful (a job number, an RMA), then scan. "Other" needs a note.
- **Return:** scan the order barcode on the packing slip that came back, then each item in the box (see Returns below).
- **Lookup:** scan anything to see its live stock, barcode, SKU, QR and recent changes.

**Sounds:** a short high blip when a product goes on the list, a double boop when it was already on it, a low buzz for
an error (unknown code, a feed product, a gun, a read-only product), and a rising two-tone when changes are saved.

**Barcodes:** UPC-A, EAN-13 and GTIN-14 forms of the same number match (`012345678905` = `0012345678905`). A SKU
typed or scanned works too. **The last 3 to 6 letters or digits** of a barcode or SKU also find the product (a torn
barcode, a long SKU), as in the Locator: exact matches always come first, and if several products end the same way,
the station asks which one. (Not in Return mode, where a short number is more likely an order number.) **An unknown barcode** offers to link it: search by name or SKU, pick the product, and the
barcode is saved to that product's GTIN/UPC field in WooCommerce (`global_unique_id`), so your register recognizes it
too. WooCommerce refuses a barcode that's already on another product, and the station says which one.

**Changes go out in order**, one product at a time: each is re-read live, changed by the difference (so a sale between
your scan and your commit isn't overwritten), and read back. Progress shows under the list. If WooCommerce is down
they're retried with growing waits and never lost; if WooCommerce refuses the key (it's Read-only, or was revoked),
changes pause until someone resumes them on the Log tab.

## Products

The **Products** tab searches and browses every in-shop product, like the Locator's Inventory tab. Press `/` to jump to
the search box.

- **One search box** for name, SKU, UPC or category, any words in any order: `sig mag`, `HORN-9MM`, `apparel`. A UPC
  matches with or without its leading zeros (`90255000001` finds `090255000001`), and you can scan one straight in.
- **Pickers:** category (WooCommerce's categories stand in for the Locator's "type"; a variation counts under its
  parent's), stock (in stock, 5 or fewer, out of stock, below zero), products with **no UPC** or **no SKU**, and
  in-shop or read-only.
- **Sort** by name, SKU, category, stock or price. The count of products and units shown is above the table.
- **Click a product** (or press Enter when the search finds just one) to open its Lookup card: live stock, history,
  link a barcode, give it a SKU. **Label** adds it to the label list.

Feed products and guns aren't listed, the same as at the station. The filters are remembered on each device.

## Returns

**The packing slip barcode:** in ShipStation's packing slip template, add a barcode of the **order number** (Code 128 is
fine). If ShipStation puts a prefix or `#` in front (`SS-12345`), Wojtek Stock ignores it. Orders renumbered by a
plugin (`WW-777`) work too: the order is matched by its number, never mistaken for another order whose internal id
happens to be the same.

**At the station:** Return mode, scan the slip's barcode (or type the order number and press Enter). The order opens
with each product, how many were sold and how many are already back. Then scan each item from the box: each scan is
checked against the order, so an item that wasn't on it, or one more than was sold, gets the error buzz. `+3` after a
scan makes it 3; **Everything came back** marks the whole order. Set any damaged item to **Damaged: don't restock**:
it's logged against the order but stock doesn't change. Feed products and guns on an order are shown but can't be
restocked here. Press **Return**.

Restocked items go back in stock right away (re-read live, changed, read back, like every change), and a note goes
on the order, for example:

```
Returned via Wojtek Stock by Ana:
- 2 × SIG P365 10rd magazine (SIG-P365-MAG10): restocked (2 to 4)
- 1 × Wojtek T-shirt (Size: L) (WW-TEE-L): damaged, not restocked
Note: Wrong size
When refunding, leave "Restock refunded items" unticked: Wojtek Stock already put the stock back.
```

**Refunding:** refund in WooCommerce as usual, but **leave "Restock refunded items" unticked**. Wojtek Stock has already
done the restocking. Two safety nets:

- If WooCommerce restocked something first (a refund with the box ticked, or a cancelled order), Wojtek Stock sees it
  in the order's notes and won't restock it again.
- If an order Wojtek Stock restocked is later refunded with the box ticked, the next quick sync notices. A banner
  appears, and the Setup tab lists the order and product under **Possible double restocks**: lower that product's stock
  with Adjust (Count correction), then clear the list.

## Purchasing (parts only)

The **Purchasing** tab has purchase orders, vendors, and the distributor feed search. Guns never go on a PO, and
neither do feed products: a PO holds in-shop parts whose stock Wojtek Stock manages.

**Vendors:** name, your account number, rep, the email POs go to, phone, terms, usual lead time, and (optional) the
SKU prefix of their feed in your store (`ORI`). Wojtek Stock remembers each vendor's part number and last price for
every part you order from them.

**A purchase order:**

1. **New purchase order**, pick the vendor, then scan or search parts onto it; **Parts from this vendor** lists the
   ones you've ordered from them before (out-of-stock ones ticked). Set quantities and unit costs; the vendor's part
   number and last price fill in when known. Add notes for the vendor.
2. **Save draft** while you work on it; **Download PDF** for a clean PO with your letterhead (`SHOP_NAME`,
   `SHOP_ADDRESS`, `SHOP_PHONE`, `SHOP_EMAIL` in `stock.env`), the vendor, ship-to, lines, total, and a QR code of its
   number. **Email it to (vendor)** sends it (see Emailing POs below); **Save and mark sent** is for a PO you send another
   way. Either locks it and opens it for receiving.
3. **When the box comes:** at the station, scan the PO's QR code (or press **Receive at the station** on the PO). Scan
   what's in the box: each scan is checked against what's still due, and `+5` after a scan makes it 5. Something that
   wasn't ordered gets the error buzz and the offer to **add it as an extra**, with its cost. **Everything due
   arrived** marks it all.
4. Enter the **invoice number, freight and fees**, then **Receive**. Stock goes up the same safe way as everything
   else (re-read live, changed, read back, logged as "Received on PO-0001"), and **Print labels for these** is offered
   for what arrived.

**Partials and backorders:** what hasn't arrived stays due, and the PO shows **Partly received** until the rest comes;
receive it the same way when it does. **The rest isn't coming: close it** cancels what's still due and keeps what
arrived. A sent PO with nothing received can go **back to draft** or be **cancelled**.

**Landed costs:** a receipt's freight and fees are spread over what arrived in it, by each line's value (by quantity
when nothing has a price), into each part's landed unit cost; the receiving screen shows each one as you go. A part's
Lookup card shows its **last landed cost**, its **average** over everything received, what's **on order**, and its
vendors with their part numbers and prices. In dry run, receipts are worked out and logged but nothing is counted as
received.

**Reorder points:** a part's **reorder point** and **order-up-to level** are set on its Lookup card (**Reorder → Set**),
or for many parts at once with **Suggest from sales** on the Purchasing tab: from the last 90 days of WooCommerce
orders, enough to cover the vendor's lead time plus a week, then a month's sales on top (adjust any before saving).
After every sync, a part at or below its reorder point goes on its vendor's **automatic draft PO** (marked *auto*),
with enough to bring it up to its order-up-to level. What's already on an open PO, drafts included, counts as coming,
so nothing is ordered twice. The vendor is the one set for the part, or the one it was last bought from or priced by;
a part with neither is flagged. **Draft POs now** runs the check on the spot. Lines with no known price print as "–",
asking the vendor to confirm.

**Emailing POs:** with email set up (below), **Email it to (vendor)** sends the PO with its PDF attached, a short
message listing the parts, replies going to `SHOP_EMAIL`, and a copy to `PO_EMAIL_CC`, then marks it sent. A sent PO
can be emailed again. A vendor can be set to **email its automatic POs by themselves**: each day at `AUTO_PO_TIME`
(3 PM unless you change it), its automatic draft is emailed and marked sent with nobody pressing anything. Drafts you
make by hand are never sent by themselves. If an email fails (a wrong token, the mail server down), the PO stays as it
was and says why.

**Email setup (Proton Mail):** Proton → Settings → All settings → IMAP/SMTP → **SMTP tokens** → Generate token, for the
address POs should come from (it must be on your own domain, and needs a paid plan). Put what it shows in `stock.env`:
`SMTP_HOST=smtp.protonmail.ch`, `SMTP_PORT=587`, `SMTP_USER`, `SMTP_TOKEN`, `SMTP_FROM` (that address), optionally
`PO_EMAIL_CC`; then `sudo docker compose up -d`. **Setup → Email → Send a test email** checks it. Your Proton password
never goes in `stock.env`, and revoking the token in Proton cuts off Wojtek Stock alone.

**From a distributor feed:** search the feed listings by name, SKU or UPC, then either

- **Copy as a new product**: a new in-shop WooCommerce product with the listing's name, descriptions, images (the same
  pictures, not copies), categories, price and weight, its own SKU (the next `WW-` number or one you type), stock
  tracked from 0, and Draft, Published or Private as you choose. The listing itself is never changed. WooCommerce
  allows a UPC on one product only, so the listing keeps its UPC; Wojtek Stock keeps it as an **extra barcode** of the
  new product, so the station finds it (your register may not).
- **Link to a part I have**: the listing and your part are the same thing. The listing's UPC scans to your part at the
  station, and if a vendor has the listing's feed prefix, the listing's SKU becomes that vendor's part number for it.

## GunBroker

Wojtek Stock connects to GunBroker directly (phase 3). **You create your listings on GunBroker as usual**, giving each
the product's SKU; Wojtek Stock does the rest:

- **Listings are linked to in-shop products by SKU.** The **GunBroker** tab shows every active listing, the product
  it's linked to, or why not (no matching SKU, a distributor feed product, a serialized gun: feed products and guns are
  never linked). Auctions are shown but not synced.
- **Quantities follow WooCommerce stock.** Every few minutes (`GB_MINUTES`), each linked fixed-price listing is set to
  WooCommerce stock less `GB_BUFFER` (units held back so the last one can't sell twice). When a product sells out here,
  Wojtek Stock tries to set its listing to 0; GunBroker may refuse a quantity of 0, and if it does the listing is
  flagged, with a banner, so you can end it on GunBroker. Wojtek Stock never ends or edits listings beyond quantity.
- **GunBroker sales come in.** Each new sale of a linked product becomes a WooCommerce order (paid, processing, the
  buyer's shipping address, a note with the GunBroker order number, and no buyer email, so WooCommerce emails them
  nothing), so ShipStation, your sales history and Return mode all treat it like a website order. Stock goes down
  exactly once: if WooCommerce lowered it when the order was made, that's it; if not, Wojtek Stock lowers it (logged as
  "Sold on GunBroker"). Parts on an order that aren't linked are noted and left alone; an order with nothing linked is
  skipped. A sale cancelled on GunBroker after it came in is flagged for you to check. `GB_CREATE_ORDERS=off` lowers
  stock without making orders.
- **Dry run first.** With `GB_SYNC=dryrun` (the default), the GunBroker tab shows what would be set and which sales
  would come in, and nothing changes on GunBroker or in WooCommerce. Set `GB_SYNC=on` when it looks right; sales made
  during the dry run are not brought in afterwards (you'll have handled those by hand).

**Setup:** ask GunBroker for an API developer key (the sandbox, their test site, has a separate key: `GB_SANDBOX=on`
uses it). In `stock.env`: `GB_DEVKEY`, `GB_USERNAME`, `GB_PASSWORD` (only ever sent to GunBroker, for an access token),
`GB_SYNC=dryrun`, optionally `GB_BUFFER`; then `sudo docker compose up -d`, and **GunBroker → Sync now**.

## Labels

Each label carries a **QR code of the product's SKU** and **the SKU in large letters**, with the name and variation
smaller underneath. The UPC is optional (tick **UPC** under Label design). Only the SKU goes in the QR: a scanner types
out whatever is in it, a short code scans fastest, and a price in the code would go stale. Scanning the label at the
station shows everything, always current.

**2 × 3 in, read wide** is the default: the label reads with its 3 in side left to right, the QR code (about 1 in)
on the left and the SKU large and centred beside it, on two lines (split at a hyphen) when that makes the letters
bigger, with the name underneath.

**The whole name always prints.** Text is sized to fit: a long name wraps onto more lines and gets smaller before the
SKU does, while the SKU keeps its large letters. Only a name too long to fit even at a small size (a few hundred
characters on a 2 × 1 in roll) is cut short, ending in "...". The labels still feed through the Zebra 2 in across, so it prints them sideways,
the same way the Locator prints its 2 × 3 gun tags. **2 × 3 in, upright** (QR on top) and 2 × 1 in rolls are in the
Size picker too. **Label design** on the Labels tab shows a live preview of the first product on the list.

**Matching a UPC and a SKU** doesn't need the label: on the Products tab, search the UPC to see its SKU, or the SKU
to see its UPC; on Lookup, **Link a barcode** saves a UPC to a product, and **Give it a SKU** gives one to a product
that has a UPC but no SKU.

- Add products by scanning or searching on the Labels tab, with the buttons (products with no barcode, never
  labeled, all), from **Lookup → Add to labels**, or with **Print labels for these** after a Receive (one per unit).
- **Download PDF:** at the label printer size (one label per page), on rolls of other sizes, or on Avery 5160 / 5163
  sheets (with "skip used labels" for a partly used sheet).
- **Print on the Zebra:** set `LABEL_PRINTER` (and `LABEL_DPI`) in `stock.env`; the size is whatever you pick under
  Label design (rolls only; Avery sheets are for a regular printer). The Zebra uses its own built-in font, a little
  narrower than the preview's, so its SKU lettering can come out slightly different.
- **Print a test label** (beside Print on the Zebra) sends a label of the chosen size with its edges drawn, a QR code
  and the time: the first thing to try after changing anything.
- **When a label doesn't print** (lessons from setting up the Locator's Zebra, the same printer):
  - **Check the printer's light first.** A paused Zebra accepts every job and prints nothing, so it looks connected.
    Also: out of labels, or the head open.
  - **Check the route between networks.** The Zebra (`10.42.1.132`) is on a different network from the server. On the
    server: `timeout 3 bash -c '</dev/tcp/10.42.1.132/9100' && echo open`.
  - **A blank label** means the wrong media type (direct thermal vs thermal transfer with a ribbon). **Cut off or
    shifted** means the size settings or a calibration (hold Pause and Cancel for 2 seconds on most desktop Zebras).
- **Products without a SKU** are listed on the Labels tab. **Give the ticked products SKUs** saves new ones like
  `WW-000123` to WooCommerce, after you confirm. It never touches a product that already has a SKU.

## People, badges and terminals

The **People** tab adds people, imports the Locator's People export, prints badges and the command sheet, and sets
**Require a badge** (on by default once anyone is added).

**Terminals:** a station computer or tablet gets its own key instead of the shop password. Add one on the People tab
and open its setup link once on that device (or scan the link's QR with its camera). Revoking it stops it at once.
Terminals can scan and change stock, but can't change Setup, people or settings: those need the shop password.

**Kiosk:** on a Linux Mint station, `bash setup-kiosk.sh 'http://10.42.10.246:8090/terminal/KEY'` opens Wojtek Stock
full screen at every login (its own Firefox profile, separate from a Locator kiosk on the same machine).

## Stock changed outside sales

The Setup tab lists stock that changed without a WooCommerce order or a Wojtek Stock change to explain it: an edit
in wp-admin, another plugin or app writing stock (BuckFFL, a marketplace sync), or a register that doesn't create
orders. A few now and then is normal. Many, especially soon after Wojtek Stock changed the same products, means
something else is also writing stock, and it's worth finding out what before relying on counts.

## How syncing works

- **Full sync** nightly at `FULL_SYNC_TIME` (and on demand): reads every product, 100 at a time (about 230 requests
  for 23,000 products), plus the variations of in-shop variable products. Feed products' variations are never read.
- **Quick sync** every `SYNC_MINUTES`: products changed since the last one, plus everything on orders placed or
  changed since (a sale changes stock without always changing a product's modified date).
- **Progress:** while a sync runs, the header chip shows how far along it is (Syncing 42%); click it for the Setup
  tab's progress bar, with the step it's on (page 98 of 230, then each in-shop product's variations) and about how
  long is left, worked out from its actual pace. The Setup tab also shows how long the last full sync took.
- **Live reads** whenever it matters: Lookup, Count review, and before and after every change.
- Requests go one at a time with a short gap (`WOO_MIN_GAP`), so a sync never hammers the website. A firewall page,
  timeout or 5xx is retried.
- If WooCommerce suddenly returns far fewer products than before, the local copy is kept and the Setup tab says why
  (`ALLOW_BIG_DROP=on` lets one sync through when the drop is real).

## Backups

**Dropbox** (Setup tab): each night, the change log, the product list, people, settings changes and a copy of the
database go into a dated folder. The Wojtek Locator's Dropbox app works; give Wojtek Stock its own folder (the default
is `/Wojtek Stock backups`).

**By hand:** everything is in `~/wojtek-stock/data/`. A consistent copy while it runs:
```bash
cd ~/wojtek-stock && sudo docker compose exec stock python3 -c "import sqlite3; s=sqlite3.connect('/data/stock.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d)"
```
A Proxmox backup of the VM covers it too.

## Safeguards

- **A password is required.** Without `STOCK_PASSWORD` in `stock.env`, Wojtek Stock refuses to start (the log says
  why), rather than run open to anyone who can reach it.
- Feed prefixes and gun categories are checked against the **live** product before every write (and the product's
  parent, for a variation), not just the local copy.
- Changes are locked until Setup is confirmed; new installs start in dry run.
- Only `stock_quantity`, `global_unique_id` (linking a barcode), `sku` (generating one, only when empty) and order
  notes (a return) are ever written to existing products and orders; new products are created only when you copy a
  feed listing (the listing itself is only read), and new orders only for GunBroker sales. On GunBroker, only a linked
  listing's quantity is ever changed. Every product write is read back to confirm.
- A return can't bring back more than the order sold, minus what already came back here or was restocked by
  WooCommerce; this is checked again, live, when you press Return.
- Receive and Adjust apply the difference to live stock, so sales aren't overwritten; Count refuses if stock moved
  since the review; nothing goes below zero.
- Settings, Setup, people and terminals need the shop password, not a station terminal. Requests from other websites
  are refused.
- Spreadsheet exports keep text that starts like a formula (`=`, `+`, `-`, `@`) as plain text.
- The container runs as an unprivileged user, and `stock.env` is never in the zip.

## Good to know

- **Logs:** `cd ~/wojtek-stock && sudo docker compose logs -f`
- **Restart:** `cd ~/wojtek-stock && sudo docker compose restart`
- **Stop:** `cd ~/wojtek-stock && sudo docker compose down` (data stays in `data/`)
- **"Can't write to /data"** in the log: `cd ~/wojtek-stock && sudo chown -R 10001:10001 data`, then up again.
- **Sync says "web page instead of data":** a firewall (Cloudflare, Wordfence, the host's) is challenging the API.
  Allow `/wp-json/wc/` for your server's public IP.
- **Test connection says the key was refused:** check the key is Read/Write and belongs to an admin or shop manager.
  If your host strips the Authorization header, `WOO_AUTH=auto` already falls back to sending the key in the address.
- Port 8090 is for your internal network; reach it remotely through your VPN, as with the Locator.

## Tests

`./run-tests.sh` (in `~/wojtek-stock`) runs the server tests in a throwaway Python container: about 3 minutes, against
a pretend WooCommerce store with a temporary database, never your settings, your data or your store. The last line says
how many passed and names any that failed. `bash tests/test_deploy.sh` checks `deploy.sh` with a pretend GitHub, and
`python3 tests/zpl_preview.py` draws what the Zebra will print, from the printer commands themselves (a check that no
text runs over is part of the tests), and `tests/test_ui.py` drives the page in a headless browser (it needs Playwright, so it's run before each version is
handed over rather than on the server). The tests live in `tests/` and aren't built into the image.

## eBay and Amazon

- **eBay and Amazon:** best done with WP-Lister Pro (a WooCommerce plugin) rather than in Wojtek Stock: it keeps
  WooCommerce as the master copy and brings marketplace orders in as WooCommerce orders, which Wojtek Stock already
  handles. Many gun parts can't be sold there anyway.
