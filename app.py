from __future__ import annotations

import io
import json
import os
import textwrap
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
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN", "")
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY", "") or os.getenv("DROPBOX_CLIENT_ID", "")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET", "") or os.getenv("DROPBOX_CLIENT_SECRET", "")

app = FastAPI(title=APP_TITLE, version="1.3.0")


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


def _require_api_key(given: Optional[str]) -> None:
    if not API_KEY:
        raise HTTPException(status_code=500, detail="API_KEY is not configured on the server.")
    if not given or given != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key.")


def _build_dropbox_client() -> dropbox.Dropbox:
    """
    Prefer refresh-token auth so Dropbox can rotate short-lived access tokens automatically.
    Fallback to a static access token for backward compatibility.
    """
    if DROPBOX_REFRESH_TOKEN:
        if not DROPBOX_APP_KEY or not DROPBOX_APP_SECRET:
            raise HTTPException(
                status_code=500,
                detail=(
                    "DROPBOX_REFRESH_TOKEN is configured, but DROPBOX_APP_KEY / "
                    "DROPBOX_APP_SECRET are missing."
                ),
            )
        return dropbox.Dropbox(
            oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
            app_key=DROPBOX_APP_KEY,
            app_secret=DROPBOX_APP_SECRET,
        )

    if DROPBOX_ACCESS_TOKEN:
        return dropbox.Dropbox(DROPBOX_ACCESS_TOKEN)

    raise HTTPException(
        status_code=500,
        detail=(
            "Dropbox credentials are not configured. Set either "
            "DROPBOX_REFRESH_TOKEN + DROPBOX_APP_KEY + DROPBOX_APP_SECRET "
            "or DROPBOX_ACCESS_TOKEN."
        ),
    )


def _require_dropbox_client() -> dropbox.Dropbox:
    try:
        dbx = _build_dropbox_client()
        # Early auth check so token problems fail with a clear message before upload starts.
        dbx.users_get_current_account()
        return dbx
    except dropbox.exceptions.AuthError as e:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Dropbox authentication failed.",
                "error": str(e),
                "hint": (
                    "Use DROPBOX_REFRESH_TOKEN + DROPBOX_APP_KEY + DROPBOX_APP_SECRET "
                    "for automatic token refresh."
                ),
            },
        )


def _as_text(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(x) for x in value) if value else "-"
    return str(value)


def _normalize_request_body(body: Dict[str, Any]) -> tuple[Optional[str], Dict[str, Any]]:
    """
    Accept multiple body shapes to survive wrapper quirks from Actions:
    1) {"api_key": "...", "trade": {...}}
    2) {"api_key": "...", "payload": {"trade": {...}}}
    3) {"api_key": "...", "data": {"trade": {...}}}
    4) {"api_key": "...", "input": {"trade": {...}}}
    5) {"api_key": "...", "kwargs": {"trade": {...}}}
    6) {"api_key": "...", <trade fields at root>}
    7) {"trade": {...}}  -> api_key absent => handled later
    """
    api_key = body.get("api_key")

    if isinstance(body.get("trade"), dict):
        return api_key, body["trade"]

    for wrapper_key in ("payload", "data", "input", "kwargs"):
        wrapper = body.get(wrapper_key)
        if isinstance(wrapper, dict):
            if isinstance(wrapper.get("trade"), dict):
                return api_key or wrapper.get("api_key"), wrapper["trade"]
            return api_key or wrapper.get("api_key"), wrapper

    root_trade = {
        k: v for k, v in body.items()
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


@app.post("/create-export")
async def create_export(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")

    api_key, trade_raw = _normalize_request_body(body)
    _require_api_key(api_key)

    try:
        trade = TradePayload.model_validate(trade_raw)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail={"message": "Invalid trade payload.", "errors": e.errors()})

    dbx = _require_dropbox_client()

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
