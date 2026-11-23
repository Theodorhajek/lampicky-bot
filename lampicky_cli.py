"""Ovládání bota z příkazové řádky (používá ho i sezení v Telegramu).

  .venv/bin/python lampicky_cli.py stav                     – běží bot? poslední akce, předané konverzace
  .venv/bin/python lampicky_cli.py konverzace "Jméno"       – posledních 25 zpráv + co o něm bot zapsal
  .venv/bin/python lampicky_cli.py napis "Jméno" "text"     – bot napíše zprávu v telefonu (prázdný řádek = další zpráva)
  .venv/bin/python lampicky_cli.py botovi "Jméno"           – bot zase odpovídá sám
  .venv/bin/python lampicky_cli.py prevzit "Jméno"          – bot mlčí, převezmeš to ty
  .venv/bin/python lampicky_cli.py fronta                   – co čeká na odeslání z dashboardu/Telegramu
  .venv/bin/python lampicky_cli.py objednavky               – objednávky pro server (Shopify) a jejich stav
  .venv/bin/python lampicky_cli.py test-objednavky ID       – ověřit objednávku na serveru (test, nic se nezapíše)
  .venv/bin/python lampicky_cli.py poslat-objednavku ID     – NAOSTRO do Shopify (jen po Theodorově „ano“)
  .venv/bin/python lampicky_cli.py platby                   – poslední příchozí platby
  .venv/bin/python lampicky_cli.py telegram zap|vyp         – zapnout / úplně vypnout napojení na Telegram
  .venv/bin/python lampicky_cli.py prehled                  – co čeká na Theodora (návrhy, předané konverzace, objednávky, chyby)
  .venv/bin/python lampicky_cli.py navrhy                   – návrhy obrázků ke schválení (cesta k obrázku → SEND_FILE)
  .venv/bin/python lampicky_cli.py poslat-navrh ID ["popisek"] – schválit návrh, bot ho pošle zákazníkovi
  .venv/bin/python lampicky_cli.py zamitnout-navrh ID       – návrh zahodit (zákazník nic nedostane)
  .venv/bin/python lampicky_cli.py lekce "text"             – nová lekce pro bota (nahoru do lekce.md)
  .venv/bin/python lampicky_cli.py skupiny                  – fronta příspěvků do skupin a jejich stav
  .venv/bin/python lampicky_cli.py odkaz-skupiny "Název" URL – bot bude skupinu otevírat odkazem (bez hledání)
  .venv/bin/python lampicky_cli.py objednavka ID            – celý koncept objednávky (JSON) + obrázky
  .venv/bin/python lampicky_cli.py upravit-objednavku ID 'cesta.klic=hodnota' … – změnit údaj konceptu (např. zakaznik.telefon=777…)
  .venv/bin/python lampicky_cli.py zahodit-objednavku ID    – koncept zahodit
"""

import subprocess
import sys
import time
from pathlib import Path

import data

HERE = Path(__file__).resolve().parent


