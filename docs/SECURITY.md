# Instaclean — Security Model (Phase 0 baseline)

Audit date: 2026-10-18. Ripetere l'audit a fine progetto (vedi checklist in fondo).

## 1. Cosa il repository non deve mai contenere

Password, cookie, session token, profili browser, header di autenticazione,
API key, HAR/trace, screenshot, log, checkpoint, dump DOM. Tutto questo è
escluso in `.gitignore`. Il profilo browser, inoltre, vive **fuori dal repo**
(`~/.instaclean/browser-profile`), quindi nemmeno un `.gitignore` rotto può
farlo finire in un commit.

Verifica prima di ogni commit:

```bash
git status --short
git diff --cached --stat
git ls-files | grep -Ei 'env$|cookie|session|profile|storage_state|\.har|\.png|\.log' || echo "clean"
```

## 2. Profilo browser persistente

| Domanda | Risposta |
|---|---|
| Dove | `~/.instaclean/browser-profile` (override con `INSTACLEAN_PROFILE_DIR`). Creato con permessi `0700`. |
| Cosa contiene | Profilo Chromium completo: cookie Instagram (= sessione loggata), localStorage/IndexedDB, cache, cronologia di quel solo profilo. Nessun dato del tuo Chrome/Safari personale. |
| Nota macOS | Playwright avvia Chromium con `--use-mock-keychain`: i cookie **non** sono protetti dal Portachiavi macOS e il programma non tocca il Keychain. Conseguenza: chi ha accesso in lettura alla cartella ha di fatto la tua sessione Instagram. Da qui i permessi `0700`. |
| Esclusione da Git | Sta fuori dal repo; in più `.gitignore` esclude `profiles/`, `browser-profile/`, `sessions/`, `auth/`, `.instaclean/`, `storage_state*.json`. |
| Export | Il codice non chiamerà mai `storage_state()`, `context.cookies()` né scriverà cookie/token su file o log. |
| Cancellazione | `rm -rf ~/.instaclean/browser-profile` (logout locale). Per invalidare anche lato server: Instagram → Impostazioni → *Dove hai effettuato l'accesso* → disconnetti la sessione. |

## 3. Dipendenze

Policy: minimo indispensabile, versioni esatte, **lock con hash SHA-256**
(`uv pip compile --generate-hashes`), installazione con `--require-hashes`,
preferenza per release con almeno ~2 settimane di età.

### Runtime (`requirements.lock`)

| Pacchetto | Versione | Perché | Provenienza | Vuln. note (OSV + pip-audit) |
|---|---|---|---|---|
| playwright | 1.63.0 (2026-09-15) | automazione browser | Microsoft, github.com/microsoft/playwright-python, Apache-2.0 | 0 |
| pyee | 13.0.1 | dep. di playwright (event emitter) | github.com/jfhbrook/pyee, MIT | 0 |
| greenlet | 3.5.6 | dep. di playwright (sync API) | github.com/python-greenlet/greenlet | 0 |
| typing-extensions | 4.16.0 | dep. di pyee | python/typing_extensions (PSF) | 0 |

Scelta 1.63.0 invece di 1.64.0: la 1.64.0 era uscita da 5 giorni.

### Dev only (`requirements-dev.lock`)

| Pacchetto | Versione | Perché |
|---|---|---|
| pytest | 9.1.1 | test automatici. Le versioni < 9.0.3 hanno GHSA-6w46-j5rx-g56g / PYSEC-2026-1845 (tmpdir) — 9.1.1 è fixata. |
| pip-audit | 2.10.1 | audit vulnerabilità (PyPA). Porta con sé ~25 dipendenze transitive (requests, rich, cyclonedx…), **mai importate dal codice runtime**. |

Risultato `pip-audit` (2026-10-18), sia sui lock con hash sia sull'ambiente installato:
`No known vulnerabilities found`.

Nessun pacchetto Node installato da noi. Playwright include internamente un
runtime Node.js (driver) dentro il wheel PyPI firmato da Microsoft.

## 4. Codice e processi

Il nostro codice **non** userà: `eval`, `exec`, `os.system`, `subprocess`,
`shell=True`, import dinamici, download-and-execute, codice offuscato.

