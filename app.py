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

app = FastAPI(title=APP_TITLE, version="1.5.0")

CM = 28.3464566929
PAGE_W, PAGE_H = A4
LEFT_MARGIN = 2.5 * CM
RIGHT_MARGIN = 2.0 * CM
TOP_MARGIN = 2.0 * CM
BOTTOM_MARGIN = 2.0 * CM
CONTENT_W = PAGE_W - LEFT_MARGIN - RIGHT_MARGIN
COL_W = 7.5 * CM
COL_GAP = 1.5 * CM
LEFT_COL_X = LEFT_MARGIN
RIGHT_COL_X = LEFT_MARGIN + COL_W + COL_GAP
ACCENT_WIN = colors.HexColor("#00eeff")
ACCENT_LOSS = colors.HexColor("#e200ff")
ACCENT_BE = colors.HexColor("#ffe800")
TEXT = colors.black
TEXT_MUTED = colors.Color(0, 0, 0, alpha=0.5)
LINE = colors.Color(0, 0, 0, alpha=0.12)
CARD_BG = colors.Color(0.965, 0.965, 0.985)
CARD_BORDER = colors.Color(0.60, 0.54, 0.85)

GERMAN_WEEKDAYS = {
    "Monday": "Montag",
    "Tuesday": "Dienstag",
    "Wednesday": "Mittwoch",
    "Thursday": "Donnerstag",
    "Friday": "Freitag",
    "Saturday": "Samstag",
    "Sunday": "Sonntag",
}
GERMAN_MONTHS = {
    1: "Januar",
    2: "Februar",
    3: "März",
    4: "April",
    5: "Mai",
    6: "Juni",
    7: "Juli",
    8: "August",
    9: "September",
    10: "Oktober",
    11: "November",
    12: "Dezember",
}


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


def _format_date_long(value: Any) -> str:
    dt = _parse_iso_datetime(value)
    if not dt:
        return "-"
    weekday = GERMAN_WEEKDAYS.get(dt.strftime("%A"), dt.strftime("%A"))
    month = GERMAN_MONTHS.get(dt.month, dt.strftime("%B"))
    return f"{weekday}, {dt.day:02d}. {month} {dt.year}"


def _format_datetime_de(value: Any) -> str:
    dt = _parse_iso_datetime(value)
    if not dt:
        return "-"
    return f"{dt.day:02d}.{dt.month:02d}.{dt.year} {dt.hour:02d}:{dt.minute:02d}"


def _format_number(value: Any, decimals: int = 2, suffix: str = "") -> str:
    if value is None or value == "":
        return "-"
    num = _safe_float(value)
    if num is None:
        return str(value)
    formatted = f"{num:,.{decimals}f}"
    formatted = formatted.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{formatted}{suffix}"


def _format_currency(value: Any, suffix: str = " $") -> str:
    return _format_number(value, 2, suffix)


def _format_percent(value: Any) -> str:
    return _format_number(value, 2, " %")


def _format_r_multiple(value: Any) -> str:
    num = _safe_float(value)
    if num is None:
        return "-"
    return f"{_format_number(num)}R"


def _format_hold_time(minutes: Any) -> str:
    total = _safe_float(minutes)
    if total is None:
        return "-"
    total_minutes = int(round(total))
    days = total_minutes // 1440
    remaining = total_minutes % 1440
    hours = remaining // 60
    mins = remaining % 60
    parts: list[str] = []
    if days:
        parts.append(f"{days} Tag" if days == 1 else f"{days} Tage")
    if hours:
        parts.append(f"{hours} Std")
    if mins or not parts:
        parts.append(f"{mins} Min" if parts else f"{mins} Minuten")
    return " ".join(parts)


def _format_list(value: Any) -> list[str]:
    if not value:
        return ["-"]
    if isinstance(value, (list, tuple)):
        items = [str(x).strip() for x in value if x not in (None, "")]
        return items or ["-"]
    text = str(value).strip()
    if not text:
        return ["-"]
    if "\n" in text:
        parts = [part.strip("-• \t") for part in text.splitlines() if part.strip()]
        return parts or [text]
    return [text]


