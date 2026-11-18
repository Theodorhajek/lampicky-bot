# Napojení bota na server – odpovědi a požadavky

**Pro:** Honza / Claude s přístupem k serveru
**Od:** Theodor (bot na lampičky, repozitář `Theodorhajek/lampicky-bot`)
**Navazuje na:** `docs/napojeni-objednavek.md`

Děkujeme za rozbor. Níže jsou odpovědi na otevřené otázky a souhrn toho, co od serveru očekáváme.
Návrh i implementaci endpointu necháváme na vašem úsudku – bot se přizpůsobí formátu, který určíte.

---

## 1. Návrh (PNG) je u každé položky

Zakázka „bez návrhu“ je objednávka běžného motivu (typicky logo značky – Kawasaki, Yamaha, Honda, BMW apod.),
u které zákazník o návrh výslovně nežádal. Bot ale obrázek motivu vygeneruje ke každé položce automaticky.

**Důsledek pro server:** PNG můžete brát jako povinné. Každá položka objednávky bude mít vlastní obrázek.

## 2. Více motivů v jedné objednávce – podporovat od začátku

Objednávky s více lampičkami (různé motivy, případně různé kusy) nejsou výjimečné. Prosíme o podporu rovnou:

- jedna objednávka (prodej) = více položek,
- každá položka má vlastní motiv, počet kusů, cenu a **vlastní PNG**,
- doprava se účtuje jednou za celou zásilku.

## 3. Výdejní místo – kompletní adresa ze strany bota

Kompletní adresu výdejního místa dohledáme na naší straně (API Zásilkovny, u PPL obdobně).
Bot pošle strukturovaně:

| Údaj | Příklad |
|---|---|
| Dopravce | `zasilkovna` / `ppl` |
| ID výdejního místa | `12345` |
| Název místa | Z-BOX Přerov, Čechova |
| Ulice a číslo | Čechova 12 |
| PSČ | 750 02 |
| Město | Přerov |
| Země | CZ |

Při doručení na adresu pošleme stejná pole bez ID a názvu výdejního místa.

## 4. E-mail zákazníka

E-mail posíláme. Do Shopify se propisovat nemusí – stačí, když zůstane ve Skupinách.

## 5. Souhlas s navrženým řešením

| Bod | Souhlas |
|---|---|
| Jeden požadavek: JSON + PNG jako soubory (jeden na položku) | ano |
| Adresa pod `https://app.lumao.cz/webhooks/…` | ano |
| Ověření tokenem v hlavičce | ano |
| Idempotence podle ID objednávky od bota (opakované odeslání nezdvojí prodej) | ano |
| Zápis do Skupin a okamžité založení v Shopify | ano |
| Příznak `test` – validace bez zápisu | ano |
| Schválení před výrobou dál přes 👍 v Telegramu | ano |
| Rozpad dobírky (doprava předem, na dobírku jen zboží) | ano – odpovídá tomu, jak prodáváme |

---

## Co od vás potřebujeme

1. **URL endpointu** (a případně endpointu pro změnu stavu: vyrobeno / odesláno s číslem zásilky / zrušeno).
2. **Přesný formát požadavku** – ukázka JSON s názvy polí a způsob, jak se PNG soubory přiřadí k položkám
   (např. `multipart/form-data` s poli `navrh_0`, `navrh_1`, … nebo jinak dle vás).
3. **Token** – předaný bezpečně (ne v čitelném e-mailu ani v gitu).
4. **Tvar odpovědi** – ideálně s číslem objednávky v Shopify – a tvar chybové odpovědi (kódy, text chyby).
5. **Limity** – maximální velikost PNG / celého požadavku, pokud nějaké jsou.

## Co připravíme na naší straně

- Dohledání výdejního místa přes API Zásilkovny (ID, ulice, PSČ, město).
- Automatické vygenerování PNG ke každé položce; návrhy vyžádané zákazníkem dál schvaluje Theodor.
- Odesílací frontu s opakováním při výpadku (se stejným ID) a zobrazení stavu „zapsáno v Shopify“ v dashboardu.
- Zkušební objednávku s příznakem `test`, jakmile bude endpoint připravený.
