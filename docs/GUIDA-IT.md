# Instaclean — Guida operativa (IT)

> README pubblico in inglese con diagrammi: [../README.md](../README.md)

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

Documenti: [architettura](ARCHITECTURE.md) · [modello di sicurezza](SECURITY.md)

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
.venv/bin/playwright install chromium
.venv/bin/python -m instaclean --doctor    # controllo ambiente: deve dire "environment OK"
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
| `INSTACLEAN_MIN_HOURS_BETWEEN_SESSIONS` | `12` | pausa obbligatoria dopo una sessione reale di dimensione normale (≥ 80) |
| `INSTACLEAN_BLOCK_COOLDOWN_HOURS` | `72` | pausa obbligatoria dopo CAPTCHA, Action Blocked, rate limit, verifica identità, avvisi di sicurezza |
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
Rilancia più tardi per continuare: il real mode resta bloccato per **12 ore**
dopo una sessione normale e per **72 ore** dopo qualunque segnale di blocco
(`--status` mostra fino a quando).

### Quanto ci vuole (es. 30.000 Like)

30.000 / 500 = **60 sessioni** da ~10–15 minuti l'una (stima: ~1,5–2,5 min per
batch da ~90). Tempo macchina totale ~10–15 ore; tempo di calendario:

| Ritmo | Like/giorno | Durata |
|---|---|---|
| 2 sessioni/giorno (massimo consentito dai default) | 1.000 | ~30 giorni |
| 1 sessione/giorno (consigliato) | 500 | ~60 giorni |
| 1 sessione/giorno da 300 | 300 | ~100 giorni |

Ogni blocco aggiunge almeno 3 giorni. Misurato nel dry run reale del 2026-10-18: caricare 99 Like ≈ 13 s,
selezionarne 93 ≈ 29 s; Unlike + verifica ancora stimati. Il numero reale di
Like si trova nell'export di Instagram (*Centro gestione account → Le tue
informazioni → Scarica le tue informazioni*). Se la pagina web non mostra i Like
più vecchi, Instaclean non può raggiungerli.

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

Instagram applica l'Unlike in blocco in modo asincrono (osservato il 2026-10-18:
18 voci su 94 ancora in lista ~10 s dopo la conferma). Per questo, se dopo il
reload alcune voci ricompaiono, Instaclean **ricontrolla** fino a 2 volte a
distanza di 45 s (solo lettura). Se restano, il batch rimane in sospeso e il
giro successivo lo ricontrolla prima di fare qualsiasi altra cosa.

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

Dopo un blocco: lascia fermare il programma, completa a mano su Instagram
eventuali verifiche, aspetta almeno 72 ore e riparti con un
`INSTACLEAN_MAX_ACTIONS_PER_SESSION` più basso (es. 200). Nessuno strumento può
garantire che l'account non venga limitato: Instaclean non si maschera, riduce
il volume e si ferma subito.

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
| `cooldown` | aspetta fino all'orario indicato (`--status`) |
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
browser o file personali; nessun `sudo`. Dettagli in [SECURITY.md](SECURITY.md).

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

## Opzioni

| Variabile | Valori | Effetto |
|---|---|---|
| `INSTACLEAN_BROWSER` | `visible` (default) · `background` · `headless` | finestra normale · finestra ridotta a icona · nessuna finestra (Instagram vede "HeadlessChrome": niente è nascosto) |
| `INSTACLEAN_CONTENT` | `all` (default) · `posts` · `reels` | tutto · solo foto e caroselli · solo video/reel |
| `INSTACLEAN_SESSION_MAX_DAYS` | `14` (default), `0` = mai | dopo N giorni dal login il profilo viene cancellato (logout locale) e si rifà il login |
| `INSTACLEAN_WIPE_PROFILE_AFTER_RUN` | `false` (default) | `true`: profilo cancellato dopo ogni giro (login ogni volta, niente esecuzione pianificata) |

Comandi: `--login` (finestra visibile, login a mano, poi si chiude) e
`--logout` (cancella subito il profilo). Il salvataggio password e
l'autocompilazione sono disattivati nel profilo: la password non finisce mai su disco.

Testato sul tuo account il 2026-10-19: tutte e tre le modalità finestra e il
filtro `posts` funzionano in dry run. Con `posts` la raccolta di un batch è più
lenta (~55 s invece di ~13 s) perché la maggior parte dei tuoi Like sono reel.

## Esecuzione pianificata (opzionale)

Dopo aver completato tutti gli stage puoi far girare una sessione normale al
giorno senza digitare `UNLIKE`:

1. in `.env`: `INSTACLEAN_ALLOW_UNATTENDED=true` (l'interruttore; `false` lo spegne)
2. comando: `.venv/bin/python -m instaclean --real --unattended`

Vale solo per sessioni normali (niente `--limit`). Pausa di 12 h, cooldown di
72 h dopo un blocco, stop e recovery restano invariati.

Durante l'esecuzione (~7–15 minuti al giorno) servono: Mac acceso e **non in
stop** (lo schermo può essere bloccato), il tuo utente macOS connesso, la
connessione internet di sempre, e lo scheduler attivo (per i task di Claude:
app aperta; se era chiusa, il task parte alla riapertura).

## Buone pratiche

- Stesso Mac, stessa rete. **Niente VPN, proxy, Tor o cambi di hotspot**: un IP o
  un paese nuovo sembra un furto d'account e può far scattare una verifica d'identità.
- Un solo dispositivo alla volta; non togliere Like in massa a mano mentre gira.
- 2FA attiva; login solo dalla finestra di Instaclean (`--login`).
- Nessun'altra automazione sullo stesso account.
- Tieni i default (500/giorno, pausa 12 h) o scendi; dopo un avviso scendi a 200–300.
- Se un giro si ferma, leggi il motivo prima di rilanciare (`--status`, `logs/`).
- Non cambiare la lingua dell'account durante la pulizia.
- Lascia scadere il login (14 giorni) e controlla ogni tanto *Dove hai effettuato l'accesso*.
- A fine lavoro: `--logout`, logout della sessione su Instagram, cancella `checkpoints/ logs/ debug/`.

## Orari e recupero

Le fasce `07:00–09:30, 10:00–12:00, 18:30–22:00` sono **preferite**, non
obbligatorie. Se il Mac era spento e nelle ultime 16 ore non è partita nessuna
sessione, Instaclean fa un **recupero** anche fuori fascia
(`INSTACLEAN_CATCHUP_AFTER_HOURS`, `0` = fasce rigide). Mai di notte: nessuna
sessione tra le 00:30 e le 06:30 (`INSTACLEAN_QUIET_HOURS`). Con la pausa di
12 ore si arriva al massimo a **due sessioni al giorno**.

Il task pianificato controlla **ogni ora dalle 7:40 alle 23:40**: quasi sempre
viene rifiutato in un secondo, e parte solo quando una sessione è dovuta.
