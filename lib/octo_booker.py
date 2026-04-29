"""
OCTO Booker — reservation + confirmation flow via OCTO REST API.

Extracted from complete_booking.py (LMDH pipeline). Standalone, no imports
from the parent project. Uses httpx instead of requests.

Two-step flow:
  1. POST /reservations — creates a hold (reservation UUID)
  2. POST /bookings/{uuid}/confirm — confirms the hold

Handles:
  - Fallback to POST /bookings if /reservations returns 400/404/405
  - 409 re-resolution (stale availability_id → fetch fresh + retry)
  - Orphaned hold cleanup via DELETE /bookings/{uuid}
  - Single retry with jitter on transient 5xx errors
"""

import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import httpx


class BookingError(Exception):
    """Base class for booking errors."""


class BookingUnavailableError(BookingError):
    """Slot is no longer available (409/422)."""


class BookingTimeoutError(BookingError):
    """Network timeout or repeated failures."""


@dataclass
class BookingResult:
    confirmation: str
    supplier_reference: str
    meta: dict = field(default_factory=dict)


_RETRYABLE_5XX = frozenset({429, 500, 502, 503, 504})


def _retry_delay() -> float:
    return 1.0 + random.uniform(0, 0.5)


class OCTOBooker:
    """Execute OCTO reservation + confirmation via HTTP."""

    def __init__(self, base_url: str, api_key: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def book(
        self,
        product_id: str,
        option_id: str,
        availability_id: str,
        unit_id: str,
        quantity: int,
        customer_name: str,
        customer_email: str,
        customer_phone: str = "",
        customer_country: str = "PT",
        start_time: str | None = None,
    ) -> BookingResult:
        """
        Execute full reservation → confirm flow.

        Returns BookingResult with confirmation UUID, supplier reference,
        and timing metadata.
        """
        meta: dict = {"attempts": 0, "retries": 0, "retry_stage": None}

        contact = {
            "fullName": customer_name,
            "emailAddress": customer_email,
            "phoneNumber": customer_phone,
            "country": customer_country,
            "locales": ["en"],
        }

        reseller_ref = f"WDG-{int(time.time())}"
        qty = max(1, quantity)
        unit_items = [{"unitId": unit_id} for _ in range(qty)]

        def _post(url: str, stage: str, **kwargs) -> httpx.Response:
            resp = None
            for attempt in range(1, 3):
                meta["attempts"] += 1
                try:
                    resp = httpx.post(
                        url, headers=self.headers, timeout=self.timeout, **kwargs
                    )
                except httpx.RequestError as exc:
                    if attempt == 2:
                        raise BookingTimeoutError(
                            f"OCTO {stage} network error after 2 attempts: {exc}"
                        ) from exc
                    meta["retries"] += 1
                    meta["retry_stage"] = stage
                    time.sleep(_retry_delay())
                    continue

                if attempt == 1 and resp.status_code in _RETRYABLE_5XX:
                    meta["retries"] += 1
                    meta["retry_stage"] = stage
                    time.sleep(_retry_delay())
                    continue
                return resp
            return resp  # type: ignore

        def _cleanup(reservation_uuid: str) -> None:
            for attempt in range(1, 3):
                try:
                    resp = httpx.delete(
                        f"{self.base_url}/bookings/{reservation_uuid}",
                        headers=self.headers,
                        timeout=10,
                    )
                    if resp.is_success or resp.status_code == 404:
                        return
                    if attempt == 2:
                        meta["cleanup_required"] = True
                        meta["cleanup_reservation_uuid"] = reservation_uuid
                        return
                    time.sleep(_retry_delay())
                except Exception:
                    if attempt == 2:
                        meta["cleanup_required"] = True
                        meta["cleanup_reservation_uuid"] = reservation_uuid
                        return
                    time.sleep(_retry_delay())

        def _build_payload(avail_id: str) -> dict:
            return {
                "productId": product_id,
                "optionId": option_id,
                "availabilityId": avail_id,
                "unitItems": unit_items,
                "resellerReference": reseller_ref,
                "contact": contact,
            }

        # Step 1: Create reservation
        payload = _build_payload(availability_id)
        t0 = time.monotonic()
        resp = _post(f"{self.base_url}/reservations", "reservation", json=payload)

        # Fallback: some suppliers don't support /reservations
        if resp.status_code in (400, 404, 405):
            resp = _post(f"{self.base_url}/bookings", "reservation_fallback", json=payload)

        # 409 re-resolution: availability_id is stale
        if resp.status_code == 409 and start_time:
            try:
                now = datetime.now(timezone.utc)
                avail_resp = httpx.post(
                    f"{self.base_url}/availability",
                    headers=self.headers,
                    json={
                        "productId": product_id,
                        "optionId": option_id,
                        "localDateStart": now.strftime("%Y-%m-%d"),
                        "localDateEnd": (now + timedelta(days=8)).strftime("%Y-%m-%d"),
                        "units": [{"id": unit_id, "quantity": qty}],
                    },
                    timeout=15,
                )
                if avail_resp.is_success:
                    orig_start = (start_time or "")[:16]
                    for slot in avail_resp.json():
                        if slot.get("id") == availability_id:
                            break
                        if slot.get("status") not in ("AVAILABLE", "FREESALE", "LIMITED"):
                            continue
                        fs_start = (slot.get("localDateTimeStart") or slot.get("localDate") or "")[:16]
                        if orig_start and fs_start and fs_start != orig_start:
                            continue
                        new_id = slot.get("id")
                        if new_id and new_id != availability_id:
                            fresh_payload = _build_payload(new_id)
                            resp = _post(f"{self.base_url}/reservations", "reservation_retry", json=fresh_payload)
                            if resp.status_code in (400, 404, 405):
                                resp = _post(f"{self.base_url}/bookings", "reservation_retry_fallback", json=fresh_payload)
                            meta["re_resolved"] = True
                            meta["new_availability_id"] = new_id
                            break
            except Exception:
                pass

        meta["reservation_ms"] = round((time.monotonic() - t0) * 1000)

        if resp.status_code == 409:
            raise BookingUnavailableError("Availability slot is no longer available (409)")
        if resp.status_code == 422:
            raise BookingUnavailableError(f"Unprocessable reservation: {resp.text[:300]}")
        if not resp.is_success:
            raise BookingError(f"Reservation failed {resp.status_code}: {resp.text[:300]}")

        reservation = resp.json()
        reservation_uuid = reservation.get("uuid") or reservation.get("id")
        if not reservation_uuid:
            raise BookingError(f"No UUID in reservation response: {resp.text[:300]}")

        # Step 2: Confirm
        confirm_payload = {"contact": contact, "resellerReference": reseller_ref}
        t1 = time.monotonic()
        try:
            resp = _post(
                f"{self.base_url}/bookings/{reservation_uuid}/confirm",
                "confirm",
                json=confirm_payload,
            )
        except Exception:
            _cleanup(reservation_uuid)
            raise

        meta["confirm_ms"] = round((time.monotonic() - t1) * 1000)

        if not resp.is_success:
            _cleanup(reservation_uuid)
            raise BookingError(f"Confirmation failed {resp.status_code}: {resp.text[:300]}")

        booking = resp.json()
        booking_uuid = booking.get("uuid") or booking.get("id") or reservation_uuid
        supplier_ref = booking.get("supplierReference") or booking.get("reference") or ""

        if booking.get("status") not in ("CONFIRMED", "ON_HOLD", ""):
            raise BookingError(f"Unexpected booking status: {booking.get('status')}")

        return BookingResult(
            confirmation=booking_uuid,
            supplier_reference=supplier_ref,
            meta=meta,
        )
