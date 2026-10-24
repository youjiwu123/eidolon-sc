"""Choosing each model slot's protocol, and the server refusing to start with a chat model
that cannot stream or call tools."""

import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from eidolon.__main__ import main
from eidolon.config import LLMProtocol, LLMSettings, Settings
from eidolon.llm import ChatMessage, LLMError
from eidolon.llm.openai_compat import OpenAICompatLLM
from eidolon.llm.providers import check_chat_llm, open_llm
from tests.conftest import CharacterConfig

SLOTS = ("CHAT", "BACKGROUND")
NAMES = (
    "BASE_URL", "API_KEY", "MODEL", "PROTOCOL", "MAX_TOKENS", "REASONING_EFFORT", "EXTRA_BODY",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for slot in SLOTS:
        for name in NAMES:
            monkeypatch.delenv(f"{slot}_LLM_{name}", raising=False)
    monkeypatch.delenv("CHAT_LLM_CALL_REASONING_EFFORT", raising=False)
    for name in ("SILICONFLOW_API_KEY", "AI_GATEWAY_API_KEY", "JEV_API_KEY", "JUDGE_PROVIDER"):
        monkeypatch.delenv(name, raising=False)


def set_chat_model(monkeypatch: pytest.MonkeyPatch, base_url: str, **extra: str) -> None:
    monkeypatch.setenv("CHAT_LLM_BASE_URL", base_url)
    monkeypatch.setenv("CHAT_LLM_API_KEY", "chat-key")
    monkeypatch.setenv("CHAT_LLM_MODEL", "chat-model")
    for name, value in extra.items():
        monkeypatch.setenv(name, value)


async def requested_path(settings: LLMSettings) -> str:
    """The path the provider built from `settings` sends a turn to."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, text="data: {}\n\n")

    llm = open_llm(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    async for _ in llm.stream([ChatMessage("user", "你好")]):
        pass
    await llm.aclose()
    return paths[0]


async def test_each_model_slot_chooses_its_own_protocol(monkeypatch: pytest.MonkeyPatch):
    set_chat_model(
        monkeypatch,
        "https://chat.example/v1",
        CHAT_LLM_PROTOCOL="anthropic",
        BACKGROUND_LLM_PROTOCOL="responses",
        BACKGROUND_LLM_BASE_URL="https://background.example/v1",
    )

    settings = Settings.from_env()

    assert settings.chat_llm == LLMSettings(
        "https://chat.example/v1", "chat-key", "chat-model", protocol="anthropic"
    )
    background = settings.background_llm
    assert background is not None
    assert background == LLMSettings(
        "https://background.example/v1", "chat-key", "chat-model", protocol="responses"
    )
    assert await requested_path(settings.chat_llm) == "/v1/messages"
    assert await requested_path(background) == "/v1/responses"


async def test_the_protocol_defaults_to_chat_completions_and_the_background_follows_the_chat(
    monkeypatch: pytest.MonkeyPatch,
):
    set_chat_model(monkeypatch, "https://chat.example/v1")

    settings = Settings.from_env()
    assert await requested_path(settings.chat_llm) == "/v1/chat/completions"

    # The background model follows the chat model's protocol, but not its reply cap: a cap
    # that suits the chat could cut a drafted sheet short.
    monkeypatch.setenv("CHAT_LLM_PROTOCOL", "responses")
    monkeypatch.setenv("CHAT_LLM_MAX_TOKENS", "2048")
    settings = Settings.from_env()
    assert settings.chat_llm.max_tokens == 2048
    assert settings.background_llm == LLMSettings(
        "https://chat.example/v1", "chat-key", "chat-model", "responses"
    )


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("CHAT_LLM_PROTOCOL", "openai", "CHAT_LLM_PROTOCOL.*chat-completions"),
        ("BACKGROUND_LLM_PROTOCOL", "claude", "BACKGROUND_LLM_PROTOCOL.*anthropic"),
        ("CHAT_LLM_MAX_TOKENS", "lots", "CHAT_LLM_MAX_TOKENS.*正整数"),
        ("BACKGROUND_LLM_MAX_TOKENS", "²", "BACKGROUND_LLM_MAX_TOKENS.*正整数"),
        ("CHAT_LLM_REASONING_EFFORT", "off", "CHAT_LLM_REASONING_EFFORT.*none、minimal"),
        ("CHAT_LLM_CALL_REASONING_EFFORT", "off", "CHAT_LLM_CALL_REASONING_EFFORT.*none、minimal"),
        ("CHAT_LLM_EXTRA_BODY", "{enable_thinking: false}", "CHAT_LLM_EXTRA_BODY.*合法的 JSON"),
        ("BACKGROUND_LLM_EXTRA_BODY", '{"model": "x"}', "BACKGROUND_LLM_EXTRA_BODY.*「model」"),
    ],
)
def test_a_misconfigured_model_is_reported(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, message: str
):
    set_chat_model(monkeypatch, "https://chat.example/v1", **{name: value})

    with pytest.raises(RuntimeError, match=message):
        Settings.from_env()


def test_each_slot_has_its_own_effort_and_extra_body(monkeypatch: pytest.MonkeyPatch):
    set_chat_model(
        monkeypatch,
        "https://chat.example/v1",
        CHAT_LLM_REASONING_EFFORT="none",
        CHAT_LLM_EXTRA_BODY='{"enable_thinking": false}',
        BACKGROUND_LLM_EXTRA_BODY='{"thinking": {"type": "disabled"}}',
    )

    settings = Settings.from_env()

    assert settings.chat_llm.reasoning_effort == "none"
    assert settings.chat_llm.extra_body == {"enable_thinking": False}
    # Neither follows the chat model's: what suits a call may not suit drafting a sheet.
    assert settings.background_llm.reasoning_effort is None
    assert settings.background_llm.extra_body == {"thinking": {"type": "disabled"}}


def test_an_effort_the_anthropic_protocol_has_no_way_to_write_is_reported(
    monkeypatch: pytest.MonkeyPatch,
):
    set_chat_model(
        monkeypatch,
        "https://chat.example/v1",
        BACKGROUND_LLM_PROTOCOL="anthropic",
        BACKGROUND_LLM_REASONING_EFFORT="minimal",
    )

    with pytest.raises(RuntimeError, match="BACKGROUND_LLM_REASONING_EFFORT.*Anthropic"):
        Settings.from_env()


def test_the_chat_model_has_an_effort_of_its_own_for_calls(monkeypatch: pytest.MonkeyPatch):
    set_chat_model(
        monkeypatch,
        "https://chat.example/v1",
        CHAT_LLM_REASONING_EFFORT="high",
        CHAT_LLM_CALL_REASONING_EFFORT="none",
    )

    settings = Settings.from_env()

    assert settings.chat_model.text.reasoning_effort == "high"
    assert settings.chat_model.call == replace(settings.chat_llm, reasoning_effort="none")
    assert settings.background_llm.reasoning_effort is None


def test_a_call_effort_the_chat_models_protocol_has_no_way_to_write_is_reported(
    monkeypatch: pytest.MonkeyPatch,
):
    set_chat_model(
        monkeypatch,
        "https://chat.example/v1",
        CHAT_LLM_PROTOCOL="anthropic",
        CHAT_LLM_CALL_REASONING_EFFORT="minimal",
    )

    with pytest.raises(RuntimeError, match="CHAT_LLM_CALL_REASONING_EFFORT.*Anthropic"):
        Settings.from_env()


# A stand-in model service on a local port, for the server's own startup to talk to.

Handler = Callable[[dict], tuple[int, str, str]]
"""Given the request body, the status, content type and body of the reply."""


@pytest.fixture
def model_service() -> Iterator[Callable[[Handler], str]]:
    servers: list[ThreadingHTTPServer] = []

    def start(handle: Handler) -> str:
        class Serve(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                status, content_type, reply = handle(body)
                data = reply.encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Serve)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}/v1"

    yield start
    for server in servers:
        server.shutdown()


STREAMED = "data: " + json.dumps({"choices": [{"delta": {"content": "<ja>はい</ja>"}}]}) + "\n\n"
NOT_STREAMED = json.dumps({"choices": [{"message": {"content": "<ja>はい</ja>"}}]})


def serve(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    characters: CharacterConfig,
    base_url: str,
) -> tuple[int, str]:
    set_chat_model(monkeypatch, base_url)
    monkeypatch.setenv("EIDOLON_DATABASE_PATH", str(tmp_path / "eidolon.db"))
    monkeypatch.setenv("EIDOLON_CHARACTERS_PATH", str(characters.path))
    code = main(["serve"])
    return code, capsys.readouterr().err


def test_the_server_does_not_start_with_a_chat_model_that_does_not_stream(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    characters: CharacterConfig,
    model_service: Callable[[Handler], str],
):
    url = model_service(lambda _: (200, "application/json", NOT_STREAMED))

    code, err = serve(monkeypatch, capsys, tmp_path, characters, url)

    assert code == 1
    assert "流式输出" in err


def test_the_server_does_not_start_with_a_chat_model_that_cannot_call_tools(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    characters: CharacterConfig,
    model_service: Callable[[Handler], str],
):
    def handle(body: dict) -> tuple[int, str, str]:
        if "tools" in body:
            error = {"error": {"message": "this model does not support tools"}}
            return 400, "application/json", json.dumps(error)
        return 200, "text/event-stream", STREAMED

    url = model_service(handle)

    code, err = serve(monkeypatch, capsys, tmp_path, characters, url)

    assert code == 1
    assert "不支持工具调用" in err
    assert "does not support tools" in err


def test_the_server_does_not_start_when_the_chat_model_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    characters: CharacterConfig,
    model_service: Callable[[Handler], str],
):
    url = model_service(lambda _: (401, "application/json", '{"error":"invalid api key"}'))

    code, err = serve(monkeypatch, capsys, tmp_path, characters, url)

    assert code == 1
    assert "invalid api key" in err


# The check passes for a model that streams and accepts tools, whether or not it calls one.

PASSING: dict[LLMProtocol, str] = {
    "chat-completions": STREAMED,
    "responses": 'data: {"type":"response.output_text.delta","delta":"はい"}\n\n',
    "anthropic": (
        'data: {"type":"content_block_start","index":0,"content_block":'
        '{"type":"tool_use","id":"t1","name":"ping","input":{}}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    ),
}


@pytest.mark.parametrize("protocol", PASSING)
async def test_a_model_that_streams_and_takes_tools_passes_the_check(protocol: LLMProtocol):
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, text=PASSING[protocol])

    llm = open_llm(
        LLMSettings("https://llm.example/v1", "k", "m", protocol),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    await check_chat_llm(llm)

    body = bodies[0]
    assert body["stream"] is True
    assert body["tools"]
    # A model that does not call the tool is asked just the once; the Anthropic stream
    # above calls it, and is asked again with the result.
    assert len(bodies) == (2 if protocol == "anthropic" else 1)


async def test_the_check_opens_as_a_turn_does_with_a_part_marked_for_caching():
    """A service that would refuse a turn's prompt for the mark fails the check, not every
    turn after it."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if any("cache_control" in block for block in body.get("system") or []):
            return httpx.Response(
                400, json={"error": {"message": "system.0.cache_control: Extra inputs are not permitted"}}
            )
        return httpx.Response(200, text=PASSING["anthropic"])

    llm = open_llm(
        LLMSettings("https://llm.example/v1", "k", "m", "anthropic"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(LLMError, match="cache_control: Extra inputs are not permitted"):
        await check_chat_llm(llm)

    [block] = bodies[0]["system"]
    assert block["cache_control"] == {"type": "ephemeral"}


def mid_conversation_system_refused(bodies: list[dict]) -> OpenAICompatLLM:
    """A Chat Completions service that takes system messages only at the start."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if any(m["role"] == "system" for m in body["messages"][1:]):
            return httpx.Response(
                400, json={"error": {"message": "system messages must come first"}}
            )
        return httpx.Response(200, text=STREAMED)

    return OpenAICompatLLM(
        "https://llm.example/v1", "k", "m", reasoning_effort="high",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_a_service_refusing_a_system_message_mid_conversation_as_a_turn_has_is_told_so():
    """Whatever the effort: answering without the message, the model took it."""
    bodies: list[dict] = []

    with pytest.raises(LLMError) as raised:
        await check_chat_llm(mid_conversation_system_refused(bodies), "high")

    assert [m["role"] for m in bodies[0]["messages"]] == [
        "system", "user", "assistant", "system", "user"
    ]
    assert str(raised.value).startswith("对话模型不接受对话里的缓存标记或中途的系统消息")
    assert "system messages must come first" in str(raised.value)


async def test_a_passing_failure_is_not_blamed_on_the_system_message_mid_conversation():
    replies = iter([
        httpx.Response(429, json={"error": "rate limited"}),
        httpx.Response(429, json={"error": "rate limited"}),
        httpx.Response(200, text=STREAMED),
        httpx.Response(200, text=STREAMED),
        httpx.Response(200, text=STREAMED),
    ])
    llm = OpenAICompatLLM(
        "https://llm.example/v1", "k", "m",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: next(replies))),
    )

    await check_chat_llm(llm)


async def test_the_check_reports_what_failed_when_both_requests_fail():
    llm = OpenAICompatLLM(
        "https://llm.example/v1", "k", "m",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"ok": True}))
        ),
    )

    with pytest.raises(LLMError, match="流式输出和工具调用"):
        await check_chat_llm(llm)


async def test_a_passing_failure_is_not_blamed_on_tool_calls():
    replies = iter([
        httpx.Response(429, json={"error": "rate limited"}),
        httpx.Response(200, text=STREAMED),
        httpx.Response(200, text=STREAMED),
    ])
    llm = OpenAICompatLLM(
        "https://llm.example/v1", "k", "m",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: next(replies))),
    )

    await check_chat_llm(llm)


# A model that calls the tool is checked through the round after it, where models that
# want their reasoning back fail.

PING_CALL = "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [{
    "index": 0, "id": "call_p", "type": "function",
    "extra_content": {"google": {"thought_signature": "c2ln"}},
    "function": {"name": "ping", "arguments": "{}"},
}]}}]}) + "\n\n"


def thinking_model(
    bodies: list[dict], second_round: httpx.Response | None = None
) -> OpenAICompatLLM:
    """A model that calls the tool whenever it may, and answers its result with
    `second_round`, if given, else with text."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if any(m["role"] == "tool" for m in body["messages"]):
            return second_round or httpx.Response(200, text=STREAMED)
        return httpx.Response(200, text=PING_CALL if body.get("tools") else STREAMED)

    return OpenAICompatLLM(
        "https://llm.example/v1", "k", "m",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_a_call_to_the_tool_is_answered_and_the_model_asked_again():
    bodies: list[dict] = []

    await check_chat_llm(thinking_model(bodies))

    first, second = bodies
    call, result = second["messages"][len(first["messages"]):]
    assert call["role"] == "assistant"
    assert [c["id"] for c in call["tool_calls"]] == ["call_p"]
    assert (result["role"], result["tool_call_id"]) == ("tool", "call_p")
    assert result["content"]
    # The same tools, which the model may not call again.
    assert second["tools"] == first["tools"]
    assert second["tool_choice"] == "none"


async def test_a_round_after_the_call_that_fails_fails_the_check():
    bodies: list[dict] = []
    refused = httpx.Response(
        400, json={"error": {"message": "Function call is missing a thought_signature"}}
    )

    with pytest.raises(LLMError, match="收到工具结果后.*thought_signature") as raised:
        await check_chat_llm(thinking_model(bodies, refused))

    # The model can call tools, so it is not told it cannot, nor asked without them.
    assert "不支持工具调用" not in str(raised.value)
    assert len(bodies) == 2


# A check with a reasoning effort set puts a failure down to the effort, in the service's
# own words.

REFUSED_EFFORT = httpx.Response(
    400, json={"error": {"message": "Unsupported value: 'xhigh' is not supported"}}
)


async def test_a_refused_effort_is_named_ahead_of_what_the_service_said():
    llm = OpenAICompatLLM(
        "https://llm.example/v1", "k", "m", reasoning_effort="xhigh",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: REFUSED_EFFORT)),
    )

    with pytest.raises(LLMError) as raised:
        await check_chat_llm(llm, "xhigh")

    assert str(raised.value).startswith(
        "思考强度『极高』被服务拒绝：模型返回 400（请求 https://llm.example/v1/chat/completions）: "
        '{"error":{"message":"Unsupported value: \'xhigh\' is not supported"}}'
    )


async def test_an_effort_refused_in_the_round_after_the_call_is_named_too():
    bodies: list[dict] = []

    with pytest.raises(LLMError, match="^思考强度『关闭』被服务拒绝：模型返回 400"):
        await check_chat_llm(thinking_model(bodies, REFUSED_EFFORT), "none")


async def test_a_model_that_cannot_call_tools_is_told_so_whatever_its_effort():
    def handler(request: httpx.Request) -> httpx.Response:
        if "tools" in json.loads(request.content):
            return httpx.Response(400, json={"error": "tools are not supported"})
        return httpx.Response(200, text=STREAMED)

    llm = OpenAICompatLLM(
        "https://llm.example/v1", "k", "m", reasoning_effort="high",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    # Answering without tools, it took the effort: the tools are what it refuses.
    with pytest.raises(LLMError, match="^对话模型不支持工具调用.*tools are not supported"):
        await check_chat_llm(llm, "high")
