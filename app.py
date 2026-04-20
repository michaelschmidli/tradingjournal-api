from __future__ import annotations

import base64
import io
import json
import os
import textwrap
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import urlparse
from urllib.request import urlopen

import dropbox
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

load_dotenv()

APP_TITLE = "91 Tradingjournal Export API"
DROPBOX_ROOT = os.getenv("DROPBOX_ROOT", "/91 Tradingjournal")
API_KEY = os.getenv("API_KEY", "")
DROPBOX_ACCESS_TOKEN = os.getenv("DROPBOX_ACCESS_TOKEN", "")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN", "")
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY", "") or os.getenv("DROPBOX_CLIENT_ID", "")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET", "") or os.getenv("DROPBOX_CLIENT_SECRET", "")

app = FastAPI(title=APP_TITLE, version="1.4.0")


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


def _normalize_request_body(body: Dict[str, Any]) -> tuple[Optional[str], Dict[str, Any]]:
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


def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        return datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_datetime(value: Any) -> str:
    dt = _parse_iso_datetime(value)
    if not dt:
        return "-"
    return dt.strftime("%d.%m.%Y %H:%M")


def _format_number(value: Any, decimals: int = 2, suffix: str = "") -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return "Ja" if value else "Nein"
    num = _safe_float(value)
    if num is None:
        return str(value)
    return f"{num:.{decimals}f}{suffix}"


def _format_list(value: Any) -> str:
    if not value:
        return "-"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(x) for x in value if x not in (None, "")) or "-"
    return str(value)


def _format_hold_time(minutes: Any) -> str:
    total = _safe_float(minutes)
    if total is None:
        return "-"
    total_minutes = int(round(total))
    if total_minutes < 60:
        return f"{total_minutes} Minuten"
    hours = total_minutes // 60
    rest = total_minutes % 60
    if rest == 0:
        return f"{hours} Std"
    return f"{hours} Std {rest} Min"


def _as_text(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return "Ja" if value else "Nein"
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(x) for x in value) if value else "-"
    return str(value)


def _calc_hold_time_minutes(entry_time: str, exit_time: str) -> Optional[int]:
    entry_dt = _parse_iso_datetime(entry_time)
    exit_dt = _parse_iso_datetime(exit_time)
    if not entry_dt or not exit_dt:
        return None
    delta = int(round((exit_dt - entry_dt).total_seconds() / 60))
    return max(delta, 0)


def _calc_weekday(value: str) -> str:
    dt = _parse_iso_datetime(value)
    return dt.strftime("%A") if dt else ""


def _generate_bot_assessment(trade: TradePayload) -> Dict[str, Any]:
    existing = trade.bot_assessment or {}
    has_meaningful_existing = any(existing.get(key) not in (None, "", [], {}) for key in (
        "rating", "summary", "strengths", "weaknesses", "live_coaching"
    ))
    if has_meaningful_existing:
        return existing

    pnl = _safe_float(trade.pnl) or 0.0
    rules_followed = trade.journal.get("rules_followed")
    setup_rating = str(trade.journal.get("setup_rating") or "").strip().upper()
    strengths: list[str] = []
    weaknesses: list[str] = []

    if rules_followed is True:
        strengths.append("Regeln eingehalten")
    elif rules_followed is False:
        weaknesses.append("Regelverstoß dokumentiert")

    if pnl > 0:
        strengths.append("Trade im Gewinn geschlossen")
    elif pnl < 0:
        weaknesses.append("Trade im Verlust geschlossen")

    if setup_rating in {"A", "B"}:
        strengths.append(f"Nutzerbewertung {setup_rating}")
    elif setup_rating in {"C", "D"}:
        weaknesses.append(f"Nutzerbewertung {setup_rating}")

    if trade.journal.get("confluence_factors"):
        strengths.append("Konfluenz vorhanden")

    if not strengths:
        strengths.append("Trade sauber dokumentiert")
    if not weaknesses:
        weaknesses.append("Feinabstimmung bei Timing und Management prüfen")

    if pnl > 0 and rules_followed is True:
        rating = "A"
        summary = "Sauberer Trade mit guter Umsetzung."
        coaching = "Behalte diese Disziplin bei und dokumentiere weiter konsequent deine Konfluenz."
    elif pnl >= 0:
        rating = "B"
        summary = "Insgesamt solide Ausführung mit kleinen Verbesserungspunkten."
        coaching = "Achte weiter auf präzise Entries und halte dein Management konstant."
    elif rules_followed is False:
        rating = "D"
        summary = "Der Trade hatte klare Schwächen in der Ausführung oder Regeltreue."
        coaching = "Arbeite vor allem an Regelkonformität und warte auf sauberere Bestätigungen vor dem Entry."
    else:
        rating = "C"
        summary = "Verbesserungsfähiger Trade mit erkennbaren Lernpunkten."
        coaching = "Nimm dir vor dem nächsten Entry etwas mehr Geduld für Bestätigung und Timing."

    return {
        "rating": rating,
        "summary": summary,
        "strengths": strengths[:3],
        "weaknesses": weaknesses[:3],
        "live_coaching": coaching,
    }


