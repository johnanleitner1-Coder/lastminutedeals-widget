"""
Stripe webhook handler — payment → OCTO fulfillment.

Returns 200 immediately after spawning a daemon thread for booking execution.
Idempotency enforced via in-memory lock + Supabase record.
"""

import threading
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, get_operator
from app.services.booking import execute_booking_async
from app.services.analytics_store import record_event

router = APIRouter()

stripe.api_key = STRIPE_SECRET_KEY

# In-memory idempotency: prevent concurrent duplicate processing
_webhook_lock = threading.Lock()
_webhook_in_flight: dict[str, bool] = {}


@router.post("/api/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        return JSONResponse({"error": "Invalid signature"}, status_code=400)

    event_type = event.get("type", "")

    if event_type == "checkout.session.completed":
        session = event["data"]["object"]
        session_id = session.get("id", "")
        metadata = session.get("metadata", {})

        # Idempotency check
        with _webhook_lock:
            if _webhook_in_flight.get(session_id):
                return JSONResponse({"status": "ok", "note": "already_processing"})
            _webhook_in_flight[session_id] = True

        operator_id = metadata.get("operator_id", "")
        operator = get_operator(operator_id)
        if not operator:
            with _webhook_lock:
                _webhook_in_flight.pop(session_id, None)
            return JSONResponse({"error": "Unknown operator"}, status_code=400)

        payment_intent = session.get("payment_intent", "")
        amount_total = session.get("amount_total", 0)
        session_token = metadata.get("session_token", "")

        # Look up conversation by session token
        from app.services.conversation import get_conversation_status
        conv_status = None
        if session_token:
            import asyncio
            conv_status = await get_conversation_status(session_token)

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
        operator_id = metadata.get("operator_id", "")
        await record_event(operator_id, None, "checkout_expired")

    return JSONResponse({"status": "ok"})
