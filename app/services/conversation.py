"""
Conversation state — channel-agnostic (serves both web widget and WhatsApp).

Backed by Supabase Postgres via REST API. Each conversation tracks messages,
state transitions, and booking outcome for analytics attribution.
"""

import json
import secrets
from datetime import datetime, timezone

import httpx

from app.config import SUPABASE_URL, SUPABASE_KEY


def _headers(*, prefer: str = "") -> dict:
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def generate_session_token() -> str:
    return secrets.token_hex(32)


async def get_or_create_conversation(
    operator_id: str,
    *,
    session_token: str | None = None,
    whatsapp_phone: str | None = None,
    channel: str = "web",
    referrer: str = "",
    user_agent: str = "",
) -> dict:
    """
    Look up an existing conversation or create a new one.
    Web: keyed by session_token. WhatsApp: keyed by phone number.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Look up existing
        if channel == "web" and session_token:
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/widget_conversations",
                headers=_headers(),
                params={
                    "operator_id": f"eq.{operator_id}",
                    "session_token": f"eq.{session_token}",
                    "select": "*",
                    "limit": "1",
                },
            )
            if resp.is_success and resp.json():
                return resp.json()[0]
        elif channel == "whatsapp" and whatsapp_phone:
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/widget_conversations",
                headers=_headers(),
                params={
                    "operator_id": f"eq.{operator_id}",
                    "whatsapp_phone": f"eq.{whatsapp_phone}",
                    "order": "updated_at.desc",
                    "select": "*",
                    "limit": "1",
                },
            )
            if resp.is_success and resp.json():
                conv = resp.json()[0]
                # Check if conversation is stale (>24h)
                updated = datetime.fromisoformat(conv["updated_at"].replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - updated).total_seconds() < 86400:
                    return conv

        # Create new conversation
        token = session_token or generate_session_token()
        row = {
            "operator_id": operator_id,
            "channel": channel,
            "session_token": token if channel == "web" else None,
            "whatsapp_phone": whatsapp_phone if channel == "whatsapp" else None,
            "messages": json.dumps([]),
            "state": "greeting",
            "context": json.dumps({}),
            "message_count": 0,
            "referrer": referrer,
            "user_agent": user_agent,
        }
        resp = await client.post(
            f"{SUPABASE_URL}/rest/v1/widget_conversations",
            headers=_headers(prefer="return=representation"),
            json=row,
        )
        resp.raise_for_status()
        return resp.json()[0]


async def append_message(
    conversation_id: str,
    role: str,
    content: str,
    *,
    tool_use: dict | None = None,
) -> None:
    """Append a message to the conversation's message history."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Get current messages
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/widget_conversations",
            headers=_headers(),
            params={"id": f"eq.{conversation_id}", "select": "messages,message_count"},
        )
        resp.raise_for_status()
        row = resp.json()[0]
        messages = json.loads(row["messages"]) if isinstance(row["messages"], str) else row["messages"]

        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if tool_use:
            msg["tool_use"] = tool_use
        messages.append(msg)

        await client.patch(
            f"{SUPABASE_URL}/rest/v1/widget_conversations",
            headers=_headers(),
            params={"id": f"eq.{conversation_id}"},
            json={
                "messages": json.dumps(messages),
                "message_count": row["message_count"] + 1,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )


async def update_conversation_state(conversation_id: str, state: str, **kwargs) -> None:
    """Update conversation state and optional fields."""
    update = {"state": state, "updated_at": datetime.now(timezone.utc).isoformat()}
    if "context" in kwargs:
        update["context"] = json.dumps(kwargs["context"])
    if "converted" in kwargs:
        update["converted"] = kwargs["converted"]
    if "booking_id" in kwargs:
        update["booking_id"] = kwargs["booking_id"]
    if "revenue_cents" in kwargs:
        update["revenue_cents"] = kwargs["revenue_cents"]

    async with httpx.AsyncClient(timeout=30.0) as client:
        await client.patch(
            f"{SUPABASE_URL}/rest/v1/widget_conversations",
            headers=_headers(),
            params={"id": f"eq.{conversation_id}"},
            json=update,
        )


async def get_conversation_by_id(conversation_id: str) -> dict | None:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/widget_conversations",
            headers=_headers(),
            params={"id": f"eq.{conversation_id}", "select": "*"},
        )
        if resp.is_success and resp.json():
            return resp.json()[0]
    return None


async def get_conversation_by_phone(operator_id: str, whatsapp_phone: str) -> dict | None:
    """Look up the most recent conversation by WhatsApp phone number."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/widget_conversations",
            headers=_headers(),
            params={
                "operator_id": f"eq.{operator_id}",
                "whatsapp_phone": f"eq.{whatsapp_phone}",
                "order": "updated_at.desc",
                "select": "id,state,booking_id,converted",
                "limit": "1",
            },
        )
        if resp.is_success and resp.json():
            return resp.json()[0]
    return None


async def get_conversation_status(session_token: str) -> dict | None:
    """Get conversation state for polling (widget checks this after payment)."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/widget_conversations",
            headers=_headers(),
            params={
                "session_token": f"eq.{session_token}",
                "select": "id,state,booking_id,converted",
            },
        )
        if resp.is_success and resp.json():
            return resp.json()[0]
    return None
