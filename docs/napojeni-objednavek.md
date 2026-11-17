# Úkol: API pro objednávky lampiček (server → Shopify)

> Tento dokument je zadání pro Clauda (nebo vývojáře) s přístupem k našemu serveru na Hetzneru.
> Klientskou stranu (bot na Macu) dodělá Theodor, až budou hotové body z části „Co vrátit zpět“.

## Kontext

Ručně dělané LED lampičky (gravírované akrylové sklo 15 × 15 cm na bukovém podstavci) prodáváme přes Facebook.
Objednávky domlouvá bot v Messengeru (Python na Theodorově Macu, repozitář `Theodorhajek/lampicky-bot`).
Jakmile bot od zákazníka získá všechny údaje, má objednávku poslat na náš server. Server ji uloží a založí
objednávku v Shopify. Později bot pošle změny stavu (vyrobeno, odesláno, zrušeno).

**Platby server neřeší.** Theodor si je páruje sám. Objednávka nese jen informaci, jak zákazník platí.

## Co implementovat

1. HTTPS endpointy pod základní adresou, např. `https://<server>/api/lampicky`:
   - `POST /objednavky` – založení objednávky
   - `PATCH /objednavky/{id}` – změna stavu
   - (volitelně) `GET /objednavky/{id}` – stav a číslo objednávky v Shopify
2. Ověření tokenem v hlavičce `Authorization: Bearer <token>`. Vygeneruj náhodný token (min. 32 znaků).
3. Uložení objednávek do DB serveru (stačí tabulka s `id`, JSON těla, stavem, `shopify_order_id`, časy).
4. Založení a aktualizace objednávky v Shopify přes Admin GraphQL API (viz „Zápis do Shopify“).
5. Testovací režim: hlavička `X-Test: 1` → validace a odpověď jako naostro, ale v Shopify nic naostro
   nevzniká (nic, nebo jen koncept se štítkem `test`).
6. Idempotence podle `id`: stejný `POST` dvakrát nesmí v Shopify založit dvě objednávky.

Tělo požadavků i odpovědí: `application/json; charset=utf-8` (čeština a emoji musí projít).

## `POST /objednavky`

```json
{
  "id": "lamp-20261117-0012",
  "vytvoreno": "2026-11-17T14:32:00+01:00",
  "zdroj": "messenger",
  "zakaznik": {
    "jmeno": "Jana Nováková",
    "telefon": "+420777123456",
    "email": "jana@example.cz",
    "facebook": "Jana Nováková"
  },
  "doprava": {
    "dopravce": "zasilkovna",
    "vydejni_misto": "Z-BOX Přerov, Čechova 12",
    "adresa": null,
    "cena": 85
  },
  "polozky": [
    { "motiv": "BMW + jméno Raďa vpravo nahoře", "kusy": 1, "cena_za_kus": 690, "navrh_url": null }
  ],
  "platba": { "zpusob": "dobirka", "predem": 105, "dobirka": 690 },
  "celkem": 795,
  "poznamka": "Jméno svítí stejnou barvou jako logo.",
  "stav": "domluveno"
}
```

### Pole

| Pole | Typ | Povinné | Popis |
|---|---|---|---|
| `id` | text | ano | ID od bota, jedinečné; klíč pro idempotenci |
| `vytvoreno` | ISO 8601 | ano | Kdy byla objednávka domluvena (Europe/Prague) |
| `zdroj` | text | ano | `messenger` nebo `komentar` |
| `zakaznik.jmeno` | text | ano | Jméno a příjmení pro zásilku |
| `zakaznik.telefon` | text | ano | Jak ho zákazník napsal; bot zkusí E.164 |
| `zakaznik.email` | text | ano | |
| `zakaznik.facebook` | text | ne | Jméno na Facebooku (dohledání konverzace) |
| `doprava.dopravce` | text | ano | `zasilkovna` nebo `ppl` |
| `doprava.vydejni_misto` | text | jedno z dvou | Název + adresa výdejního místa (ID místa bot zatím nezná) |
| `doprava.adresa` | objekt | jedno z dvou | `{ulice, mesto, psc, zeme}` při doručení na adresu |
| `doprava.cena` | číslo | ano | 85 Kč (předem) / 105 Kč (dobírka); za zásilku jednou |
| `polozky[]` | pole | ano | Každý motiv zvlášť, `cena_za_kus` standardně 690 |
| `polozky[].navrh_url` | text | ne | Odkaz na schválený návrh (PNG 1500 × 1500), pokud existuje |
| `platba.zpusob` | text | ano | `predem` nebo `dobirka` |
| `platba.predem` | číslo | ano | Kolik pošle zákazník převodem |
| `platba.dobirka` | číslo | ano | Částka na dobírku (u `predem` 0) |
| `celkem` | číslo | ano | Zboží + doprava v Kč |
| `poznamka` | text | ne | Volná poznámka k výrobě |
| `stav` | text | ano | `domluveno` / `vyrobeno` / `odeslano` / `zruseno` |