Processi figli generati **dalla libreria** (non dal nostro codice), documentati:
1. Playwright avvia il proprio driver Node (`asyncio.create_subprocess_exec`
   in `playwright/_impl/_transport.py`), comunicazione via pipe stdin/stdout,
   nessuna porta di rete.
2. Il driver avvia Chromium, controllato via pipe CDP (non TCP).

Il nostro codice esegue JavaScript nella pagina solo tramite costanti fisse in
`instagram.py` (`page.evaluate`) per **leggere** il DOM; non costruisce mai
codice da contenuti della pagina. Unico `subprocess` del repository: in
`tests/test_security_static.py`, che lancia `git check-ignore` con argomenti fissi.

Un test automatico (`tests/test_security_static.py`) fallisce se il pacchetto
importa `subprocess`, `socket`, `requests`, `urllib.request`, ecc., chiama
`eval`/`exec`/`os.system`, accede a `cookies()`/`storage_state()`, o menziona
endpoint interni (`/api/v1`, `graphql`, `i.instagram.com`).

Il programma non legge password, cookie di altri browser, chiavi SSH,
Keychain, file personali; non richiede `sudo`; non installa LaunchAgent né
software. File scritti: solo il profilo dedicato e, nel repo, `logs/`,
`checkpoints/`, `debug/` (tutti gitignorati).

Contenuto dei file locali:
- `checkpoints/`: solo contatori, stati, timestamp e hash SHA-256 abbreviati (16 hex) delle miniature del batch in corso.
- `logs/`: eventi, conteggi, conteggi dei selettori, messaggi di errore (possono includere URL di pagine Instagram). Nessun cookie o token.
- `debug/` (solo con `--debug`): screenshot e HTML della pagina. **Possono contenere
  miniature e nomi di account dei post a cui hai messo Like**: restano in locale,
  cancellali con `rm -rf debug/` quando non servono.

## 5. Rete

| Origine | Destinazione | Quando |
|---|---|---|
| `uv`/`pip` | pypi.org, files.pythonhosted.org | installazione dipendenze |
| `pip-audit` | pypi.org / osv.dev (vulnerability DB) | solo quando lanci l'audit |
| `playwright install chromium` | cdn.playwright.dev, playwright.download.prss.microsoft.com | una tantum (eseguito il 2026-10-18: Chromium 1243 + headless shell + ffmpeg, ~564 MB in `~/Library/Caches/ms-playwright`) |
| test mock DOM | nessuna: ogni richiesta è servita localmente o abortita, e il test verifica che nessuna richiesta esterna sia partita | `pytest` |
| Chromium | instagram.com, CDN Meta (cdninstagram.com, fbcdn.net…) | navigazione normale |
| Chromium | possibili servizi Google di base (component updater ecc.) | Playwright li riduce con `--disable-background-networking`, `--disable-component-update`; **da confermare** con un'ispezione del traffico in Phase 1 |

Il codice Instaclean non apre socket e non fa richieste HTTP. Nessun
analytics, telemetry, error reporting o logging remoto. Analisi statica del
driver Playwright 1.63.0: nessun endpoint di telemetria; le sole occorrenze di
"telemetry" sono preferenze Firefox che la **disabilitano**.

## 6. Permessi macOS

Nessuno richiesto: niente Accessibilità, Registrazione schermo, Accesso completo
al disco, Automazione/AppleEvents. Il browser è una finestra normale.

## 7. Comportamento verso Instagram

Nessun bypass di CAPTCHA, challenge, rate limit, Action Blocked. Nessuna
tecnica di stealth. Su qualunque segnale anomalo il programma si ferma e
lascia il browser aperto per l'ispezione manuale. Limiti di batch e sessione
sono **limiti operativi locali**, non soglie "sicure" di Instagram: usare un
tool di automazione può comunque comportare restrizioni sull'account.

## 8. Checklist di regression audit (fine progetto)

```bash
.venv/bin/pip-audit --strict --require-hashes --disable-pip -r requirements.lock -r requirements-dev.lock
uv pip list --python .venv/bin/python
grep -rnE 'eval\(|exec\(|os\.system|subprocess|shell=True|__import__|importlib|requests|urllib|socket|http\.client|cookies\(|storage_state' src/ || echo "clean"
git status && git log --stat && git ls-files
```
