from __future__ import annotations

import io
import json
import os
import textwrap
from datetime import datetime
from typing import Any, Dict, Optional

import dropbox
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

load_dotenv()

APP_TITLE = "91 Tradingjournal Export API"
DROPBOX_ROOT = os.getenv("DROPBOX_ROOT", "/91 Tradingjournal")
API_KEY = os.getenv("API_KEY", "")
DROPBOX_ACCESS_TOKEN = os.getenv("DROPBOX_ACCESS_TOKEN", "")

app = FastAPI(title=APP_TITLE, version="1.2.0")


class TradePayload(BaseModel):
    trade_id: str
    date: str = ""
    asset: str = ""
    side: str = ""
    setup: str = ""
    session: str = ""
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    entry_time: str = ""
    exit_time: str = ""
    pnl: Optional[float] = None
    risk_reward: Optional[float] = None
    risk_per_trade_r: Optional[float] = None
    notes: Optional[str] = None
    journal: Dict[str, Any] = Field(default_factory=dict)
    metrics: Dict[str, Any] = Field(default_factory=dict)
    bot_assessment: Dict[str, Any] = Field(default_factory=dict)
    attachments: Dict[str, Any] = Field(default_factory=dict)


class CreateExportRequest(BaseModel):
    api_key: str
    trade: TradePayload


class FileInfo(BaseModel):
    path: str
    url: str


class CreateExportResponse(BaseModel):
    success: bool
    trade_id: str
    json_file: FileInfo
    pdf_file: FileInfo


def _require_api_key(given: Optional[str]) -> None:
    if not API_KEY:
        raise HTTPException(status_code=500, detail="API_KEY is not configured on the server.")
    if not given or given != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key.")


def _require_dropbox_client() -> dropbox.Dropbox:
    if not DROPBOX_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="DROPBOX_ACCESS_TOKEN is not configured on the server.")
    return dropbox.Dropbox(DROPBOX_ACCESS_TOKEN)


def _as_text(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(x) for x in value) if value else "-"
    return str(value)


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None

    raw = value.strip()
    candidates = [raw]
    if raw.endswith("Z"):
        candidates.append(raw[:-1] + "+00:00")
    if " " in raw and "T" not in raw:
        candidates.append(raw.replace(" ", "T", 1))

    for item in candidates:
        try:
            return datetime.fromisoformat(item)
        except ValueError:
            continue
    return None


def _ensure_derived_fields(trade: TradePayload) -> TradePayload:
    metrics = dict(trade.metrics or {})

    entry_dt = _parse_iso_datetime(trade.entry_time)
    exit_dt = _parse_iso_datetime(trade.exit_time)

    if not trade.date:
        trade.date = trade.exit_time or trade.entry_time or ""

    if metrics.get("hold_time_minutes") is None and entry_dt and exit_dt:
        metrics["hold_time_minutes"] = int((exit_dt - entry_dt).total_seconds() / 60)

    fees = metrics.get("fees_usd")
    if metrics.get("net_profit_after_fees") is None:
        if trade.pnl is not None:
            metrics["net_profit_after_fees"] = trade.pnl - fees if isinstance(fees, (int, float)) else trade.pnl
        else:
            metrics["net_profit_after_fees"] = None

    if metrics.get("win_flag") is None:
        metrics["win_flag"] = None if trade.pnl is None else trade.pnl > 0

    if metrics.get("loss_flag") is None:
        metrics["loss_flag"] = None if trade.pnl is None else trade.pnl < 0

    if not metrics.get("weekday") and trade.date:
        date_dt = _parse_iso_datetime(trade.date)
        if date_dt:
            metrics["weekday"] = date_dt.strftime("%A")

    if trade.risk_reward is None and isinstance(metrics.get("realized_r_multiple"), (int, float)):
        trade.risk_reward = float(metrics["realized_r_multiple"])

    if trade.risk_per_trade_r is None and isinstance(metrics.get("planned_r_multiple"), (int, float)):
        trade.risk_per_trade_r = float(metrics["planned_r_multiple"])

    trade.metrics = metrics
    return trade


