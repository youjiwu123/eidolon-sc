"""Speech recognition on Alibaba Cloud Model Studio (阿里云百炼), over DashScope's realtime
recognition WebSocket protocol: a Pipecat STT service for calls, and trying a key.

The call takes turns on what Soniox gives it (see `call.CallTurns`): an utterance starts
with its first words, ends a little after its last, and no words heard is no utterance.
DashScope is made to give the same. A task opens with `run-task` and, once
`task-started`, is sent the audio, 16 kHz 16-bit mono PCM a packet of 100 ms at a time,
and sends back each sentence as far as it has heard it (`result-generated`), the whole of
it so far each time, until the sentence ends (`sentence_end`). The first of a sentence
with words in it (not the empty one DashScope sends as soon as it hears a voice, nor its
heartbeats) starts the utterance; the sentence's end ends it, transcribed unless it came
to no words (a cough, say). Near-field voice detection (`vad_model`) is what keeps coughs
from being heard as sentences at all; the model and parameters are those measured in
ticket bailian-stt 01. The call's hot words (`hot_words`) go with the task as its
`vocabulary`, made for the task alone: no hot word list is kept on anyone's account.

DashScope ends a task sent nothing for 23 seconds, so while no audio comes (the
microphone muted, say) the service sends silence itself.

A key works only in its region. Beijing is tried first and, if it refuses the key at the
handshake, Singapore; a key whose region is known goes straight there. A key refused in
both, or a task that fails, leaves the service unusable, which ends the call.
"""

import asyncio
import json
import logging
import unicodedata
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from functools import partial
from http import HTTPStatus
from typing import Any

from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InterimTranscriptionFrame,
    ProposedUserStartedSpeakingFrame,
    ProposedUserStoppedSpeakingFrame,
    StartFrame,
    STTMetadataFrame,
    TranscriptionFrame,
)
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import WebsocketSTTService
from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies
from pipecat.utils.time import time_now_iso8601
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException
from websockets.protocol import State

from eidolon.config import BailianRegion
from eidolon.proxy import ProxiedWebsocket

log = logging.getLogger(__name__)

REGION_URLS: dict[BailianRegion, str] = {
    "beijing": "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
    "singapore": "wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference",
}
"""Where each region's recognition is, in the order a key is tried in. The old domains,
without a WorkspaceId; Singapore's is to be retired some day (ADR-0010)."""

REGION_NAMES: dict[BailianRegion, str] = {"beijing": "北京", "singapore": "新加坡"}

SAMPLE_RATE = 16000
PACKET_BYTES = SAMPLE_RATE * 2 // 10
"""100 ms of audio, as DashScope is sent it."""

PARAMETERS: dict[str, Any] = {
    "format": "pcm",
    "sample_rate": SAMPLE_RATE,
    "vad_model": "near_meeting_16k",
    "language_hints": ["zh", "ja"],
    "heartbeat": True,
}
"""Each task's: near-field voice detection, which hears no sentence in a cough, Chinese and
Japanese, and heartbeats. How long a pause ends a sentence is DashScope's default (1.3 s),
which has it end about 1.0–1.2 s after the last word."""

HOT_WORD_WEIGHT = 4
"""How strongly each hot word is listened out for, of 1 to 5: what DashScope recommends.
Higher has words that merely sound alike heard as the hot word."""

LATIN_HOT_WORD_WEIGHT = 50
"""How strongly a hot word in Latin letters (L'Antica, noctchill) is listened out for: as a
super hot word, of which DashScope takes at most `MAX_LATIN_HOT_WORDS`. Said in Chinese, at
`HOT_WORD_WEIGHT` they came out as transliterations (L'Antica as 梁缇卡) whether or not the
language hints include English; at 50 they were heard right and the Chinese names and
ordinary sentences (田中, 巡逻) the same (ticket retrieval-and-hotwords 04). Numbers (283)
stay at `HOT_WORD_WEIGHT`, so that ordinary numbers are not heard as them."""

MAX_LATIN_HOT_WORDS = 50
"""The most super hot words DashScope takes; those past it are listened out for at
`HOT_WORD_WEIGHT`."""

FOLLOW_ON_TIMEOUT = 3.0
"""How long, in seconds since the user was last heard, a follow-on may take to be
transcribed (see `call.Hearing`). A sentence ended at most 1.61 s after the last of it was
heard, as measured; this is about twice that."""

