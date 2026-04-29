"""
Payment service — Stripe checkout session creation and capture.

All Stripe calls use the OPERATOR'S Stripe key, not LMDH's.
LMDH is a software vendor — booking revenue flows directly to the operator.
Uses manual capture: authorization hold first, capture only after OCTO booking confirmed.
"""

import stripe

from app.config import WIDGET_BASE_URL, OperatorConfig


def create_checkout_session(
    operator: OperatorConfig,
    product_name: str,
    price_per_person: float,
    quantity: int,
    customer_email: str,
    metadata: dict,
) -> dict:
    """
    Create a Stripe Checkout Session on the operator's Stripe account.

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
        api_key=operator.stripe_secret_key,
    )

    return {
        "checkout_url": session.url,
        "session_id": session.id,
    }


def capture_payment(operator: OperatorConfig, payment_intent_id: str) -> bool:
    """Capture a held payment on the operator's Stripe account."""
    try:
        stripe.PaymentIntent.capture(
            payment_intent_id,
            api_key=operator.stripe_secret_key,
        )
        return True
    except Exception as e:
        print(f"[PAYMENT] Capture failed: {e}")
        return False


def cancel_payment(operator: OperatorConfig, payment_intent_id: str) -> bool:
    """Cancel a payment hold on the operator's Stripe account (full refund)."""
    try:
        stripe.PaymentIntent.cancel(
            payment_intent_id,
            api_key=operator.stripe_secret_key,
        )
        return True
    except Exception as e:
        print(f"[PAYMENT] Cancel failed: {e}")
        return False
