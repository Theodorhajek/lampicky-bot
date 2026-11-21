# API pro bota na lampičky (server Lumao, app.lumao.cz)

Od Honzy (kód na serveru `src/profit/routes/bot-orders.js`), 21. 11. 2026. Token v `.env` (`LUMAO_TOKEN`), nikdy v gitu.
Bot pošle jeden požadavek s objednávkou (JSON) a návrhem (PNG) ke každé položce → zápis do Skupin a hned založení v Shopify.
Zákazníkovi se nic neposílá. Výroba začne až po 👍 Schváleno v Telegramu.

## 1. Adresy
| Co | Metoda | URL |
|---|---|---|
| Nová objednávka | POST | `/webhooks/lampicky-bot/objednavky` |
| Stav objednávky | GET | `/webhooks/lampicky-bot/objednavky/{id_objednavky}` |
| Příchozí platby | GET | `/webhooks/lampicky-bot/platby?od=YYYY-MM-DD` |

Hlavička `Authorization: Bearer <token>`, jinak 401. Zrušení dělá Honza v Shopify → bot uvidí `stav: "zruseno"`.

## 2. Požadavek POST (multipart/form-data)
Pole `objednavka` (JSON) + `navrh_0`, `navrh_1`, … (jeden soubor na položku, PNG/JPG/WEBP, povinné).

```json
{
  "id_objednavky": "LB-2026-000123", "test": false, "datum": "2026-11-20", "skupina": "Motorkáři CZ",
  "zakaznik": { "jmeno": "Petr Novák", "telefon": "777 123 456", "email": "petr@example.cz", "facebook": "https://facebook.com/petr.novak" },
  "doruceni": { "typ": "vydejni_misto", "dopravce": "zasilkovna", "id_mista": "12345", "nazev_mista": "Z-BOX Přerov, Čechova",
                "ulice": "Čechova 12", "psc": "750 02", "mesto": "Přerov", "zeme": "CZ" },
  "platba": { "zpusob": "dobirka", "zaplaceno": false },
  "doprava_cena": 89,
  "polozky": [ { "motiv": "Kawasaki", "pocet": 1, "cena_ks": 690 },
               { "motiv": "BMW", "pocet": 2, "cena_ks": 650, "podstavec": "tepla_bila" } ],
  "poznamka": "volitelné, do poznámky objednávky v Shopify"
}
```

| Pole | Povinné | Pravidla |
|---|---|---|
| `id_objednavky` | ano | `A–Z a–z 0–9 . _ : -`, max. 100 znaků; podle něj se pozná opakované odeslání |
| `test` | ne | `true` = jen kontrola, nic se nezapíše |
| `datum` | ne | `YYYY-MM-DD` prodeje, jinak dnešek (Praha) |
| `skupina` | ano | název FB skupiny (Skupiny podle něj počítají zisk) |
| `zakaznik.jmeno` | ano | poslední slovo = příjmení |
| `zakaznik.telefon` | ano | aspoň 9 číslic; dopravce posílá SMS |
| `zakaznik.email`, `facebook` | ne | jen ve Skupinách, do Shopify nejde |
| `doruceni.typ` | ne | `vydejni_misto` (výchozí) / `adresa` (vždy PPL) |
| `doruceni.dopravce` | u výdejního místa | `zasilkovna` / `ppl` |
| `doruceni.id_mista`, `nazev_mista` | u výdejního místa | z API Zásilkovny/PPL |
| `doruceni.ulice`, `psc`, `mesto` | ano | u výdejního místa jeho adresa |
| `platba.zpusob` | ano | `dobirka` / `prevod` / `hotove` |
| `platba.zaplaceno` | ne | u převodu/hotovosti, jestli peníze dorazily; u dobírky `false` |
| `doprava_cena` | ne | Kč za dopravu; jinak z Nastavení |
| `polozky[].motiv`, `cena_ks` | ano | „Lampička – Kawasaki – barevný podstavec“ |
| `polozky[].pocet` | ne | od 1 |
| `polozky[].podstavec` | ne | `barevny` (výchozí) / `tepla_bila` |

Max. 20 položek. Dobírka: doprava předem, v Shopify doprava 0 Kč, na dobírku jen zboží.

## 3. Odpovědi
Úspěch 201 (nová) / 200 (opakovaná): `{"ok":true,"id_objednavky":…,"prodej_id":42,"shopify":{"id":…,"cislo":"#L1300"},"duplicitni":false}`.
Chyba: `{"ok":false,"chyba":…,"zprava":…,"pole":…}`.

| HTTP | chyba | Co dělat |
|---|---|---|
| 400 | `neplatna_data`, `spatny_format` | neopakovat, opravit |
| 401 | `neplatny_token` | neopakovat |
| 404 | `nenalezeno` (GET) | – |
| 409 | `zpracovava_se` | zopakovat za chvíli, stejné ID |
| 413 | `prilis_velke` | zmenšit |
| 502 | `shopify` (`"opakovat": true`) | zopakovat se stejným ID (1, 5, 15 min) |
| 503 | `nenastaveno` | zopakovat později, ozvat se Honzovi |
| 5xx / timeout | – | zopakovat se stejným ID |

Idempotence: stejné ID nic nezdvojí (`"duplicitni": true`). Platí data z prvního odeslání, opravy jen přes Honzu.

## 4. Test (`"test": true`)
Všechny kontroly vč. návrhů, nic se nezapíše; vrací 200 a `nahled` (položky, doprava, platba, částka v Shopify, poznámka).

## 5. Stav (GET)
`{"ok":true,…,"stav":"odeslano","zasilky":[{"dopravce":"Packeta","cislo":"Z1234567890","odkaz":"https://tracking.packeta.com/…"}]}`

`prijato` (v Shopify zatím není → opakovat POST) · `ceka_na_schvaleni` · `ve_vyrobe` · `vyrobeno` · `odeslano` · `doruceno` · `zruseno` ·
`v_shopify` (Shopify neodpovídá, pole `upozorneni`). Stačí jednou za 15–30 min.

## 5a. Příchozí platby (GET `/platby?od=YYYY-MM-DD`)
Jen příchozí platby na účet lampiček, server je čte z upozornění Air Bank každých 10 min. `od` nepovinné (den platby, Praha), bez něj max. 500, od nejstarší.
`{"ok":true,"od":…,"platby":[{"id":12,"datum":"2026-11-21","prijato":"…Z","castka":1099,"mena":"CZK","jmeno":"Eva Nováková","ucet":"…","vs":"777123456","zprava":"Lampička Eva","kod_transakce":"…"}]}`
`id` je stálé; chybějící pole jsou `null`; server nic neoznačuje jako zaplacené, párování dělá bot.

## 6. Limity
Návrh max. 15 MB, max. 20 položek, JSON max. 256 kB, jen CZ.
