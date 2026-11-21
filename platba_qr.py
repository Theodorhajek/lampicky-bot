"""QR platba (český formát SPAYD) na účet lampiček – zákazník ji načte v bankovní aplikaci.

  .venv/bin/python platba_qr.py 3535 "Libor Myslivecek"      → navrhy/qr/<jméno>-<částka>.png
"""
import re
import sys
import unicodedata
from pathlib import Path

import qrcode

HERE = Path(__file__).resolve().parent
def _ucet():
    for line in (HERE / ".env").read_text().splitlines():
        if line.startswith("PLATBY_UCET="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("chybí PLATBY_UCET v .env")


UCET = _ucet()  # účet lampiček – jen v .env, ne v gitu


def iban(ucet=UCET):
    num, bank = ucet.split("/")
    prefix, _, number = num.rpartition("-")
    bban = bank.zfill(4) + prefix.zfill(6) + number.zfill(10)
    check = 98 - int(bban + "123500") % 97  # C=12, Z=35, "00"
    return f"CZ{check:02d}{bban}"


def ascii_(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[*]", "", s).upper()[:60]


def make(amount, message, out=None):
    spd = f"SPD*1.0*ACC:{iban()}*AM:{float(amount):.2f}*CC:CZK*MSG:{ascii_(message)}"
    img = qrcode.make(spd, box_size=12, border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
    out = Path(out) if out else HERE / "navrhy" / "qr" / f"{re.sub(r'[^a-z0-9]+', '-', ascii_(message).lower()).strip('-')}-{int(amount)}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out, spd


if __name__ == "__main__":
    f, spd = make(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "Lampicka")
    print(f)
    print(spd)
