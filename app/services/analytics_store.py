"""
Analytics event store — records widget events for conversion tracking.

Events flow into widget_events table in SQLite for dashboard queries.
"""

from app.services.database import db_record_event


async def record_event(
    operator_id: str,
    conversation_id: str | None,
    event_type: str,
    metadata: dict | None = None,
) -> None:
    """Record a widget event for analytics."""
    try:
        db_record_event(operator_id, conversation_id, event_type, metadata)
    except Exception as e:
        # Analytics failures are non-fatal — never block the booking flow
        print(f"[ANALYTICS] Event recording failed (non-fatal): {e}")