def _as_text(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return "Ja" if value else "Nein"
    if isinstance(value, float):
        return _format_number(value)
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


def _trade_result(trade: TradePayload) -> tuple[str, colors.Color]:
    pnl = _safe_float(trade.pnl)
    if pnl is None:
        return "-", TEXT
    if pnl > 0:
        return "Gewinn", ACCENT_WIN
    if pnl < 0:
        return "Verlust", ACCENT_LOSS
    return "Break-even", ACCENT_BE


def _pick_first(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _generate_bot_assessment(trade: TradePayload) -> Dict[str, Any]:
    existing = trade.bot_assessment or {}
    if any(existing.get(key) not in (None, "", [], {}) for key in ("rating", "summary", "strengths", "weaknesses", "live_coaching")):
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

    if trade.metrics.get("hold_time_minutes") in (None, ""):
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


def _set_font(c: canvas.Canvas, style: str, size: float) -> None:
    font = "Helvetica"
    if style == "bold":
        font = "Helvetica-Bold"
    c.setFont(font, size)


def _draw_section_title(c: canvas.Canvas, x: float, y: float, title: str, width: float, accent: colors.Color = TEXT) -> float:
    _set_font(c, "bold", 11)
    c.setFillColor(TEXT)
    c.drawString(x, y, title.upper())
    y -= 8
    c.setStrokeColor(accent)
    c.setLineWidth(1)
    c.line(x, y, x + width, y)
    return y - 14


def _draw_labeled_rows(
    c: canvas.Canvas,
    x: float,
    y: float,
    width: float,
    rows: list[tuple[str, str, Optional[colors.Color]]],
    label_size: float,
    value_size: float,
) -> float:
    label_w = 2.4 * CM
    for label, value, value_color in rows:
        _set_font(c, "normal", label_size)
        c.setFillColor(TEXT_MUTED)
        c.drawString(x, y, label)
        _set_font(c, "normal", value_size)
        c.setFillColor(value_color or TEXT)
        wrapped = textwrap.wrap(value or "-", width=max(12, int((width - label_w) / 5.2))) or ["-"]
        line_y = y
        for idx, chunk in enumerate(wrapped):
            c.drawString(x + label_w, line_y, chunk)
            if idx < len(wrapped) - 1:
                line_y -= value_size + 3
        row_bottom = line_y - 10
        c.setStrokeColor(LINE)
        c.setLineWidth(0.6)
        c.line(x, row_bottom, x + width, row_bottom)
        y = row_bottom - 12
    return y


def _draw_card(c: canvas.Canvas, x: float, y_top: float, width: float, height: float, title: str) -> float:
    c.setFillColor(CARD_BG)
    c.setStrokeColor(CARD_BORDER)
    c.roundRect(x, y_top - height, width, height, 10, fill=1, stroke=0)
    _set_font(c, "bold", 10)
    c.setFillColor(TEXT)
    c.drawString(x + 12, y_top - 18, title)
    return y_top - 34


def _draw_bullet_list(c: canvas.Canvas, x: float, y: float, width: float, items: list[str], size: float = 9) -> float:
    bullet_indent = 10
    wrap_chars = max(10, int((width - bullet_indent) / 4.8))
    for item in items:
        wrapped = textwrap.wrap(item or "-", width=wrap_chars) or ["-"]
        _set_font(c, "normal", size)
        c.setFillColor(TEXT)
        c.drawString(x, y, "•")
        line_y = y
        for idx, chunk in enumerate(wrapped):
            c.drawString(x + bullet_indent, line_y, chunk)
            if idx < len(wrapped) - 1:
                line_y -= size + 3
        y = line_y - 12
    return y


def build_pdf_bytes(trade: TradePayload) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    c.setTitle(f"Trade {trade.trade_id}")

    result_text, result_color = _trade_result(trade)
    leverage_value = _pick_first(trade.metrics.get("leverage"), trade.journal.get("leverage"), trade.attachments.get("leverage"))
    qty_value = _pick_first(
        trade.metrics.get("order_amount_usd"),
        trade.metrics.get("position_notional_usd"),
        trade.journal.get("order_amount_usd"),
        trade.journal.get("quantity_usd"),
        trade.journal.get("quantity"),
    )

    title_y = PAGE_H - TOP_MARGIN + 8
    _set_font(c, "bold", 14)
    c.setFillColor(TEXT)
    c.drawString(LEFT_MARGIN, title_y, f"Trade {trade.trade_id}")

    page1_top = title_y - 28

    left_rows = [
        ("Datum", _format_date_long(trade.date), None),
        ("Asset", _as_text(trade.asset), None),
        ("Richtung", _as_text(trade.side), None),
        ("Leverage", _as_text(leverage_value), None),
        ("Ordermenge", _format_currency(qty_value) if _safe_float(qty_value) is not None else _as_text(qty_value), None),
        ("Entry Preis", _format_currency(trade.entry_price), None),
        ("Exit Preis", _format_currency(trade.exit_price), None),
        ("Session", _as_text(trade.session), None),
        ("Entry Zeit", _format_datetime_de(trade.entry_time), None),
        ("Exit Zeit", _format_datetime_de(trade.exit_time), None),
        ("Hold Time", _format_hold_time(trade.metrics.get("hold_time_minutes")), None),
    ]

    pnl_num = _safe_float(trade.pnl)
    roi_num = _safe_float(trade.metrics.get("roi_percent"))
    rr_num = _safe_float(trade.risk_reward)
    net_num = _safe_float(trade.metrics.get("net_profit_after_fees"))
    mfe_num = _safe_float(trade.metrics.get("mfe"))
    mae_num = _safe_float(trade.metrics.get("mae"))

    performance_color = result_color if result_text != "-" else TEXT
    right_rows = [
        ("Ergebnis", result_text, performance_color),
        ("PnL", _format_currency(trade.pnl), ACCENT_WIN if (pnl_num or 0) > 0 else ACCENT_LOSS if (pnl_num or 0) < 0 else ACCENT_BE if pnl_num == 0 else TEXT),
        ("ROI", _format_percent(roi_num), ACCENT_WIN if (roi_num or 0) > 0 else ACCENT_LOSS if (roi_num or 0) < 0 else ACCENT_BE if roi_num == 0 else TEXT),
        ("R-Multiple", _format_r_multiple(rr_num), ACCENT_WIN if (rr_num or 0) > 0 else ACCENT_LOSS if (rr_num or 0) < 0 else ACCENT_BE if rr_num == 0 else TEXT),
        ("Netto nach Fees", _format_currency(net_num), ACCENT_WIN if (net_num or 0) > 0 else ACCENT_LOSS if (net_num or 0) < 0 else ACCENT_BE if net_num == 0 else TEXT),
        ("Setup", _as_text(trade.setup), None),
        ("Nutzerbewertung", _as_text(trade.journal.get("setup_rating")), None),
        ("Stop Loss", _format_currency(trade.journal.get("stop_loss")), None),
        ("Take Profit", _format_currency(trade.journal.get("take_profit")), None),
        ("MFE", _format_currency(mfe_num), None),
        ("MAE", _format_currency(mae_num), None),
    ]

    left_y = _draw_section_title(c, LEFT_COL_X, page1_top, "Trade Details", COL_W, ACCENT_WIN)
    left_y = _draw_labeled_rows(c, LEFT_COL_X, left_y, COL_W, left_rows, 9, 9)

    right_y = _draw_section_title(c, RIGHT_COL_X, page1_top, "Performance & Setup", COL_W, ACCENT_WIN)
    right_y = _draw_labeled_rows(c, RIGHT_COL_X, right_y, COL_W, right_rows, 10, 10)

    chart_top = min(left_y, right_y) - 8
    chart_title_y = _draw_section_title(c, LEFT_COL_X, chart_top, "Chart", 16 * CM, ACCENT_WIN)
    chart_height = 9 * CM
    chart_width = 16 * CM
    chart_bottom = chart_title_y - chart_height
    chart = _load_chart_image(trade.attachments.get("chart_screenshot"))
    c.setStrokeColor(LINE)
    c.setLineWidth(0.8)
    c.rect(LEFT_COL_X, chart_bottom, chart_width, chart_height, stroke=1, fill=0)
    if chart is not None:
        try:
            c.drawImage(chart, LEFT_COL_X, chart_bottom, width=chart_width, height=chart_height, preserveAspectRatio=True, anchor='c')
        except Exception:
            _set_font(c, "normal", 10)
            c.setFillColor(TEXT_MUTED)
            c.drawString(LEFT_COL_X + 12, chart_bottom + chart_height / 2, "Chart konnte nicht geladen werden.")
    else:
        _set_font(c, "normal", 10)
        c.setFillColor(TEXT_MUTED)
        c.drawString(LEFT_COL_X + 12, chart_bottom + chart_height / 2, "Kein Chart-Screenshot vorhanden oder abrufbar.")

    _set_font(c, "normal", 10)
    c.setFillColor(TEXT_MUTED)
    c.drawRightString(PAGE_W - RIGHT_MARGIN, BOTTOM_MARGIN - 8, "Seite 1 von 2")

    c.showPage()

    page2_top = PAGE_H - TOP_MARGIN + 8
    psych_y = _draw_section_title(c, LEFT_MARGIN, page2_top, "Psychologie", CONTENT_W, CARD_BORDER)
    psych_card_w = (CONTENT_W - 2 * 8) / 3
    psych_card_h = 3.3 * CM
    psych_titles = ["Vor dem Trade", "Während des Trades", "Nach dem Exit"]
    psych_values = [
        _as_text(trade.journal.get("emotion_before")),
        _as_text(trade.journal.get("emotion_during")),
        _as_text(trade.journal.get("emotion_after")),
    ]
    x = LEFT_MARGIN
    for idx in range(3):
        content_y = _draw_card(c, x, psych_y, psych_card_w, psych_card_h, psych_titles[idx])
        _set_font(c, "normal", 9)
        c.setFillColor(TEXT)
        wrapped = textwrap.wrap(psych_values[idx], width=24) or ["-"]
        y = content_y
        for chunk in wrapped[:5]:
            c.drawString(x + 12, y, chunk)
            y -= 12
        x += psych_card_w + 8

    lessons_y = psych_y - psych_card_h - 20
    lessons_y = _draw_section_title(c, LEFT_MARGIN, lessons_y, "Lessons Learned", CONTENT_W, CARD_BORDER)
    lesson_titles = ["Gut", "Schlecht", "Nächstes Mal"]
    lesson_values = [
        _format_list(trade.journal.get("lessons_good")),
        _format_list(trade.journal.get("lessons_bad")),
        _format_list(trade.journal.get("lessons_next_time")),
    ]
    x = LEFT_MARGIN
    lesson_card_h = 3.8 * CM
    for idx in range(3):
        content_y = _draw_card(c, x, lessons_y, psych_card_w, lesson_card_h, lesson_titles[idx])
        _draw_bullet_list(c, x + 12, content_y, psych_card_w - 24, lesson_values[idx], size=9)
        x += psych_card_w + 8

    coach_y = lessons_y - lesson_card_h - 20
    coach_y = _draw_section_title(c, LEFT_MARGIN, coach_y, "Live Coaching", CONTENT_W, CARD_BORDER)
    coach_h = 5.6 * CM
    c.setFillColor(CARD_BG)
    c.setStrokeColor(CARD_BORDER)
    c.roundRect(LEFT_MARGIN, coach_y - coach_h, CONTENT_W, coach_h, 12, fill=1, stroke=0)

    left_block_w = 2.8 * CM
    _set_font(c, "bold", 10)
    c.setFillColor(TEXT)
    c.drawString(LEFT_MARGIN + 12, coach_y - 20, "Botbewertung")
    rating = _as_text(trade.bot_assessment.get("rating"))
    _set_font(c, "bold", 38)
    c.setFillColor(CARD_BORDER)
    c.drawString(LEFT_MARGIN + 18, coach_y - 72, rating)
    _set_font(c, "normal", 10)
    c.setFillColor(TEXT)
    c.drawString(LEFT_MARGIN + 14, coach_y - 96, "Automatische")
    c.drawString(LEFT_MARGIN + 14, coach_y - 110, "Bot-Bewertung")

    divider_x = LEFT_MARGIN + left_block_w + 18
    c.setStrokeColor(colors.Color(0, 0, 0, alpha=0.12))
    c.line(divider_x, coach_y - 16, divider_x, coach_y - coach_h + 16)

    content_x = divider_x + 14
    content_w = CONTENT_W - left_block_w - 40
    _set_font(c, "bold", 10)
    c.setFillColor(TEXT)
    c.drawString(content_x, coach_y - 20, "Kurzfazit")
    _set_font(c, "normal", 9)
    summary_lines = textwrap.wrap(_as_text(trade.bot_assessment.get("summary")), width=70) or ["-"]
    y = coach_y - 36
    for chunk in summary_lines[:3]:
        c.drawString(content_x, y, chunk)
        y -= 12

    sub_y = y - 8
    sub_w = (content_w - 16) / 3
    c.line(content_x, sub_y + 8, content_x + content_w, sub_y + 8)

    # strengths
    _set_font(c, "bold", 10)
    c.drawString(content_x, sub_y - 8, "Stärken")
    y1 = _draw_bullet_list(c, content_x, sub_y - 24, sub_w, _format_list(trade.bot_assessment.get("strengths")), size=9)

    # weaknesses
    block2_x = content_x + sub_w + 8
    _set_font(c, "bold", 10)
    c.drawString(block2_x, sub_y - 8, "Schwächen")
    y2 = _draw_bullet_list(c, block2_x, sub_y - 24, sub_w, _format_list(trade.bot_assessment.get("weaknesses")), size=9)

    # coaching
    block3_x = block2_x + sub_w + 8
    _set_font(c, "bold", 10)
    c.drawString(block3_x, sub_y - 8, "Coaching")
    coach_lines = textwrap.wrap(_as_text(trade.bot_assessment.get("live_coaching")), width=24) or ["-"]
    _set_font(c, "normal", 9)
    yy = sub_y - 24
    for chunk in coach_lines[:6]:
        c.drawString(block3_x, yy, chunk)
        yy -= 12

    _set_font(c, "normal", 10)
    c.setFillColor(TEXT_MUTED)
    c.drawRightString(PAGE_W - RIGHT_MARGIN, BOTTOM_MARGIN - 8, "Seite 2 von 2")

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
