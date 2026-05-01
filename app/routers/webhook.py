"""
Stripe webhook handler — payment → OCTO fulfillment.

Each operator's Stripe connected account sends webhooks to /api/stripe-webhook/{operator_id}.
The operator_id in the URL determines which webhook signing secret to use.
Returns 200 immediately after spawning a daemon thread for booking execution.
Idempotency enforced via in-memory lock.

Stripe Connect: webhooks from connected accounts include a Stripe-Account header.
We use the platform secret key for all API calls, with stripe_account for the operator.
"""

import threading
import time

import stripe
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import get_operator, STRIPE_PLATFORM_SECRET_KEY
from app.services.booking import execute_booking_async
from app.services.analytics_store import record_event

router = APIRouter()

# In-memory idempotency: prevent concurrent duplicate processing
# Stores {session_id: timestamp} with TTL-based cleanup
_webhook_lock = threading.Lock()
_webhook_in_flight: dict[str, float] = {}
_WEBHOOK_TTL = 600  # 10 minutes


@router.post("/api/stripe-webhook/{operator_id}")
async def stripe_webhook(operator_id: str, request: Request):
    operator = get_operator(operator_id)
    if not operator:
        return JSONResponse({"error": "Unknown operator"}, status_code=404)

    if not operator.stripe_webhook_secret:
        return JSONResponse({"error": "Webhook not configured for operator"}, status_code=500)

    payload = await request.body()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, operator.stripe_webhook_secret
        )
    except Exception as e:
        return JSONResponse({"error": "Invalid signature"}, status_code=400)

    event_type = event.get("type", "")

    if event_type == "checkout.session.completed":
        session = event["data"]["object"]
        session_id = session.get("id", "")
        metadata = session.get("metadata", {})

        # Idempotency check with TTL cleanup
        with _webhook_lock:
            now = time.time()
            stale = [k for k, t in _webhook_in_flight.items() if now - t > _WEBHOOK_TTL]
            for k in stale:
                del _webhook_in_flight[k]

            if session_id in _webhook_in_flight:
                return JSONResponse({"status": "ok", "note": "already_processing"})
            _webhook_in_flight[session_id] = now

        payment_intent = session.get("payment_intent", "")
        amount_total = session.get("amount_total", 0)
        session_token = metadata.get("session_token", "")
        whatsapp_phone = metadata.get("whatsapp_phone", "")

        # Look up conversation: by session_token (web) or phone number (WhatsApp)
        from app.services.conversation import get_conversation_status, get_conversation_by_phone
        conv_status = None
        if session_token:
            conv_status = await get_conversation_status(session_token)
        elif whatsapp_phone:
            conv_status = await get_conversation_by_phone(operator_id, whatsapp_phone)

        conversation_id = conv_status["id"] if conv_status else None

        # Spawn async fulfillment
        execute_booking_async(
            operator=operator,
            conversation_id=conversation_id or "",
            payment_intent_id=payment_intent,
            product_id=metadata.get("product_id", ""),
            option_id=metadata.get("option_id", ""),
            availability_id=metadata.get("availability_id", ""),
            unit_id=metadata.get("unit_id", ""),
            quantity=int(metadata.get("quantity", 1)),
            customer_name=metadata.get("customer_name", ""),
            customer_email=metadata.get("customer_email", session.get("customer_email", "")),
            customer_phone=metadata.get("customer_phone", ""),
            start_time=metadata.get("start_time", ""),
            amount_cents=amount_total,
        )

        await record_event(
            operator_id, conversation_id, "payment_completed",
            metadata={"session_id": session_id, "amount_cents": amount_total},
        )

    elif event_type == "checkout.session.expired":
        metadata = event["data"]["object"].get("metadata", {})
        await record_event(operator_id, None, "checkout_expired")

    return JSONResponse({"status": "ok"})
