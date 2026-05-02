"""
AI service — OpenAI GPT-4.1-mini with tool use for the booking assistant.

Anti-hallucination design: every factual claim comes from either the
operator's product catalog or live OCTO availability data, both injected
as structured context in the system prompt. The AI never generates prices,
times, or descriptions from its own knowledge.
"""

import json
from datetime import datetime, timezone

from openai import AsyncOpenAI

from app.config import OPENAI_API_KEY, OperatorConfig
from app.services.availability import build_ai_product_context, get_availability_for_date, search_all_availability

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
            "name": "search_availability",
            "description": (
                "Search availability across ALL tours for a date range. Use this when the customer "
                "asks general questions like 'what's available next week?' or 'anything on May 5th?' "
                "This checks all tours in parallel and returns which ones have open slots."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date_start": {"type": "string", "description": "Start date in YYYY-MM-DD format"},
                    "date_end": {"type": "string", "description": "End date in YYYY-MM-DD format"},
                    "quantity": {"type": "integer", "description": "Number of people", "minimum": 1, "maximum": 20, "default": 1},
                    "time_of_day": {
                        "type": "string",
                        "enum": ["morning", "afternoon", "evening", ""],
                        "description": "Optional time filter: morning (before noon), afternoon (noon-5pm), evening (after 5pm)",
                    },
                },
                "required": ["date_start", "date_end"],
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
    now_dt = datetime.now(timezone.utc)
    now = now_dt.strftime("%Y-%m-%d %H:%M UTC")
    today = now_dt.strftime("%Y-%m-%d")
    year = now_dt.year

    escalation_info = ""
    if operator.human_escalation.email:
        escalation_info += f"\nEmail: {operator.human_escalation.email}"
    if operator.human_escalation.whatsapp:
        escalation_info += f"\nWhatsApp: {operator.human_escalation.whatsapp}"

    return f"""You are the AI booking assistant for {operator.display_name} in {operator.city}, {operator.country}.

IMPORTANT — TODAY'S DATE: {today} (year {year}). Use this date for ALL date calculations.
When the customer says "tomorrow", that means {today} + 1 day. "This week" starts from {today}. "Next week" starts 7 days from {today}. Always use year {year} in YYYY-MM-DD dates.

Current time: {now}
Display prices exactly as they appear in the availability data — use the currency shown there (e.g., if the data says "USD", show USD).

TOURS WE OFFER (descriptions from operator, availability from live booking system):

{product_context}

RULES:
1. ONLY state facts that appear in the tour data above. Never invent descriptions, prices, or availability. ONLY list tours that appear in the data above — never add extra tours. Always show EXACT prices from the data — never say "about", "around", or "approximately". If prices differ across dates, show the price for each date separately.
2. When the customer asks "what tours do you offer" or "what tours do you have" (no date mentioned), list tours from the data above with a brief description.
3. When the customer mentions a DATE (e.g., "what's available tomorrow", "tours on Saturday", "next week"), use search_availability to check ALL tours at once for that date range. Use check_availability only when checking a SPECIFIC tour the customer already picked. Availability is real-time from the booking system — always check, never guess. When showing availability results, always include the start time for each slot (e.g., "9:00 AM") — customers need to know when tours depart.
IMPORTANT — PARTY SIZE BEFORE PRICES: You MUST ask for the party size BEFORE calling search_availability or check_availability. Do NOT call any availability tool until you know how many people are in the group. Prices depend on group size, so checking availability without the correct quantity gives wrong prices. When the customer asks about dates without mentioning group size, respond ONLY with "How many people will be in your group?" — do not check availability yet.
ONCE you know the party size, ALWAYS include prices when listing available tours. Show the TOTAL price for the group (use the "total_price" field from the availability data), not the per-unit price. Format example: "Tour Name — May 10 at 9:00 AM — $352 total for 4 people". Never say "per unit" — that is an internal system term customers will not understand. Never describe the pricing structure (e.g., don't say "per vehicle" or "per group") — you don't know how the operator structures their pricing.
4. If a customer asks about a specific tour that is not in the list above, say "That tour isn't in our catalog. Here's what we offer:" and list the available tours.
5. You can answer general conversational questions (greetings, "can you help me", "are you there", etc.) naturally. Only use escalate_to_human when the customer asks a SPECIFIC question about tour details that aren't in the data above (e.g., dietary requirements, wheelchair access, custom itineraries), or when they explicitly ask to speak with a person.
6. Before checkout, collect: full name, email, phone number, party size. All four are required.
7. Before initiating payment, show a clear summary: tour name, date/time, party size, price per person, and total price.
8. Show cancellation policy BEFORE payment, not after.
9. Respond in the customer's language (detect from their messages). Default to English.
10. When confirming a booking, include: meeting point with map link, what to bring, cancellation policy, and operator contact info.
11. Be warm, helpful, and concise. You represent {operator.display_name}.
12. Use start_checkout only after the customer has reviewed and confirmed the booking summary.
13. When you call start_checkout, tell the customer a payment link is being prepared — do NOT say the booking is "confirmed" until after payment. Say something like "I'm setting up your payment now — you'll receive a link to complete the booking."
14. When the customer changes the party size, ALWAYS re-check availability with the new quantity using check_availability. Never calculate prices by multiplying — prices can change with group size (e.g., private tours have per-vehicle pricing). Always get fresh prices from the booking system.
15. The booking system supports up to 20 people. If a customer requests more than 20, use escalate_to_human — large group arrangements need human coordination. Do not suggest splitting into multiple bookings.
16. When you ask the customer a question and they reply with a short answer (a number, "yes", "no", a name, etc.), interpret it as the answer to your most recent question. For example, if you asked "How many people?" and they say "2", that means 2 people — do not ask for clarification. Proceed with the information.
17. Never promise capabilities you don't have. You cannot: send reminders, schedule follow-ups, "keep a note", notify the customer later, make phone calls, send emails, or check back with them in the future. If a date is too far out for the booking system, say so honestly and suggest they check back closer to the date, or use escalate_to_human if they want to discuss with the team.
18. When no availability is found for the requested date, do NOT just say "nothing available, try another date" and wait for the customer to guess. Instead, proactively search a wider date range (e.g., the next 7-14 days) to find the earliest available date, and suggest it. For example: "Nothing available this weekend, but I found availability starting May 10th — would you like to see those options?"

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
        model="gpt-4.1-mini",
        max_tokens=2048,
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

    # Execute each tool and add results (deduplicate identical calls)
    result_cache: dict[str, str] = {}
    for tu in tool_uses:
        cache_key = f"{tu['name']}:{json.dumps(tu['input'], sort_keys=True)}"
        if cache_key in result_cache:
            result_json = result_cache[cache_key]
        else:
            result = await _execute_tool(operator, tu["name"], tu["input"])
            result_json = json.dumps(result)
            result_cache[cache_key] = result_json
        openai_messages.append({
            "role": "tool",
            "tool_call_id": tu["id"],
            "content": result_json,
        })

    # Get follow-up response
    response = await client.chat.completions.create(
        model="gpt-4.1-mini",
        max_tokens=2048,
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


def _fix_date_year(date_str: str) -> str:
    """Fix dates where the AI used the wrong year (common with smaller models)."""
    today = datetime.now(timezone.utc)
    try:
        parsed = datetime.strptime(date_str, "%Y-%m-%d")
        if parsed.year != today.year:
            corrected = f"{today.year}{date_str[4:]}"
            print(f"[DATE FIX] {date_str} -> {corrected}")
            return corrected
    except ValueError:
        pass
    return date_str


async def _execute_tool(operator: OperatorConfig, tool_name: str, tool_input: dict) -> dict:
    """Execute a single tool call and return the result."""
    if tool_name == "check_availability":
        try:
            date = _fix_date_year(tool_input["date"])
            slots = get_availability_for_date(
                operator=operator,
                product_id=tool_input["product_id"],
                option_id=tool_input["option_id"],
                unit_id=tool_input["unit_id"],
                quantity=tool_input["quantity"],
                date_start=date,
                date_end=date,
            )
            if not slots:
                return {"available": False, "message": "No availability for this date.", "slots": []}
            return {"available": True, "slots": slots}
        except Exception as e:
            return {"error": f"Could not check availability: {str(e)[:200]}"}

    elif tool_name == "search_availability":
        try:
            date_start = _fix_date_year(tool_input["date_start"])
            date_end = _fix_date_year(tool_input["date_end"])
            results = search_all_availability(
                operator=operator,
                date_start=date_start,
                date_end=date_end,
                quantity=tool_input.get("quantity", 1),
                time_of_day=tool_input.get("time_of_day", ""),
            )
            tours_with_slots = [r for r in results if r["slots"]]
            tours_without = [r["tour"] for r in results if not r["slots"]]
            print(f"[TOOL] search_availability: {len(tours_with_slots)} available, {len(tours_without)} empty")
            for t in tours_with_slots:
                print(f"[TOOL]   {t['tour']}: {len(t['slots'])} slots")
            return {
                "available_tours": tours_with_slots,
                "unavailable_tours": tours_without,
                "date_range": f"{tool_input['date_start']} to {tool_input['date_end']}",
                "total_checked": len(results),
            }
        except Exception as e:
            print(f"[TOOL] search_availability ERROR: {e}")
            return {"error": f"Could not search availability: {str(e)[:200]}"}

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
