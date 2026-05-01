"""
Checkout endpoint — creates Stripe checkout session from widget.

Called when the AI's start_checkout tool fires. The widget frontend
receives the checkout_url and opens it in a new tab / in-app browser.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.config import get_operator
from app.models.schemas import CheckoutRequest
from app.services.payment import create_checkout_session
from app.services.availability import get_availability_for_date, get_products_with_catalog
from app.services.conversation import update_conversation_state
from app.services.analytics_store import record_event

router = APIRouter()


@router.post("/api/checkout")
async def create_checkout(req: CheckoutRequest):
    operator = get_operator(req.operator_id)
    if not operator:
        return JSONResponse({"error": "Unknown operator"}, status_code=404)

    # Fresh availability check — verify the slot is still available
    try:
        slots = get_availability_for_date(
            operator=operator,
            product_id=req.product_id,
            option_id=req.option_id,
            unit_id=req.unit_id,
            quantity=req.quantity,
            date_start=req.start_time[:10] if req.start_time else "",
            date_end=req.start_time[:10] if req.start_time else "",
        )
        # Find matching slot — match on start_time (availability_id can change between queries)
        matching = [s for s in slots if s["availability_id"] == req.availability_id]
        if not matching and req.start_time:
            # Fallback: match by start time (AI may hallucinate availability_id)
            req_time = req.start_time[:16]  # Compare up to minutes
            matching = [s for s in slots if s.get("start_time", "")[:16] == req_time]
        if not matching:
            return JSONResponse(
                {"error": "This slot is no longer available. Please check other times."},
                status_code=409,
            )
        slot = matching[0]
        # Use the real availability_id from the fresh OCTO response
        req.availability_id = slot["availability_id"]
    except Exception as e:
        return JSONResponse(
            {"error": f"Could not verify availability: {str(e)[:200]}"},
            status_code=503,
        )

    # Get product name from catalog
    products = get_products_with_catalog(operator)
    product_name = req.product_id
    for p in products:
        if p["octo_product_id"] == req.product_id:
            product_name = p.get("display_name", p.get("octo_internal_name", req.product_id))
            break

    price = slot.get("price_per_unit")
    if not price or price <= 0:
        return JSONResponse({"error": "Price not available for this slot."}, status_code=400)

    # Create Stripe checkout session
    try:
        result = create_checkout_session(
            operator=operator,
            product_name=product_name,
            price_per_person=price,
            quantity=req.quantity,
            customer_email=req.customer_email,
            metadata={
                "product_id": req.product_id,
                "option_id": req.option_id,
                "availability_id": req.availability_id,
                "unit_id": req.unit_id,
                "quantity": str(req.quantity),
                "customer_name": req.customer_name,
                "customer_email": req.customer_email,
                "customer_phone": req.customer_phone,
                "session_token": req.session_token,
                "start_time": req.start_time or "",
            },
        )
    except Exception as e:
        return JSONResponse(
            {"error": "Payment system error. Please try again."},
            status_code=500,
        )

    # Record analytics event
    await record_event(
        operator.operator_id, None, "checkout_created",
        metadata={
            "product_id": req.product_id,
            "quantity": req.quantity,
            "amount": price * req.quantity,
            "currency": operator.currency,
        },
    )

    slot_currency = slot.get("currency", operator.currency)
    currency_symbol = "$" if slot_currency == "USD" else "€" if slot_currency == "EUR" else slot_currency + " "
    return JSONResponse({
        "checkout_url": result["checkout_url"],
        "session_id": result["session_id"],
        "product_name": product_name,
        "price_per_person": price,
        "total_price": price * req.quantity,
        "currency": slot_currency,
        "currency_symbol": currency_symbol,
        "quantity": req.quantity,
    })
