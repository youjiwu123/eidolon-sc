"""OpenAI-compatible Chat Completions provider."""

import base64
import json
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


class OpenAICompatLLM:
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
        self._url = base_url.rstrip("/") + "/chat/completions"
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
            # The tokens it is billed for come in a last chunk, sent only when asked for.
            "stream_options": {"include_usage": True},
            "messages": _messages(messages),
        }
        if self._max_tokens is not None:
            body["max_tokens"] = self._max_tokens
        if self._effort is not None:
            body["reasoning_effort"] = self._effort
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
            if forbid_calls:
                body["tool_choice"] = "none"
        body = merge_extra_body(body, self._extra_body)
        calls: dict[int, dict[str, Any]] = {}  # index 鈫?id, name and arguments so far
        reasoning = ""
        usage: dict[str, Any] | None = None
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async for event in stream_events(self._client, self._url, body, headers):
            if event.get("error"):
                raise LLMError(f"妯″瀷鍦ㄦ祦寮忚緭鍑轰腑鎶ラ敊: {event['error']}")
            if isinstance(event.get("usage"), dict):  # some services send it as it grows
                usage = event["usage"]
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                if content := delta.get("content"):
                    yield content
                if isinstance(part := delta.get("reasoning_content"), str):
                    reasoning += part
                for fragment in delta.get("tool_calls") or []:
                    call = calls.setdefault(
                        fragment.get("index", 0), {"id": "", "name": "", "arguments": ""}
                    )
                    call["id"] = fragment.get("id") or call["id"]
                    function = fragment.get("function") or {}
                    call["name"] = function.get("name") or call["name"]
                    arguments = function.get("arguments") or ""
                    # A few providers send the arguments as an object rather than JSON text.
                    if not isinstance(arguments, str):
                        arguments = json.dumps(arguments, ensure_ascii=False)
                    call["arguments"] += arguments
                    # Gemini's thought signature, which it wants back with the call.
                    if isinstance(extra := fragment.get("extra_content"), dict):
                        call["extra_content"] = extra
        if usage is not None:
            bill(
                "tokens", usage.get("prompt_tokens"), usage.get("completion_tokens"),
                _cache_hits(usage),
            )
        assembled = [
            (call["id"] or f"call_{index}", call)  # the tool result must name its call
            for index, call in sorted(calls.items())
        ]
        to_send_back: dict[str, Any] = {}
        if reasoning:
            to_send_back["reasoning_content"] = reasoning
        if extras := {i: c["extra_content"] for i, c in assembled if "extra_content" in c}:
            to_send_back["extra_content"] = extras  # by call id
        if to_send_back:
            yield Reasoning(to_send_back)
        for call_id, call in assembled:
            yield ToolCall(call_id, call["name"], parse_arguments(call["arguments"]))

    async def aclose(self) -> None:
        await self._client.aclose()


def _cache_hits(usage: dict[str, Any]) -> int | None:
    """How many of the prompt tokens, which are all of the input, the provider read from its
    cache: where most services say, else under DeepSeek's own name, else Kimi's older one."""
    details = usage.get("prompt_tokens_details")
    for told in (
        details.get("cached_tokens") if isinstance(details, dict) else None,
        usage.get("prompt_cache_hit_tokens"),
        usage.get("cached_tokens"),
    ):
        if isinstance(told, int):
            return told
    return None


def _messages(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    """The messages as Chat Completions takes them; the system messages they open with go
    as one, joined by a blank line, as every service takes them."""
    opening, rest = opening_system(messages)
    sent = [_message(m) for m in rest]
    if opening:
        system = "\n\n".join(m.content for m in opening)
        sent.insert(0, {"role": "system", "content": system})
    return sent


def _message(message: ChatMessage) -> dict[str, Any]:
    if message.role == "tool":
        return {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content}
    if message.tool_calls:
        # What the model thought as it made the calls goes back as its service sent it.
        reasoning = "".join(r.data.get("reasoning_content", "") for r in message.reasoning)
        extras = {
            call_id: extra
            for r in message.reasoning
            for call_id, extra in r.data.get("extra_content", {}).items()
        }
        assistant: dict[str, Any] = {"role": "assistant", "content": message.content or None}
        if reasoning:
            assistant["reasoning_content"] = reasoning
        assistant["tool_calls"] = [_call(call, extras.get(call.id)) for call in message.tool_calls]
        return assistant
    if message.image is not None:  # the user sent a picture: parts, as this protocol takes them
        data, mime = message.image
        return {
            "role": message.role,
            "content": [
                *(
                    [{"type": "text", "text": message.content}]
                    if message.content
                    else []
                ),
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"},
                },
            ],
        }
    return {"role": message.role, "content": message.content}


def _call(call: ToolCall, extra: dict[str, Any] | None) -> dict[str, Any]:
    sent: dict[str, Any] = {
        "id": call.id,
        "type": "function",
        "function": {
            "name": call.name,
            "arguments": json.dumps(call.arguments or {}, ensure_ascii=False),
        },
    }
    if extra is not None:
        sent["extra_content"] = extra
    return sent

