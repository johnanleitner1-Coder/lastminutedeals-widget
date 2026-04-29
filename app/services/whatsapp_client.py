"""
WhatsApp Business API client — Meta Cloud API wrapper.

Handles sending messages, interactive buttons, and links via the
Meta Graph API. Receives incoming messages via webhook (see routers/whatsapp.py).
"""

import httpx

from app.config import WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_ACCESS_TOKEN

META_API_URL = "https://graph.facebook.com/v21.0"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


async def send_text_message(to_phone: str, text: str) -> dict:
    """Send a plain text message."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{META_API_URL}/{WHATSAPP_PHONE_NUMBER_ID}/messages",
            headers=_headers(),
            json={
                "messaging_product": "whatsapp",
                "to": to_phone,
                "type": "text",
                "text": {"body": text},
            },
            timeout=15,
        )
        return resp.json()


async def send_interactive_buttons(
    to_phone: str,
    body_text: str,
    buttons: list[dict],
) -> dict:
    """
    Send an interactive message with up to 3 buttons.

    buttons: [{"id": "book_now", "title": "Book Now"}, ...]
    """
    button_rows = [
        {"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}}
        for b in buttons[:3]
    ]

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{META_API_URL}/{WHATSAPP_PHONE_NUMBER_ID}/messages",
            headers=_headers(),
            json={
                "messaging_product": "whatsapp",
                "to": to_phone,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": body_text},
                    "action": {"buttons": button_rows},
                },
            },
            timeout=15,
        )
        return resp.json()


async def send_link_message(
    to_phone: str,
    body_text: str,
    link_url: str,
) -> dict:
    """Send a text message with a clickable link (Stripe checkout URL)."""
    full_text = f"{body_text}\n\n{link_url}"
    return await send_text_message(to_phone, full_text)


async def send_template_message(
    to_phone: str,
    template_name: str,
    language_code: str = "en",
    components: list[dict] | None = None,
) -> dict:
    """Send a pre-approved template message (for booking confirmations after 24h window)."""
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
        },
    }
    if components:
        payload["template"]["components"] = components

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{META_API_URL}/{WHATSAPP_PHONE_NUMBER_ID}/messages",
            headers=_headers(),
            json=payload,
            timeout=15,
        )
        return resp.json()


def extract_incoming_message(webhook_payload: dict) -> dict | None:
    """
    Extract the customer's message from a Meta webhook payload.

    Returns dict with: phone, name, message_text, message_type, button_id
    or None if the payload doesn't contain a customer message.
    """
    try:
        entry = webhook_payload.get("entry", [])
        if not entry:
            return None

        changes = entry[0].get("changes", [])
        if not changes:
            return None

        value = changes[0].get("value", {})
        messages = value.get("messages", [])
        if not messages:
            return None

        msg = messages[0]
        contacts = value.get("contacts", [{}])
        contact_name = contacts[0].get("profile", {}).get("name", "") if contacts else ""

        result = {
            "phone": msg.get("from", ""),
            "name": contact_name,
            "message_type": msg.get("type", "text"),
            "message_text": "",
            "button_id": "",
        }

        if msg["type"] == "text":
            result["message_text"] = msg.get("text", {}).get("body", "")
        elif msg["type"] == "interactive":
            interactive = msg.get("interactive", {})
            if interactive.get("type") == "button_reply":
                result["button_id"] = interactive.get("button_reply", {}).get("id", "")
                result["message_text"] = interactive.get("button_reply", {}).get("title", "")
        elif msg["type"] == "button":
            result["message_text"] = msg.get("button", {}).get("text", "")

        return result

    except (IndexError, KeyError):
        return None
