# 91 Tradingjournal – Backend Lösung mit Dropbox Refresh Token

Dieses Paket löst dein aktuelles Dropbox-Problem dauerhaft sauberer:

Custom GPT fragt den Trade ab → ruft eine Action auf → Backend erzeugt JSON + PDF → Dropbox Access Token wird bei Bedarf automatisch über Refresh Token erneuert → direkte Download-Links werden zurückgegeben.

## Dateien
- `app.py` – FastAPI Backend mit Dropbox Refresh-Token-Support
- `requirements.txt` – Python-Abhängigkeiten
- `.env.example` – Beispiel für Umgebungsvariablen (optional selbst anlegen)
- `openapi.yaml` – Schema für die GPT Action

## Neue Dropbox-Umgebungsvariablen
Empfohlen ist jetzt **nicht mehr** ein statischer `DROPBOX_ACCESS_TOKEN`, sondern diese 3 Variablen:

- `DROPBOX_REFRESH_TOKEN`
- `DROPBOX_APP_KEY`
- `DROPBOX_APP_SECRET`

Optional und weiterhin unterstützt:
- `DROPBOX_ACCESS_TOKEN` (nur Fallback / alt)
- `DROPBOX_ROOT`
- `API_KEY`

## Empfohlenes Setup in Render
Setze in Render unter **Environment** mindestens:

- `API_KEY`
- `DROPBOX_REFRESH_TOKEN`
- `DROPBOX_APP_KEY`
- `DROPBOX_APP_SECRET`
- optional `DROPBOX_ROOT`

Danach **neu deployen**.

## Verhalten der neuen Version
Die neue Version verwendet automatisch:

1. Refresh Token Flow, wenn `DROPBOX_REFRESH_TOKEN` vorhanden ist
2. sonst alten `DROPBOX_ACCESS_TOKEN` als Fallback

Zusätzlich prüft das Backend die Dropbox-Authentifizierung direkt beim Aufbau des Clients. Dadurch bekommst du früh eine klare Fehlermeldung statt eines späten Upload-Fehlers.

## Schnellstart
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

## Render: Schritt für Schritt
1. `app.py` ersetzen
2. In Render → **Environment**:
   - alten `DROPBOX_ACCESS_TOKEN` optional drin lassen oder später entfernen
   - `DROPBOX_REFRESH_TOKEN` eintragen
   - `DROPBOX_APP_KEY` eintragen
   - `DROPBOX_APP_SECRET` eintragen
3. speichern
4. Deploy neu starten
5. `/health` testen
6. danach `createExport` testen

## GPT Action
Die `openapi.yaml` bleibt für den GPT-Builder gleich nutzbar:
- Auth = `None`
- `api_key` wird weiterhin im Body gesendet
- `trade` bleibt weiterhin im Body

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
