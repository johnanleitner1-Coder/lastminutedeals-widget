"""
Availability service — merges live OCTO data with static product catalog.

Product catalog provides descriptions, meeting points, inclusions (from Eduardo).
OCTO provides live availability, pricing, and capacity (from Bokun).

The merged result is what the AI system prompt reads from.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from app.config import OperatorConfig, load_product_catalog, BASE_DIR
from lib.octo_client import OCTOClient


# In-memory cache: {operator_id: {"products": [...], "catalog": [...], "fetched_at": float}}
_product_cache: dict[str, dict] = {}
_CACHE_TTL = 4 * 3600  # 4 hours for product list refresh
_AVAILABILITY_CACHE_TTL = 300  # 5 minutes for availability


def _get_client(operator: OperatorConfig) -> OCTOClient:
    return OCTOClient(
        base_url=operator.base_url,
        api_key=operator.api_key,
        pricing_capability=True,
    )


def get_products_with_catalog(operator: OperatorConfig) -> list[dict]:
    """
    Get OCTO products merged with the static product catalog.
    Cached for 4 hours. New OCTO products without catalog entries are flagged.
    """
    cache = _product_cache.get(operator.operator_id)
    if cache and (time.time() - cache["fetched_at"]) < _CACHE_TTL:
        return cache["merged"]

    with _get_client(operator) as client:
        octo_products = client.get_products(vendor_id=operator.bokun_vendor_id)

    catalog = load_product_catalog(operator)
    catalog_by_id = {p["octo_product_id"]: p for p in catalog if p.get("octo_product_id")}

    merged = []
    for prod in octo_products:
        pid = prod.get("id", "")
        cat_entry = catalog_by_id.get(pid)

        # Extract options and unit types from OCTO
        options = prod.get("options", [])
        default_option = options[0] if options else {}
        unit_types = default_option.get("units", [])

        entry = {
            "octo_product_id": pid,
            "octo_internal_name": prod.get("internalName", ""),
            "option_id": default_option.get("id", "DEFAULT"),
            "unit_types": [
                {"id": u.get("id", ""), "type": u.get("type", ""), "internal_name": u.get("internalName", "")}
                for u in unit_types
            ],
            "instant_confirmation": prod.get("instantConfirmation", False),
            "available": prod.get("availabilityType", ""),
            # Catalog content (from Eduardo)
            "display_name": "",
            "short_description": "",
            "duration": "",
            "meeting_point": "",
            "meeting_point_maps_url": "",
            "highlights": [],
            "inclusions": [],
            "exclusions": [],
            "what_to_bring": [],
            "languages": [],
            "max_group_size": None,
            "images": [],
            "cancellation_summary": "",
            "has_catalog_entry": False,
        }

        if cat_entry:
            entry.update({
                "display_name": cat_entry.get("display_name", entry["octo_internal_name"]),
                "short_description": cat_entry.get("short_description", ""),
                "duration": cat_entry.get("duration", ""),
                "meeting_point": cat_entry.get("meeting_point", ""),
                "meeting_point_maps_url": cat_entry.get("meeting_point_maps_url", ""),
                "highlights": cat_entry.get("highlights", []),
                "inclusions": cat_entry.get("inclusions", []),
                "exclusions": cat_entry.get("exclusions", []),
                "what_to_bring": cat_entry.get("what_to_bring", []),
                "languages": cat_entry.get("languages", []),
                "max_group_size": cat_entry.get("max_group_size"),
                "images": cat_entry.get("images", []),
                "cancellation_summary": cat_entry.get("cancellation_summary", ""),
                "has_catalog_entry": True,
            })
        else:
            # Use OCTO internal name as fallback display name
            entry["display_name"] = prod.get("internalName", pid)

        merged.append(entry)

    _product_cache[operator.operator_id] = {
        "merged": merged,
        "fetched_at": time.time(),
    }
    return merged


def get_availability_for_date(
    operator: OperatorConfig,
    product_id: str,
    option_id: str,
    unit_id: str,
    quantity: int,
    date_start: str,
    date_end: str,
) -> list[dict]:
    """
    Fetch live availability for a specific product and date range.
    Returns availability slots with pricing.
    """
    with _get_client(operator) as client:
        slots = client.get_availability(
            product_id=product_id,
            option_id=option_id,
            units=[{"id": unit_id, "quantity": quantity}],
            date_start=date_start,
            date_end=date_end,
            vendor_id=operator.bokun_vendor_id,
        )

    result = []
    for slot in slots:
        if slot.get("status") not in ("AVAILABLE", "FREESALE", "LIMITED"):
            continue

        # Extract pricing from unitPricing if available
        # Bokun returns: original, retail, net — we use retail (customer-facing price)
        pricing = slot.get("unitPricing", [])
        price_per_unit = None
        currency = operator.currency
        for up in pricing:
            if up.get("unitId") == unit_id or not price_per_unit:
                raw_price = up.get("retail") or up.get("original") or up.get("price") or 0
                precision = up.get("currencyPrecision", 2)
                price_per_unit = raw_price / (10 ** precision)
                currency = up.get("currency", currency)
                if up.get("unitId") == unit_id:
                    break

        # Fallback: try top-level pricing (total for all units)
        if price_per_unit is None and slot.get("pricing"):
            raw = slot["pricing"].get("retail") or slot["pricing"].get("original") or 0
            precision = slot["pricing"].get("currencyPrecision", 2)
            qty = max(1, quantity)
            price_per_unit = (raw / (10 ** precision)) / qty

        result.append({
            "availability_id": slot.get("id", ""),
            "start_time": slot.get("localDateTimeStart", slot.get("localDate", "")),
            "end_time": slot.get("localDateTimeEnd", ""),
            "status": slot.get("status", ""),
            "vacancies": slot.get("vacancies"),
            "price_per_unit": price_per_unit,
            "currency": currency,
        })

    return result


def search_all_availability(
    operator: OperatorConfig,
    date_start: str,
    date_end: str,
    quantity: int = 1,
    time_of_day: str = "",
) -> list[dict]:
    """
    Check availability across ALL operator tours for a date range, in parallel.
    Returns a list of tours with their available slots.
    time_of_day: optional filter — "morning", "afternoon", or "evening".
    """
    products = get_products_with_catalog(operator)

    def _check_one(product: dict) -> dict:
        if not product.get("unit_types"):
            return {"tour": product["display_name"], "slots": []}
        unit_id = product["unit_types"][0]["id"]
        try:
            slots = get_availability_for_date(
                operator=operator,
                product_id=product["octo_product_id"],
                option_id=product["option_id"],
                unit_id=unit_id,
                quantity=quantity,
                date_start=date_start,
                date_end=date_end,
            )
        except Exception as e:
            print(f"[AVAIL] Error checking {product['display_name']} ({product['octo_product_id']}): {e}")
            slots = []

        # Filter by time of day if requested
        if time_of_day and slots:
            filtered = []
            for s in slots:
                start = s.get("start_time", "")
                hour = -1
                if "T" in start:
                    try:
                        hour = int(start.split("T")[1][:2])
                    except (ValueError, IndexError):
                        pass
                if time_of_day == "morning" and 0 <= hour < 12:
                    filtered.append(s)
                elif time_of_day == "afternoon" and 12 <= hour < 17:
                    filtered.append(s)
                elif time_of_day == "evening" and 17 <= hour <= 23:
                    filtered.append(s)
                elif not time_of_day:
                    filtered.append(s)
            slots = filtered

        return {
            "tour": product["display_name"],
            "product_id": product["octo_product_id"],
            "option_id": product["option_id"],
            "unit_id": unit_id,
            "slots": slots[:5],  # cap at 5 slots per tour
        }

    # Check all products in parallel (max 6 threads to avoid hammering the API)
    results = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_check_one, p): p for p in products}
        for future in as_completed(futures):
            results.append(future.result())

    # Sort: tours with availability first
    results.sort(key=lambda r: (0 if r["slots"] else 1, r["tour"]))
    return results


def build_ai_product_context(operator: OperatorConfig, availability_by_product: dict | None = None) -> str:
    """
    Build the structured product context block for the AI system prompt.
    Merges static catalog content with live availability data.
    """
    products = get_products_with_catalog(operator)
    lines = []

    for prod in products:
        if not prod["has_catalog_entry"]:
            continue  # Don't show products without catalog content

        lines.append("---")
        lines.append(f"TOUR: {prod['display_name']}")
        if prod["short_description"]:
            lines.append(f"DESCRIPTION: {prod['short_description']}")
        if prod["duration"]:
            lines.append(f"DURATION: {prod['duration']}")
        if prod["meeting_point"]:
            mp = prod["meeting_point"]
            if prod["meeting_point_maps_url"]:
                mp += f" ({prod['meeting_point_maps_url']})"
            lines.append(f"MEETING POINT: {mp}")
        if prod["inclusions"]:
            lines.append(f"INCLUDES: {', '.join(prod['inclusions'])}")
        if prod["exclusions"]:
            lines.append(f"EXCLUDES: {', '.join(prod['exclusions'])}")
        if prod["what_to_bring"]:
            lines.append(f"WHAT TO BRING: {', '.join(prod['what_to_bring'])}")
        if prod["languages"]:
            lines.append(f"LANGUAGES: {', '.join(prod['languages'])}")
        if prod["max_group_size"]:
            lines.append(f"MAX GROUP: {prod['max_group_size']} people")
        if prod["cancellation_summary"]:
            lines.append(f"CANCELLATION: {prod['cancellation_summary']}")

        # Live availability
        if availability_by_product and prod["octo_product_id"] in availability_by_product:
            avail_slots = availability_by_product[prod["octo_product_id"]]
            if avail_slots:
                lines.append("AVAILABLE SLOTS (live):")
                for s in avail_slots[:10]:  # cap at 10 slots
                    start = s.get("start_time", "")
                    price = s.get("price_per_unit")
                    currency = s.get("currency", operator.currency)
                    vacancies = s.get("vacancies")
                    line = f"  - {start}"
                    if price is not None:
                        line += f" — {price:.0f} {currency}/person"
                    if vacancies is not None:
                        line += f" ({vacancies} spots left)"
                    lines.append(line)
            else:
                lines.append("AVAILABLE SLOTS: None currently available")

        lines.append(f"PRODUCT_ID: {prod['octo_product_id']}")
        lines.append(f"OPTION_ID: {prod['option_id']}")
        if prod["unit_types"]:
            lines.append(f"UNIT_ID: {prod['unit_types'][0]['id']}")
        lines.append("")

    return "\n".join(lines)
