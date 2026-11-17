# Objednávky lampiček → náš server: jaký formát máme posílat?

> Pro Honzu / Clauda s přístupem k našemu serveru.
> **Prosba:** napište nám, v jakém formátu (JSON) a kam máme objednávky posílat. Bot se přizpůsobí vašemu systému.

## Kontext

Ručně dělané LED lampičky (gravírované akrylové sklo 15 × 15 cm na bukovém podstavci) prodáváme přes Facebook.
Objednávky domlouvá bot v Messengeru (Python na Theodorově Macu, repozitář `Theodorhajek/lampicky-bot`).
Jakmile má od zákazníka všechny údaje, chceme objednávku poslat na náš server, odkud se má **rovnou propsat do Shopify**.
Později chceme posílat i změnu stavu (vyrobeno, odesláno s číslem zásilky, zrušeno).
Platby server neřeší, páruje si je Theodor.

## Jaké údaje o objednávce máme

| Údaj | Příklad | Poznámka |
|---|---|---|
| ID objednávky od bota | `lamp-20261117-0012` | jedinečné; při výpadku můžeme stejnou objednávku poslat znovu |
| Datum a čas domluvy | `2026-11-17T14:32:00+01:00` | |
| Zdroj | `messenger` / `komentar` | |
| Jméno a příjmení | Jana Nováková | |
| Telefon | +420 777 123 456 | jak ho zákazník napsal |
| E-mail | jana@example.cz | |
| Jméno na Facebooku | Jana Nováková | pro dohledání konverzace |
| Dopravce | Zásilkovna / PPL | |
| Výdejní místo **nebo** adresa | „Z-BOX Přerov, Čechova 12“ / ulice, město, PSČ | ID výdejního místa zatím nemáme, jen text od zákazníka |
| Cena dopravy | 85 Kč (předem) / 105 Kč (dobírka) | za zásilku jednou |
| Položky | „BMW + jméno Raďa“, 1 ks, 690 Kč | víc motivů = víc položek |
| Návrh (obrázek) | PNG 1500 × 1500 px | jen u zakázek s návrhem; umíme poslat jako soubor nebo URL |
| Způsob platby | předem / dobírka | |
| Částky | předem 105 Kč, dobírka 690 Kč, celkem 795 Kč | |
| Poznámka | „Jméno svítí stejnou barvou jako logo.“ | volný text k výrobě |
| Stav | domluveno / vyrobeno / odesláno / zrušeno | |
| Číslo zásilky | Z 123 4567 890 | až při odeslání |

Pro představu, jak by to mohlo vypadat (není závazné – pošlete nám svůj formát):

```json
{
  "id": "lamp-20261117-0012",
  "zakaznik": { "jmeno": "Jana Nováková", "telefon": "+420777123456", "email": "jana@example.cz" },
  "doprava": { "dopravce": "zasilkovna", "vydejni_misto": "Z-BOX Přerov, Čechova 12", "cena": 85 },
  "polozky": [ { "motiv": "BMW + jméno Raďa", "kusy": 1, "cena_za_kus": 690 } ],
  "platba": { "zpusob": "dobirka", "predem": 105, "dobirka": 690 },
  "celkem": 795,
  "stav": "domluveno"
}
```

## Co od vás potřebujeme

1. **Adresu (URL)**, kam objednávky posílat – a kam posílat změnu stavu.
2. **Formát JSON** pro založení objednávky i pro změnu stavu (stačí ukázka s názvy polí).
3. **Ověření** – jak se má bot přihlásit (např. token v hlavičce `Authorization: Bearer …`) a token předaný bezpečně (ne v čitelném e-mailu ani v gitu).
4. **Odpověď** – co server vrátí (ideálně číslo objednávky v Shopify, ať ho bot ukáže v dashboardu) a jak vypadá chyba.
5. **Opakování** – jestli je v pořádku, že bot při výpadku stejnou objednávku (se stejným ID) pošle znovu, a server ji nezdvojí.
6. **Obrázky návrhů** – chcete je posílat taky? Jako soubor, nebo URL?
7. **Testovací režim** – ideálně způsob, jak poslat zkušební objednávku, aniž by se zapsala do Shopify naostro.

Jakmile to budeme mít, Theodor napojí bota a dashboard a pošle zkušební objednávku.
