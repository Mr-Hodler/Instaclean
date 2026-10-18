# Instaclean

Tool locale (macOS) per rimuovere in batch i propri Like da Instagram tramite la
normale interfaccia web, usando Playwright + Chromium con login manuale.

> **Stato: Phase 0 — sicurezza e architettura.** L'automazione Instagram non è
> ancora implementata. La documentazione completa arriverà con Phase 1.

- Architettura e motivazioni: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Modello di sicurezza e audit dipendenze: [docs/SECURITY.md](docs/SECURITY.md)

## Setup ambiente (verificato)

```bash
uv venv .venv --python 3.14
uv pip install --python .venv/bin/python --require-hashes -r requirements.lock -r requirements-dev.lock
.venv/bin/pip-audit --strict --require-hashes --disable-pip -r requirements.lock -r requirements-dev.lock
```

Nessuna password, cookie o token va mai inserito in file di configurazione.
