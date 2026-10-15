// Messenger asistent pro prodej lampiček.
// Hlídá Messenger v prohlížeči, na nové zprávy od zákazníků odpovídá přes Claude.
//
//   npm run login   – otevře prohlížeč, přihlásíš se na Facebook (jen poprvé)
//   npm run debug   – ukáže, co bot na stránce "vidí" (nic neposílá)
//   npm run dry     – běží naostro, ale odpovědi jen vypisuje, NEPOSÍLÁ
//   npm start       – běží a odpovídá

import fs from "node:fs";
import path from "node:path";
import { execFile } from "node:child_process";
import os from "node:os";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const here = path.dirname(fileURLToPath(import.meta.url));
const file = (name) => path.join(here, name);
const config = JSON.parse(fs.readFileSync(file("config.json"), "utf8"));
const MODE = process.argv[2] || "run"; // login | debug | dry | run
const SEND = MODE === "run";
const CATCH_UP = process.argv.includes("--catch-up");

// ---------- stav (co už bot vyřídil) ----------

const STATE_FILE = file("state.json");
const state = fs.existsSync(STATE_FILE)
  ? JSON.parse(fs.readFileSync(STATE_FILE, "utf8"))
  : { initialized: false, threads: {} };
const saveState = () => fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
const thread = (id) =>
  (state.threads[id] ??= { name: "", preview: "", botSent: [], botReplies: 0, pausedUntil: 0, handedOff: false, handled: "" });

function log(threadName, action, text) {
  const line = [new Date().toLocaleString("cs-CZ"), threadName, action, text]
    .map((v) => `"${String(v ?? "").replaceAll('"', '""').replaceAll("\n", " / ")}"`)
    .join(";");
  fs.appendFileSync(file("log.csv"), line + "\n");
  console.log(`[${new Date().toLocaleTimeString("cs-CZ")}] ${threadName} – ${action}${text ? ": " + text : ""}`);
}

