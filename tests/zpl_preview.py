#!/usr/bin/env python3
"""Draws the ZPL Wojtek Stock sends to the Zebra as a picture, to check a label's layout without a printer (an idea
from the Locator). Understands what Stock uses: ^PW ^LL ^FO ^A0N/^A0R ^FB ^FD ^FS ^BQN ^GB. The font stands in for the
Zebra's font 0 (a narrow bold), so the real label can differ a little; overflow() reports text wider than its box.
    python3 tests/zpl_preview.py   (draws sample labels into /tmp/zpl-preview)
Needs Pillow."""
import os
import re
import sys

from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf"
HERE = os.path.dirname(os.path.abspath(__file__))


def _qr(text):
    os.environ.setdefault("STOCK_DB", "/tmp/zpl-preview.db")
    sys.path.insert(0, os.path.dirname(HERE))
    import importlib.util
    spec = importlib.util.spec_from_file_location("stock_server", os.path.join(os.path.dirname(HERE), "server.py"))
    if "stock_server" not in sys.modules:
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        sys.modules["stock_server"] = m
    return sys.modules["stock_server"].qr_encode(text)


def render(zpl, scale=1):
    """(image, problems): the label as the printer would draw it (as it comes out of the printer)."""
    W = int(re.search(r"\^PW(\d+)", zpl).group(1))
    H = int(re.search(r"\^LL(\d+)", zpl).group(1))
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    problems = []
    x = y = 0
    font = ("N", 30, 30)
    fb = None
    qr_mag = None
    for cmd in zpl.split("^")[1:]:
        op, arg = cmd[:2], cmd[2:]
        if op == "FO":
            x, y = (int(v) for v in arg.split(",")[:2])
        elif op == "A0":
            o, h, w = arg[0], *(int(v) for v in arg[2:].split(",")[:2])
            font = (o, h, w)
        elif op == "FB":
            p = arg.split(",")
            fb = (int(p[0]), int(p[1]), int(p[2] or 0), p[3] or "L")
        elif op == "BQ":
            qr_mag = int(arg.split(",")[2])
        elif op == "GB":
            w, h, t = (int(v) for v in arg.split(",")[:3])
            d.rectangle([x, y, x + w - 1, y + h - 1], outline=0, width=t)
        elif op == "FD":
            text = arg
            if qr_mag:
                g = _qr(text[3:] if text.startswith("MA,") else text)
                for r, row in enumerate(g):
                    for c, v in enumerate(row):
                        if v:
                            d.rectangle([x + c * qr_mag, y + r * qr_mag, x + (c + 1) * qr_mag - 1, y + (r + 1) * qr_mag - 1], fill=0)
                if x + len(g) * qr_mag > W or y + len(g) * qr_mag > H:
                    problems.append(f"QR code runs off the label at {x},{y}")
                continue
            o, h, w = font
            bw, n, sp, al = fb or (10 ** 5, 1, 0, "L")
            lines = text.split("\\&")[:n]
            box = Image.new("L", (bw if fb else W, h * len(lines) + sp * (len(lines) - 1)), 255)
            bd = ImageDraw.Draw(box)
            f = ImageFont.truetype(FONT, max(6, int(h * 0.92)))
            for i, ln in enumerate(lines):
                tw = bd.textlength(ln, font=f) * (w / h) * 0.86   # font 0 is narrower than this stand-in at the same height
                if fb and tw > bw + 2:
                    problems.append(f"Too wide by {int(tw - bw)} dots: {ln!r}")
                tmp = Image.new("L", (int(bd.textlength(ln, font=f)) + 4, h + 4), 255)
                ImageDraw.Draw(tmp).text((0, 0), ln, font=f, fill=0)
                tmp = tmp.resize((max(1, int(tmp.width * (w / h) * 0.86)), tmp.height))
                lx = 0 if al == "L" else (box.width - tmp.width) // 2 if al == "C" else box.width - tmp.width
                box.paste(tmp, (max(0, lx), i * (h + sp)), mask=Image.eval(tmp, lambda v: 255 - v))
            if o == "R":   # rotated 90 degrees clockwise
                box = box.rotate(-90, expand=True)
            img.paste(0, (x, y), mask=Image.eval(box, lambda v: 255 - v))
            if x + box.width > W + 2 or y + box.height > H + 2:
                problems.append(f"Text runs off the label: {text[:40]!r}")
            fb = None
        elif op == "FS":
            qr_mag = None
    if scale != 1:
        img = img.resize((W * scale, H * scale))
    return img, problems


def as_read(img, sideways):
    """A sideways label turned the way it's read."""
    return img.rotate(90, expand=True) if sideways else img


if __name__ == "__main__":
    s = sys.modules.get("stock_server") or (_qr("x") and sys.modules["stock_server"])
    out = "/tmp/zpl-preview"
    os.makedirs(out, exist_ok=True)
    samples = [{"sku": "HORN-9MM-115", "name": "Hornady 9mm 115gr FTX", "attrs": "", "gtin": "090255000001"},
               {"sku": "MAG570-BLK", "name": "Magpul PMAG 30 AR/M4 GEN M3 Window 5.56x45 NATO / .223 Remington 30-Round Polymer Magazine, Black",
                "attrs": "", "gtin": "873750008036"},
               {"sku": "WW-TEE-S", "name": "Wojtek T-shirt", "attrs": "Size: S", "gtin": ""}]
    for i, r in enumerate(samples):
        for w, l, sw, tag in ((3, 2, True, "3x2-wide"), (2, 3, False, "2x3-upright"), (2, 1, False, "2x1")):
            img, problems = render(s.zpl_label(r, w, l, True, False, sw))
            as_read(img, sw).save(f"{out}/{i}-{tag}.png")
            print(f"{i}-{tag}: {'OK' if not problems else '; '.join(problems)}")
