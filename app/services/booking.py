"""
Booking service — OCTO reservation + confirmation, integrated with Stripe.

Handles the full lifecycle:
  1. Create OCTO reservation (hold)
  2. Confirm OCTO booking
  3. Capture Stripe payment
  4. Auto-refund on failure
"""

import json
import threading
import time
from datetime import datetime, timezone

from app.config import OperatorConfig
from app.services.payment import capture_payment, cancel_payment
from app.services.conversation import update_conversation_state
from app.services.analytics_store import record_event
from lib.octo_booker import OCTOBooker, BookingResult, BookingError


def execute_booking_async(
    operator: OperatorConfig,
    conversation_id: str,
    payment_intent_id: str,
    product_id: str,
    option_id: str,
    availability_id: str,
    unit_id: str,
    quantity: int,
    customer_name: str,
    customer_email: str,
    customer_phone: str,
    start_time: str = "",
    amount_cents: int = 0,
) -> None:
    """
    Spawn a daemon thread to execute the booking.
    Returns immediately — the widget polls for status.
    """
    threading.Thread(
        target=_fulfill_booking,
        args=(
            operator, conversation_id, payment_intent_id,
            product_id, option_id, availability_id, unit_id,
            quantity, customer_name, customer_email, customer_phone,
            start_time, amount_cents,
        ),
        daemon=True,
        name=f"fulfill-{conversation_id[:12]}",
    ).start()


def _fulfill_booking(
    operator: OperatorConfig,
    conversation_id: str,
    payment_intent_id: str,
    product_id: str,
    option_id: str,
    availability_id: str,
    unit_id: str,
    quantity: int,
    customer_name: str,
    customer_email: str,
    customer_phone: str,
    start_time: str,
    amount_cents: int,
) -> None:
    """Execute OCTO booking, capture payment, update conversation state."""
    import asyncio

    async def _async_fulfill():
        booker = OCTOBooker(
            base_url=operator.base_url,
            api_key=operator.api_key,
        )

        try:
            result = booker.book(
                product_id=product_id,
                option_id=option_id,
                availability_id=availability_id,
                unit_id=unit_id,
                quantity=quantity,
                customer_name=customer_name,
                customer_email=customer_email,
                customer_phone=customer_phone,
                customer_country=operator.country,
                start_time=start_time,
            )

            # OCTO booking succeeded — capture the payment
            if payment_intent_id:
                captured = capture_payment(payment_intent_id)
                if not captured:
                    print(f"[BOOKING] Capture failed — would cancel OCTO booking {result.confirmation}")
                    cancel_payment(payment_intent_id)
                    await update_conversation_state(
                        conversation_id, "checkout",
                        context={"error": "Payment capture failed. You have been refunded."},
                    )
                    return

            # Success — update conversation
            await update_conversation_state(
                conversation_id, "confirmed",
                converted=True,
                booking_id=result.confirmation,
                revenue_cents=amount_cents,
            )
            await record_event(
                operator.operator_id, conversation_id, "booking_confirmed",
                metadata={
                    "confirmation": result.confirmation,
                    "supplier_reference": result.supplier_reference,
                    "revenue_cents": amount_cents,
                    "product_id": product_id,
                },
            )
            print(f"[BOOKING] Confirmed: {result.confirmation} (ref: {result.supplier_reference})")

        except BookingError as e:
            print(f"[BOOKING] Failed: {e}")
            if payment_intent_id:
                cancel_payment(payment_intent_id)
            await update_conversation_state(
                conversation_id, "checkout",
                context={"error": f"Booking could not be completed: {str(e)[:200]}. You have been fully refunded."},
            )
            await record_event(
                operator.operator_id, conversation_id, "booking_failed",
                metadata={"error": str(e)[:500]},
            )

        except Exception as e:
            print(f"[BOOKING] Unexpected error: {e}")
            if payment_intent_id:
                cancel_payment(payment_intent_id)
            await update_conversation_state(
                conversation_id, "checkout",
                context={"error": "An unexpected error occurred. You have been fully refunded."},
            )

    asyncio.run(_async_fulfill())
