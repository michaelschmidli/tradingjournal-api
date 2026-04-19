# 91 Tradingjournal – Backend Lösung 3 (Fix)

Dieses Paket behebt den aktuellen Fehler zwischen Chatbot und Backend, **ohne die Abfrage-Logik im Chat zu ändern**.

## Was wurde korrigiert?

### 1. OpenAPI-Schema präzisiert
Der Fehler `UnrecognizedKwargsError: trade` entsteht typischerweise dann, wenn die GPT-Action das Request-Schema nicht sauber als festes Objekt erkennt.

Deshalb wurde `openapi.yaml` geändert:
- klares `CreateExportRequest`-Schema
- `api_key` und `trade` jetzt explizit als Pflichtfelder
- `trade_id` im Trade-Payload explizit als Pflichtfeld
- feste Response-Schemas ergänzt

### 2. Backend robuster gemacht
Das Backend akzeptiert jetzt zusätzlich Wrapper-Formen, die bei Actions gelegentlich auftreten können, z. B.:
- `payload`
- `data`
- `input`
- `kwargs`
- JSON-Strings statt echter Objekte
- Root-Level-Trade-Felder

Dadurch bricht der Export auch dann nicht sofort, wenn die Action den Body leicht anders übergibt.

### 3. Automatische Unified-Felder ergänzt
Falls noch nicht gesetzt, werden jetzt automatisch ergänzt:
- `date`
- `metrics.hold_time_minutes`
- `metrics.net_profit_after_fees`
- `metrics.win_flag`
- `metrics.loss_flag`
- `metrics.weekday`
- `risk_reward`
- `risk_per_trade_r`

Damit bleibt das Unified-Format konsistent.

## Wichtig
Die Chatbot-Abfrage selbst wird dadurch **nicht verändert**.
Nur die Schnittstelle zwischen GPT-Action und Backend wurde stabilisiert.

## Deployment
Nach dem Austausch der Dateien bitte:

1. Backend neu deployen
2. im GPT-Builder die Action mit der neuen `openapi.yaml` erneut speichern / neu importieren
3. Test mit einem echten Export durchführen

## Enthaltene Dateien
- `app.py`
- `openapi.yaml`
- `README_de.md`