KEEPALIVE_TIMEOUT = 1.0
"""How long, in seconds, the service may go without audio to send before it sends 100 ms
of silence itself, well within the 23 s DashScope waits."""

START_TIMEOUT = 10.0
"""How long, in seconds, a task may take to start; DashScope starts one in about 0.3 s."""

FINISH_TIMEOUT = 3.0
"""How long, in seconds, a task told to finish may take to, sending what is left of the
sentence it was hearing; after that it is dropped."""


class BailianCheckFailed(Exception):
    """Trying a key failed; the message says why, for whoever filled it in."""


class _Refused(Exception):
    """DashScope will not recognize speech on this key with this model: neither region
    takes the key, or the task fails to start. Trying again would not help."""


Connect = Callable[..., Awaitable[ClientConnection]]


async def find_region(api_key: str, model: str, proxy: str | None = None) -> BailianRegion:
    """The region `api_key` works in, found by starting a task with `model` there and
    finishing it at once, with no audio sent, through `proxy` or direct. Raises
    BailianCheckFailed saying why, if the key works in neither, the model is not enabled
    for it, or DashScope is out of reach."""
    try:
        websocket, task_id, region = await _start_task(
            partial(connect, proxy=proxy), api_key, model, list(REGION_URLS)
        )
    except _Refused as e:
        raise BailianCheckFailed(str(e)) from None
    except (OSError, TimeoutError, WebSocketException) as e:
        raise BailianCheckFailed(f"连不上阿里云百炼：{e!r}") from None
    try:  # the key works: how the task ends is no matter
        await websocket.send(_finish_task_message(task_id))
        async with asyncio.timeout(FINISH_TIMEOUT):
            async for message in websocket:
                if _event(message).get("header", {}).get("event") in ("task-finished", "task-failed"):
                    break
    except (TimeoutError, ConnectionClosed):
        pass
    finally:
        await websocket.close()
    return region


