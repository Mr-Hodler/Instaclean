# Instaclean

Tool locale per macOS che rimuove in batch i **tuoi** Like da Instagram usando la
normale interfaccia web (*Your activity → Interactions → Likes*), in un Chromium
visibile pilotato da Playwright. Login e 2FA li fai tu, a mano.

- Nessuna API privata, nessuna richiesta HTTP diretta, nessun reverse engineering.
- Nessun bypass di CAPTCHA, rate limit o sistemi anti-bot; nessuna tecnica stealth.
- Dry run di default. Il real mode richiede due interruttori espliciti e una
  progressione di stage manuale.

> **Nessuna garanzia.** Usare automazione su Instagram può comportare restrizioni
> dell'account. I limiti di batch/sessione sono limiti operativi locali, non
> soglie "sicure" di Instagram.

Documenti: [architettura](docs/ARCHITECTURE.md) · [modello di sicurezza](docs/SECURITY.md)

---

## 1. Requisiti

| | |
|---|---|
| macOS | Apple Silicon o Intel, versione recente |
| Python | 3.11+ (testato con **3.14.6** via `uv`) |
| Spazio disco | ~600 MB per Chromium di Playwright (`~/Library/Caches/ms-playwright`) |
| Permessi macOS | nessuno (né Accessibilità, né Registrazione schermo) |

## 2. Installazione

```bash
git clone https://github.com/Mr-Hodler/Instaclean.git
cd Instaclean
uv venv .venv --python 3.14
uv pip install --python .venv/bin/python --require-hashes -r requirements.lock -r requirements-dev.lock
```

`--require-hashes` fa sì che venga installato esattamente il codice verificato
(hash SHA-256 nei file `.lock`). Senza `uv`: `python3 -m venv .venv` e
`.venv/bin/pip install --require-hashes -r requirements.lock -r requirements-dev.lock`.

## 3. Playwright / browser

```bash
.venv/bin/playwright install chromium
```

Scarica una volta Chromium da `cdn.playwright.dev` / `playwright.download.prss.microsoft.com`.
Instaclean usa questo Chromium con un **profilo dedicato**, mai il tuo Chrome o Safari.

## 4. Autenticazione (manuale)

1. Avvia Instaclean (anche in dry run).
2. Si apre una finestra Chromium su instagram.com.
3. Se non sei loggato, fai login tu, inclusa la 2FA. Instaclean aspetta fino a 10 minuti.
4. La sessione resta nel profilo dedicato per le esecuzioni successive.

| | |
|---|---|
| Dove | `~/.instaclean/browser-profile` (permessi `0700`), fuori dal repository |
| Cosa contiene | cookie di sessione Instagram, cache, storage di quel solo profilo |
| Git | fuori dal repo + pattern in `.gitignore` |
| Cancellare | `rm -rf ~/.instaclean/browser-profile`, poi Instagram → Impostazioni → *Dove hai effettuato l'accesso* → esci da quella sessione |

Instaclean non legge, non esporta e non logga cookie o token.

## 5. Configurazione

```bash
cp .env.example .env     # .env è gitignorato
```

| Variabile | Default | Significato |
|---|---|---|
| `INSTACLEAN_DRY_RUN` | `true` | deve essere `false` **e** serve `--real` per agire davvero |
| `INSTACLEAN_BATCH_MIN` / `MAX` | `80` / `100` | intervallo della dimensione casuale del batch; 100 è un tetto fisso nel codice |
| `INSTACLEAN_MAX_ACTIONS_PER_SESSION` | `500` | tetto di Unlike per esecuzione |
| `INSTACLEAN_INITIAL_TEST_SIZE` | `3` | dimensione del primo test reale |
| `INSTACLEAN_DEBUG` | `false` | screenshot + snapshot DOM in `debug/` |
| `INSTACLEAN_PROFILE_DIR` | `~/.instaclean/browser-profile` | rifiutato se dentro `~/Library/Application Support`, `~/.ssh`, ecc. |

Le stesse variabili possono arrivare dall'ambiente della shell (che ha la
precedenza sul file `.env`).

## 6. Procedura (stage obbligatori, nessun passaggio automatico)

Ogni stage si sblocca solo dopo che il precedente è riuscito ed è registrato in
`checkpoints/state.json`. Lo stato si vede con `.venv/bin/python -m instaclean --status`.

### Test 1 — Dry run
```bash
.venv/bin/python -m instaclean
```
Naviga fino ai Likes, entra in modalità *Seleziona*, seleziona un batch (80–100
o meno se ne hai meno), verifica che la selezione corrisponda, poi **annulla
ricaricando la pagina**. Non preme mai Unlike. Guarda nel log la riga
`Diagnostics:` (conteggi dei selettori) e che i post selezionati siano Like reali.

### Test 2 — 3 Like reali
In `.env` imposta `INSTACLEAN_DRY_RUN=false`, poi:
```bash
.venv/bin/python -m instaclean --real --limit 3
```
Chiede di digitare `UNLIKE`. Rimuove 3 Like, ricarica la pagina e verifica che
i 3 non compaiano più. Esito atteso: `session_limit_reached`, `successful: 3`.

### Test 3 — Verifica manuale
Controlla da telefono o da web che quei 3 post non abbiano più il tuo Like e
che nient'altro sia cambiato. Poi:
```bash
.venv/bin/python -m instaclean --mark-verified
```

### Test 4 — Primo batch piccolo
```bash
.venv/bin/python -m instaclean --real --limit 20
```

