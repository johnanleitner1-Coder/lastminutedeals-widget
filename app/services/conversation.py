"""
Conversation state — channel-agnostic (serves both web widget and WhatsApp).

In-memory store for fast reads. SQLite persistence survives redeploys
and powers the analytics dashboard.
"""

import json
import secrets
from datetime import datetime, timezone

from app.services.database import (
    db_find_conversation,
    db_insert_conversation,
    db_update_messages,
    db_update_state,
    db_find_conversation_by_id,
    db_get_conversation_status,
)


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
    Checks in-memory store first, falls back to SQLite.
    """
    key = _mem_key(session_token=session_token, whatsapp_phone=whatsapp_phone)

    # 1. Check in-memory store
    if key and key in _memory:
        return _memory[key]

    # 2. Check SQLite
    try:
        conv = db_find_conversation(
            operator_id,
            session_token=session_token,
            whatsapp_phone=whatsapp_phone,
        )
        if conv:
            if whatsapp_phone:
                updated = datetime.fromisoformat(conv["updated_at"].replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - updated).total_seconds() >= 86400:
                    conv = None  # Stale WhatsApp conversation — create new
            if conv:
                if key:
                    _memory[key] = conv
                return conv
    except Exception as e:
        print(f"[CONV] SQLite lookup failed: {e}")

    # 3. Create new conversation
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

    # Persist to SQLite
    try:
        db_insert_conversation(conv)
    except Exception as e:
        print(f"[CONV] SQLite insert failed: {e}")

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
    messages = []
    if key and key in _memory:
        conv = _memory[key]
        messages = conv.get("messages", [])
        if isinstance(messages, str):
            messages = json.loads(messages)
        messages.append(msg)
        conv["messages"] = messages
        conv["message_count"] = len(messages)
        conv["updated_at"] = datetime.now(timezone.utc).isoformat()

    # Persist to SQLite
    try:
        db_update_messages(conversation_id, messages if messages else [msg], len(messages) if messages else 1)
    except Exception:
        pass


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

    # Persist to SQLite
    try:
        db_update_state(conversation_id, state, **kwargs)
    except Exception:
        pass


async def get_conversation_by_id(conversation_id: str) -> dict | None:
    # Check in-memory first
    for conv in _memory.values():
        if conv.get("id") == conversation_id:
            return conv

    try:
        return db_find_conversation_by_id(conversation_id)
    except Exception:
        pass
    return None


async def get_conversation_by_phone(operator_id: str, whatsapp_phone: str) -> dict | None:
    """Look up the most recent conversation by WhatsApp phone number."""
    key = _mem_key(whatsapp_phone=whatsapp_phone)
    if key and key in _memory:
        return _memory[key]

    try:
        return db_find_conversation(operator_id, whatsapp_phone=whatsapp_phone)
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
            "context": conv.get("context", {}),
        }

    try:
        return db_get_conversation_status(session_token)
    except Exception:
        pass
    return None
