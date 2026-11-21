# API serveru Lumao pro bota (odvozeno z chybových hlášek a z aplikace Skupiny, 21. 11. 2026)

`POST https://app.lumao.cz/webhooks/lampicky-bot/objednavky` – multipart/form-data:
pole `objednavka` (JSON) + soubory `navrh_0`, `navrh_1`, … (PNG, jeden na položku, v pořadí `polozky`).
`GET …/objednavky/{id_objednavky}` – stav. Hlavička `Authorization: Bearer <LUMAO_TOKEN>` (token v `.env`, ne v gitu).

```json
{
  "test": true,
  "id_objednavky": "lamp-20261121-0001",
  "skupina": "Motorkáři CZ",
  "zakaznik": { "jmeno": "…", "telefon": "+420…", "email": "…" },
  "doruceni": { "dopravce": "zasilkovna", "typ": "vydejni_misto",
                "id_mista": "12345", "nazev_mista": "Z-BOX Přerov",
                "ulice": "Čechova 12", "psc": "75002", "mesto": "Přerov", "zeme": "CZ" },
  "polozky": [ { "motiv": "BMW + jméno Raďa", "pocet": 1, "cena_ks": 690, "podstavec": "barevny" } ],
  "platba": { "zpusob": "dobirka" },
  "poznamka": "volný text k výrobě"
}
```

| Pole | Hodnoty |
|---|---|
| `id_objednavky` | A–Z, 0–9, `. _ : -`, max. 100 znaků; opakované odeslání nezdvojí |
| `doruceni.dopravce` | `zasilkovna`, `ppl` |
| `doruceni.typ` | `vydejni_misto` (povinné `id_mista`, `nazev_mista` + adresa místa), `adresa` (jde přes PPL) |
| `platba.zpusob` | `dobirka`, `prevod`, `hotove` |
| `polozky[].podstavec` | `barevny` (výchozí), `tepla_bila` |
| cena dopravy | neposílá se – server ji bere z nastavení Skupin (`GROUP_SHIP_PRICE_CZK`, u dobírky + příplatek) |

Limity: 15 MB na PNG, 20 položek, jen ČR. Chyba: `{"ok":false,"chyba":"neplatna_data","zprava":"…","pole":"…"}`,
`neplatny_token`; 502 s `"opakovat": true` → poslat znovu se stejným ID.
Stavy (GET): `ceka_na_schvaleni`, `ve_vyrobe`, `odeslano` (+ číslo zásilky), `zruseno` – kontrolovat jednou za 15–30 min.
