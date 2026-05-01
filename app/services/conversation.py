"""
Conversation state — channel-agnostic (serves both web widget and WhatsApp).

In-memory store for fast reads. Supabase persistence is best-effort
(writes in background, reads used as fallback on cold start).
"""

import json
import secrets
from datetime import datetime, timezone

import httpx

from app.config import SUPABASE_URL, SUPABASE_KEY


# ── In-memory conversation store ───────────────────────────────────────────
# Keyed by session_token (web) or whatsapp_phone (whatsapp).
# Survives for the lifetime of the process. Lost on redeploy.
_memory: dict[str, dict] = {}


def _mem_key(*, session_token: str | None = None, whatsapp_phone: str | None = None) -> str | None:
    if session_token:
        return f"web:{session_token}"
    if whatsapp_phone:
        return f"wa:{whatsapp_phone}"
    return None


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
    Checks in-memory store first, falls back to Supabase.
    """
    key = _mem_key(session_token=session_token, whatsapp_phone=whatsapp_phone)

    # 1. Check in-memory store
    if key and key in _memory:
        return _memory[key]

    # 2. Try Supabase (best-effort)
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
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
                    conv = resp.json()[0]
                    if key:
                        _memory[key] = conv
                    return conv
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
                    updated = datetime.fromisoformat(conv["updated_at"].replace("Z", "+00:00"))
                    if (datetime.now(timezone.utc) - updated).total_seconds() < 86400:
                        if key:
                            _memory[key] = conv
                        return conv
    except Exception as e:
        print(f"[CONV] Supabase lookup failed (using in-memory): {e}")

    # 3. Create new conversation (in-memory, persist to Supabase best-effort)
    token = session_token or generate_session_token()
    conv = {
        "id": secrets.token_hex(16),
        "operator_id": operator_id,
        "channel": channel,
        "session_token": token if channel == "web" else None,
        "whatsapp_phone": whatsapp_phone if channel == "whatsapp" else None,
        "messages": [],
        "state": "greeting",
        "context": {},
        "message_count": 0,
        "referrer": referrer,
        "user_agent": user_agent,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "converted": False,
        "booking_id": None,
        "revenue_cents": None,
    }
    if key:
        _memory[key] = conv

    # Best-effort Supabase write
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            row = {**conv, "messages": json.dumps([]), "context": json.dumps({})}
            resp = await client.post(
                f"{SUPABASE_URL}/rest/v1/widget_conversations",
                headers=_headers(prefer="return=representation"),
                json=row,
            )
            if resp.is_success and resp.json():
                db_conv = resp.json()[0]
                conv["id"] = db_conv["id"]
                if key:
                    _memory[key] = conv
    except Exception:
        pass  # In-memory conv is sufficient

    return conv


async def append_message(
    conversation_id: str,
    role: str,
    content: str,
    *,
    session_token: str | None = None,
    tool_use: dict | None = None,
) -> None:
    """Append a message to the conversation's message history."""
    msg = {
        "role": role,
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if tool_use:
        msg["tool_use"] = tool_use

    # Update in-memory store
    key = _mem_key(session_token=session_token)
    if key and key in _memory:
        conv = _memory[key]
        messages = conv.get("messages", [])
        if isinstance(messages, str):
            messages = json.loads(messages)
        messages.append(msg)
        conv["messages"] = messages
        conv["message_count"] = len(messages)
        conv["updated_at"] = datetime.now(timezone.utc).isoformat()

    # Best-effort Supabase write
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/widget_conversations",
                headers=_headers(),
                params={"id": f"eq.{conversation_id}", "select": "messages,message_count"},
            )
            if resp.is_success and resp.json():
                row = resp.json()[0]
                db_messages = json.loads(row["messages"]) if isinstance(row["messages"], str) else row["messages"]
                db_messages.append(msg)
                await client.patch(
                    f"{SUPABASE_URL}/rest/v1/widget_conversations",
                    headers=_headers(),
                    params={"id": f"eq.{conversation_id}"},
                    json={
                        "messages": json.dumps(db_messages),
                        "message_count": row["message_count"] + 1,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
    except Exception:
        pass  # In-memory is the source of truth


async def update_conversation_state(conversation_id: str, state: str, **kwargs) -> None:
    """Update conversation state and optional fields."""
    # Update in-memory (find by id)
    for conv in _memory.values():
        if conv.get("id") == conversation_id:
            conv["state"] = state
            if "context" in kwargs:
                conv["context"] = kwargs["context"]
            if "converted" in kwargs:
                conv["converted"] = kwargs["converted"]
            if "booking_id" in kwargs:
                conv["booking_id"] = kwargs["booking_id"]
            if "revenue_cents" in kwargs:
                conv["revenue_cents"] = kwargs["revenue_cents"]
            break

    # Best-effort Supabase write
    try:
        update = {"state": state, "updated_at": datetime.now(timezone.utc).isoformat()}
        if "context" in kwargs:
            update["context"] = json.dumps(kwargs["context"])
        if "converted" in kwargs:
            update["converted"] = kwargs["converted"]
        if "booking_id" in kwargs:
            update["booking_id"] = kwargs["booking_id"]
        if "revenue_cents" in kwargs:
            update["revenue_cents"] = kwargs["revenue_cents"]

        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.patch(
                f"{SUPABASE_URL}/rest/v1/widget_conversations",
                headers=_headers(),
                params={"id": f"eq.{conversation_id}"},
                json=update,
            )
    except Exception:
        pass


async def get_conversation_by_id(conversation_id: str) -> dict | None:
    # Check in-memory first
    for conv in _memory.values():
        if conv.get("id") == conversation_id:
            return conv

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/widget_conversations",
                headers=_headers(),
                params={"id": f"eq.{conversation_id}", "select": "*"},
            )
            if resp.is_success and resp.json():
                return resp.json()[0]
    except Exception:
        pass
    return None


async def get_conversation_by_phone(operator_id: str, whatsapp_phone: str) -> dict | None:
    """Look up the most recent conversation by WhatsApp phone number."""
    key = _mem_key(whatsapp_phone=whatsapp_phone)
    if key and key in _memory:
        return _memory[key]

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
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
    except Exception:
        pass
    return None


async def get_conversation_status(session_token: str) -> dict | None:
    """Get conversation state for polling (widget checks this after payment)."""
    key = _mem_key(session_token=session_token)
    if key and key in _memory:
        conv = _memory[key]
        return {
            "id": conv.get("id"),
            "state": conv.get("state"),
            "booking_id": conv.get("booking_id"),
            "converted": conv.get("converted"),
        }

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
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
    except Exception:
        pass
    return None
