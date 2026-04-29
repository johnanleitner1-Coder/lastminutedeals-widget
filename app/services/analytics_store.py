"""
Analytics event store — records widget events for conversion tracking.

Events flow into widget_events table in Supabase for dashboard queries.
"""

import json
from datetime import datetime, timezone

import httpx

from app.config import SUPABASE_URL, SUPABASE_KEY


def _headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


async def record_event(
    operator_id: str,
    conversation_id: str | None,
    event_type: str,
    metadata: dict | None = None,
) -> None:
    """Record a widget event for analytics."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return

    row = {
        "operator_id": operator_id,
        "conversation_id": conversation_id,
        "event_type": event_type,
        "metadata": json.dumps(metadata or {}),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{SUPABASE_URL}/rest/v1/widget_events",
                headers=_headers(),
                json=row,
            )
    except Exception as e:
        # Analytics failures are non-fatal — never block the booking flow
        print(f"[ANALYTICS] Event recording failed (non-fatal): {e}")
