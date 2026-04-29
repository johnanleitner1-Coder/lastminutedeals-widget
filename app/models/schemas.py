"""Pydantic request/response models for the widget API."""

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    operator_id: str
    session_token: str
    message: str = Field(..., max_length=2000)


class CheckoutRequest(BaseModel):
    operator_id: str
    session_token: str
    product_id: str
    option_id: str
    availability_id: str
    unit_id: str
    quantity: int = Field(ge=1, le=20)
    customer_name: str = Field(..., max_length=200)
    customer_email: str = Field(..., max_length=200)
    customer_phone: str = Field(..., max_length=50)
    start_time: str | None = None


class WhatsAppWebhookPayload(BaseModel):
    """Subset of Meta Cloud API webhook payload we care about."""
    object: str = ""
    entry: list[dict] = []