### Esecuzione normale
```bash
.venv/bin/python -m instaclean --real
```
Batch di dimensione `randint(80, 100)`, mai più di 100, mai più di quelli
rimasti nella sessione o presenti in pagina. Se restano meno di 80 Like, li
processa tutti. A 500 azioni: `SESSION LIMIT REACHED` e uscita ordinata.
Rilancia più tardi per continuare.

## 7. Verifica dei batch

Dopo ogni batch: attende la chiusura del dialog di conferma, ricarica la pagina,
ricarica almeno tante voci quante ne erano caricate prima e confronta le identità.

| Esito | Significato |
|---|---|
| `attempted` | Unlike inviato dalla UI |
| `successful` | non più presente dopo il reload, con copertura sufficiente |
| `failed` | ancora presente → ancora liked → **STOP** |
| `uncertain` | non verificabile (es. lista non caricata abbastanza, errore dopo il click) → **STOP** |

Un'azione incerta non viene mai ripetuta automaticamente.

## 8. Checkpoint e recovery

`checkpoints/session-<id>.json` viene salvato prima del click distruttivo e dopo
ogni batch (scrittura atomica, `0600`). Contiene solo contatori e, per il batch
in corso, hash SHA-256 abbreviati delle voci: nessun URL o nome utente.

Se il programma si interrompe a metà batch, all'avvio successivo esegue **solo
una verifica** (nessun Unlike): aggiorna i contatori della vecchia sessione,
segna `recovered` e si ferma. Rilancia per proseguire.

## 9. Stop automatici

CAPTCHA, verifica identità/challenge, login richiesto, *Action Blocked*, *Try again
later*/rate limit, avvisi di sicurezza o "comportamento automatizzato", *Something
went wrong*, pagina diversa da quella attesa, selettori che falliscono più volte,
selezione diversa da quella attesa, verifica impossibile, browser chiuso.
Il browser resta aperto finché premi Invio, così puoi guardare.

Errori di caricamento non distruttivi (timeout, elemento spostato) sono ritentati
al massimo 3 volte.

Codici di uscita: `0` ok/limite raggiunto/recovery, `2` stop, `3` incerto,
`4` lista vuota non confermata, `1` errore.

## 10. Test

```bash
.venv/bin/python -m pytest -q                    # tutti (≈50 s)
.venv/bin/python -m pytest -q -m "not browser"   # solo logica pura (<1 s)
```

- **AUTOMATED TEST**: batch 80–100, tetto 100, limite sessione, < 80 rimasti,
  lista vuota, checkpoint, recovery, dry run, selection mismatch, verification
  failure, uncertain, stop condition, gate degli stage, audit statico del codice.
- **AUTOMATED TEST (mock DOM)**: il vero layer Playwright contro un'imitazione
  locale della pagina Likes (rete bloccata). Prova il codice, **non** il markup
  reale di Instagram.
- **MANUAL TEST REQUIRED**: tutto ciò che riguarda l'Instagram reale (sezione 6).

## 11. Troubleshooting

| Sintomo | Cosa fare |
|---|---|
| `stage_gate` | esegui lo stage indicato nel messaggio |
| `Real mode refused` | `INSTACLEAN_DRY_RUN=false` in `.env` **e** `--real` |
| `selector_failure` / `Menu navigation failed` | Instagram ha cambiato markup o lingua: guarda `Diagnostics:` nel log, rilancia con `--debug`, aggiorna `instaclean/selectors.py` |
| `selection_mismatch` | Instagram potrebbe limitare quante voci selezionare: riduci `INSTACLEAN_BATCH_MAX` |
| `empty_unconfirmed` | nessun pulsante *Seleziona* e nessuna miniatura: controlla a mano se hai finito |
| `login_required` a metà sessione | rifai login nella finestra e rilancia |
| `action_blocked`, `rate_limit`, `captcha`, `identity_verification` | fermati. Risolvi a mano su Instagram e aspetta prima di riprovare. Instaclean non aggira questi blocchi |
| Chromium già in uso | chiudi altre istanze di Instaclean (un profilo, un processo) |

## 12. Modello di sicurezza (riassunto)

Una dipendenza runtime (Playwright) bloccata con hash; nessuna telemetria;
nessuna rete propria; nessun `subprocess`/`eval` nel nostro codice (verificato da
un test); profilo dedicato fuori dal repo; nessun accesso a Keychain, SSH, altri
browser o file personali; nessun `sudo`. Dettagli in [docs/SECURITY.md](docs/SECURITY.md).

## 13. Limiti noti

- I selettori sono ricavati dall'interfaccia web pubblica e **devono essere
  confermati** dal dry run sul tuo account; il markup di Instagram cambia.
- Testi riconosciuti: inglese, italiano, spagnolo, francese, tedesco, portoghese,
  olandese. Altre lingue: aggiungerle in `selectors.py` / `safety.py`.
- L'identità di una voce è il nome file della sua miniatura sul CDN. Se Instagram
  cambiasse questo schema, la verifica fallirebbe in modo sicuro (stop).
- La verifica assume l'ordine "dal più recente". Se metti nuovi Like durante una
  sessione, la copertura può risultare insufficiente → `uncertain` → stop.
- La modalità *Seleziona* si annulla ricaricando la pagina.
- Il rilevamento degli stop si basa su URL, frame e testo visibile: un blocco
  presentato in modo nuovo potrebbe non essere riconosciuto. Il browser è sempre
  visibile proprio per questo.
