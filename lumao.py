"""Objednávky na server Lumao (Skupiny → Shopify). Formát: docs/lumao-api.md, token v .env (LUMAO_TOKEN).

Tok: bot domluví objednávku → extract_order() z konverzace udělá koncept → v dashboardu ho zkontroluješ
(„Zkusit“ = test, nic se nezapíše; „Poslat do Shopify“ = naostro) → tick() v botovi opakuje odeslání
při výpadku / 502 a jednou za 20 minut zjišťuje stav (ve výrobě, odesláno + číslo zásilky).

  .venv/bin/python lumao.py test        – zkušební objednávka (test: true, nic se nezapíše)
  .venv/bin/python lumao.py seznam      – objednávky pro server a jejich stav
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

import data

HERE = Path(__file__).resolve().parent
STATUS_EVERY = 20 * 60
STATES = {"koncept": "Koncept – zkontroluj", "k_odeslani": "Čeká na odeslání", "zapsano": "Zapsáno v Shopify",
          "chyba": "Chyba – oprav a pošli znovu", "zahozeno": "Zahozeno"}
SERVER_STATES = {"prijato": "ve Skupinách, Shopify zatím ne", "v_shopify": "v Shopify (neodpovídá)",
                 "ceka_na_schvaleni": "čeká na 👍 v Telegramu", "ve_vyrobe": "ve výrobě", "vyrobeno": "vyrobeno",
                 "doruceno": "doručeno", "odeslano": "odesláno",
                 "zruseno": "zrušeno"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS server_orders (
  id INTEGER PRIMARY KEY, ts REAL, person TEXT, oid TEXT UNIQUE, payload TEXT, files TEXT,
  status TEXT DEFAULT 'koncept', shopify TEXT, server_state TEXT, tracking TEXT, error TEXT,
  tries INTEGER DEFAULT 0, sent_ts REAL, checked_ts REAL, test_result TEXT);
"""


def env():
    out = dict(os.environ)
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                out.setdefault(k.strip(), v.strip())
    return out


def url():
    return env().get("LUMAO_URL", "https://app.lumao.cz/webhooks/lampicky-bot/objednavky")


def headers():
    tok = env().get("LUMAO_TOKEN")
    if not tok:
        raise RuntimeError("chybí LUMAO_TOKEN v .env")
    return {"Authorization": f"Bearer {tok}"}


def db():
    con = data.db()
    con.executescript(SCHEMA)
    return con


def new_oid():
    day = time.strftime("%Y%m%d")
    with db() as con:
        n = con.execute("SELECT count(*) FROM server_orders WHERE oid LIKE ?", (f"lamp-{day}-%",)).fetchone()[0]
    return f"lamp-{day}-{n + 1:04d}"


# ---------- odeslání ----------

def post(payload, files):
    """Pošle objednávku. Vrátí (http_kód, json_odpověď). files = cesty k PNG v pořadí položek."""
    opened = [open(f, "rb") for f in files]
    try:
        parts = [(f"navrh_{i}", (Path(f).name, fh, "image/png")) for i, (f, fh) in enumerate(zip(files, opened))]
        r = requests.post(url(), headers=headers(), data={"objednavka": json.dumps(payload, ensure_ascii=False)},
                          files=parts, timeout=60)
    finally:
        for fh in opened:
            fh.close()
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"ok": False, "chyba": "neni_json", "zprava": r.text[:300]}


def check_files(payload, files):
    n = len(payload.get("polozky") or [])
    if len(files) != n:
        return f"počet obrázků ({len(files)}) nesedí s počtem položek ({n})"
    for f in files:
        if not Path(f).is_file():
            return f"chybí obrázek {f}"
    return ""