def _enrich_trade(trade: TradePayload) -> TradePayload:
    trade.journal = dict(trade.journal or {})
    trade.metrics = dict(trade.metrics or {})
    trade.bot_assessment = dict(trade.bot_assessment or {})
    trade.attachments = dict(trade.attachments or {})

    if not trade.date:
        trade.date = trade.exit_time or trade.entry_time or ""

    hold_time_minutes = trade.metrics.get("hold_time_minutes")
    if hold_time_minutes in (None, ""):
        trade.metrics["hold_time_minutes"] = _calc_hold_time_minutes(trade.entry_time, trade.exit_time)

    if trade.metrics.get("net_profit_after_fees") in (None, ""):
        pnl = _safe_float(trade.pnl)
        fees = _safe_float(trade.metrics.get("fees_usd"))
        if pnl is not None:
            trade.metrics["net_profit_after_fees"] = pnl - fees if fees is not None else pnl

    if trade.metrics.get("win_flag") in (None, ""):
        trade.metrics["win_flag"] = None if trade.pnl is None else (_safe_float(trade.pnl) or 0) > 0

    if trade.metrics.get("loss_flag") in (None, ""):
        trade.metrics["loss_flag"] = None if trade.pnl is None else (_safe_float(trade.pnl) or 0) < 0

    if trade.metrics.get("weekday") in (None, "") and trade.date:
        trade.metrics["weekday"] = _calc_weekday(trade.date)

    if trade.risk_reward in (None, ""):
        realized = _safe_float(trade.metrics.get("realized_r_multiple"))
        if realized is not None:
            trade.risk_reward = round(realized, 2)
        else:
            entry = _safe_float(trade.entry_price)
            exit_price = _safe_float(trade.exit_price)
            stop_loss = _safe_float(trade.journal.get("stop_loss"))
            if entry is not None and exit_price is not None and stop_loss is not None:
                risk = abs(entry - stop_loss)
                reward = abs(exit_price - entry)
                if risk > 0:
                    trade.risk_reward = round(reward / risk, 2)

    if trade.risk_per_trade_r in (None, ""):
        planned = _safe_float(trade.metrics.get("planned_r_multiple"))
        if planned is not None:
            trade.risk_per_trade_r = round(planned, 2)

    trade.bot_assessment = _generate_bot_assessment(trade)
    return trade


def build_json_bytes(trade: TradePayload) -> bytes:
    return json.dumps(trade.model_dump(), ensure_ascii=False, indent=2).encode("utf-8")


