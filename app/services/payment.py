"""
Payment service — Stripe checkout session creation and capture.

All checkout sessions are created in the operator's currency (EUR for Eduardo).
Uses manual capture: authorization hold first, capture only after OCTO booking confirmed.
"""

import os

import stripe

from app.config import STRIPE_SECRET_KEY, WIDGET_BASE_URL, OperatorConfig


stripe.api_key = STRIPE_SECRET_KEY


def create_checkout_session(
    operator: OperatorConfig,
    product_name: str,
    price_per_person: float,
    quantity: int,
    customer_email: str,
    metadata: dict,
) -> dict:
    """
    Create a Stripe Checkout Session with manual capture.

    Returns dict with checkout_url and session_id.
    """
    price_cents = int(price_per_person * 100)
    currency = operator.currency.lower()

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": currency,
                "product_data": {
                    "name": product_name[:80],
                    "description": f"{operator.display_name} — {operator.city}",
                },
                "unit_amount": price_cents,
            },
            "quantity": quantity,
        }],
        mode="payment",
        payment_intent_data={"capture_method": "manual"},
        customer_email=customer_email,
        success_url=f"{WIDGET_BASE_URL}/booking/confirmed?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{WIDGET_BASE_URL}/booking/cancelled",
        metadata={
            **metadata,
            "operator_id": operator.operator_id,
            "source": "widget",
        },
    )

    return {
        "checkout_url": session.url,
        "session_id": session.id,
    }


def capture_payment(payment_intent_id: str) -> bool:
    """Capture a held payment after successful OCTO booking."""
    try:
        stripe.PaymentIntent.capture(payment_intent_id)
        return True
    except Exception as e:
        print(f"[PAYMENT] Capture failed: {e}")
        return False


def cancel_payment(payment_intent_id: str) -> bool:
    """Cancel a payment hold (full refund — card never charged)."""
    try:
        stripe.PaymentIntent.cancel(payment_intent_id)
        return True
    except Exception as e:
        print(f"[PAYMENT] Cancel failed: {e}")
        return False
