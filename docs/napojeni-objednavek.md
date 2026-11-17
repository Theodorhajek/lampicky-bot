# Objednávky lampiček: JSON, který posíláme na server

> Pro Honzu / Clauda s přístupem k našemu serveru. Popisuje **jen to, co bot posílá**.
> Jak to napárovat na náš stávající systém a Shopify, je na vás.

## Kontext

Ručně dělané LED lampičky (gravírované akrylové sklo 15 × 15 cm na bukovém podstavci) prodáváme přes Facebook.
Objednávky domlouvá bot v Messengeru (repozitář `Theodorhajek/lampicky-bot`). Jakmile má od zákazníka všechny
údaje, pošle objednávku na server – ta se má **rovnou propsat do Shopify**. Později pošle změny stavu
(vyrobeno, odesláno, zrušeno). Platby server neřeší, páruje si je Theodor.

## Co od serveru potřebujeme

- Endpoint, kam bot pošle JSON níže (`POST`), a endpoint pro změnu stavu (`PATCH`) – adresy a cesty dle vás.
- Ověření tokenem v hlavičce `Authorization: Bearer <token>`.
- Když stejné `id` přijde dvakrát (bot po výpadku posílá znovu), nesmí vzniknout dvě objednávky.
- Odpověď s číslem objednávky v Shopify, ať ho bot ukáže v dashboardu.

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

Odpověď ideálně stejná jako u založení.

## Chyby

| Kód | Kdy | Tělo |
|---|---|---|
| `400` | Chybná data | `{"ok": false, "chyba": "<česky, co je špatně>"}` |
| `401` | Chybí / špatný token | `{"ok": false, "chyba": "..."}` |
| `404` | Neznámé `id` u PATCH/GET | |
| `409` | Stejné `id`, jiný obsah | `{"ok": false, "chyba": "...", "shopify_name": "#1042"}` |
| `5xx` | Server / Shopify nedostupné | Bot opakuje: 1, 5, 15, 60 min, pak po hodině, max 2 dny |

## Bezpečnost

- Jen HTTPS. Token nikdy do gitu ani do logů.
- Data obsahují osobní údaje zákazníků – ukládat jen to, co je potřeba k vyřízení objednávky.
- Endpoint nepřijímá požadavky bez tokenu; omezit velikost těla (např. 100 kB).

## Co vrátit zpět Theodorovi

1. Adresu endpointu pro založení a pro změnu stavu (případně testovací režim).
2. Token – předat bezpečně (ne v čitelném e-mailu ani v gitu).
3. Změny formátu oproti tomuto dokumentu, pokud nějaké chcete (stačí upravená ukázka JSON) – bot se přizpůsobí.
4. Tvar odpovědi, pokud se liší od ukázky výše.
