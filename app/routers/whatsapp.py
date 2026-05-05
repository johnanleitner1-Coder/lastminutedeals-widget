"""
WhatsApp webhook — incoming messages from Meta Cloud API.

GET  /api/whatsapp/webhook — Meta verification handshake
POST /api/whatsapp/webhook — incoming customer messages

Same AI pipeline as the web widget, different transport:
  - No SSE streaming (WhatsApp is request/response)
  - Messages keyed by phone number, not session token
  - Payment links sent as WhatsApp messages
"""

import hashlib
import hmac
import json
import os

from fastapi import APIRouter, Request, Query
from fastapi.responses import PlainTextResponse, JSONResponse

from app.config import WHATSAPP_VERIFY_TOKEN, get_operator

# Meta app secret for webhook signature verification
_WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")
from app.services.conversation import (
    get_or_create_conversation,
    append_message,
    update_conversation_state,
)
from app.services.ai import chat, handle_tool_calls
from app.services.availability import build_ai_product_context, get_availability_for_date, get_products_with_catalog
from app.services.payment import create_checkout_session
from app.services.whatsapp_client import (
    send_text_message,
    send_interactive_buttons,
    send_link_message,
    extract_incoming_message,
)
from app.services.analytics_store import record_event

router = APIRouter()

# Default operator for WhatsApp (single-tenant for now)
DEFAULT_OPERATOR_ID = "oturista"


@router.get("/api/whatsapp/webhook")
async def whatsapp_verify(
    hub_mode: str = Query("", alias="hub.mode"),
    hub_token: str = Query("", alias="hub.verify_token"),
    hub_challenge: str = Query("", alias="hub.challenge"),
):
    """Meta webhook verification handshake."""
    if hub_mode == "subscribe" and hub_token == WHATSAPP_VERIFY_TOKEN:
        return PlainTextResponse(hub_challenge)
    return PlainTextResponse("Forbidden", status_code=403)


