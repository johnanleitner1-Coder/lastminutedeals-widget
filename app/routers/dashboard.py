"""
Analytics dashboard — server-rendered HTML with Chart.js.

Eduardo bookmarks: /dashboard?op=oturista&token={hmac}
Shows conversations, bookings, revenue, conversion rates.
"""

import hashlib
import hmac
import json
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.config import (
    SUPABASE_URL, SUPABASE_KEY, DASHBOARD_HMAC_SECRET, get_operator,
)

router = APIRouter()
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _verify_token(operator_id: str, token: str) -> bool:
    expected = hmac.new(
        DASHBOARD_HMAC_SECRET.encode(),
        operator_id.encode(),
        hashlib.sha256,
    ).hexdigest()[:32]
    return hmac.compare_digest(token, expected)


def _supabase_headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


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

    async with httpx.AsyncClient() as client:
        # Total conversations
        conv_resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/widget_conversations",
            headers={**_supabase_headers(), "Prefer": "count=exact"},
            params={
                "operator_id": f"eq.{op}",
                "created_at": f"gte.{since}",
                "select": "id",
            },
        )
        total_conversations = int(conv_resp.headers.get("content-range", "0-0/0").split("/")[-1])

        # Confirmed bookings
        booked_resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/widget_conversations",
            headers={**_supabase_headers(), "Prefer": "count=exact"},
            params={
                "operator_id": f"eq.{op}",
                "converted": "eq.true",
                "created_at": f"gte.{since}",
                "select": "id,revenue_cents,channel,booking_id,created_at",
            },
        )
        bookings = booked_resp.json() if booked_resp.is_success else []
        total_bookings = int(booked_resp.headers.get("content-range", "0-0/0").split("/")[-1])

        # Revenue
        total_revenue_cents = sum(b.get("revenue_cents", 0) or 0 for b in bookings)

        # Per-channel breakdown
        web_bookings = sum(1 for b in bookings if b.get("channel") == "web")
        whatsapp_bookings = sum(1 for b in bookings if b.get("channel") == "whatsapp")

        # Per-channel conversations
        web_conv_resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/widget_conversations",
            headers={**_supabase_headers(), "Prefer": "count=exact"},
            params={
                "operator_id": f"eq.{op}",
                "channel": "eq.web",
                "created_at": f"gte.{since}",
                "select": "id",
            },
        )
        web_conversations = int(web_conv_resp.headers.get("content-range", "0-0/0").split("/")[-1])

        wa_conv_resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/widget_conversations",
            headers={**_supabase_headers(), "Prefer": "count=exact"},
            params={
                "operator_id": f"eq.{op}",
                "channel": "eq.whatsapp",
                "created_at": f"gte.{since}",
                "select": "id",
            },
        )
        whatsapp_conversations = int(wa_conv_resp.headers.get("content-range", "0-0/0").split("/")[-1])

        # Escalations
        esc_resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/widget_conversations",
            headers={**_supabase_headers(), "Prefer": "count=exact"},
            params={
                "operator_id": f"eq.{op}",
                "state": "eq.human_escalation",
                "created_at": f"gte.{since}",
                "select": "id",
            },
        )
        total_escalations = int(esc_resp.headers.get("content-range", "0-0/0").split("/")[-1])

    conversion_rate = (total_bookings / total_conversations * 100) if total_conversations > 0 else 0
    currency_symbol = "€" if operator.currency == "EUR" else "$"

    return JSONResponse({
        "period_days": days,
        "total_conversations": total_conversations,
        "total_bookings": total_bookings,
        "total_revenue": total_revenue_cents / 100,
        "currency": operator.currency,
        "currency_symbol": currency_symbol,
        "conversion_rate": round(conversion_rate, 1),
        "escalation_rate": round(total_escalations / total_conversations * 100, 1) if total_conversations > 0 else 0,
        "channels": {
            "web": {"conversations": web_conversations, "bookings": web_bookings},
            "whatsapp": {"conversations": whatsapp_conversations, "bookings": whatsapp_bookings},
        },
        "recent_bookings": [
            {
                "booking_id": b.get("booking_id", ""),
                "channel": b.get("channel", ""),
                "revenue": (b.get("revenue_cents", 0) or 0) / 100,
                "created_at": b.get("created_at", ""),
            }
            for b in bookings[:20]
        ],
    })