### Odpověď

- `201 Created` – nově založeno
- `200 OK` – objednávka s tímto `id` už existuje se stejným obsahem (opakované odeslání)

```json
{ "ok": true, "id": "lamp-20261117-0012", "shopify_order_id": "gid://shopify/Order/1234567890", "shopify_name": "#1042" }
```

## `PATCH /objednavky/{id}`

```json
{ "stav": "vyrobeno" }
{ "stav": "odeslano", "dopravce": "zasilkovna", "cislo_zasilky": "Z 123 4567 890" }
{ "stav": "zruseno", "duvod": "zákazník si to rozmyslel" }
```

Odpověď `200 OK` se stejným tělem jako u založení; neznámé `id` → `404`.

## Chyby

| Kód | Kdy | Tělo |
|---|---|---|
| `400` | Chybná data | `{"ok": false, "chyba": "<česky, co je špatně>"}` |
| `401` | Chybí / špatný token | `{"ok": false, "chyba": "..."}` |
| `404` | Neznámé `id` u PATCH/GET | |
| `409` | Stejné `id`, jiný obsah | `{"ok": false, "chyba": "...", "shopify_name": "#1042"}` |
| `5xx` | Server / Shopify nedostupné | Bot opakuje: 1, 5, 15, 60 min, pak po hodině, max 2 dny |

## Zápis do Shopify

Návrh – uprav podle toho, jak je obchod nastavený:

- **Objednávka:** `orderCreate` (nebo `draftOrderCreate` + dokončení). Jedna řádková položka na každý motiv
  s názvem např. „LED lampička – BMW + jméno Raďa“ a cenou z objednávky.
- **Produkt:** jeden obecný produkt „LED lampička na zakázku“ (např. SKU `LAMP-15`), motiv v názvu/vlastnostech položky.
  Pokud produkt neexistuje, založ ho (nebo použij custom line item).
- **Doprava:** shipping line „Zásilkovna“ / „PPL“ s cenou `doprava.cena`. Výdejní místo zapiš tak, jak ho potřebuje
  aplikace Zásilkovny / PPL v obchodě (adresa, poznámka nebo metafield).
- **Zákazník:** najít podle e-mailu, jinak založit.
- **Platba:** finanční stav `pending`. Na zaplaceno ji přepíná Theodor ručně.
- **Štítky:** `lampicky`, `messenger`. Do poznámky ID od bota a jméno na Facebooku.
- **Stav `vyrobeno`:** štítek `vyrobeno`.
- **Stav `odeslano`:** fulfillment s číslem zásilky a dopravcem.
- **Stav `zruseno`:** zrušit objednávku v Shopify (bez refundu – platby řeší Theodor).

## Bezpečnost

- Jen HTTPS. Token nikdy do gitu ani do logů.
- Data obsahují osobní údaje zákazníků – ukládat jen to, co je potřeba k vyřízení objednávky.
- Endpoint nepřijímá požadavky bez tokenu; omezit velikost těla (např. 100 kB).

## Co vrátit zpět Theodorovi

1. Základní URL API (produkční + jak zapnout testovací režim).
2. Token – předat bezpečně (ne v čitelném e-mailu ani v gitu).
3. Případné změny formátu oproti tomuto dokumentu (stačí upravená ukázka JSON).
4. Jak se v Shopify zapsala testovací objednávka (číslo `#…`) a jak se v ní zobrazuje výdejní místo.
5. Odpověď na otázku: mají se obrázky návrhů nahrávat na server (např. `POST /navrhy` → URL)?

Otevřené rozhodnutí na straně Theodora: zda objednávku zakládat už při stavu `domluveno`, nebo až při
`vyrobeno` / `odeslano`. API výše funguje pro všechny varianty.