def main():
    a = sys.argv[1:] or ["stav"]
    cmd = a[0]
    if cmd == "stav":
        print(subprocess.run([str(HERE / "ovladani.sh"), "stav"], capture_output=True, text=True).stdout)
        log = HERE / "log.csv"
        if log.exists():
            lines = log.read_text(encoding="utf-8").splitlines()
            print("--- předáno tobě (posledních 10):")
            for l in [x for x in lines if "PŘEDÁNO" in x or "ručně" in x.lower()][-10:]:
                print(l[:220])
    elif cmd == "konverzace":
        person = a[1]
        with data.db() as con:
            rows = con.execute("SELECT direction, text FROM messages WHERE person=? ORDER BY id DESC LIMIT 25", (person,)).fetchall()
        for d_, t in reversed(rows):
            print({"in": "Zákazník", "bot": "Bot", "manual": "Theodor"}.get(d_, d_) + ":", t)
        print("--- komentáře:", data.comments_of(person) or "žádné")
        log = HERE / "log.csv"
        if log.exists():
            print("--- log bota:")
            for l in [x for x in log.read_text(encoding="utf-8").splitlines() if person in x][-8:]:
                print(l[:220])
    elif cmd == "napis":
        data.outbox_add("zprava", a[1], a[2])
        print("zařazeno – bot to napíše při nejbližší kontrole (do pár minut, mimo noční pauzu 01:15–05:00)")
    elif cmd in ("botovi", "prevzit"):
        data.outbox_add(cmd, a[1])
        print("zařazeno")
    elif cmd == "fronta":
        with data.db() as con:
            for r in con.execute("SELECT id, kind, person, status, coalesce(error,''), substr(text,1,80) FROM outbox ORDER BY id DESC LIMIT 15"):
                print(r)
    elif cmd == "objednavky":
        import lumao
        for r in lumao.rows(tuple(lumao.STATES)):
            print(f"#{r[0]} {r[3]} {r[2]} – {lumao.STATES.get(r[6], r[6])} {r[7] or ''} {lumao.SERVER_STATES.get(r[8] or '', r[8] or '')} {r[10] or ''}")
            print("   ", r[4].replace("\n", " ")[:400])
    elif cmd in ("test-objednavky", "poslat-objednavku"):
        import lumao
        ok, msg = lumao.send(int(a[1]), test=cmd.startswith("test"))
        print("OK" if ok else "CHYBA", msg)
    elif cmd == "platby":
        import platby
        for r in platby.recent(15):
            print(time.strftime("%d.%m.", time.localtime(r[0])), r[1:])
    elif cmd == "telegram":
        import json
        c = HERE / "config.json"
        cfg = json.loads(c.read_text())
        if len(a) > 1:
            cfg["telegram"] = a[1] in ("zap", "on", "1", "ano")
            c.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
        print("Telegram:", "ZAPNUTO" if cfg.get("telegram") else "VYPNUTO (bot nic do Telegramu neposílá)")
    elif cmd in ("objednavka", "upravit-objednavku", "zahodit-objednavku"):
        import json
        import lumao
        rid = int(a[1])
        with lumao.db() as con:
            row = con.execute("SELECT payload, files, status FROM server_orders WHERE id=?", (rid,)).fetchone()
        if not row:
            print("neexistuje")
            return
        if cmd == "zahodit-objednavku":
            with lumao.db() as con:
                con.execute("UPDATE server_orders SET status='zahozeno' WHERE id=? AND status IN ('koncept','chyba')", (rid,))
            print("zahozeno")
            return
        payload = json.loads(row[0])
        if cmd == "upravit-objednavku":
            if row[2] not in ("koncept", "chyba"):
                print("už odeslaná – měnit nejde")
                return
            for kv in a[2:]:
                path, _, val = kv.partition("=")
                try:
                    val = json.loads(val)
                except ValueError:
                    pass
                cur = payload
                keys = [int(k) if k.isdigit() else k for k in path.split(".")]
                for k in keys[:-1]:
                    cur = cur[k]
                cur[keys[-1]] = val
            with lumao.db() as con:
                con.execute("UPDATE server_orders SET payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False, indent=1), rid))
            print("uloženo")
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        print("stav:", row[2], "| obrázky:", row[1])
    elif cmd == "navrhy":
        for pid, ts, person, file, motif, label, caption, st in data.proposals():
            print(f"#{pid} {person} – {motif} {label or ''} | popisek: {caption}\n    {file}")
        print("(žádné)" if not data.proposals() else "")
    elif cmd in ("poslat-navrh", "zamitnout-navrh"):
        import json
        pid = int(a[1])
        row = next((r for r in data.proposals() if r[0] == pid), None)
        if not row:
            print("návrh už je vyřízený / neexistuje")
            return
        caption = a[2] if len(a) > 2 else row[6]
        if cmd == "poslat-navrh":
            data.proposal_set(pid, "schvaleno", caption)
            data.outbox_add("navrh", row[2], json.dumps({"pid": pid, "file": row[3], "text": caption}, ensure_ascii=False))
            print("schváleno – bot návrh pošle při nejbližší kontrole")
        else:
            data.proposal_set(pid, "zamitnuto", caption)
            print("zamítnuto")
    elif cmd == "lekce":
        f = HERE / "lekce.md"
        lines = f.read_text(encoding="utf-8").splitlines()
        i = next((k for k, l in enumerate(lines) if l.startswith("- ")), len(lines))
        lines.insert(i, "- " + " ".join(a[1:]).strip().lstrip("- "))
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("lekce přidána (bot ji použije od další odpovědi)")
    elif cmd == "skupiny":
        import json
        for j in json.loads((HERE / "fronta.json").read_text(encoding="utf-8")):
            print(f"{j['group'][:45]:45} {j.get('status',''):22} {j.get('last','')} {(j.get('error') or '')[:70]}")
    elif cmd == "odkaz-skupiny":
        import json
        f = HERE / "plany" / "skupiny_odkazy.json"
        links = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        links[a[1]] = a[2]
        f.write_text(json.dumps(links, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("uloženo")
    elif cmd == "prehled":
        print("NÁVRHY KE SCHVÁLENÍ:", len(data.proposals()))
        for r in data.proposals():
            print(f"  #{r[0]} {r[2]} – {r[4]} {r[5] or ''}")
        log = HERE / "log.csv"
        lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        last = {}
        for l in lines:
            parts = l.split(";")
            if len(parts) > 2:
                last[parts[1]] = l
        print("PŘEDANÉ / ČEKÁ NA TEBE:")
        recent = {time.strftime("%d. %m. %Y", time.localtime(time.time() - k * 86400)) for k in range(7)}
        for person, l in last.items():
            if ("PŘEDÁNO" in l or "čeká na tebe" in l) and l[:12] in recent:
                print("  ", l[:200])
        try:
            import lumao
            drafts = lumao.rows(("koncept", "chyba"))
            print("OBJEDNÁVKY (koncept/chyba):", len(drafts))
            for r in drafts:
                print(f"  #{r[0]} {r[2]} – {lumao.STATES.get(r[6], r[6])} {r[9] or ''}")
        except Exception as e:  # noqa: BLE001
            print("objednávky:", e)
        print("CHYBY DNES:")
        today = time.strftime("%d. %m. %Y")
        for l in [x for x in lines if x.startswith(today) and "CHYBA" in x][-8:]:
            print("  ", l[:200])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
