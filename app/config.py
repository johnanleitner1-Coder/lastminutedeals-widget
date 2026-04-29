"""
Operator configuration and environment loading.

Each operator entry defines everything needed to serve that operator's
widget: Bokun credentials, branding, product catalog path, and contact info.
Adding a new operator = add an entry here + deploy.
"""

import json
import os
from pathlib import Path
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass
class OperatorBranding:
    primary_color: str = "#1a5632"
    bubble_text: str = "Ask about tours!"
    welcome_message: str = "Hi! I can help you find and book the perfect tour. What are you looking for?"


@dataclass
class HumanEscalation:
    email: str = ""
    whatsapp: str = ""


@dataclass
class OperatorConfig:
    operator_id: str
    display_name: str
    bokun_vendor_id: int
    bokun_display_name: str
    base_url: str
    city: str
    country: str
    currency: str
    locale: str
    timezone: str
    commission_pct: float
    product_catalog_path: str
    branding: OperatorBranding
    human_escalation: HumanEscalation
    privacy_policy_url: str
    allowed_origins: list[str] = field(default_factory=list)

    @property
    def api_key(self) -> str:
        return os.getenv("BOKUN_API_KEY", "")

    @property
    def currency_symbol(self) -> str:
        return "€" if self.currency == "EUR" else "$"


# ── Operator Registry ────────────────────────────────────────────────────────

OPERATORS: dict[str, OperatorConfig] = {
    "oturista": OperatorConfig(
        operator_id="oturista",
        display_name="O Turista Tours",
        bokun_vendor_id=103510,
        bokun_display_name="Ó Turista! Tours and Trips",
        base_url="https://api.bokun.io/octo/v1",
        city="Lisbon",
        country="PT",
        currency="EUR",
        locale="pt_PT",
        timezone="Europe/Lisbon",
        commission_pct=0.30,
        product_catalog_path="data/operators/oturista/products.json",
        branding=OperatorBranding(
            primary_color="#1a5632",
            bubble_text="Ask about tours!",
            welcome_message=(
                "Hi! I can help you find and book the perfect tour "
                "in Lisbon or Sintra. What are you looking for?"
            ),
        ),
        human_escalation=HumanEscalation(
            email="",      # confirm with Eduardo
            whatsapp="",   # confirm with Eduardo
        ),
        privacy_policy_url="https://widget.lastminutedealshq.com/privacy",
        allowed_origins=[],  # populated from env or Eduardo's domain
    ),
}


def get_operator(operator_id: str) -> OperatorConfig | None:
    return OPERATORS.get(operator_id)


def load_product_catalog(operator: OperatorConfig) -> list[dict]:
    """Load the operator's static product catalog from JSON."""
    path = BASE_DIR / operator.product_catalog_path
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


# ── Environment shortcuts ────────────────────────────────────────────────────

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SECRET_KEY", "")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
WIDGET_BASE_URL = os.getenv("WIDGET_BASE_URL", "https://widget.lastminutedealshq.com")
DASHBOARD_HMAC_SECRET = os.getenv("DASHBOARD_HMAC_SECRET", "")
if not DASHBOARD_HMAC_SECRET:
    print("[WARNING] DASHBOARD_HMAC_SECRET not set — dashboard will be inaccessible")
    DASHBOARD_HMAC_SECRET = "unset-generate-a-random-secret"

# WhatsApp (Meta Cloud API)
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
