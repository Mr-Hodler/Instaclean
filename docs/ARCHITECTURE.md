# Instaclean — Architecture Decision (Phase 0)

Status: **accepted** · Date: 2026-10-18

## Contesto

Rimuovere in batch i propri Like da Instagram **solo tramite la normale UI web
autenticata**, in locale su macOS. Esclusi per scelta: API private, reverse
engineering, richieste HTTP dirette, bypass di CAPTCHA / rate limit / anti-bot.

## Opzioni valutate

Scala: ●●● buono · ●● medio · ● scarso

| Criterio | Playwright + Chromium | Selenium | Chrome esistente (CDP) | Appium + Emulator | ADB / UIAutomator |
|---|---|---|---|---|---|
| Velocità | ●●● | ●● | ●●● | ● | ●● |
| Affidabilità (auto-wait, retry) | ●●● auto-wait nativo, locator lazy | ●● wait espliciti manuali | ●●● (stessa API Playwright) | ●● | ● |
| Debugging | ●●● trace viewer, screenshot, `PWDEBUG`, headed | ●● | ●● | ● | ● |
| Stabilità ai cambi UI | ●● role/aria/text locator | ●● | ●● | ●● accessibility id app | ● resource-id spesso offuscati |
| Gestione sessione | ●●● profilo persistente dedicato, login manuale | ●● | ● richiede avviare Chrome con `--remote-debugging-port` sul profilo **personale** | ●● login nell'app | ●● |
| RAM / CPU | ●● ~300–600 MB | ●● + chromedriver | ●●● | ● emulatore 2–4 GB | ●● (serve device/emulatore) |
| Sviluppo con Claude Code | ●●● Python puro, testabile | ●● | ●● | ● toolchain Android SDK/Java | ● |
| Manutenzione | ●●● 1 dipendenza, driver incluso | ●● chromedriver da allineare | ● | ● SDK, immagini, Appium server Node | ● |
| Superficie di sicurezza | ●●● profilo isolato | ●● | ● espone il browser personale (tutti i cookie, password salvate) via porta di debug | ●● | ●● |

### Perché non il Chrome esistente
Collegarsi al Chrome personale via CDP significa aprire una porta di debug sul
profilo che contiene **tutte** le sessioni, password salvate e cookie
dell'utente: qualunque processo locale potrebbe pilotarlo. Viola il principio
"nessun accesso a dati personali non necessari".

### Perché non Android
L'emulatore aggiunge Android SDK, immagini di sistema, server Appium (Node) e
2–4 GB di RAM, per ottenere la stessa funzione già presente in
*Your activity → Likes* sul web. Non c'è evidenza tecnica che il browser sia
insufficiente → non lo implementiamo.

## Decisione

**Playwright (Python, sync API) + Chromium bundled da Playwright, con un
profilo persistente dedicato fuori dal repository, browser visibile (headed).**

- Il browser è sempre visibile: l'utente vede ogni azione e fa login/2FA a mano.
- Nessuna tecnica "stealth": `navigator.webdriver` resta com'è, nessun
  user-agent falsificato, nessuna patch anti-detection.
- Nessuna richiesta HTTP diretta del nostro codice: solo `page.goto`, click e
  lettura del DOM.

## Struttura prevista (Phase 1+)

```
Instaclean/
├── src/instaclean/
│   ├── config.py        # limiti, DRY_RUN, DEBUG; validazione (hard cap 100)
│   ├── browser.py       # lancio contesto persistente, chiusura pulita
│   ├── selectors.py     # TUTTI i locator in un unico punto (role/aria prima)
│   ├── instagram.py     # navigazione Profile → Your activity → Likes, select, unlike
│   ├── safety.py        # rilevamento stop condition (CAPTCHA, login, blocked…)
│   ├── batch.py         # calcolo batch 80–100 vincolato, loop di sessione
│   ├── verifier.py      # verifica post-azione: attempted/successful/failed/uncertain
│   ├── checkpoint.py    # JSON atomico (write tmp + rename) dopo ogni batch
│   ├── errors.py        # tassonomia errori: Retryable / Stop / Uncertain
│   └── main.py          # CLI: --dry-run (default), --real, --test-size N
├── tests/               # pytest, senza Instagram (fake page / unit)
├── docs/                # ARCHITECTURE.md, SECURITY.md
└── pyproject.toml, requirements*.lock
```

Separazione chiave: **la logica pura** (batch, checkpoint, verifica dei
conteggi, classificazione errori) non importa Playwright ed è coperta da test
automatici. **Il layer Instagram** è sottile e si verifica con test manuali.

## Principi di correttezza

1. Un click non è un successo. Successo = elemento non più presente/non più
   liked dopo un cambio di stato osservato nel DOM.
2. Esito incerto → STOP, nessun retry di azioni distruttive.
3. Retry limitati solo per operazioni non distruttive (navigazione, attese,
   ri-scoperta di elementi).
4. Le attese si basano su condizioni (`wait_for`, conteggi, scomparsa
   elementi), non su `sleep` fissi. Piccole pause variabili solo come margine
   per il rendering, mai come meccanismo di elusione.
5. `DRY_RUN=True` di default; il real mode richiede un flag esplicito e non è
   mai attivato automaticamente.
6. Batch: `randint(80, 100)` poi `min(batch, 100, rimanenti_sessione,
   disponibili)`. 100 è un **hard cap nel codice**, non configurabile oltre.

## Da verificare manualmente in Phase 1 (non assunto)

- Struttura reale del DOM di *Your activity → Likes* nella lingua dell'account.
- Se Instagram limita il numero di elementi selezionabili per azione.
- Comportamento dopo Unlike (gli elementi spariscono? serve reload?).
- Testo/struttura delle schermate di blocco, CAPTCHA e challenge.
