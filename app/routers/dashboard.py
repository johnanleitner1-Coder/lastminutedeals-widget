"""
Analytics dashboard — server-rendered HTML with Chart.js.

Eduardo bookmarks: /dashboard?op=oturista&token={hmac}
Shows conversations, bookings, revenue, conversion rates.
"""

import hashlib
import hmac
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.config import DASHBOARD_HMAC_SECRET, get_operator
from app.services.database import db_analytics_data, db_lookup_by_email, db_delete_by_email

router = APIRouter()
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _verify_token(operator_id: str, token: str) -> bool:
    expected = hmac.new(
        DASHBOARD_HMAC_SECRET.encode(),
        operator_id.encode(),
        hashlib.sha256,
    ).hexdigest()[:32]
    return hmac.compare_digest(token, expected)


@router.get("/dashboard")
async def dashboard(request: Request, op: str = "", token: str = ""):
    if not op or not token:
        return HTMLResponse("Missing operator or token", status_code=400)

    if not _verify_token(op, token):
        return HTMLResponse("Invalid token", status_code=403)

    operator = get_operator(op)
    if not operator:
        return HTMLResponse("Unknown operator", status_code=404)

    return TEMPLATES.TemplateResponse("dashboard.html", {
        "request": request,
        "operator": operator,
        "operator_id": op,
        "token": token,
    })


@router.get("/api/analytics")
async def analytics_data(op: str = "", token: str = "", days: int = 30):
    if not op or not token or not _verify_token(op, token):
        return JSONResponse({"error": "Unauthorized"}, status_code=403)

    operator = get_operator(op)
    if not operator:
        return JSONResponse({"error": "Unknown operator"}, status_code=404)

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    try:
        data = db_analytics_data(op, since)
    except Exception as e:
        print(f"[DASHBOARD] Analytics query failed: {e}")
        data = {
            "total_conversations": 0, "total_bookings": 0,
            "total_revenue_cents": 0, "web_conversations": 0,
            "whatsapp_conversations": 0, "web_bookings": 0,
            "whatsapp_bookings": 0, "total_escalations": 0,
            "fully_automated": 0, "after_hours": 0,
            "total_messages": 0, "avg_messages": 0,
            "recent_bookings": [],
        }

    total_conversations = data["total_conversations"]
    total_bookings = data["total_bookings"]
    conversion_rate = (total_bookings / total_conversations * 100) if total_conversations > 0 else 0
    currency_symbol = operator.currency_symbol

    # Estimate hours saved: ~4 minutes per conversation if handled by a human
    est_hours_saved = round(data["total_conversations"] * 4 / 60, 1)

    return JSONResponse({
        "period_days": days,
        "total_conversations": total_conversations,
        "total_bookings": total_bookings,
        "total_revenue": data["total_revenue_cents"] / 100,
        "currency": operator.currency,
        "currency_symbol": currency_symbol,
        "conversion_rate": round(conversion_rate, 1),
        "escalation_rate": round(data["total_escalations"] / total_conversations * 100, 1) if total_conversations > 0 else 0,
        "channels": {
            "web": {"conversations": data["web_conversations"], "bookings": data["web_bookings"]},
            "whatsapp": {"conversations": data["whatsapp_conversations"], "bookings": data["whatsapp_bookings"]},
        },
        "recent_bookings": data["recent_bookings"],
        "team_savings": {
            "fully_automated": data["fully_automated"],
            "after_hours": data["after_hours"],
            "total_messages": data["total_messages"],
            "avg_messages": data["avg_messages"],
            "est_hours_saved": est_hours_saved,
        },
    })


# ── GDPR endpoints ──────────────────────────────────────────────────────────


@router.get("/api/gdpr/lookup")
async def gdpr_lookup(op: str = "", token: str = "", email: str = ""):
    """Look up all data associated with an email (GDPR access request)."""
    if not op or not token or not _verify_token(op, token):
        return JSONResponse({"error": "Unauthorized"}, status_code=403)
    if not email:
        return JSONResponse({"error": "Email required"}, status_code=400)

    data = db_lookup_by_email(email)
    return JSONResponse({
        "email": email,
        "conversations": len(data["conversations"]),
        "bookings": len(data["bookings"]),
        "events": len(data["events"]),
        "data": data,
    })


@router.post("/api/gdpr/delete")
async def gdpr_delete(op: str = "", token: str = "", email: str = ""):
    """Delete all data associated with an email (GDPR erasure request)."""
    if not op or not token or not _verify_token(op, token):
        return JSONResponse({"error": "Unauthorized"}, status_code=403)
    if not email:
        return JSONResponse({"error": "Email required"}, status_code=400)

    summary = db_delete_by_email(email)
    return JSONResponse({
        "email": email,
        "action": "erasure_completed",
        "summary": summary,
        "note": "Booking records retained as required by law; personal details scrubbed.",
    })