def send(row_id, test=False):
    """Odešle uloženou objednávku (test=True jen ověří). Vrátí (ok, text pro člověka)."""
    with db() as con:
        r = con.execute("SELECT oid, payload, files, tries FROM server_orders WHERE id=?", (row_id,)).fetchone()
    if not r:
        return False, "objednávka neexistuje"
    oid, payload, files, tries = r
    payload = json.loads(payload)
    files = json.loads(files or "[]")
    payload["id_objednavky"] = oid
    payload["test"] = bool(test)
    pl = payload.setdefault("platba", {})
    if pl.get("zpusob") == "dobirka":
        pl["zaplaceno"] = False
    elif not pl.get("zaplaceno"):
        try:
            import platby  # noqa: PLC0415
            with db() as con:
                person = con.execute("SELECT person FROM server_orders WHERE id=?", (row_id,)).fetchone()[0]
            total = sum(i.get("pocet", 1) * i.get("cena_ks", 0) for i in payload.get("polozky") or [])
            pl["zaplaceno"] = sum(a for a, _ in platby.paid_by(person)) >= total > 0
        except Exception:  # noqa: BLE001
            pass
    err = check_files(payload, files)
    if err:
        return False, err
    try:
        code, res = post(payload, files)
    except requests.RequestException as e:
        if not test:
            with db() as con:
                con.execute("UPDATE server_orders SET status='k_odeslani', tries=?, error=? WHERE id=?",
                            (tries + 1, f"výpadek: {e}"[:300], row_id))
        return False, f"server nedostupný ({e.__class__.__name__}) – zkusím znovu"
    msg = res.get("zprava") or res.get("chyba") or f"HTTP {code}"
    if test:
        with db() as con:
            con.execute("UPDATE server_orders SET test_result=? WHERE id=?", (json.dumps(res, ensure_ascii=False), row_id))
        return bool(res.get("ok")), msg
    with db() as con:
        if res.get("ok"):
            shop = (res.get("shopify") or {}).get("cislo") or ""
            con.execute("UPDATE server_orders SET status='zapsano', shopify=?, server_state=?, error='', sent_ts=?, "
                        "tries=? WHERE id=?", (shop, res.get("stav") or "ceka_na_schvaleni", time.time(), tries + 1, row_id))
            return True, f"zapsáno v Shopify {shop}".strip()
        retry = code >= 500 or code == 409 or res.get("opakovat")
        con.execute("UPDATE server_orders SET status=?, tries=?, error=? WHERE id=?",
                    ("k_odeslani" if retry and tries < 10 else "chyba", tries + 1, msg[:300], row_id))
    return False, msg + (" – zkusím znovu" if retry else "")


def status(oid):
    r = requests.get(f"{url()}/{oid}", headers=headers(), timeout=30)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {}


def auto_ready(row_id):
    """Smí koncept odejít sám? Úplný (bez poznámky), jednoznačné výdejní místo, ne návrh na míru, a zaplaceno:
    převod = spárované platby pokryjí zboží + dopravu, dobírka = zaplacené poštovné. Vrátí (ano, důvod)."""
    import platby  # noqa: PLC0415
    with db() as con:
        person, payload, note = con.execute("SELECT person, payload, coalesce(error,'') FROM server_orders WHERE id=?",
                                            (row_id,)).fetchone()
    if note.strip():
        return False, note
    p = json.loads(payload)
    if any(re.search(r"(?i)jméno|nápis", i.get("motiv", "")) for i in p.get("polozky") or []):
        return False, "návrh na míru"
    paid = sum(a for a, _ in platby.paid_by(person))
    zpusob = (p.get("platba") or {}).get("zpusob")
    goods = sum(i.get("pocet", 1) * i.get("cena_ks", 0) for i in p.get("polozky") or [])
    if zpusob == "prevod":
        need = goods + (p.get("doprava_cena") or 85)
        return (paid >= need, f"zaplaceno {paid:.0f} z {need} Kč")
    if zpusob == "dobirka":
        return (paid >= 85, f"poštovné {'zaplaceno' if paid >= 85 else 'zatím nezaplaceno'}")
    return False, "platba hotově – ručně"