function notify(title, text) {
  if (process.platform !== "darwin") return process.stdout.write("\x07");
  const esc = (s) => s.replaceAll("\\", "\\\\").replaceAll('"', '\\"');
  execFile("osascript", ["-e", `display notification "${esc(text)}" with title "${esc(title)}" sound name "Glass"`]);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const randomDelay = () => {
  const [min, max] = config.replyDelaySeconds;
  return (min + Math.random() * (max - min)) * 1000;
};
const norm = (s) => s.replace(/\s+/g, " ").trim();

// ---------- čtení Messengeru ze stránky ----------

const CONVERSATION_LINK = 'a[href*="/messages/"][href*="/t/"]';

async function readConversationList(page) {
  return page.evaluate((limit) => {
    const seen = new Set();
    const out = [];
    for (const a of document.querySelectorAll('a[href*="/messages/"][href*="/t/"]')) {
      if (a.closest('[role="main"]')) continue;
      if (a.getAttribute("href").includes("/requests/") || a.getBoundingClientRect().width === 0) continue; // složka Žádosti / skryté
      // běžné i šifrované chaty: /messages/t/123 nebo /messages/e2ee/t/123
      const id = a.getAttribute("href").match(/\/messages\/((?:e2ee\/)?t\/[^/?#]+)/)?.[1];
      if (!id || seen.has(id)) continue;
      seen.add(id);
      const lines = a.innerText.split("\n").map((l) => l.trim())
        .filter((l) => l && !/^(Právě aktivní|Aktivní)/.test(l));
      const preview = lines.slice(1).filter((l) => l !== "·" && !/^Nepřečten/.test(l));
      if (preview.length > 1) preview.pop(); // čas („1 t", „6 d") – mění se sám, nepatří do porovnání
      out.push({ id, name: lines[0] || id, preview: preview.join(" | ") });
      if (out.length >= limit) break;
    }
    return out;
  }, config.recentConversationsToWatch);
}

// Vrátí zprávy v otevřené konverzaci: { from: "them" | "me", text, images }.
// Kdo psal, pozná podle skrytého popisku „Zprávu poslal(a) Já…“, jinak podle strany (zákazník vlevo, já vpravo).
async function readMessages(page) {
  return page.evaluate(() => {
    const log = document.querySelector('[role="main"] [role="log"]') || document.querySelector('[role="main"]');
    if (!log) return [];
    const box = log.getBoundingClientRect();
    const mid = box.left + box.width / 2;
    const SENDER = /(?:poslal\(a\)|poslána|poslán|odeslal\(a\))\s+(.+?)\s+v\s+\d/;
    const STATUS = /^(Odesláno|Doručeno|Přečteno|Zobrazeno|Upraveno|Zadat)$/;
    const out = [];
    for (const msg of log.querySelectorAll('[role="article"], [role="row"]')) {
      if (msg.parentElement.closest('[role="article"], [role="row"]')) continue;
      const leaves = [...msg.querySelectorAll('[dir="auto"]')].filter((el) => !el.querySelector('[dir="auto"]'));
      let sender = null;
      const texts = [];
      const rects = [];
      for (const el of leaves) {
        const t = el.innerText.trim();
        const m = t.match(SENDER);
        if (m) { sender = m[1]; continue; }
        if (t.startsWith("Zadat,")) continue; // skrytý popisek bez odesílatele
        if (!t || STATUS.test(t)) continue;
        texts.push(t);
        const r = el.getBoundingClientRect();
        if (r.width > 0) rects.push(r);
      }
      const images = [...msg.querySelectorAll("img")].filter((img) => img.getBoundingClientRect().width >= 80);
      for (const img of images) rects.push(img.getBoundingClientRect());
      if (!texts.length && !images.length) continue;
      let from;
      if (sender) from = sender === "Já" ? "me" : "them";
      else if (rects.length) {
        const center = (Math.min(...rects.map((r) => r.left)) + Math.max(...rects.map((r) => r.right))) / 2;
        from = center < mid ? "them" : "me";
      } else continue;
      out.push({ from, text: [...new Set(texts)].join("\n"), images: images.length });
    }
    return out;
  });
}

async function openThread(page, id) {
  await page.goto(`https://www.facebook.com/messages/${id}/`, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForSelector('[role="main"] [role="article"], [role="main"] [role="row"]', { timeout: 20000 }).catch(() => {});
  await sleep(2500);
}

async function sendMessage(page, text) {
  const box = page.locator('[role="main"] [role="textbox"][contenteditable="true"]').last();
  await box.click();
  const lines = text.split("\n");
  for (let i = 0; i < lines.length; i++) {
    if (i > 0) await page.keyboard.press("Shift+Enter");
    await page.keyboard.type(lines[i], { delay: 15 + Math.random() * 30 });
  }
  await sleep(500);
  await page.keyboard.press("Enter");
  await sleep(1500);
}

// ---------- Claude ----------

const REPLY_SCHEMA = {
  type: "object",
  properties: {
    action: { type: "string", enum: ["reply", "none"] },
    message: { type: "string" },
    handoff: { type: "boolean" },
    note: { type: "string" },
  },
  required: ["action", "message", "handoff", "note"],
  additionalProperties: false,
};

function systemPrompt() {
  const info = fs.readFileSync(file("info.md"), "utf8");
  return `Odpovídáš v Messengeru za prodejce ručně dělaných lampiček na zakázku (auta, motorky, loga, cokoliv podle fotky). Zákazníci píšou většinou z Facebook skupin, kde viděli jeho příspěvek.

Píšeš za něj, v první osobě, česky. Vykáš (prodejce píše zákazníkům „Zdravím“, „Dobrý den“, „dejte vědět“) – tykej jen tehdy, když zákazníkovi už v konverzaci tyká i prodejce. Krátce a přátelsky, jako člověk v chatu: 1–4 věty, žádné odrážky ani formální fráze, emoji střídmě. Nepiš víc zpráv najednou, než je potřeba.

Cíl: aby zákazník nemusel čekat a objednávka se posunula – odpověz na dotaz, zjisti co chce a postupně sesbírej, co je potřeba k objednávce (podle informací níže). Na nic se neptej dvakrát, pokud už to v konverzaci je.

Pravidla:
- Používej jen informace níže. Nikdy si nevymýšlej ceny, termíny, slevy, parametry ani sliby. Pokud odpověď neznáš, napiš něco jako „tohle ti potvrdím, ozvu se co nejdřív" a nastav handoff=true.
- handoff=true nastav také, když: zákazník reklamuje nebo je nespokojený, chce něco atypického / individuální cenu, smlouvá o ceně nad rámec pravidel, objednávka má všechny potřebné údaje (prodejce ji musí zkontrolovat a vyrobit – zákazníkovi při tom pošli souhrn a platební údaje), zákazník píše, že zaplatil (prodejce musí platbu ověřit), nebo si nejsi jistý.
- Údaje v hranatých závorkách [takhle] jsou nevyplněné – ber je jako neznámé.
- Pokud se zákazník přímo zeptá, jestli píše s robotem, nelži: řekni, že na rychlé odpovědi používáš asistenta a všechno po něm čteš.
- Pokud poslední zpráva nepotřebuje odpověď (např. jen „ok", „díky", palec nahoru) a nic nečeká, dej action="none".
- „[fotka]" znamená, že zákazník poslal obrázek (nevidíš ho) – poděkuj a ber to jako dodanou fotku.
- note: jedna krátká věta pro prodejce, co se v konverzaci děje (např. „chce lampičku s E46, čeká na platbu").

INFORMACE O PRODEJI:
${info}`;
}

function transcript(messages) {
  return messages
    .slice(-30)
    .map((m) => {
      const body = [m.text, m.images ? "[fotka]".repeat(Math.min(m.images, 3)) : ""].filter(Boolean).join(" ");
      return `${m.from === "them" ? "Zákazník" : "Já"}: ${body}`;
    })
    .join("\n");
}

// Volá nainstalovaný Claude Code (claude -p), takže jede na předplatném, ne na placeném API.
function askClaude(name, messages) {
  const prompt = `Konverzace se zákazníkem (${name}), nejstarší nahoře:\n\n${transcript(messages)}\n\nNapiš další odpověď za mě.`;
  const args = [
    "-p", "--output-format", "json",
    "--model", config.model,
    "--effort", config.effort,
    "--system-prompt", systemPrompt(),
    "--json-schema", JSON.stringify(REPLY_SCHEMA),
    "--tools", "",
    "--strict-mcp-config",
    "--setting-sources", "",
    "--no-session-persistence",
  ];
  const env = { ...process.env };
  delete env.ANTHROPIC_API_KEY; // ať se nikdy neplatí přes API, jen předplatné
  return new Promise((resolve, reject) => {
    const child = execFile(config.claudePath, args, { cwd: os.tmpdir(), env, maxBuffer: 10_000_000, timeout: 180_000 },
      (err, stdout, stderr) => {
        if (err && !stdout) return reject(new Error(`claude: ${stderr || err.message}`));
        let out;
        try { out = JSON.parse(stdout); } catch { return reject(new Error(`claude vrátil nečitelný výstup: ${stdout.slice(0, 300)}`)); }
        if (out.is_error) return reject(new Error(`claude: ${out.result || out.api_error_status || "chyba"} (vyčerpaný limit předplatného?)`));
        if (!out.structured_output) {
          return resolve({ action: "none", message: "", handoff: true, note: "Claude nevrátil odpověď – vyřiď ručně." });
        }
        resolve(out.structured_output);
      });
    child.stdin.end(prompt);
  });
}

// ---------- jedna konverzace ----------

async function handleThread(page, conv) {
  const t = thread(conv.id);
  t.name = conv.name;
  await openThread(page, conv.id);
  let messages = await readMessages(page);
  const last = messages.at(-1);
  if (!last) return log(conv.name, "nepřečetl jsem zprávy", "zkontroluj `npm run debug`");

  if (last.from === "me") {
    // Poslední zprávu jsem psal já. Pokud ji neposlal bot, převzal jsi konverzaci ručně.
    const fromBot = t.botSent.some((s) => norm(s) === norm(last.text));
    if (!fromBot && last.text && Date.now() > t.pausedUntil) {
      t.pausedUntil = Date.now() + config.pauseAfterManualReplyHours * 3600_000;
      t.botReplies = 0;
      t.handedOff = false;
      log(conv.name, `odpověděl jsi ručně – bot tu ${config.pauseAfterManualReplyHours} h mlčí`);
    }
    return;
  }

  const fingerprint = norm(transcript(messages.slice(-5)));
  if (t.handled === fingerprint) return;

  if (t.handedOff) return log(conv.name, "nová zpráva, čeká na tebe (předáno)", last.text);
  if (Date.now() < t.pausedUntil) return log(conv.name, "nová zpráva, bot pozastaven (píšeš ručně)", last.text);
  if (t.botReplies >= config.maxBotRepliesInRow) {
    t.handedOff = true;
    notify("Lampičky – převezmi konverzaci", `${conv.name}: bot už odpověděl ${t.botReplies}×`);
    return log(conv.name, "limit odpovědí bota – předáno tobě");
  }

  // Chvíli počkej, ať to nepůsobí jako automat a zákazník stihne dopsat.
  await sleep(randomDelay());
  messages = await readMessages(page);
  if (messages.at(-1)?.from !== "them") return;

  const answer = await askClaude(conv.name, messages);
  t.handled = norm(transcript(messages.slice(-5)));

  if (answer.action === "reply" && answer.message.trim()) {
    if (SEND) {
      await sendMessage(page, answer.message.trim());
      t.botSent = [...t.botSent, answer.message.trim()].slice(-20);
      t.botReplies++;
      log(conv.name, "odesláno", answer.message);
    } else {
      log(conv.name, "NÁVRH (neodesláno)", answer.message);
    }
  } else {
    log(conv.name, "bez odpovědi", answer.note);
  }
  if (answer.handoff) {
    t.handedOff = SEND;
    notify("Lampičky – potřebuju tě", `${conv.name}: ${answer.note}`);
    log(conv.name, "PŘEDÁNO TOBĚ", answer.note);
  }
}

// ---------- hlavní smyčka ----------

async function main() {
  const context = await chromium.launchPersistentContext(file("profil-prohlizece"), {
    headless: false,
    channel: config.browser === "chromium" ? undefined : config.browser,
    viewport: { width: 1280, height: 900 },
    locale: "cs-CZ",
  });
  const page = context.pages()[0] ?? (await context.newPage());
  await page.goto(config.messagesUrl, { waitUntil: "domcontentloaded" });

  if (MODE === "login") {
    console.log("Přihlas se ve otevřeném okně na Facebook. Až uvidíš své zprávy, okno zavři.");
    await new Promise((r) => context.on("close", r));
    return;
  }

  await page.waitForSelector(CONVERSATION_LINK, { timeout: 60000 }).catch(() => {
    console.error("Nevidím seznam konverzací – nejsi odhlášený? Spusť `npm run login`.");
    process.exit(1);
  });
  await sleep(3000);

  if (MODE === "debug") {
    const list = await readConversationList(page);
    console.log("\nKONVERZACE:", list);
    if (list[0]) {
      await openThread(page, list[0].id);
      console.log(`\nZPRÁVY v "${list[0].name}" (them = zákazník, me = já):`, await readMessages(page));
    }
    console.log("\nZkontroluj, že sedí jména, texty i strany. Okno zavři, až budeš hotový.");
    await new Promise((r) => context.on("close", r));
    return;
  }

  console.log(SEND ? "Bot běží a ODPOVÍDÁ. Zastavíš ho Ctrl+C." : "Zkušební režim – odpovědi jen vypisuju, nic neposílám.");

  // Seznam chatů se na stránce aktualizuje sám – stránku stačí občas obnovit.
  let lastReload = Date.now();
  while (true) {
    try {
      const inRequests = await page.evaluate(() =>
        [...document.querySelectorAll('a[href*="/messages/"][href*="/t/"]')].some((a) => a.getAttribute("href").includes("/requests/")));
      if (inRequests || Date.now() - lastReload > 10 * 60_000 || !(await page.$(CONVERSATION_LINK))) {
        await page.goto(config.messagesUrl, { waitUntil: "domcontentloaded", timeout: 60000 });
        await page.waitForSelector(CONVERSATION_LINK, { timeout: 60000 });
        await sleep(3000);
        lastReload = Date.now();
      }
      const list = await readConversationList(page);

      if (!state.initialized && !CATCH_UP) {
        // Při prvním spuštění jen zapamatovat stávající konverzace, na staré zprávy neodpovídat.
        for (const c of list) Object.assign(thread(c.id), { name: c.name, preview: c.preview });
        state.initialized = true;
        saveState();
        console.log(`Zapamatováno ${list.length} konverzací. Odpovídat budu jen na nové zprávy.`);
      }
      state.initialized = true;

      for (const conv of list) {
        const t = thread(conv.id);
        if (t.preview === conv.preview) continue;
        if (t.preview == null) { // zatím nezapamatovaná – jen uložit, neodpovídat
          Object.assign(t, { name: conv.name, preview: conv.preview });
          continue;
        }
        t.preview = conv.preview;
        try {
          await handleThread(page, conv);
        } catch (err) {
          t.preview = ""; // zkusit znovu v dalším kole
          log(conv.name, "CHYBA", err.message);
          notify("Lampičky – chyba bota", `${conv.name}: ${err.message}`.slice(0, 200));
        }
        saveState();
      }
    } catch (err) {
      console.error("Chyba ve smyčce:", err.message);
      if (/closed|Target page/.test(err.message)) process.exit(1);
    }
    await sleep(config.checkEverySeconds * 1000);
  }
}

main();
