"""
AI service — OpenAI GPT-4.1-nano with tool use for the booking assistant.

Anti-hallucination design: every factual claim comes from either the
operator's product catalog or live OCTO availability data, both injected
as structured context in the system prompt. The AI never generates prices,
times, or descriptions from its own knowledge.
"""

import json
from datetime import datetime, timezone

from openai import AsyncOpenAI

from app.config import OPENAI_API_KEY, OperatorConfig
from app.services.availability import build_ai_product_context, get_availability_for_date

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    return _client


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_availability",
            "description": (
                "Check live availability for a specific tour on a specific date. "
                "Call this when the customer asks about availability for a particular date."
            ),
            "parameters": {
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
    },
    {
        "type": "function",
        "function": {
            "name": "start_checkout",
            "description": (
                "Initiate payment checkout after the customer has confirmed all details. "
                "Only call this after collecting: full name, email, phone, party size, "
                "and the customer has confirmed the booking summary."
            ),
            "parameters": {
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
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_to_human",
            "description": (
                "Escalate to a human team member when you cannot answer a question "
                "from the available tour data, or when the customer explicitly requests "
                "to speak with someone."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Why escalation is needed"},
                },
                "required": ["reason"],
            },
        },
    },
]


def build_system_prompt(operator: OperatorConfig, product_context: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    currency_symbol = operator.currency_symbol

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
2. When the customer asks what tours are available (general question), list all tours from the data above with a brief description of each. Do NOT call check_availability for general "what tours do you have" questions — just show the catalog.
3. Only use check_availability when the customer asks about a SPECIFIC tour on a SPECIFIC date (e.g., "Is the Sintra tour available Saturday for 2 people?").
4. If a customer asks about a specific tour that is not in the list above, say "That tour isn't in our catalog. Here's what we offer:" and list alternatives.
5. You can answer general conversational questions (greetings, "can you help me", "are you there", etc.) naturally. Only use escalate_to_human when the customer asks a SPECIFIC question about tour details that aren't in the data above (e.g., dietary requirements, wheelchair access, custom itineraries), or when they explicitly ask to speak with a person.
6. Before checkout, collect: full name, email, phone number, party size. All four are required.
7. Before initiating payment, show a clear summary: tour name, date/time, party size, price per person, total price in {operator.currency}.
8. Show cancellation policy BEFORE payment, not after.
9. Respond in the customer's language (detect from their messages). Default to English.
10. When confirming a booking, include: meeting point with map link, what to bring, cancellation policy, and operator contact info.
11. Be warm, helpful, and concise. You represent {operator.display_name}.
12. Use start_checkout only after the customer has reviewed and confirmed the booking summary.

OPERATOR CONTACT (for escalation):{escalation_info if escalation_info else " Contact information not yet configured."}"""


async def chat(
    operator: OperatorConfig,
    messages: list[dict],
    product_context: str,
) -> dict:
    """
    Send conversation to GPT-4.1-nano and get response.

    Returns dict with:
      - "content": the text response
      - "tool_use": list of tool calls (if any)
      - "stop_reason": why the model stopped
    """
    client = _get_client()
    system_prompt = build_system_prompt(operator, product_context)

    # Build OpenAI messages
    openai_messages = [{"role": "system", "content": system_prompt}]
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role in ("user", "assistant"):
            openai_messages.append({"role": role, "content": content})

    response = await client.chat.completions.create(
        model="gpt-4.1-nano",
        max_tokens=1024,
        messages=openai_messages,
        tools=TOOLS,
    )

    choice = response.choices[0]
    text = choice.message.content or ""
    tool_uses = []

    if choice.message.tool_calls:
        for tc in choice.message.tool_calls:
            tool_uses.append({
                "id": tc.id,
                "name": tc.function.name,
                "input": json.loads(tc.function.arguments),
            })

    return {
        "content": text,
        "tool_use": tool_uses,
        "stop_reason": choice.finish_reason,
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

    # Build OpenAI messages including tool results
    openai_messages = [{"role": "system", "content": system_prompt}]
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role in ("user", "assistant"):
            openai_messages.append({"role": role, "content": content})

    # Add the assistant message with tool calls
    openai_messages.append({
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": tu["id"],
                "type": "function",
                "function": {
                    "name": tu["name"],
                    "arguments": json.dumps(tu["input"]),
                },
            }
            for tu in tool_uses
        ],
    })

    # Execute each tool and add results
    for tu in tool_uses:
        result = await _execute_tool(operator, tu["name"], tu["input"])
        openai_messages.append({
            "role": "tool",
            "tool_call_id": tu["id"],
            "content": json.dumps(result),
        })

    # Get follow-up response
    response = await client.chat.completions.create(
        model="gpt-4.1-nano",
        max_tokens=1024,
        messages=openai_messages,
        tools=TOOLS,
    )

    choice = response.choices[0]
    text = choice.message.content or ""
    new_tool_uses = []

    if choice.message.tool_calls:
        for tc in choice.message.tool_calls:
            new_tool_uses.append({
                "id": tc.id,
                "name": tc.function.name,
                "input": json.loads(tc.function.arguments),
            })

    return {
        "content": text,
        "tool_use": new_tool_uses,
        "stop_reason": choice.finish_reason,
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