def tick(notify=None):
    """Z hlavní smyčky bota: dopošle objednávky po výpadku, zaplacené koncepty pošle samo (config autoObjednavky)
    a jednou za 20 min zjistí stav."""
    try:
        auto = json.loads((HERE / "config.json").read_text()).get("autoObjednavky", False)
    except (OSError, ValueError):
        auto = False
    if auto:
        with db() as con:
            drafts = con.execute("SELECT id, person FROM server_orders WHERE status='koncept'").fetchall()
        for rid, person in drafts:
            try:
                ok_, why = auto_ready(rid)
            except Exception:  # noqa: BLE001
                continue
            if ok_:
                ok, msg = send(rid)
                if notify:
                    notify("Lampičky – objednávka odeslána sama" if ok else "Lampičky – objednávka se neodeslala",
                           f"{person}: {msg} ({why})")
    with db() as con:
        waiting = con.execute("SELECT id, person FROM server_orders WHERE status='k_odeslani'").fetchall()
        due = con.execute("SELECT id, oid, person, server_state FROM server_orders WHERE status='zapsano' "
                          "AND coalesce(server_state,'') NOT IN ('doruceno','zruseno') AND coalesce(checked_ts,0) < ?",
                          (time.time() - STATUS_EVERY,)).fetchall()
    for rid, person in waiting:
        ok, msg = send(rid)
        if notify and ok:
            notify("Lampičky – objednávka v Shopify", f"{person}: {msg}")
    for rid, oid, person, old in due:
        try:
            code, res = status(oid)
        except requests.RequestException:
            continue
        st = res.get("stav") or old
        if st == "prijato":
            with db() as con:
                con.execute("UPDATE server_orders SET status='k_odeslani', checked_ts=? WHERE id=?", (time.time(), rid))
            continue
        zs = res.get("zasilky") or []  # seznam zásilek; číslo pod cislo / cislo_zasilky / tracking (podle serveru)
        track = ", ".join(str(z.get("cislo") or z.get("cislo_zasilky") or z.get("tracking") or z) if isinstance(z, dict) else str(z)
                          for z in zs) or res.get("cislo_zasilky") or ""
        with db() as con:
            con.execute("UPDATE server_orders SET server_state=?, tracking=coalesce(nullif(?,''),tracking), checked_ts=? "
                        "WHERE id=?", (st, track, time.time(), rid))
        if notify and st != old and st in SERVER_STATES:
            notify("Lampičky – stav objednávky", f"{person}: {SERVER_STATES[st]} {track}".strip())


# ---------- koncept z konverzace ----------

ORDER_SCHEMA = {
    "type": "object",
    "properties": {
        "complete": {"type": "boolean"},
        "missing": {"type": "string"},
        "jmeno": {"type": "string"}, "telefon": {"type": "string"}, "email": {"type": "string"},
        "dopravce": {"type": "string", "enum": ["zasilkovna", "ppl"]},
        "typ": {"type": "string", "enum": ["vydejni_misto", "adresa"]},
        "nazev_mista": {"type": "string"}, "id_mista": {"type": "string"},
        "ulice": {"type": "string"}, "psc": {"type": "string"}, "mesto": {"type": "string"},
        "zpusob": {"type": "string", "enum": ["dobirka", "prevod", "hotove"]},
        "polozky": {"type": "array", "items": {"type": "object", "properties": {
            "motiv": {"type": "string"}, "text": {"type": "string"}, "pocet": {"type": "integer"},
            "cena_ks": {"type": "integer"}, "podstavec": {"type": "string", "enum": ["barevny", "tepla_bila"]}},
            "required": ["motiv", "text", "pocet", "cena_ks", "podstavec"], "additionalProperties": False}},
        "poznamka": {"type": "string"},
    },
    "required": ["complete", "missing", "jmeno", "telefon", "email", "dopravce", "typ", "nazev_mista", "id_mista",
                 "ulice", "psc", "mesto", "zpusob", "polozky", "poznamka"],
    "additionalProperties": False,
}

ORDER_PROMPT = """Z konverzace prodejce LED lampiček se zákazníkem vytáhni objednávku. Vyplň jen to, co v konverzaci opravdu je;
co chybí, nech prázdné a napiš do missing (česky, krátce). complete=true jen když je jméno, telefon, doprava s adresou
(u výdejního místa aspoň jeho název/adresa), způsob platby a aspoň jedna položka.
- motiv = značka/obrázek (např. „BMW“, „Kawasaki Ninja“), text = jméno nebo nápis na lampičce (jinak prázdné).
- cena_ks je 690, pokud se nedomluvili jinak. podstavec „barevny“, pokud zákazník nechtěl teplou bílou.
- dopravce: Zásilkovna/Z-BOX → zasilkovna, PPL nebo doručení na adresu → ppl. typ: výdejní místo / Z-BOX / ParcelShop → vydejni_misto.
- zpusob: dobírka → dobirka, předem na účet → prevod.
- psc jen číslice. id_mista jen když ho zákazník napsal (číslo místa Zásilkovny).
- poznamka: co je důležité pro výrobu (barva světla, umístění jména…), jinak prázdné."""


