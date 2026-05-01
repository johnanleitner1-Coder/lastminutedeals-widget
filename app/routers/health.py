"""Health check and debug endpoints."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.config import get_operator
from app.services.availability import get_products_with_catalog, search_all_availability, _get_client

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok", "service": "widget"}


@router.get("/debug/availability/{operator_id}")
async def debug_availability(operator_id: str, date_start: str = "", date_end: str = "", quantity: int = 1):
    """Debug endpoint — check raw availability for all products."""
    operator = get_operator(operator_id)
    if not operator:
        return JSONResponse({"error": "Unknown operator"}, status_code=404)
    if not date_start or not date_end:
        return JSONResponse({"error": "Provide ?date_start=YYYY-MM-DD&date_end=YYYY-MM-DD"}, status_code=400)

    products = get_products_with_catalog(operator)
    product_summary = [
        {
            "id": p["octo_product_id"],
            "name": p["display_name"],
            "option_id": p["option_id"],
            "unit_types": p["unit_types"][:2],
            "has_catalog": p["has_catalog_entry"],
        }
        for p in products
    ]

    results = search_all_availability(operator, date_start, date_end, quantity=quantity)

    return JSONResponse({
        "products_loaded": len(products),
        "product_details": product_summary,
        "availability_results": results,
        "date_range": f"{date_start} to {date_end}",
    })


@router.get("/debug/raw-products/{operator_id}")
async def debug_raw_products(operator_id: str):
    """Debug endpoint — raw OCTO product data (currency fields)."""
    operator = get_operator(operator_id)
    if not operator:
        return JSONResponse({"error": "Unknown operator"}, status_code=404)

    with _get_client(operator) as client:
        products = client.get_products(vendor_id=operator.bokun_vendor_id)

    summary = []
    for p in products:
        summary.append({
            "id": p.get("id"),
            "internalName": p.get("internalName"),
            "defaultCurrency": p.get("defaultCurrency"),
            "availableCurrencies": p.get("availableCurrencies"),
            "currency": p.get("currency"),
            "locale": p.get("locale"),
            "all_keys": sorted(p.keys()),
        })
    return JSONResponse({"products": summary})
