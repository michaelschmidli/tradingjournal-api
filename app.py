from __future__ import annotations

import io
import json
import os
from datetime import datetime
from typing import Any, Dict, Optional

import dropbox
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


load_dotenv()

APP_TITLE = "91 Tradingjournal Export API"
DROPBOX_ROOT = os.getenv("DROPBOX_ROOT", "/91 Tradingjournal")
API_KEY = os.getenv("API_KEY", "")
DROPBOX_ACCESS_TOKEN = os.getenv("DROPBOX_ACCESS_TOKEN", "")

app = FastAPI(title=APP_TITLE, version="1.0.0")


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
    attachments: Dict[str, Any] = Field(default_factory=dict)


class ExportRequest(BaseModel):
    api_key: str
    trade: TradePayload


def _require_api_key(given: str) -> None:
    if not API_KEY:
        raise HTTPException(status_code=500, detail="API_KEY is not configured on the server.")
    if given != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key.")


def _require_dropbox_client() -> dropbox.Dropbox:
    if not DROPBOX_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="DROPBOX_ACCESS_TOKEN is not configured on the server.")
    return dropbox.Dropbox(DROPBOX_ACCESS_TOKEN)


def _as_text(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(x) for x in value) if value else "-"
    return str(value)


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
    c.drawString(left_x, y, "Weitere Notizen")
    y -= 20
    c.setFont("Helvetica", 10)
    notes = _as_text(trade.notes)
    for chunk in textwrap.wrap(notes, width=85) if notes != "-" else ["-"]:
        c.drawString(left_x, y, chunk)
        y -= line_gap

    # right-side quick stats
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


@app.post("/create-export")
def create_export(request: ExportRequest):
    _require_api_key(request.api_key)
    dbx = _require_dropbox_client()

    trade = request.trade
    trade_id = trade.trade_id.strip()
    if not trade_id:
        raise HTTPException(status_code=400, detail="trade_id is required.")

    json_bytes = build_json_bytes(trade)
    pdf_bytes = build_pdf_bytes(trade)

    json_info = upload_to_dropbox(dbx, f"{DROPBOX_ROOT}/json", f"{trade_id}.json", json_bytes)
    pdf_info = upload_to_dropbox(dbx, f"{DROPBOX_ROOT}/pdf", f"{trade_id}.pdf", pdf_bytes)

    return JSONResponse(
        {
            "success": True,
            "trade_id": trade_id,
            "json_file": json_info,
            "pdf_file": pdf_info,
        }
    )