def extract_order(person, transcript, claude_path, model, comments=""):
    prompt = f"Zákazník: {person}\n\nKonverzace (nejstarší nahoře):\n{transcript}"
    if comments:
        prompt += f"\n\nJeho komentáře pod příspěvky:\n{comments}"
    envv = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # jen předplatné
    proc = subprocess.run([claude_path, "-p", "--output-format", "json", "--model", model,
                           "--system-prompt", ORDER_PROMPT, "--json-schema", json.dumps(ORDER_SCHEMA),
                           "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence"],
                          input=prompt, capture_output=True, text=True, encoding="utf-8",
                          cwd=tempfile.gettempdir(), env=envv, timeout=180)
    out = json.loads(proc.stdout)
    if out.get("is_error") or not out.get("structured_output"):
        raise RuntimeError(f"claude: {out.get('result') or 'bez odpovědi'}"[:200])
    return out["structured_output"]


def group_of(person):
    try:
        with data.db() as con:
            r = con.execute("SELECT grp FROM comments WHERE person=? AND coalesce(grp,'')<>'' ORDER BY ts DESC LIMIT 1",
                            (person,)).fetchone()
        return r[0] if r else ""
    except sqlite3.Error:
        return ""


# ---------- výdejní místa Zásilkovny (veřejný seznam přes klíč API, obnova jednou denně) ----------

def _points():
    f = HERE / "data" / "zasilkovna_mista.json"
    if not f.exists() or time.time() - f.stat().st_mtime > 86400:
        key = env().get("ZASILKOVNA_API_KEY")
        if not key:
            return []
        r = requests.get(f"https://pickup-point.api.packeta.com/v5/{key}/branch/json?lang=cs", timeout=120)
        r.raise_for_status()
        d = r.json()
        items = d.get("data", d) if isinstance(d, dict) else d
        items = list(items.values()) if isinstance(items, dict) else items
        keep = [{k: b.get(k) for k in ("id", "name", "place", "street", "city", "zip")} for b in items if b.get("country") == "cz"]
        f.write_text(json.dumps(keep, ensure_ascii=False))
    return json.loads(f.read_text())


def find_point(*texts):
    """Najde výdejní místo Zásilkovny podle textu od zákazníka (název, ulice, město). Vrátí dict nebo None."""
    import unicodedata  # noqa: PLC0415
    fold = lambda s: unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    q = fold(" ".join(t for t in texts if t))
    words = set(re.findall(r"[a-z0-9]{3,}", q))
    best, score, second = None, 0, 0
    for b in _points():
        hay = fold(f"{b['name']} {b['place']} {b['street']} {b['city']} {b['zip']}")
        sc = sum(1 for w in words if w in hay)
        if fold(b["city"]) in q:
            sc += 2
        if sc > score:
            best, score, second = b, sc, score
        elif sc > second:
            second = sc
    if score < 3:
        return None
    return dict(best, jisty=score >= 4 and score > second)  # jisty = jednoznačná shoda (jen pak jde objednávka sama)


def to_payload(o, group, fb_name):
    d = {"dopravce": o["dopravce"], "typ": o["typ"], "ulice": o["ulice"], "psc": re.sub(r"\D", "", o["psc"]),
         "mesto": o["mesto"], "zeme": "CZ"}
    if o["typ"] == "vydejni_misto":
        d.update({"id_mista": o["id_mista"], "nazev_mista": o["nazev_mista"]})
        if o["dopravce"] == "zasilkovna" and not o["id_mista"]:
            try:
                pt = find_point(o["nazev_mista"], o["ulice"], o["mesto"], o["psc"])
            except Exception:  # noqa: BLE001
                pt = None
            if pt:
                d.update({"id_mista": str(pt["id"]), "nazev_mista": pt["place"] or pt["name"], "ulice": pt["street"],
                          "psc": re.sub(r"\D", "", pt["zip"] or ""), "mesto": pt["city"], "_jisty": pt.get("jisty", False)})
    p = {"skupina": group or "Messenger",
         "datum": time.strftime("%Y-%m-%d"),
         "zakaznik": {"jmeno": o["jmeno"] or fb_name, "telefon": o["telefon"], "email": o["email"], "facebook": fb_name},
         "doruceni": d,
         "polozky": [{"motiv": " + ".join(x for x in (i["motiv"], f"jméno {i['text']}" if i["text"] else "") if x),
                      "pocet": max(1, i["pocet"]), "cena_ks": i["cena_ks"] or 690, "podstavec": i["podstavec"]}
                     for i in o["polozky"]],
         "platba": {"zpusob": o["zpusob"], "zaplaceno": False}}
    if o["poznamka"]:
        p["poznamka"] = o["poznamka"]
    return p