def _maybe_parse_json_string(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def _normalize_request_body(body: Dict[str, Any]) -> tuple[Optional[str], Dict[str, Any]]:
    """
    Accept multiple body shapes to survive wrapper quirks from Actions.
    Supported examples:
    1) {"api_key": "...", "trade": {...}}
    2) {"payload": {"api_key": "...", "trade": {...}}}
    3) {"data": {"api_key": "...", "trade": {...}}}
    4) {"input": {"api_key": "...", "trade": {...}}}
    5) {"kwargs": {"api_key": "...", "trade": {...}}}
    6) {"api_key": "...", <trade fields at root>}
    """
    body = _maybe_parse_json_string(body)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must resolve to a JSON object.")

    for wrapper_key in ("payload", "data", "input", "kwargs"):
        wrapped = _maybe_parse_json_string(body.get(wrapper_key))
        if isinstance(wrapped, dict):
            body = {**wrapped, **{k: v for k, v in body.items() if k not in {wrapper_key}}}
            break

    api_key = body.get("api_key")
    trade_candidate = _maybe_parse_json_string(body.get("trade"))
    if isinstance(trade_candidate, dict):
        return api_key, trade_candidate

    root_trade = {
        k: _maybe_parse_json_string(v)
        for k, v in body.items()
        if k not in {"api_key", "trade", "payload", "data", "input", "kwargs"}
    }
    return api_key, root_trade


def build_json_bytes(trade: TradePayload) -> bytes:
    return json.dumps(trade.model_dump(), ensure_ascii=False, indent=2).encode("utf-8")


def build_pdf_bytes(trade: TradePayload) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    y = height - 40
    left_x = 50
    right_x = 320
    line_gap = 16

    def line(label: str, value: Any, x: int = left_x) -> None:
        nonlocal y
        c.drawString(x, y, f"{label}: {_as_text(value)}")
        y -= line_gap

    c.setFont("Helvetica-Bold", 14)
    c.drawString(left_x, y, f"Trade {trade.trade_id}")
    y -= 28

    c.setFont("Helvetica", 10)
    line("Datum", trade.date)
    line("Asset", trade.asset)
    line("Richtung", trade.side)
    line("Setup", trade.setup)
    line("Session", trade.session)
    line("Entry Preis", trade.entry_price)
    line("Exit Preis", trade.exit_price)
    line("PnL", trade.pnl)
    line("R-Multiple", trade.risk_reward)
    line("Hold Time", trade.metrics.get("hold_time_minutes"))

    y -= 8
    c.setFont("Helvetica-Bold", 11)
    c.drawString(left_x, y, "Psychologie")
    y -= 20
    c.setFont("Helvetica", 10)
    line("Vor dem Trade", trade.journal.get("emotion_before"))
    line("Während des Trades", trade.journal.get("emotion_during"))
    line("Nach dem Exit", trade.journal.get("emotion_after"))

    y -= 8
    c.setFont("Helvetica-Bold", 11)
    c.drawString(left_x, y, "Lessons Learned")
    y -= 20
    c.setFont("Helvetica", 10)
    line("Gut", trade.journal.get("lessons_good"))
    line("Schlecht", trade.journal.get("lessons_bad"))
    line("Nächstes Mal", trade.journal.get("lessons_next_time"))

    y -= 8
    c.setFont("Helvetica-Bold", 11)
    c.drawString(left_x, y, "Bewertung")
    y -= 20
    c.setFont("Helvetica", 10)
    line("Nutzerbewertung", trade.journal.get("setup_rating"))
    line("Bot-Bewertung", trade.bot_assessment.get("rating"))

    y -= 8
    c.setFont("Helvetica-Bold", 11)
    c.drawString(left_x, y, "Live Coaching")
    y -= 20
    c.setFont("Helvetica", 10)
    coaching_lines = [
        f"Kurzfazit: {_as_text(trade.bot_assessment.get('summary'))}",
        f"Staerken: {_as_text(trade.bot_assessment.get('strengths'))}",
        f"Schwaechen: {_as_text(trade.bot_assessment.get('weaknesses'))}",
        f"Coaching: {_as_text(trade.bot_assessment.get('live_coaching'))}",
    ]
    for raw in coaching_lines:
        for chunk in textwrap.wrap(raw, width=85):
            c.drawString(left_x, y, chunk)
            y -= line_gap

    y2 = height - 68
    c.setFont("Helvetica-Bold", 11)
    c.drawString(right_x, y2, "Kennzahlen")
    y2 -= 18
    c.setFont("Helvetica", 10)
    stats = [
        ("ROI", trade.metrics.get("roi_percent")),
        ("Netto nach Fees", trade.metrics.get("net_profit_after_fees")),
        ("Win Flag", trade.metrics.get("win_flag")),
        ("Loss Flag", trade.metrics.get("loss_flag")),
        ("Weekday", trade.metrics.get("weekday")),
        ("MFE", trade.metrics.get("mfe")),
        ("MAE", trade.metrics.get("mae")),
    ]
    for label, value in stats:
        c.drawString(right_x, y2, f"{label}: {_as_text(value)}")
        y2 -= line_gap

    c.save()
    pdf = buffer.getvalue()
    buffer.close()
    return pdf


def upload_to_dropbox(dbx: dropbox.Dropbox, folder: str, filename: str, payload: bytes) -> dict[str, str]:
    remote_path = f"{folder}/{filename}"
    dbx.files_upload(payload, remote_path, mode=dropbox.files.WriteMode.overwrite)
    link = dbx.files_get_temporary_link(remote_path)
    return {"path": remote_path, "url": link.link}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/create-export", response_model=CreateExportResponse)
async def create_export(request: Request):
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}") from exc

    api_key, trade_raw = _normalize_request_body(body)
    _require_api_key(api_key)

    try:
        trade = TradePayload.model_validate(trade_raw)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail={"message": "Invalid trade payload.", "errors": e.errors()})

    trade = _ensure_derived_fields(trade)
    dbx = _require_dropbox_client()

    trade_id = trade.trade_id.strip()
    if not trade_id:
        raise HTTPException(status_code=400, detail="trade_id is required.")

    json_bytes = build_json_bytes(trade)
    pdf_bytes = build_pdf_bytes(trade)

    try:
        json_info = upload_to_dropbox(dbx, f"{DROPBOX_ROOT}/json", f"{trade_id}.json", json_bytes)
        pdf_info = upload_to_dropbox(dbx, f"{DROPBOX_ROOT}/pdf", f"{trade_id}.pdf", pdf_bytes)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Dropbox upload failed: {exc}") from exc

    return JSONResponse(
        {
            "success": True,
            "trade_id": trade_id,
            "json_file": json_info,
            "pdf_file": pdf_info,
        }
    )
