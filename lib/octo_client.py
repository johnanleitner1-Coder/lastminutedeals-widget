"""
OCTO API client — thin wrapper for product listing + availability queries.

Extracted from fetch_octo_slots.py (LMDH pipeline). Standalone, no imports
from the parent project.
"""

import httpx


class OCTOClient:
    """Thin wrapper around the OCTO REST API for a single supplier."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: int = 30,
        pricing_capability: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.pricing_capability = pricing_capability
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=timeout,
        )

    def close(self):
        self._client.close()

    def get_products(self, vendor_id: int | None = None) -> list[dict]:
        """
        Fetch products — no pricing capability header (avoids Bokun timeout).

        When vendor_id is provided, uses a vendor-scoped token
        (Authorization: Bearer KEY/VENDOR_ID) which returns only that
        vendor's products with no 100-product cap.
        """
        headers = {}
        if vendor_id is not None:
            headers["Authorization"] = f"Bearer {self.api_key}/{vendor_id}"
        resp = self._client.get(f"{self.base_url}/products", headers=headers)
        resp.raise_for_status()
        return resp.json()

    def get_availability(
        self,
        product_id: str,
        option_id: str,
        units: list[dict],
        date_start: str,
        date_end: str,
        vendor_id: int | None = None,
    ) -> list[dict]:
        """
        POST /availability — get available time slots for a product in a date range.

        Args:
            product_id:  OCTO product identifier
            option_id:   OCTO option identifier (usually "DEFAULT")
            units:       list of {id: unit_type_id, quantity: int}
            date_start:  local date string "YYYY-MM-DD"
            date_end:    local date string "YYYY-MM-DD"
            vendor_id:   optional vendor ID for vendor-scoped auth
        """
        normalized_units = [
            {"id": u.get("id") or u.get("unitId", ""), "quantity": u.get("quantity", 1)}
            for u in units
        ]
        payload = {
            "productId": product_id,
            "optionId": option_id,
            "localDateStart": date_start,
            "localDateEnd": date_end,
            "units": normalized_units,
        }
        extra_headers = {}
        if self.pricing_capability:
            extra_headers["Octo-Capabilities"] = "octo/pricing"
        if vendor_id is not None:
            extra_headers["Authorization"] = f"Bearer {self.api_key}/{vendor_id}"
        resp = self._client.post(
            f"{self.base_url}/availability",
            json=payload,
            headers=extra_headers,
        )
        resp.raise_for_status()
        return resp.json()