def pngs_for(person, items):
    """PNG ke každé položce: schválený návrh pro zákazníka, jinak vygenerovaný motiv."""
    import navrh  # noqa: PLC0415
    approved = [r for r in data.proposals("odeslano") + data.proposals("schvaleno") if r[2] == person]
    out = []
    for it in items:
        hit = next((r[3] for r in reversed(approved) if (r[4] or "").lower() in it["motiv"].lower()), None)
        out.append(str(hit) if hit and Path(hit).is_file() else str(navrh.make(it["motiv"], it.get("text", ""), person)))
    return out


def add_draft(person, payload, files, note=""):
    with db() as con:
        con.execute("INSERT INTO server_orders(ts, person, oid, payload, files, error) VALUES (?,?,?,?,?,?)",
                    (time.time(), person, new_oid(), json.dumps(payload, ensure_ascii=False, indent=1),
                     json.dumps([str(f) for f in files]), note))
        return con.execute("SELECT last_insert_rowid()").fetchone()[0]


def draft_from_conversation(person, transcript, claude_path, model):
    """Volá bot, když má objednávka všechny údaje. Koncept jde do dashboardu ke schválení."""
    with db() as con:
        if con.execute("SELECT 1 FROM server_orders WHERE person=? AND status IN ('koncept','k_odeslani') ",
                       (person,)).fetchone():
            return None  # koncept už čeká
    o = extract_order(person, transcript, claude_path, model, data.comments_of(person))
    payload = to_payload(o, group_of(person), person)
    files = pngs_for(person, o["polozky"]) if o["polozky"] else []
    note = "" if o["complete"] else f"Chybí: {o['missing']}"
    if any(i.get("text") for i in o["polozky"]):
        note = (note + "; " if note else "") + "návrh na míru (jméno/nápis) – zkontroluj obrázek"
    if o["typ"] == "vydejni_misto" and not payload["doruceni"].get("id_mista"):
        note = (note + "; " if note else "") + "doplň ID výdejního místa"
    elif o["typ"] == "vydejni_misto" and not o["id_mista"] and not payload["doruceni"].pop("_jisty", False):
        note = (note + "; " if note else "") + f"výdejní místo dohledáno: {payload['doruceni']['nazev_mista']} – zkontroluj"
    return add_draft(person, payload, files, note)


def rows(statuses=("koncept", "k_odeslani", "zapsano", "chyba")):
    with db() as con:
        q = ",".join("?" * len(statuses))
        return con.execute(f"SELECT id, ts, person, oid, payload, files, status, shopify, server_state, tracking, error, "
                           f"test_result FROM server_orders WHERE status IN ({q}) ORDER BY id DESC", statuses).fetchall()


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "seznam"
    if cmd == "test":
        f = HERE / "navrhy" / "zakaznici" / "zdenka-bmw-rada-3.png"
        payload = {"skupina": "Test bota", "zakaznik": {"jmeno": "Test Bot", "telefon": "+420777000000", "email": "test@example.cz", "facebook": "Test Bot"},
                   "doruceni": {"dopravce": "zasilkovna", "typ": "vydejni_misto", "id_mista": "12345", "nazev_mista": "Z-BOX Přerov",
                                "ulice": "Čechova 12", "psc": "75002", "mesto": "Přerov", "zeme": "CZ"},
                   "polozky": [{"motiv": "BMW + jméno Raďa", "pocet": 1, "cena_ks": 690, "podstavec": "barevny"}],
                   "platba": {"zpusob": "dobirka", "zaplaceno": False}, "doprava_cena": 89, "poznamka": "ZKUŠEBNÍ OBJEDNÁVKA BOTA – nic nevyrábět."}
        payload["id_objednavky"] = "test-bot-" + time.strftime("%Y%m%d-%H%M")
        payload["test"] = True
        code, res = post(payload, [f])
        print(code, json.dumps(res, ensure_ascii=False, indent=1))
    else:
        for r in rows(tuple(STATES)):
            print(r[3], r[2], STATES.get(r[6], r[6]), r[7] or "", SERVER_STATES.get(r[8] or "", r[8] or ""), r[10] or "")


if __name__ == "__main__":
    main()
