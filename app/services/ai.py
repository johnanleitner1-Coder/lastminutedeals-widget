"""
AI service — Claude Haiku with tool use for the booking assistant.

Anti-hallucination design: every factual claim comes from either the
operator's product catalog or live OCTO availability data, both injected
as structured context in the system prompt. The AI never generates prices,
times, or descriptions from its own knowledge.
"""

import json
from datetime import datetime, timezone

import anthropic

from app.config import ANTHROPIC_API_KEY, OperatorConfig
from app.services.availability import build_ai_product_context, get_availability_for_date

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


TOOLS = [
    {
        "name": "check_availability",
        "description": (
            "Check live availability for a specific tour on a specific date. "
            "Call this when the customer asks about availability for a particular date."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string", "description": "OCTO product ID from the tour listing"},
                "option_id": {"type": "string", "description": "OCTO option ID (usually DEFAULT)"},
                "unit_id": {"type": "string", "description": "Unit type ID (e.g., adult)"},
                "quantity": {"type": "integer", "description": "Number of people", "minimum": 1, "maximum": 20},
                "date": {"type": "string", "description": "Date to check in YYYY-MM-DD format"},
            },
            "required": ["product_id", "option_id", "unit_id", "quantity", "date"],
        },
    },
    {
        "name": "start_checkout",
        "description": (
            "Initiate payment checkout after the customer has confirmed all details. "
            "Only call this after collecting: full name, email, phone, party size, "
            "and the customer has confirmed the booking summary."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string"},
                "option_id": {"type": "string"},
                "availability_id": {"type": "string", "description": "Specific availability slot ID"},
                "unit_id": {"type": "string"},
                "quantity": {"type": "integer", "minimum": 1, "maximum": 20},
                "customer_name": {"type": "string"},
                "customer_email": {"type": "string"},
                "customer_phone": {"type": "string"},
                "start_time": {"type": "string", "description": "Start time of the selected slot"},
            },
            "required": [
                "product_id", "option_id", "availability_id", "unit_id",
                "quantity", "customer_name", "customer_email", "customer_phone",
            ],
        },
    },
    {
        "name": "escalate_to_human",
        "description": (
            "Escalate to a human team member when you cannot answer a question "
            "from the available tour data, or when the customer explicitly requests "
            "to speak with someone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Why escalation is needed"},
            },
            "required": ["reason"],
        },
    },
]


def build_system_prompt(operator: OperatorConfig, product_context: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    currency_symbol = "€" if operator.currency == "EUR" else "$"

    escalation_info = ""
    if operator.human_escalation.email:
        escalation_info += f"\nEmail: {operator.human_escalation.email}"
    if operator.human_escalation.whatsapp:
        escalation_info += f"\nWhatsApp: {operator.human_escalation.whatsapp}"

    return f"""You are the AI booking assistant for {operator.display_name} in {operator.city}, {operator.country}.
Current time: {now}
All prices are in {operator.currency}. Format: {currency_symbol}85, not $85.

TOURS WE OFFER (descriptions from operator, availability from live booking system):

{product_context}

RULES:
1. ONLY state facts that appear in the tour data above. Never invent descriptions, prices, or availability.
2. If a tour is not listed above, say "That tour isn't available right now. Here's what we have:" and show alternatives.
3. If you can't answer a question from the data above, say "Great question — let me connect you with our team for that detail." and use the escalate_to_human tool.
4. Before checkout, collect: full name, email, phone number, party size. All four are required.
5. Before initiating payment, show a clear summary: tour name, date/time, party size, price per person, total price in {operator.currency}.
6. Show cancellation policy BEFORE payment, not after.
7. Respond in the customer's language (detect from their messages). Default to English.
8. When confirming a booking, include: meeting point with map link, what to bring, cancellation policy, and operator contact info.
9. Be warm, helpful, and concise. You represent {operator.display_name}.
10. Use the check_availability tool when the customer asks about dates or availability. Do not guess — always check live data.
11. Use start_checkout only after the customer has reviewed and confirmed the booking summary.

OPERATOR CONTACT (for escalation):{escalation_info if escalation_info else " Contact information not yet configured."}"""


async def chat(
    operator: OperatorConfig,
    messages: list[dict],
    product_context: str,
) -> dict:
    """
    Send conversation to Claude Haiku and get response.

    Returns dict with:
      - "content": the text response
      - "tool_use": list of tool calls (if any)
      - "stop_reason": why the model stopped
    """
    client = _get_client()
    system_prompt = build_system_prompt(operator, product_context)

    # Convert our message format to Anthropic format
    anthropic_messages = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role in ("user", "assistant"):
            anthropic_messages.append({"role": role, "content": content})

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=system_prompt,
        messages=anthropic_messages,
        tools=TOOLS,
    )

    # Parse response
    text_parts = []
    tool_uses = []
    for block in response.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_uses.append({
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })

    return {
        "content": "\n".join(text_parts),
        "tool_use": tool_uses,
        "stop_reason": response.stop_reason,
    }