def _load_chart_image(chart_reference: Optional[str]) -> Optional[ImageReader]:
    if not chart_reference:
        return None

    ref = str(chart_reference).strip()
    if not ref:
        return None

    try:
        if ref.startswith("data:image") and "," in ref:
            _, encoded = ref.split(",", 1)
            return ImageReader(io.BytesIO(base64.b64decode(encoded)))

        parsed = urlparse(ref)
        if parsed.scheme in {"http", "https"}:
            with urlopen(ref, timeout=10) as response:
                return ImageReader(io.BytesIO(response.read()))

        if os.path.exists(ref):
            return ImageReader(ref)
    except Exception:
        return None

    return None


def build_pdf_bytes(trade: TradePayload) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    left_margin = 56
    right_margin = width - 56
    top_y = height - 48
    left_col_x = 56
    left_val_x = 170
    right_col_x = 330
    right_val_x = 430

    def draw_pair(label_x: float, value_x: float, y: float, label: str, value: str) -> float:
        c.setFont("Helvetica-Bold", 10)
        c.setFillColor(colors.black)
        c.drawString(label_x, y, label)
        c.setFont("Helvetica", 10)
        c.drawString(value_x, y, value)
        return y - 16

    def draw_wrapped_block(title: str, items: list[tuple[str, str]], y: float) -> float:
        c.setFont("Helvetica-Bold", 11)
        c.setFillColor(colors.black)
        c.drawString(left_col_x, y, title)
        y -= 18
        for label, value in items:
            c.setFont("Helvetica-Bold", 10)
            c.drawString(left_col_x, y, label)
            y -= 12
            c.setFont("Helvetica", 10)
            wrapped = textwrap.wrap(value or "-", width=88) or ["-"]
            for chunk in wrapped:
                c.drawString(left_val_x, y, chunk)
                y -= 12
            y -= 4
        return y - 8

    def draw_chart(y_bottom: float) -> None:
        chart = _load_chart_image(trade.attachments.get("chart_screenshot"))
        c.setFont("Helvetica-Bold", 11)
        c.setFillColor(colors.black)
        c.drawString(left_col_x, y_bottom + 212, "Chart")
        c.setStrokeColor(colors.HexColor("#C9CDD3"))
        c.rect(left_col_x, y_bottom, 450, 200, stroke=1, fill=0)
        if chart is not None:
            try:
                c.drawImage(chart, left_col_x, y_bottom, width=450, height=200, preserveAspectRatio=True, anchor='c')
            except Exception:
                c.setFont("Helvetica", 10)
                c.drawString(left_col_x + 12, y_bottom + 95, "Chart konnte nicht geladen werden.")
        else:
            c.setFont("Helvetica", 10)
            c.drawString(left_col_x + 12, y_bottom + 95, "Kein Chart-Screenshot vorhanden oder abrufbar.")

    c.setTitle(f"Trade {trade.trade_id}")

    c.setFont("Helvetica-Bold", 14)
    c.setFillColor(colors.black)
    c.drawString(left_margin, top_y, f"Trade {trade.trade_id}")
    top_y -= 28

    c.setStrokeColor(colors.HexColor("#DADDE3"))
    c.line(left_margin, top_y, right_margin, top_y)
    top_y -= 20

    leverage_value = trade.metrics.get("leverage") or trade.journal.get("leverage") or trade.attachments.get("leverage") or "-"
    hold_value = _format_hold_time(trade.metrics.get("hold_time_minutes"))
    pnl_value = _safe_float(trade.pnl)

    left_y = top_y
    left_y = draw_pair(left_col_x, left_val_x, left_y, "Datum", _format_datetime(trade.date))
    left_y = draw_pair(left_col_x, left_val_x, left_y, "Asset", _as_text(trade.asset))
    left_y = draw_pair(left_col_x, left_val_x, left_y, "Richtung", _as_text(trade.side))
    left_y = draw_pair(left_col_x, left_val_x, left_y, "Setup", _as_text(trade.setup))
    left_y = draw_pair(left_col_x, left_val_x, left_y, "Session", _as_text(trade.session))
    left_y = draw_pair(left_col_x, left_val_x, left_y, "Leverage", _as_text(leverage_value))
    left_y = draw_pair(left_col_x, left_val_x, left_y, "Entry Zeit", _format_datetime(trade.entry_time))
    left_y = draw_pair(left_col_x, left_val_x, left_y, "Exit Zeit", _format_datetime(trade.exit_time))
    left_y = draw_pair(left_col_x, left_val_x, left_y, "Hold Time", hold_value)

    right_y = top_y
    right_y = draw_pair(right_col_x, right_val_x, right_y, "Entry", _format_number(trade.entry_price))
    right_y = draw_pair(right_col_x, right_val_x, right_y, "Exit", _format_number(trade.exit_price))
    right_y = draw_pair(right_col_x, right_val_x, right_y, "Stop Loss", _format_number(trade.journal.get("stop_loss")))
    right_y = draw_pair(right_col_x, right_val_x, right_y, "Take Profit", _format_number(trade.journal.get("take_profit")))

    c.setFont("Helvetica-Bold", 10)
    c.drawString(right_col_x, right_y, "PnL")
    c.setFont("Helvetica", 10)
    c.setFillColor(colors.HexColor("#15803D") if (pnl_value is not None and pnl_value > 0) else colors.HexColor("#B91C1C") if (pnl_value is not None and pnl_value < 0) else colors.black)
    c.drawString(right_val_x, right_y, _format_number(trade.pnl))
    c.setFillColor(colors.black)
    right_y -= 16

    right_y = draw_pair(right_col_x, right_val_x, right_y, "ROI", _format_number(trade.metrics.get("roi_percent"), suffix="%"))
    right_y = draw_pair(right_col_x, right_val_x, right_y, "R-Multiple", _format_number(trade.risk_reward))
    right_y = draw_pair(right_col_x, right_val_x, right_y, "Netto nach Fees", _format_number(trade.metrics.get("net_profit_after_fees")))
    right_y = draw_pair(right_col_x, right_val_x, right_y, "Nutzerbewertung", _as_text(trade.journal.get("setup_rating")))
    right_y = draw_pair(right_col_x, right_val_x, right_y, "Bot-Bewertung", _as_text(trade.bot_assessment.get("rating")))

    content_y = min(left_y, right_y) - 20
    content_y = draw_wrapped_block(
        "Psychologie",
        [
            ("Vor dem Trade", _as_text(trade.journal.get("emotion_before"))),
            ("Während des Trades", _as_text(trade.journal.get("emotion_during"))),
            ("Nach dem Exit", _as_text(trade.journal.get("emotion_after"))),
        ],
        content_y,
    )

    content_y = draw_wrapped_block(
        "Lessons Learned",
        [
            ("Gut", _as_text(trade.journal.get("lessons_good"))),
            ("Schlecht", _as_text(trade.journal.get("lessons_bad"))),
            ("Nächstes Mal", _as_text(trade.journal.get("lessons_next_time"))),
        ],
        content_y,
    )

    content_y = draw_wrapped_block(
        "Live Coaching",
        [
            ("Kurzfazit", _as_text(trade.bot_assessment.get("summary"))),
            ("Stärken", _format_list(trade.bot_assessment.get("strengths"))),
            ("Schwächen", _format_list(trade.bot_assessment.get("weaknesses"))),
            ("Coaching", _as_text(trade.bot_assessment.get("live_coaching"))),
        ],
        content_y,
    )

    notes_text = _as_text(trade.notes)
    content_y = draw_wrapped_block("Weitere Notizen", [("Notizen", notes_text)], content_y)

    chart_bottom = 40
    min_gap_to_chart = 220
    if content_y > chart_bottom + min_gap_to_chart:
        draw_chart(chart_bottom)
    else:
        c.showPage()
        draw_chart(height - 260)

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

    trade = _enrich_trade(trade)

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
