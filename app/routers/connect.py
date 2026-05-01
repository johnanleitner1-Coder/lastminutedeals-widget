"""
Stripe Connect onboarding endpoints.

Allows operators to onboard as Stripe connected accounts under the LMDH platform.
Protected by the same HMAC token used for the analytics dashboard.
"""

import hashlib
import hmac as hmac_mod

import stripe
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.config import (
    DASHBOARD_HMAC_SECRET,
    STRIPE_PLATFORM_SECRET_KEY,
    WIDGET_BASE_URL,
    get_operator,
)

router = APIRouter(prefix="/api/connect", tags=["connect"])


def _verify_token(operator_id: str, token: str) -> bool:
    """Same HMAC verification as the analytics dashboard."""
    expected = hmac_mod.new(
        DASHBOARD_HMAC_SECRET.encode(),
        operator_id.encode(),
        hashlib.sha256,
    ).hexdigest()[:32]
    return hmac_mod.compare_digest(token, expected)


@router.post("/onboard/{operator_id}")
async def onboard_operator(operator_id: str, token: str = Query(...)):
    """
    Create a Stripe connected account for the operator and return an onboarding link.

    Uses Stripe Connect with:
    - Full dashboard access for the operator
    - Operator pays their own Stripe fees
    - Stripe handles KYC requirement collection
    """
    if not _verify_token(operator_id, token):
        return JSONResponse({"error": "Invalid token"}, status_code=403)

    operator = get_operator(operator_id)
    if not operator:
        return JSONResponse({"error": "Unknown operator"}, status_code=404)

    if not STRIPE_PLATFORM_SECRET_KEY:
        return JSONResponse({"error": "Platform Stripe key not configured"}, status_code=500)

    # If operator already has a connected account, generate a new onboarding link
    if operator.stripe_connect_account_id:
        try:
            account_link = stripe.AccountLink.create(
                account=operator.stripe_connect_account_id,
                refresh_url=f"{WIDGET_BASE_URL}/api/connect/onboard/{operator_id}?token={token}",
                return_url=f"{WIDGET_BASE_URL}/api/connect/complete?operator_id={operator_id}&token={token}",
                type="account_onboarding",
                api_key=STRIPE_PLATFORM_SECRET_KEY,
            )
            return JSONResponse({
                "onboarding_url": account_link.url,
                "connected_account_id": operator.stripe_connect_account_id,
                "note": "Existing account — resuming onboarding",
            })
        except Exception as e:
            return JSONResponse({"error": f"Failed to create onboarding link: {e}"}, status_code=500)

    # Create a new connected account
    try:
        account = stripe.Account.create(
            controller={
                "stripe_dashboard": {"type": "full"},
                "fees": {"payer": "account"},
                "requirement_collection": "stripe",
            },
            country=operator.country,
            email=operator.human_escalation.email or None,
            business_type="company",
            metadata={
                "operator_id": operator.operator_id,
                "platform": "lastminutedealshq",
            },
            api_key=STRIPE_PLATFORM_SECRET_KEY,
        )
    except Exception as e:
        return JSONResponse({"error": f"Failed to create connected account: {e}"}, status_code=500)

    connected_account_id = account.id
    print(f"[CONNECT] Created connected account {connected_account_id} for operator {operator_id}")
    print(f"[CONNECT] ACTION REQUIRED: Set stripe_connect_account_id=\"{connected_account_id}\" in config.py for {operator_id}")

    # Generate onboarding link
    try:
        account_link = stripe.AccountLink.create(
            account=connected_account_id,
            refresh_url=f"{WIDGET_BASE_URL}/api/connect/onboard/{operator_id}?token={token}",
            return_url=f"{WIDGET_BASE_URL}/api/connect/complete?operator_id={operator_id}&token={token}",
            type="account_onboarding",
            api_key=STRIPE_PLATFORM_SECRET_KEY,
        )
    except Exception as e:
        return JSONResponse({"error": f"Account created ({connected_account_id}) but link failed: {e}"}, status_code=500)

    return JSONResponse({
        "onboarding_url": account_link.url,
        "connected_account_id": connected_account_id,
        "note": "Save this connected_account_id in operator config",
    })


@router.get("/complete")
async def onboarding_complete(operator_id: str = Query(...), token: str = Query(...)):
    """
    Return URL after operator completes Stripe Connect onboarding.
    Checks whether the account is fully enabled for charges and payouts.
    """
    if not _verify_token(operator_id, token):
        return JSONResponse({"error": "Invalid token"}, status_code=403)

    operator = get_operator(operator_id)
    if not operator:
        return JSONResponse({"error": "Unknown operator"}, status_code=404)

    if not operator.stripe_connect_account_id:
        return JSONResponse({"error": "No connected account ID configured for this operator"}, status_code=400)

    try:
        account = stripe.Account.retrieve(
            operator.stripe_connect_account_id,
            api_key=STRIPE_PLATFORM_SECRET_KEY,
        )
    except Exception as e:
        return JSONResponse({"error": f"Failed to retrieve account: {e}"}, status_code=500)

    charges_enabled = account.get("charges_enabled", False)
    payouts_enabled = account.get("payouts_enabled", False)
    details_submitted = account.get("details_submitted", False)

    status = "active" if (charges_enabled and payouts_enabled) else "pending"

    return JSONResponse({
        "operator_id": operator_id,
        "connected_account_id": operator.stripe_connect_account_id,
        "status": status,
        "charges_enabled": charges_enabled,
        "payouts_enabled": payouts_enabled,
        "details_submitted": details_submitted,
    })


@router.get("/status/{operator_id}")
async def connect_status(operator_id: str, token: str = Query(...)):
    """
    Check if the operator's connected Stripe account is fully onboarded.
    """
    if not _verify_token(operator_id, token):
        return JSONResponse({"error": "Invalid token"}, status_code=403)

    operator = get_operator(operator_id)
    if not operator:
        return JSONResponse({"error": "Unknown operator"}, status_code=404)

    if not operator.stripe_connect_account_id:
        return JSONResponse({
            "operator_id": operator_id,
            "status": "not_connected",
            "connected_account_id": None,
            "charges_enabled": False,
            "payouts_enabled": False,
        })

    try:
        account = stripe.Account.retrieve(
            operator.stripe_connect_account_id,
            api_key=STRIPE_PLATFORM_SECRET_KEY,
        )
    except Exception as e:
        return JSONResponse({"error": f"Failed to retrieve account: {e}"}, status_code=500)

    charges_enabled = account.get("charges_enabled", False)
    payouts_enabled = account.get("payouts_enabled", False)
    details_submitted = account.get("details_submitted", False)

    if charges_enabled and payouts_enabled:
        status = "active"
    elif details_submitted:
        status = "pending_verification"
    else:
        status = "onboarding_incomplete"

    return JSONResponse({
        "operator_id": operator_id,
        "connected_account_id": operator.stripe_connect_account_id,
        "status": status,
        "charges_enabled": charges_enabled,
        "payouts_enabled": payouts_enabled,
        "details_submitted": details_submitted,
    })