class BailianSTTService(ProxiedWebsocket, WebsocketSTTService):
    """Alibaba Cloud Model Studio, hearing the user in one call on `api_key` with `model`:
    in `region`, if known, else where the key is found to work; listening out for
    `hot_words`; connecting through the proxy `proxy` gives as each connection is opened."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        region: BailianRegion | None,
        hot_words: Sequence[str] = (),
        proxy: Callable[[], str | None] = lambda: None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            keepalive_timeout=KEEPALIVE_TIMEOUT,
            keepalive_interval=KEEPALIVE_TIMEOUT,
            settings=STTSettings(model=model, language=None),
            **kwargs,
        )
        self._proxy = proxy
        self._api_key = api_key
        self._model = model
        self._region: BailianRegion | None = region
        self._hot_words = tuple(hot_words)
        self._task_id: str | None = None
        self._finished = asyncio.Event()  # the task has finished, told to
        self._receive_task: asyncio.Task | None = None
        self._resampler = create_stream_resampler()
        self._held = bytearray()  # audio not yet sent, less than a packet of it
        self._speaking = False  # an utterance has started and not yet ended

    def service_metadata_frame(self) -> STTMetadataFrame:
        """DashScope says where utterances start and end, as Soniox does."""
        frame = super().service_metadata_frame()
        frame.user_turn_strategies = ExternalUserTurnStrategies()
        return frame

    async def start(self, frame: StartFrame) -> None:
        """Start the call's task; if it cannot be, the call cannot be heard."""
        await super().start(frame)
        await self._connect()
        if self._websocket is None and self.is_usable:
            await self.push_error(
                "Alibaba Cloud Model Studio could not be reached", force_treat_as_permanent=True
            )

    async def stop(self, frame: EndFrame) -> None:
        await self._finish_task()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame) -> None:
        await self._finish_task(wait=False)
        await super().cancel(frame)

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        if self.sample_rate != SAMPLE_RATE:
            audio = await self._resampler.resample(audio, self.sample_rate, SAMPLE_RATE)
        self._held += audio
        while len(self._held) >= PACKET_BYTES:
            packet = bytes(self._held[:PACKET_BYTES])
            del self._held[:PACKET_BYTES]
            await self._send_audio(packet)
        yield None

    async def _send_keepalive(self, silence: bytes) -> None:
        """Send what audio is held, made up to a packet with silence."""
        packet = bytes(self._held).ljust(PACKET_BYTES, b"\0")
        self._held.clear()
        await self._send_audio(packet)

    async def _send_audio(self, packet: bytes) -> None:
        if self._websocket is not None and self._websocket.state is State.OPEN:
            try:
                await self._websocket.send(packet)
            except Exception as e:
                log.warning("could not send audio to Alibaba Cloud Model Studio: %s", e)

    async def _connect(self) -> None:
        await self._connect_websocket()
        await super()._connect()
        if self._websocket is not None and self._receive_task is None:
            self._receive_task = self.create_task(self._receive_task_handler(self._report_error))

    async def _disconnect(self) -> None:
        await super()._disconnect()
        if self._receive_task is not None:
            await self.cancel_task(self._receive_task)
            self._receive_task = None
        await self._disconnect_websocket()

    async def _connect_websocket(self) -> None:
        """Start a task, in the key's region if known, else where the key works, and
        remember the region for reconnecting."""
        if self._websocket is not None and self._websocket.state is State.OPEN:
            return
        regions: list[BailianRegion] = [self._region] if self._region else list(REGION_URLS)
        try:
            websocket, self._task_id, self._region = await _start_task(
                self._websocket_connect, self._api_key, self._model, regions, self._hot_words
            )
        except _Refused as e:
            await self.push_error(
                f"Alibaba Cloud Model Studio refused the task: {e}", force_treat_as_permanent=True
            )
            return
        except Exception as e:
            await self.push_error(f"Unable to connect to Alibaba Cloud Model Studio: {e}", e)
            return
        self._websocket = websocket
        self._finished = asyncio.Event()
        await self._end_utterance()  # one the lost connection left open, reconnecting
        await self._call_event_handler("on_connected")

    async def _disconnect_websocket(self) -> None:
        try:
            if self._websocket is not None:
                await self._websocket.close()
        except Exception as e:
            log.warning("could not close the connection to Alibaba Cloud Model Studio: %s", e)
        finally:
            self._websocket = None
            self._task_id = None
            await self._call_event_handler("on_disconnected")

    async def _finish_task(self, wait: bool = True) -> None:
        """Send what audio is held, and tell the task to finish; with `wait`, wait until it
        has, for the rest of the sentence it was hearing, if any."""
        websocket, task_id = self._websocket, self._task_id
        if websocket is None or task_id is None or websocket.state is not State.OPEN:
            return
        await self._cancel_keepalive_task()  # no audio may follow
        self._disconnecting = True  # DashScope closes the connection once it has finished
        try:
            if self._held:
                await websocket.send(bytes(self._held))
                self._held.clear()
            await websocket.send(_finish_task_message(task_id))
            if wait:
                await asyncio.wait_for(self._finished.wait(), FINISH_TIMEOUT)
        except (ConnectionClosed, TimeoutError):
            pass

    async def _receive_messages(self) -> None:
        assert self._websocket is not None
        async for message in self._websocket:
            event = _event(message)
            header = event.get("header", {})
            match header.get("event"):
                case "result-generated":
                    sentence = event.get("payload", {}).get("output", {}).get("sentence")
                    if isinstance(sentence, dict):
                        await self._heard(sentence)
                case "task-finished":
                    self._finished.set()
                    return
                case "task-failed":
                    await self.push_error(
                        f"Alibaba Cloud Model Studio's task failed: {_failure(header)}",
                        force_treat_as_permanent=True,
                    )
                    return

    async def _heard(self, sentence: dict) -> None:
        """Take a sentence as far as DashScope has heard it."""
        if sentence.get("heartbeat"):
            return
        text = sentence.get("text") or ""
        if sentence.get("sentence_end"):
            if _has_words(text):
                await self._start_utterance()
                await self.emit_stt_usage_metrics()
                await self.push_frame(
                    TranscriptionFrame(text, self._user_id, time_now_iso8601(), finalized=True)
                )
            await self._end_utterance()
        elif _has_words(text):
            await self._start_utterance()
            await self.push_frame(InterimTranscriptionFrame(text, self._user_id, time_now_iso8601()))

    async def _start_utterance(self) -> None:
        if not self._speaking:
            self._speaking = True
            await self.broadcast_frame(ProposedUserStartedSpeakingFrame)

    async def _end_utterance(self) -> None:
        if self._speaking:
            self._speaking = False
            await self.broadcast_frame(ProposedUserStoppedSpeakingFrame)


