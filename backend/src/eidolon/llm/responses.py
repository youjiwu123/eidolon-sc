"""OpenAI Responses provider."""

import base64
import json
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from eidolon.billing import bill
from eidolon.config import ReasoningEffort
from eidolon.llm import (
    ChatMessage,
    LLMError,
    Reasoning,
    Streamed,
    Tool,
    ToolCall,
    merge_extra_body,
    opening_system,
)
from eidolon.llm.sse import parse_arguments, stream_events

log = logging.getLogger(__name__)


class ResponsesLLM:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        client: httpx.AsyncClient | None = None,
        max_tokens: int | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/responses"
        self._api_key = api_key
        self._model = model
        self._max_tokens = max_tokens
        self._effort = reasoning_effort
        self._extra_body = extra_body
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10))

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[Tool] = (),
        *,
        forbid_calls: bool = False,
    ) -> AsyncIterator[Streamed]:
        body: dict[str, Any] = {
            "model": self._model,
            "stream": True,
            "store": False,  # every request carries the whole conversation
        }
        # The system messages it opens with are the instructions; a later one goes where it is.
        opening, rest = opening_system(messages)
        if system := "\n\n".join(m.content for m in opening):
            body["instructions"] = system
        body["input"] = [item for m in rest for item in _items(m)]
        if self._max_tokens is not None:
            body["max_output_tokens"] = self._max_tokens
        if self._effort is not None:
            body["reasoning"] = {"effort": self._effort}
            if self._effort != "none":
                # With `store` off, what a tool round sends back of the reasoning is all the
                # service has of it, and it comes only encrypted, only when asked for.
                body["include"] = ["reasoning.encrypted_content"]
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                }
                for t in tools
            ]
            if forbid_calls:
                body["tool_choice"] = "none"
        body = merge_extra_body(body, self._extra_body)
        calls: dict[int, dict[str, str]] = {}  # output index 鈫?call id, name and arguments
        # Output index 鈫?a reasoning item as done, which goes back as it came: with `store`
        # off, its encrypted content is all the service keeps of what the model thought.
        reasoning: dict[int, dict[str, Any]] = {}
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async for event in stream_events(self._client, self._url, body, headers):
            kind = event.get("type")
            if kind == "response.output_text.delta":
                if delta := event.get("delta"):
                    yield delta
            elif kind in ("response.output_item.added", "response.output_item.done"):
                item = event.get("item") or {}
                # What was added at first may be cut short; the done item is complete.
                if item.get("type") == "reasoning" and kind == "response.output_item.done":
                    reasoning[event.get("output_index", len(reasoning))] = item
                elif item.get("type") == "function_call":
                    call = calls.setdefault(
                        event.get("output_index", len(calls)),
                        {"id": "", "name": "", "arguments": ""},
                    )
                    call["id"] = item.get("call_id") or call["id"]
                    call["name"] = item.get("name") or call["name"]
                    # Once done, the item carries the arguments in full.
                    call["arguments"] = item.get("arguments") or call["arguments"]
            elif kind == "response.function_call_arguments.delta":
                if (call := calls.get(event.get("output_index", -1))) is not None:
                    call["arguments"] += event.get("delta") or ""
            elif kind == "error":
                raise LLMError(f"妯″瀷鍦ㄦ祦寮忚緭鍑轰腑鎶ラ敊: {event.get('message') or event}")
            elif kind == "response.failed":
                error = (event.get("response") or {}).get("error") or event
                raise LLMError(f"妯″瀷鐢熸垚澶辫触: {error}")
            elif kind == "response.incomplete":
                details = (event.get("response") or {}).get("incomplete_details")
                log.warning("the model's reply is incomplete: %s", details)
            if kind in ("response.completed", "response.incomplete"):
                usage = (event.get("response") or {}).get("usage")
                if isinstance(usage, dict):
                    # The input tokens are all of it, those read from the cache among them.
                    details = usage.get("input_tokens_details")
                    cached = details.get("cached_tokens") if isinstance(details, dict) else None
                    bill(
                        "tokens", usage.get("input_tokens"), usage.get("output_tokens"),
                        cached if isinstance(cached, int) else None,
                    )
        if reasoning:
            yield Reasoning({"items": [item for _, item in sorted(reasoning.items())]})
        for index, call in sorted(calls.items()):
            call_id = call["id"] or f"call_{index}"  # the tool result must name its call
            yield ToolCall(call_id, call["name"], parse_arguments(call["arguments"]))

    async def aclose(self) -> None:
        await self._client.aclose()


def _items(message: ChatMessage) -> list[dict[str, Any]]:
    if message.role == "tool":
        return [
            {
                "type": "function_call_output",
                "call_id": message.tool_call_id,
                "output": message.content,
            }
        ]
    # What the model thought as it made the calls goes first, as its service sent it.
    items: list[dict[str, Any]] = [
        item for r in message.reasoning for item in r.data.get("items", ())
    ]
    if message.image is not None:  # the user sent a picture: input parts, as this protocol takes them
        data, mime = message.image
        items.append(
            {
                "role": message.role,
                "content": [
                    *(
                        [{"type": "input_text", "text": message.content}]
                        if message.content
                        else []
                    ),
                    {
                        "type": "input_image",
                        "image_url": f"data:{mime};base64,{base64.b64encode(data).decode()}",
                    },
                ],
            }
        )
    elif message.content:
        items.append({"role": message.role, "content": message.content})
    items.extend(
        {
            "type": "function_call",
            "call_id": call.id,
            "name": call.name,
            "arguments": json.dumps(call.arguments or {}, ensure_ascii=False),
        }
        for call in message.tool_calls
    )
    return items
