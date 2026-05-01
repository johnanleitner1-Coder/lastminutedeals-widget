"""
Payment service — Stripe Connect checkout session creation and capture.

Uses Stripe Connect direct charges: all API calls use LMDH's platform key
with stripe_account pointing to the operator's connected account.
Money flows directly to the operator. Application fees added later.
Uses manual capture: authorization hold first, capture only after OCTO booking confirmed.
"""

import stripe

from app.config import WIDGET_BASE_URL, STRIPE_PLATFORM_SECRET_KEY, OperatorConfig


def create_checkout_session(
    operator: OperatorConfig,
    product_name: str,
    price_per_person: float,
    quantity: int,
    customer_email: str,
    metadata: dict,
) -> dict:
    """
    Create a Stripe Checkout Session as a direct charge on the operator's
    connected Stripe account via Stripe Connect.

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
        api_key=STRIPE_PLATFORM_SECRET_KEY,
        stripe_account=operator.stripe_connect_account_id,
    )

    return {
        "checkout_url": session.url,
        "session_id": session.id,
    }


def capture_payment(operator: OperatorConfig, payment_intent_id: str) -> bool:
    """Capture a held payment on the operator's connected Stripe account."""
    try:
        stripe.PaymentIntent.capture(
            payment_intent_id,
            api_key=STRIPE_PLATFORM_SECRET_KEY,
            stripe_account=operator.stripe_connect_account_id,
        )
        return True
    except Exception as e:
        print(f"[PAYMENT] Capture failed: {e}")
        return False


def cancel_payment(operator: OperatorConfig, payment_intent_id: str) -> bool:
    """Cancel a payment hold on the operator's connected Stripe account (full refund)."""
    try:
        stripe.PaymentIntent.cancel(
            payment_intent_id,
            api_key=STRIPE_PLATFORM_SECRET_KEY,
            stripe_account=operator.stripe_connect_account_id,
        )
        return True
    except Exception as e:
        print(f"[PAYMENT] Cancel failed: {e}")
        return False
