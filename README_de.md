# 91 Tradingjournal – Backend Lösung 3

Dieses Paket löst genau dein Problem:

Custom GPT fragt den Trade ab → ruft eine Action auf → Backend erzeugt JSON + PDF → speichert beides in Dropbox → gibt direkte Download-Links zurück.

## Dateien
- `app.py` – FastAPI Backend
- `requirements.txt` – Python-Abhängigkeiten
- `.env.example` – Umgebungsvariablen
- `openapi.yaml` – Schema für die GPT Action

## Schnellstart

### 1) Python-Umgebung
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2) `.env` anlegen
Kopiere `.env.example` nach `.env` und trage ein:
- `API_KEY`
- `DROPBOX_ACCESS_TOKEN`
- optional `DROPBOX_ROOT`

### 3) Server starten
```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

### 4) GPT Action einrichten
Im GPT-Builder:
- Configure
- Actions
- `openapi.yaml` einfügen
- als Auth `None` lassen, weil der `api_key` im Body mitgeschickt wird
  (oder alternativ auf Bearer/Auth umbauen)

### 5) In den GPT-Instructions ergänzen
Nach Bestätigung mit „Ja“:
- Unified-Trade-Objekt aufbauen
- Action `create-export` aufrufen
- `api_key` mitsenden
- `trade` mitsenden
- dem Nutzer die beiden zurückgegebenen Download-Links zeigen

## Dropbox-Zielordner
Standard:
- `/91 Tradingjournal/json`
- `/91 Tradingjournal/pdf`

Kann über `DROPBOX_ROOT` geändert werden.

## Rückgabe der API
```json
{
  "success": true,
  "trade_id": "2026-02-25-01",
  "json_file": {
    "path": "/91 Tradingjournal/json/2026-02-25-01.json",
    "url": "..."
  },
  "pdf_file": {
    "path": "/91 Tradingjournal/pdf/2026-02-25-01.pdf",
    "url": "..."
  }
}
```