async def _start_task(
    connect: Connect,
    api_key: str,
    model: str,
    regions: Sequence[BailianRegion],
    hot_words: Sequence[str] = (),
) -> tuple[ClientConnection, str, BailianRegion]:
    """A connection with a recognition task started on it, listening out for `hot_words`,
    the task's id, and the region, the first of `regions` whose handshake takes `api_key`.
    Raises _Refused if none takes it or the task fails to start."""
    refused: list[tuple[BailianRegion, int]] = []
    for region in regions:
        try:
            websocket = await connect(
                REGION_URLS[region], additional_headers={"Authorization": f"bearer {api_key}"}
            )
        except InvalidStatus as e:
            status = e.response.status_code
            if status not in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
                raise
            refused.append((region, status))
            continue
        try:
            task_id = await _run_task(websocket, model, hot_words)
        except BaseException:
            await websocket.close()
            raise
        return websocket, task_id, region
    names = "、".join(REGION_NAMES[region] for region, _ in refused)
    statuses = "、".join(sorted({str(status) for _, status in refused}))
    raise _Refused(f"key 无效，{names}地域都不接受（HTTP {statuses}）")


async def _run_task(websocket: ClientConnection, model: str, hot_words: Sequence[str]) -> str:
    """Start a recognition task with `model`, listening out for `hot_words`; its id, once
    it has started."""
    task_id = uuid.uuid4().hex
    parameters = dict(PARAMETERS)
    if hot_words:
        parameters["vocabulary"] = _vocabulary(hot_words)
    await websocket.send(json.dumps({
        "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
        "payload": {
            "task_group": "audio",
            "task": "asr",
            "function": "recognition",
            "model": model,
            "parameters": parameters,
            "input": {},
        },
    }))
    async with asyncio.timeout(START_TIMEOUT):
        while True:
            header = _event(await websocket.recv()).get("header", {})
            if header.get("event") == "task-started":
                return task_id
            if header.get("event") == "task-failed":
                raise _Refused(
                    f"启动不了 {model} 的识别任务（{_failure(header)}）；"
                    f"如果还没开通这个模型，去阿里云百炼控制台开通 {model}"
                )


def _vocabulary(hot_words: Sequence[str]) -> dict[str, int]:
    """The task's `vocabulary`: each of `hot_words` with its weight, the first
    `MAX_LATIN_HOT_WORDS` in Latin letters at `LATIN_HOT_WORD_WEIGHT`."""
    latin = [w for w in hot_words if w.isascii() and any(c.isalpha() for c in w)]
    super_words = set(latin[:MAX_LATIN_HOT_WORDS])
    return {
        w: LATIN_HOT_WORD_WEIGHT if w in super_words else HOT_WORD_WEIGHT for w in hot_words
    }


def _finish_task_message(task_id: str) -> str:
    return json.dumps({
        "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
        "payload": {"input": {}},
    })


def _failure(header: dict) -> str:
    """Why a task failed, as its `task-failed` says."""
    return f"{header.get('error_code')}：{header.get('error_message')}"


def _event(message: str | bytes) -> dict:
    """A DashScope event; anything else is taken as an event of no kind."""
    try:
        event = json.loads(message)
    except ValueError:
        return {}
    return event if isinstance(event, dict) else {}


def _has_words(text: str) -> bool:
    """Whether `text` has any letter or digit in it: more than punctuation and spaces."""
    return any(unicodedata.category(c)[0] in "LN" for c in text)

async def transcribe(
    *,
    api_key: str,
    model: str,
    region: "BailianRegion | None",
    pcm: bytes,
    hot_words: Sequence[str] = (),
    proxy: str | None = None,
) -> str:
    """What the voice message `pcm` says (16 kHz, 16-bit, mono), transcribed whole: one task,
    its audio sent a packet at a time, then finished and its sentences read back."""
    regions = [region] if region is not None else list(REGION_URLS)
    websocket, task_id, _ = await _start_task(
        partial(connect, proxy=proxy), api_key, model, regions, hot_words
    )
    try:
        for start in range(0, len(pcm), PACKET_BYTES):
            await websocket.send(pcm[start : start + PACKET_BYTES])
        await websocket.send(_finish_task_message(task_id))
        said: list[str] = []
        async with asyncio.timeout(FINISH_TIMEOUT):
            async for message in websocket:
                event = _event(message)
                kind = event.get("header", {}).get("event")
                if kind == "result-generated":
                    sentence = event.get("payload", {}).get("output", {}).get("sentence")
                    if isinstance(sentence, dict) and sentence.get("sentence_end"):
                        text = str(sentence.get("text") or "").strip()
                        if _has_words(text):
                            said.append(text)
                elif kind in ("task-finished", "task-failed"):
                    break
        return "".join(said)
    finally:
        await websocket.close()
