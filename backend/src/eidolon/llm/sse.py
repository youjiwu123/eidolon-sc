"""Reading a model's streamed reply: every protocol sends server-sent events of JSON."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from eidolon.llm import LLMError


async def stream_events(
    client: httpx.AsyncClient, url: str, body: dict[str, Any], headers: dict[str, str]
) -> AsyncIterator[dict[str, Any]]:
    """POST `body` and yield the data of each event in the streamed reply. A failed request,
    or a reply that is not a stream at all, is an `LLMError`."""
    try:
        async with client.stream("POST", url, json=body, headers=headers) as res:
            if res.status_code >= 400:
                await res.aread()
                # The address too: a 404 is usually the wrong path, or a service that is not
                # the one meant, and which URL was asked tells the two apart at a glance.
                raise LLMError(f"模型返回 {res.status_code}（请求 {url}）: {res.text[:500]}")
            streamed = False
            other = ""  # what came instead of events, for the error message
            async for line in res.aiter_lines():
                if not line.startswith("data:"):
                    if not line.startswith(("event:", ":", "id:", "retry:")) and len(other) < 500:
                        other += line
                    continue
                streamed = True
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    return
                if not data:
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    raise LLMError(f"模型的流式输出无法解析（{len(data)} 字）") from None
                yield event
            if not streamed:
                raise LLMError(
                    "模型没有以流式输出返回（这个模型或服务可能不支持流式输出）: " + other[:500]
                )
    except httpx.HTTPError as e:
        raise LLMError(f"请求模型失败: {e!r}") from e


def parse_arguments(text: str) -> dict[str, Any] | None:
    """A tool call's arguments, or None when they are not a JSON object."""
    try:
        arguments = json.loads(text or "{}")
    except json.JSONDecodeError:
        return None
    return arguments if isinstance(arguments, dict) else None