async def handle_tool_calls(
    operator: OperatorConfig,
    tool_uses: list[dict],
    messages: list[dict],
    product_context: str,
) -> dict:
    """
    Execute tool calls and get the AI's follow-up response.
    Returns the same format as chat().
    """
    client = _get_client()
    system_prompt = build_system_prompt(operator, product_context)

    # Build the Anthropic messages including tool results
    anthropic_messages = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role in ("user", "assistant"):
            anthropic_messages.append({"role": role, "content": content})

    # Add the assistant message with tool use
    assistant_content = []
    for tu in tool_uses:
        assistant_content.append({
            "type": "tool_use",
            "id": tu["id"],
            "name": tu["name"],
            "input": tu["input"],
        })
    anthropic_messages.append({"role": "assistant", "content": assistant_content})

    # Execute each tool and add results
    tool_results = []
    for tu in tool_uses:
        result = await _execute_tool(operator, tu["name"], tu["input"])
        tool_results.append({
            "type": "tool_result",
            "tool_use_id": tu["id"],
            "content": json.dumps(result),
        })
    anthropic_messages.append({"role": "user", "content": tool_results})

    # Get follow-up response
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=system_prompt,
        messages=anthropic_messages,
        tools=TOOLS,
    )

    text_parts = []
    new_tool_uses = []
    for block in response.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            new_tool_uses.append({
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })

    return {
        "content": "\n".join(text_parts),
        "tool_use": new_tool_uses,
        "stop_reason": response.stop_reason,
    }


async def _execute_tool(operator: OperatorConfig, tool_name: str, tool_input: dict) -> dict:
    """Execute a single tool call and return the result."""
    if tool_name == "check_availability":
        try:
            slots = get_availability_for_date(
                operator=operator,
                product_id=tool_input["product_id"],
                option_id=tool_input["option_id"],
                unit_id=tool_input["unit_id"],
                quantity=tool_input["quantity"],
                date_start=tool_input["date"],
                date_end=tool_input["date"],
            )
            if not slots:
                return {"available": False, "message": "No availability for this date.", "slots": []}
            return {"available": True, "slots": slots}
        except Exception as e:
            return {"error": f"Could not check availability: {str(e)[:200]}"}

    elif tool_name == "start_checkout":
        # Return the checkout parameters — the router layer creates the Stripe session
        return {
            "action": "checkout",
            "product_id": tool_input["product_id"],
            "option_id": tool_input["option_id"],
            "availability_id": tool_input["availability_id"],
            "unit_id": tool_input["unit_id"],
            "quantity": tool_input["quantity"],
            "customer_name": tool_input["customer_name"],
            "customer_email": tool_input["customer_email"],
            "customer_phone": tool_input.get("customer_phone", ""),
            "start_time": tool_input.get("start_time", ""),
        }

    elif tool_name == "escalate_to_human":
        return {
            "action": "escalate",
            "reason": tool_input.get("reason", ""),
            "contact": {
                "email": operator.human_escalation.email,
                "whatsapp": operator.human_escalation.whatsapp,
            },
        }

    return {"error": f"Unknown tool: {tool_name}"}
