"""
SQLite database — persistent storage for conversations and analytics.

Replaces Supabase REST API. Runs as a local file on Railway persistent volume.
In-memory store (_memory) remains the primary source of truth for active
conversations; SQLite provides persistence across redeploys and data for
the analytics dashboard.
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.config import DATABASE_PATH

_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """Get a thread-local SQLite connection (one per thread, reused)."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DATABASE_PATH, timeout=5.0)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA busy_timeout=3000")
    return _local.conn


def init_db() -> None:
    """Create tables if they don't exist. Called once at startup."""
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS widget_conversations (
            id TEXT PRIMARY KEY,
            operator_id TEXT NOT NULL,
            channel TEXT NOT NULL DEFAULT 'web',
            session_token TEXT,
            whatsapp_phone TEXT,
            messages TEXT NOT NULL DEFAULT '[]',
            state TEXT NOT NULL DEFAULT 'greeting',
            context TEXT DEFAULT '{}',
            message_count INTEGER DEFAULT 0,
            referrer TEXT,
            user_agent TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            converted INTEGER DEFAULT 0,
            booking_id TEXT,
            revenue_cents INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_conv_operator_session
            ON widget_conversations(operator_id, session_token);
        CREATE INDEX IF NOT EXISTS idx_conv_operator_phone
            ON widget_conversations(operator_id, whatsapp_phone);
        CREATE INDEX IF NOT EXISTS idx_conv_created
            ON widget_conversations(created_at);

        CREATE TABLE IF NOT EXISTS widget_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operator_id TEXT NOT NULL,
            conversation_id TEXT,
            event_type TEXT NOT NULL,
            metadata TEXT DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_events_operator_created
            ON widget_events(operator_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_events_conversation
            ON widget_events(conversation_id);

        CREATE TABLE IF NOT EXISTS pending_bookings (
            stripe_session_id TEXT PRIMARY KEY,
            operator_id TEXT NOT NULL,
            conversation_id TEXT,
            payment_intent_id TEXT,
            product_id TEXT,
            option_id TEXT,
            availability_id TEXT,
            unit_id TEXT,
            quantity INTEGER,
            customer_name TEXT,
            customer_email TEXT,
            customer_phone TEXT,
            start_time TEXT,
            amount_cents INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            completed_at TEXT
        );
    """)
    conn.commit()


# ── Conversation operations ──────────────────────────────────────────────


def db_find_conversation(
    operator_id: str,
    *,
    session_token: str | None = None,
    whatsapp_phone: str | None = None,
) -> dict | None:
    """Find a conversation by session_token or whatsapp_phone."""
    conn = _get_conn()
    if session_token:
        row = conn.execute(
            "SELECT * FROM widget_conversations WHERE operator_id=? AND session_token=? LIMIT 1",
            (operator_id, session_token),
        ).fetchone()
    elif whatsapp_phone:
        row = conn.execute(
            "SELECT * FROM widget_conversations WHERE operator_id=? AND whatsapp_phone=? ORDER BY updated_at DESC LIMIT 1",
            (operator_id, whatsapp_phone),
        ).fetchone()
    else:
        return None

    if row:
        return _row_to_dict(row)
    return None


def db_insert_conversation(conv: dict) -> None:
    """Insert a new conversation."""
    conn = _get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO widget_conversations
           (id, operator_id, channel, session_token, whatsapp_phone,
            messages, state, context, message_count, referrer, user_agent,
            created_at, updated_at, converted, booking_id, revenue_cents)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            conv["id"], conv["operator_id"], conv["channel"],
            conv.get("session_token"), conv.get("whatsapp_phone"),
            json.dumps(conv.get("messages", [])),
            conv.get("state", "greeting"),
            json.dumps(conv.get("context", {})),
            conv.get("message_count", 0),
            conv.get("referrer", ""), conv.get("user_agent", ""),
            conv["created_at"], conv["updated_at"],
            1 if conv.get("converted") else 0,
            conv.get("booking_id"), conv.get("revenue_cents"),
        ),
    )
    conn.commit()


def db_update_messages(conversation_id: str, messages: list, message_count: int) -> None:
    """Update the messages array for a conversation."""
    conn = _get_conn()
    conn.execute(
        "UPDATE widget_conversations SET messages=?, message_count=?, updated_at=? WHERE id=?",
        (json.dumps(messages), message_count, datetime.now(timezone.utc).isoformat(), conversation_id),
    )
    conn.commit()


def db_update_state(conversation_id: str, state: str, **kwargs) -> None:
    """Update conversation state and optional fields."""
    conn = _get_conn()
    fields = ["state=?", "updated_at=?"]
    values: list = [state, datetime.now(timezone.utc).isoformat()]

    if "context" in kwargs:
        fields.append("context=?")
        values.append(json.dumps(kwargs["context"]))
    if "converted" in kwargs:
        fields.append("converted=?")
        values.append(1 if kwargs["converted"] else 0)
    if "booking_id" in kwargs:
        fields.append("booking_id=?")
        values.append(kwargs["booking_id"])
    if "revenue_cents" in kwargs:
        fields.append("revenue_cents=?")
        values.append(kwargs["revenue_cents"])

    values.append(conversation_id)
    conn.execute(
        f"UPDATE widget_conversations SET {', '.join(fields)} WHERE id=?",
        values,
    )
    conn.commit()


def db_find_conversation_by_id(conversation_id: str) -> dict | None:
    """Find a conversation by ID."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM widget_conversations WHERE id=? LIMIT 1",
        (conversation_id,),
    ).fetchone()
    return _row_to_dict(row) if row else None


def db_get_conversation_status(session_token: str) -> dict | None:
    """Get conversation status for widget polling."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, state, booking_id, converted, context FROM widget_conversations WHERE session_token=? LIMIT 1",
        (session_token,),
    ).fetchone()
    if row:
        ctx = row["context"] or "{}"
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except (json.JSONDecodeError, TypeError):
                ctx = {}
        return {
            "id": row["id"],
            "state": row["state"],
            "booking_id": row["booking_id"],
            "converted": bool(row["converted"]),
            "context": ctx,
        }
    return None


# ── Analytics operations ─────────────────────────────────────────────────


def db_record_event(
    operator_id: str,
    conversation_id: str | None,
    event_type: str,
    metadata: dict | None = None,
) -> None:
    """Record an analytics event."""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO widget_events (operator_id, conversation_id, event_type, metadata, created_at) VALUES (?, ?, ?, ?, ?)",
        (operator_id, conversation_id, event_type, json.dumps(metadata or {}), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def db_analytics_data(operator_id: str, since: str) -> dict:
    """Fetch aggregate analytics data for the dashboard."""
    conn = _get_conn()

    total_conversations = conn.execute(
        "SELECT COUNT(*) FROM widget_conversations WHERE operator_id=? AND created_at>=?",
        (operator_id, since),
    ).fetchone()[0]

    total_bookings = conn.execute(
        "SELECT COUNT(*) FROM widget_conversations WHERE operator_id=? AND converted=1 AND created_at>=?",
        (operator_id, since),
    ).fetchone()[0]

    revenue_row = conn.execute(
        "SELECT COALESCE(SUM(revenue_cents), 0) FROM widget_conversations WHERE operator_id=? AND converted=1 AND created_at>=?",
        (operator_id, since),
    ).fetchone()
    total_revenue_cents = revenue_row[0]

    web_conversations = conn.execute(
        "SELECT COUNT(*) FROM widget_conversations WHERE operator_id=? AND channel='web' AND created_at>=?",
        (operator_id, since),
    ).fetchone()[0]

    whatsapp_conversations = conn.execute(
        "SELECT COUNT(*) FROM widget_conversations WHERE operator_id=? AND channel='whatsapp' AND created_at>=?",
        (operator_id, since),
    ).fetchone()[0]

    web_bookings = conn.execute(
        "SELECT COUNT(*) FROM widget_conversations WHERE operator_id=? AND channel='web' AND converted=1 AND created_at>=?",
        (operator_id, since),
    ).fetchone()[0]

    whatsapp_bookings = conn.execute(
        "SELECT COUNT(*) FROM widget_conversations WHERE operator_id=? AND channel='whatsapp' AND converted=1 AND created_at>=?",
        (operator_id, since),
    ).fetchone()[0]

    total_escalations = conn.execute(
        "SELECT COUNT(*) FROM widget_conversations WHERE operator_id=? AND state='human_escalation' AND created_at>=?",
        (operator_id, since),
    ).fetchone()[0]

    recent_bookings = conn.execute(
        "SELECT booking_id, channel, revenue_cents, created_at FROM widget_conversations WHERE operator_id=? AND converted=1 AND created_at>=? ORDER BY created_at DESC LIMIT 20",
        (operator_id, since),
    ).fetchall()

    return {
        "total_conversations": total_conversations,
        "total_bookings": total_bookings,
        "total_revenue_cents": total_revenue_cents,
        "web_conversations": web_conversations,
        "whatsapp_conversations": whatsapp_conversations,
        "web_bookings": web_bookings,
        "whatsapp_bookings": whatsapp_bookings,
        "total_escalations": total_escalations,
        "recent_bookings": [
            {
                "booking_id": r["booking_id"] or "",
                "channel": r["channel"] or "",
                "revenue": (r["revenue_cents"] or 0) / 100,
                "created_at": r["created_at"] or "",
            }
            for r in recent_bookings
        ],
    }


# ── Pending bookings (idempotency + crash recovery) ─────────────────────


def db_insert_pending_booking(stripe_session_id: str, data: dict) -> bool:
    """Insert a pending booking. Returns False if already exists (idempotency)."""
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO pending_bookings
               (stripe_session_id, operator_id, conversation_id, payment_intent_id,
                product_id, option_id, availability_id, unit_id, quantity,
                customer_name, customer_email, customer_phone, start_time,
                amount_cents, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (
                stripe_session_id, data.get("operator_id", ""),
                data.get("conversation_id", ""), data.get("payment_intent_id", ""),
                data.get("product_id", ""), data.get("option_id", ""),
                data.get("availability_id", ""), data.get("unit_id", ""),
                data.get("quantity", 1),
                data.get("customer_name", ""), data.get("customer_email", ""),
                data.get("customer_phone", ""), data.get("start_time", ""),
                data.get("amount_cents", 0),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def db_complete_pending_booking(stripe_session_id: str, status: str) -> None:
    """Mark a pending booking as completed or failed."""
    conn = _get_conn()
    conn.execute(
        "UPDATE pending_bookings SET status=?, completed_at=? WHERE stripe_session_id=?",
        (status, datetime.now(timezone.utc).isoformat(), stripe_session_id),
    )
    conn.commit()


def db_get_stale_pending_bookings(max_age_seconds: int = 600) -> list[dict]:
    """Find pending bookings older than max_age_seconds that never completed."""
    conn = _get_conn()
    cutoff = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        """SELECT * FROM pending_bookings
           WHERE status='pending'
           AND created_at < datetime(?, '-' || ? || ' seconds')""",
        (cutoff, max_age_seconds),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Helpers ──────────────────────────────────────────────────────────────


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict with parsed JSON fields."""
    d = dict(row)
    # Parse JSON string fields
    for field in ("messages", "context"):
        if field in d and isinstance(d[field], str):
            try:
                d[field] = json.loads(d[field])
            except (json.JSONDecodeError, TypeError):
                pass
    # Convert integer boolean back to Python bool
    if "converted" in d:
        d["converted"] = bool(d["converted"])
    return d