@router.post("/api/whatsapp/webhook")
async def whatsapp_incoming(request: Request):
    """Handle incoming WhatsApp messages."""
    # Verify Meta webhook signature (X-Hub-Signature-256)
    if _WHATSAPP_APP_SECRET:
        raw_body = await request.body()
        signature = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(
            _WHATSAPP_APP_SECRET.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return JSONResponse({"error": "Invalid signature"}, status_code=403)
        body = json.loads(raw_body)
    else:
        body = await request.json()

    # Extract message from Meta's nested payload
    msg_data = extract_incoming_message(body)
    if not msg_data or not msg_data.get("message_text"):
        return JSONResponse({"status": "ok"})  # Status update or empty, ignore

    phone = msg_data["phone"]
    customer_name = msg_data["name"]
    message_text = msg_data["message_text"]

    operator = get_operator(DEFAULT_OPERATOR_ID)
    if not operator:
        return JSONResponse({"status": "ok"})

    # Get or create conversation (keyed by phone number)
    conv = await get_or_create_conversation(
        operator_id=operator.operator_id,
        whatsapp_phone=phone,
        channel="whatsapp",
    )
    conversation_id = conv["id"]

    # Save incoming message
    await append_message(conversation_id, "user", message_text)

    # Record analytics
    await record_event(operator.operator_id, conversation_id, "whatsapp_message_received")

    # Build message history
    messages_raw = conv.get("messages", "[]")
    if isinstance(messages_raw, str):
        messages_raw = json.loads(messages_raw)
    messages = messages_raw + [{"role": "user", "content": message_text}]

    # Build product context and get AI response
    product_context = build_ai_product_context(operator)

    try:
        ai_response = await chat(operator, messages, product_context)

        checkout_data = None
        escalation_data = None

        if ai_response["tool_use"]:
            for tu in ai_response["tool_use"]:
                if tu["name"] == "start_checkout":
                    checkout_data = tu["input"]
                elif tu["name"] == "escalate_to_human":
                    escalation_data = tu["input"]

            followup = await handle_tool_calls(
                operator, ai_response["tool_use"], messages, product_context
            )
            ai_text = followup["content"]

            if followup["tool_use"]:
                for tu in followup["tool_use"]:
                    if tu["name"] == "start_checkout":
                        checkout_data = tu["input"]
                    elif tu["name"] == "escalate_to_human":
                        escalation_data = tu["input"]
        else:
            ai_text = ai_response["content"]

        # Save AI response
        await append_message(conversation_id, "assistant", ai_text)

        # Send response via WhatsApp
        if checkout_data:
            # Create checkout and send link
            products = get_products_with_catalog(operator)
            product_name = checkout_data.get("product_id", "Tour")
            for p in products:
                if p["octo_product_id"] == checkout_data.get("product_id"):
                    product_name = p.get("display_name", product_name)
                    break

            # Get price from availability
            try:
                slots = get_availability_for_date(
                    operator, checkout_data["product_id"], checkout_data["option_id"],
                    checkout_data["unit_id"], checkout_data["quantity"],
                    checkout_data.get("start_time", "")[:10],
                    checkout_data.get("start_time", "")[:10],
                )
                matching = [s for s in slots if s["availability_id"] == checkout_data.get("availability_id")]
                if not matching and checkout_data.get("start_time"):
                    # Fallback: match by start time (AI may hallucinate availability_id)
                    req_time = checkout_data["start_time"][:16]
                    matching = [s for s in slots if s.get("start_time", "")[:16] == req_time]
                if matching:
                    price = matching[0]["price"]
                    result = create_checkout_session(
                        operator=operator,
                        product_name=product_name,
                        price_per_person=price,
                        quantity=checkout_data["quantity"],
                        customer_email=checkout_data.get("customer_email", ""),
                        metadata={
                            "product_id": checkout_data["product_id"],
                            "option_id": checkout_data["option_id"],
                            "availability_id": checkout_data.get("availability_id", ""),
                            "unit_id": checkout_data["unit_id"],
                            "quantity": str(checkout_data["quantity"]),
                            "customer_name": checkout_data.get("customer_name", customer_name),
                            "customer_email": checkout_data.get("customer_email", ""),
                            "customer_phone": phone,
                            "whatsapp_phone": phone,
                            "channel": "whatsapp",
                        },
                    )
                    # Send AI text first, then payment link
                    await send_text_message(phone, ai_text)
                    symbol = operator.currency_symbol
                    total = matching[0].get("total_price") or price * checkout_data["quantity"]
                    await send_link_message(
                        phone,
                        f"Complete your booking — {symbol}{total:.0f} total:",
                        result["checkout_url"],
                    )
                    await update_conversation_state(conversation_id, "checkout")
                    return JSONResponse({"status": "ok"})
            except Exception as e:
                print(f"[WHATSAPP] Checkout creation failed: {e}")

            # Fallback: send just the AI text
            await send_text_message(phone, ai_text)

        elif escalation_data:
            await send_text_message(phone, ai_text)
            contact_parts = []
            if operator.human_escalation.email:
                contact_parts.append(f"Email: {operator.human_escalation.email}")
            if operator.human_escalation.whatsapp:
                contact_parts.append(f"WhatsApp: {operator.human_escalation.whatsapp}")
            if contact_parts:
                await send_text_message(phone, "\n".join(contact_parts))
            await update_conversation_state(conversation_id, "human_escalation")

        else:
            # Regular AI response — send with optional quick-reply buttons
            if len(messages) <= 2:  # Early in conversation
                await send_interactive_buttons(
                    phone, ai_text,
                    [
                        {"id": "see_tours", "title": "See Available Tours"},
                        {"id": "talk_team", "title": "Talk to Team"},
                    ],
                )
            else:
                await send_text_message(phone, ai_text)

    except Exception as e:
        print(f"[WHATSAPP] Error processing message: {e}")
        await send_text_message(
            phone,
            f"I'm having trouble right now. Please contact us directly:\n"
            f"Email: {operator.human_escalation.email}" if operator.human_escalation.email else
            "I'm having trouble right now. Please try again in a moment.",
        )

    return JSONResponse({"status": "ok"})
