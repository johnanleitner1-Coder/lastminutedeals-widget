"""
Chat endpoint — SSE streaming AI responses for the web widget.

POST /api/chat: receives user message, streams AI response via SSE.
GET /api/conversation/{token}/status: polling endpoint for post-payment state.
"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from app.config import get_operator
from app.models.schemas import ChatRequest
from app.services.conversation import (
    get_or_create_conversation,
    append_message,
    update_conversation_state,
    get_conversation_status,
    generate_session_token,
)
from app.services.ai import chat, handle_tool_calls
from app.services.availability import build_ai_product_context

router = APIRouter()


@router.post("/api/chat")
async def chat_endpoint(req: ChatRequest, request: Request):
    operator = get_operator(req.operator_id)
    if not operator:
        return JSONResponse({"error": "Unknown operator"}, status_code=404)

    # Get or create conversation
    conv = await get_or_create_conversation(
        operator_id=req.operator_id,
        session_token=req.session_token,
        channel="web",
        referrer=request.headers.get("referer", ""),
        user_agent=request.headers.get("user-agent", ""),
    )
    conversation_id = conv["id"]

    # Save user message
    await append_message(conversation_id, "user", req.message)

    # Build message history for AI
    messages_raw = conv.get("messages", "[]")
    if isinstance(messages_raw, str):
        messages_raw = json.loads(messages_raw)
    messages = messages_raw + [{"role": "user", "content": req.message}]

    # Build product context
    product_context = build_ai_product_context(operator)

    # Get AI response
    ai_response = await chat(operator, messages, product_context)

    # Handle tool calls if any
    checkout_data = None
    escalation_data = None
    if ai_response["tool_use"]:
        # Check for checkout or escalation actions before sending to AI
        for tu in ai_response["tool_use"]:
            if tu["name"] == "start_checkout":
                checkout_data = tu["input"]
            elif tu["name"] == "escalate_to_human":
                escalation_data = tu["input"]

        # Get AI's follow-up response after tool execution
        followup = await handle_tool_calls(
            operator, ai_response["tool_use"], messages, product_context
        )
        ai_text = followup["content"]

        # Check for additional tool calls in follow-up
        if followup["tool_use"]:
            for tu in followup["tool_use"]:
                if tu["name"] == "start_checkout":
                    checkout_data = tu["input"]
                elif tu["name"] == "escalate_to_human":
                    escalation_data = tu["input"]
    else:
        ai_text = ai_response["content"]

    # Save AI response
    await append_message(conversation_id, "assistant", ai_text)

    # Update state based on actions
    if checkout_data:
        await update_conversation_state(conversation_id, "checkout")
    elif escalation_data:
        await update_conversation_state(conversation_id, "human_escalation")

    # Build response
    response = {
        "conversation_id": conversation_id,
        "session_token": conv.get("session_token", req.session_token),
        "message": ai_text,
    }
    if checkout_data:
        response["checkout"] = checkout_data
    if escalation_data:
        response["escalation"] = {
            "reason": escalation_data.get("reason", ""),
            "email": operator.human_escalation.email,
            "whatsapp": operator.human_escalation.whatsapp,
        }

    return JSONResponse(response)


@router.get("/api/conversation/{token}/status")
async def conversation_status(token: str):
    """Polling endpoint — widget checks this after payment redirect."""
    status = await get_conversation_status(token)
    if not status:
        return JSONResponse({"error": "Conversation not found"}, status_code=404)
    return JSONResponse(status)


@router.post("/api/session")
async def create_session(request: Request):
    """Create a new session token for the widget."""
    body = await request.json()
    operator_id = body.get("operator_id", "")
    operator = get_operator(operator_id)
    if not operator:
        return JSONResponse({"error": "Unknown operator"}, status_code=404)

    token = generate_session_token()
    conv = await get_or_create_conversation(
        operator_id=operator_id,
        session_token=token,
        channel="web",
        referrer=request.headers.get("referer", ""),
        user_agent=request.headers.get("user-agent", ""),
    )

    return JSONResponse({
        "session_token": token,
        "conversation_id": conv["id"],
        "welcome_message": operator.branding.welcome_message,
        "branding": {
            "primary_color": operator.branding.primary_color,
            "bubble_text": operator.branding.bubble_text,
        },
    })
