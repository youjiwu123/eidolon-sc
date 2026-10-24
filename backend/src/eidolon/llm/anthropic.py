"""Anthropic Messages provider."""

import base64
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

DEFAULT_MAX_TOKENS = 16384
"""The Messages API requires a cap on the reply's length; a drafted character sheet runs to
several thousand tokens, and the model's reasoning, if it streams any, counts too."""

API_VERSION = "2023-06-01"

THINKING_DELTAS = {"thinking_delta": "thinking", "signature_delta": "signature"}
"""The deltas a thinking block streams in, each to the field of the block it adds to."""


class AnthropicLLM:
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
        self._url = base_url.rstrip("/") + "/messages"
        self._api_key = api_key
        self._model = model
        self._max_tokens = max_tokens or DEFAULT_MAX_TOKENS
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
            "max_tokens": self._max_tokens,
        }
        opening, rest = opening_system(messages)
        if system := _system(opening):
            body["system"] = system
        body["messages"] = _messages(rest)
        if self._effort == "none":
            body["thinking"] = {"type": "disabled"}
        elif self._effort is not None:
            # Not shown, what it thinks still goes back in a tool round by its signature,
            # and it starts to say something sooner.
            body["thinking"] = {"type": "adaptive", "display": "omitted"}
            body["output_config"] = {"effort": self._effort}
        if tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
            if forbid_calls:
                body["tool_choice"] = {"type": "none"}
        body = merge_extra_body(body, self._extra_body)
        calls: dict[int, dict[str, Any]] = {}  # content block index 鈫?the tool_use so far
        # Content block index 鈫?a thinking or redacted_thinking block as streamed so far,
        # which goes back as it came: its signature binds it to what came before.
        thinking: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}  # as the message started, updated as it goes
        headers = {"x-api-key": self._api_key, "anthropic-version": API_VERSION}
        async for event in stream_events(self._client, self._url, body, headers):
            kind = event.get("type")
            if kind == "message_start":
                usage.update((event.get("message") or {}).get("usage") or {})
            elif kind == "content_block_start":
                block = event.get("content_block") or {}
                if block.get("type") in ("thinking", "redacted_thinking"):
                    thinking[event.get("index", len(thinking))] = dict(block)
                elif block.get("type") == "tool_use":
                    calls[event.get("index", len(calls))] = {
                        "id": block.get("id") or "",
                        "name": block.get("name") or "",
                        "input": block.get("input") or {},
                        "json": "",
                    }
            elif kind == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta":
                    if text := delta.get("text"):
                        yield text
                elif delta.get("type") == "input_json_delta":
                    if (call := calls.get(event.get("index", -1))) is not None:
                        call["json"] += delta.get("partial_json") or ""
                elif field := THINKING_DELTAS.get(delta.get("type") or ""):
                    thought = thinking.get(event.get("index", -1))
                    if thought is not None and isinstance(part := delta.get(field), str):
                        thought[field] = (thought.get(field) or "") + part
            elif kind == "message_delta":
                usage.update(event.get("usage") or {})
                stop = (event.get("delta") or {}).get("stop_reason")
                if stop in ("max_tokens", "refusal"):
                    log.warning("the model stopped early: %s", stop)
            elif kind == "error":
                error = event.get("error") or {}
                raise LLMError(f"妯″瀷鍦ㄦ祦寮忚緭鍑轰腑鎶ラ敊: {error.get('message') or event}")
        if usage:
            cached = usage.get("cache_read_input_tokens")
            bill(
                "tokens", _input_tokens(usage), usage.get("output_tokens"),
                cached if isinstance(cached, int) else None,
            )
        if thinking:
            yield Reasoning({"blocks": [block for _, block in sorted(thinking.items())]})
        for index, call in sorted(calls.items()):
            call_id = call["id"] or f"call_{index}"  # the tool result must name its call
            # Without any input deltas, the input the block started with is all there is.
            arguments = (
                parse_arguments(call["json"])
                if call["json"]
                else call["input"] if isinstance(call["input"], dict) else None
            )
            yield ToolCall(call_id, call["name"], arguments)

    async def aclose(self) -> None:
        await self._client.aclose()


def _input_tokens(usage: dict[str, Any]) -> int | None:
    """All the input the message was billed for: the Messages API counts what it read from
    the prompt cache, and what it wrote to it, apart from the rest."""
    parts = [
        usage.get(name)
        for name in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    ]
    counted = [p for p in parts if isinstance(p, int)]
    return sum(counted) if counted else None


def _system(system: Sequence[ChatMessage]) -> str | list[dict[str, Any]]:
    """The system prompt: the messages joined by a blank line, or, with any marked for
    caching, a text block each, cached up to the end of those marked. A block's mark, not
    the request's: few other services speaking the protocol say they take that one."""
    if not any(m.cache_breakpoint for m in system):
        return "\n\n".join(m.content for m in system)
    blocks: list[dict[str, Any]] = []
    for message in system:
        if not message.content:  # the Messages API rejects empty text
            continue
        block: dict[str, Any] = {"type": "text", "text": message.content}
        blocks.append(_cached(block) if message.cache_breakpoint else block)
    return blocks


def _messages(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    """Messages API turns; the results of one round of tool calls go back together, in the
    user turn that follows the calls. A system message among them goes as a text block of its
    own first in the user turn after it: newer models alone take one past `system`, and few
    other services speaking the protocol. A message marked for caching is cached up to the
    end of its last block."""
    turns: list[dict[str, Any]] = []
    system_turn: dict[str, Any] | None = None  # the user turn a system message began
    for message in messages:
        if message.role == "tool":
            result = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id,
                "content": message.content,
            }
            last = turns[-1] if turns else None
            if last and last["role"] == "user" and isinstance(last["content"], list):
                last["content"].append(result)
            else:
                turns.append({"role": "user", "content": [result]})
            system_turn = None
            continue
        if message.tool_calls:
            # What the model thought as it made the calls goes first, as its service sent it.
            content: str | list[dict[str, Any]] = [
                block for r in message.reasoning for block in r.data.get("blocks", ())
            ]
            if message.content:
                content.append({"type": "text", "text": message.content})
            content.extend(
                {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments or {}}
                for c in message.tool_calls
            )
        elif message.image is not None:  # the user sent a picture: an image block, then the text
            data, mime = message.image
            content = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime,
                        "data": base64.b64encode(data).decode(),
                    },
                },
                *([{"type": "text", "text": message.content}] if message.content else []),
            ]
        elif message.content:
            content = message.content
        else:
            continue  # the Messages API rejects empty text
        if message.cache_breakpoint:
            *before, last = _blocks(content)
            content = [*before, _cached(last)]
        if system_turn is not None and message.role in ("user", "system"):
            system_turn["content"].extend(_blocks(content))
        elif message.role == "system":
            turns.append({"role": "user", "content": _blocks(content)})
        else:
            turns.append({"role": message.role, "content": content})
        system_turn = turns[-1] if message.role == "system" else None
    return turns


def _blocks(content: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": "text", "text": content}] if isinstance(content, str) else content


def _cached(block: dict[str, Any]) -> dict[str, Any]:
    """`block`, the prompt cached up to its end, for five minutes, as is the default."""
    return {**block, "cache_control": {"type": "ephemeral"}}
