"""The backend's HTTP API."""

import asyncio
import hashlib
import base64
import binascii
import json
import logging
import math
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing, asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eidolon.admission import CoolingDown
from eidolon.assembly import Backend
from eidolon.call import (
    CallFailed,
    CallNotFound,
    CallOptions,
    IceCandidate,
    Quality,
    SessionDescription,
)
from eidolon.characters import Character, ScriptedLine, in_idol_order
from eidolon.bailian import transcribe
from eidolon.engine import ENDED, TurnJudged, shown
from eidolon.llm import ChatLLM, ChatMessage
from eidolon.greetings import (
    LANDING_MEMORY_STILL_PATH,
    call_opener_path,
    call_poster_path,
    greeting_path,
    voice_line,
    voxels_path,
)
from eidolon.delivery_cues import spoken_by, voiced
from eidolon.reply_format import SegmentComplete, SegmentDelta, SpeechDelta
from eidolon.own_keys import BroughtKeys, CallsUnavailable, UnknownService, named_services
from eidolon.throttle import Throttled
from eidolon.producer import (
    MAX_FORM_OF_ADDRESS_LENGTH,
    Birthday,
    Gender,
    ProducerProfile,
    is_day_of_the_year,
    is_timezone,
)
from eidolon.account_api import OwnKeysInput, add_account_routes
from eidolon.admin_api import add_admin_routes
from eidolon.admin_backup_api import add_admin_backup_routes
from eidolon.dashboard import add_dashboard_routes
from eidolon.data_package_api import add_data_package_routes
from eidolon.admin_users_api import add_admin_user_routes
from eidolon.admin_config_api import add_admin_config_routes
from eidolon.admin_prices_api import add_admin_price_routes
from eidolon.announcements_api import add_announcement_routes
from eidolon import group_chat
from eidolon.gifts_api import add_gift_routes
from eidolon.photos_api import add_photo_routes
from eidolon.sessions import UserId, session_middleware
from eidolon.sponsor_api import add_sponsor_routes
from eidolon.stats import add_stats_routes
from eidolon.service_status import add_service_status_routes
from eidolon.stand_outfits import list_path, outfit_path
from eidolon.store import Conversation, ConversationEvent, IsCurrent, ReachOut, Recorded
from eidolon.turn_outcome import TurnOutcome
from eidolon.usage import OverLimit

log = logging.getLogger(__name__)
from eidolon.world import World

_PICTURE_CACHE = {"Cache-Control": "public, max-age=86400"}
"""Avatars and call backgrounds change only with a deploy (ADR-0003): browsers keep them a
day, then revalidate by ETag, so lists showing every avatar don't download them each visit."""

NO_CHAT_MODEL = (
    "还没有配置对话模型：在服务器的 .env 里填 CHAT_LLM_BASE_URL、CHAT_LLM_API_KEY、"
    "CHAT_LLM_MODEL（或在后台配置页里填），重启后端后就能对话"
)
"""What a turn answers while `EIDOLON_CHAT_LLM_OPTIONAL=1` and no model is filled in yet."""

MAX_TURN_IMAGE_BYTES = 8 * 1024 * 1024
"""The largest picture a turn may bring, in bytes: a bigger one is refused, not shrunk."""

MAX_VOICE_BYTES = 8 * 1024 * 1024
"""The largest voice message, in bytes: 16 kHz 16-bit mono, about four minutes."""

PICTURE_QUESTION = (
    "先客观描述这张图片，一两句中文，只写看得见的东西（颜色、物体、场景），不要发挥。\n"
    "然后另起一行，只回答图里的人物是谁，三选一：\n"
    "- 就是「{name}」（{name_zh}）本人：回答「本人」；\n"
    "- 是下面名单里的另一位，只写她的名字（必须名字和她的外表特征都对得上，拿不准就回答「不确定」，不要猜）；\n"
    "- 都不是，或者看不出来：回答「不确定」。\n"
    "名单：{roster}\n"
    "「{name}」的外表特征：{appearance}"
)
"""What the model is asked about a picture the user sent, apart from her character prompt: a
long role-play prompt drowns the picture out on smaller models, so what it shows is worked
out first and then told to her in words — whether it is her, and which of the others it is,
an anime character being hard to place from a picture alone."""


async def _describe_picture(
    llm: "ChatLLM",
    picture: tuple[bytes, str],
    character: Character | None = None,
    roster: str = "",
) -> tuple[str, bool | None, str | None]:
    """What the picture shows, in a sentence; whether it is her (None when it cannot be
    told); and, when it is another of the characters, which one."""
    appearance = (character.photo_appearance if character else None) or "（没有记录）"
    question = PICTURE_QUESTION.format(
        name=character.name if character else "这位角色",
        name_zh=character.name_zh if character else "",
        appearance=appearance,
        roster=roster or "（没有名单）",
    )
    said = ""
    async for item in llm.stream([ChatMessage("user", question, image=picture)]):
        if isinstance(item, str):
            said += item
    lines = [line.strip() for line in said.strip().splitlines() if line.strip()]
    if not lines:
        return "", None, None
    verdict = lines[-1]
    description = " ".join(lines[:-1]).strip()[:500] or verdict
    if "本人" in verdict:
        return description, True, None
    if "不确定" in verdict or "不是" in verdict:
        return description, None, None
    return description, False, verdict[:40]  # one of the others, as she is named


def _decode_turn_image(data_url: str | None) -> tuple[bytes, str] | None:
    """The picture a turn brought, and its media type, out of its data URL; None if it
    brought none. What is not one is refused, saying what it should look like."""
    if not data_url:
        return None
    head, _, payload = data_url.partition(",")
    if not head.startswith("data:image/") or not head.endswith(";base64"):
        raise HTTPException(422, "图片要写成 data:image/<类型>;base64,<数据>")
    mime = head[len("data:"):-len(";base64")]
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, "图片的 base64 数据不合法") from None
    if len(raw) > MAX_TURN_IMAGE_BYTES:
        raise HTTPException(413, "图片太大了，最大 8 MB")
    return raw, mime


def _picture(path: Path, request: Request) -> Response:
    """An avatar or call background, or a 304 when the browser's copy is still current;
    FileResponse sets the ETag but, unlike StaticFiles, never answers 304 itself."""
    response = FileResponse(path, headers=_PICTURE_CACHE, stat_result=path.stat())
    etag = response.headers["etag"]
    cached_etags = request.headers.get("if-none-match", "")
    if etag in (tag.strip() for tag in cached_etags.split(",")):
        return Response(status_code=304, headers={"ETag": etag, **_PICTURE_CACHE})
    return response


class NewConversation(BaseModel):
    character_id: str


class TurnInput(BaseModel):
    text: str
    image: str | None = None
    """The user's picture for this turn, as a data URL (`data:image/png;base64,…`): sent to
    a model that can see it, and kept on the turn's message for the record to show."""
    own_keys: OwnKeysInput | None = None
    """The user's own keys, which their browser keeps, for the turn to run on; the shape
    calls bring too. A turn uses only the LLMs and image generation, voice keys brought
    along go unused."""
    own_llm: object = None
    """Where pages from before brought their own LLM. Transitional, to be deleted once no
    such page is left open: a turn bringing it is refused, asking for a refresh, rather
    than quietly run on the deployer's key and counted."""


class CallRequest(BaseModel):
    conversation_id: str
    muted: bool = False
    quality: Quality = "standard"
    own_keys: OwnKeysInput | None = None
    """The user's own keys, which their browser keeps, for the call to run on."""


class BirthdayInput(BaseModel):
    month: int
    day: int

    @model_validator(mode="after")
    def _day_of_the_year(self) -> "BirthdayInput":
        if not is_day_of_the_year(self.month, self.day):
            raise ValueError(f"没有 {self.month} 月 {self.day} 日这一天")
        return self


class ProfileInput(BaseModel):
    """The whole producer profile; a field left out or empty returns to its default."""

    form_of_address: str | None = None
    gender: Gender | None = None
    timezone: str | None = None
    birthday: BirthdayInput | None = None

    @field_validator("form_of_address")
    @classmethod
    def _address(cls, value: str | None) -> str | None:
        value = (value or "").strip()
        if len(value) > MAX_FORM_OF_ADDRESS_LENGTH:
            raise ValueError(f"称呼最多 {MAX_FORM_OF_ADDRESS_LENGTH} 个字")
        return value or None

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str | None) -> str | None:
        value = (value or "").strip()
        if value and not is_timezone(value):
            raise ValueError(f"不认识的时区 {value}")
        return value or None


class Offer(BaseModel):
    """A WebRTC offer as the Pipecat client sends it; `requestData` carries our CallRequest."""

    model_config = ConfigDict(populate_by_name=True)

    sdp: str
    type: str
    pc_id: str | None = None
    restart_pc: bool | None = None
    call: CallRequest | None = Field(None, alias="requestData")


class IceCandidateInput(BaseModel):
    candidate: str
    sdp_mid: str
    sdp_mline_index: int


class IceCandidates(BaseModel):
    """ICE candidates the browser trickles in for one of its calls."""

    pc_id: str
    candidates: list[IceCandidateInput]


DeviceTimezone = Annotated[str | None, Header(alias="X-Timezone")]
"""The time zone the browser detected; the producer profile's own time zone overrides it."""


def _stand(character: Character, assets_url: str | None) -> dict | None:
    """Her stand on the call screen: where her default outfit's files are, and her outfit
    list (`stand_outfits`). None, and her call background alone, unless her idol, her
    default outfit and the stand assets are all configured."""
    if character.idol_id is None or character.default_outfit is None or assets_url is None:
        return None
    outfit = character.default_outfit
    return {
        "default_outfit": {"id": outfit, "url": f"{assets_url}/{outfit_path(outfit)}"},
        "outfits_url": f"{assets_url}/{list_path(character.idol_id)}",
    }


def _greeting(character: Character, assets_url: str | None) -> dict | None:
    """Her greeting on the landing page: what she says as people see it, its subtitle, and
    where `eidolon greeting upload` puts its audio (`greeting_path`), without asking the
    bucket whether it is there; no audio unless the stand assets and her voice are
    configured. None without a greeting."""
    return _scripted(character.greeting, greeting_path(character), assets_url)


def _call_opener(character: Character, assets_url: str | None) -> dict | None:
    """Her call opener on the landing page, as her greeting is (`call_opener_path`); None
    without one."""
    return _scripted(character.call_opener, call_opener_path(character), assets_url)


def _call_poster(character: Character, assets_url: str | None) -> str | None:
    """Her card on the landing page's incoming call (ADR-0003, 2026-10-12), at
    `call_poster_path`, listed without asking the bucket whether it is there (the page shows
    her colour instead when it is not). None unless the stand assets are served."""
    return f"{assets_url}/{call_poster_path(character)}" if assets_url else None


def _voxels(character: Character, assets_url: str | None) -> str | None:
    """Her voxel figure in the landing page's act about memory (ADR-0003, 2026-10-16), at
    `voxels_path`, listed as her call poster is."""
    return f"{assets_url}/{voxels_path(character)}" if assets_url else None


def _scripted(line: ScriptedLine | None, path: str | None, assets_url: str | None) -> dict | None:
    if line is None:
        return None
    return {
        "ja": line.ja,
        "subtitle": line.subtitle,
        "audio_url": f"{assets_url}/{path}" if assets_url and path else None,
    }


def _unit(character: Character, world: World | None) -> dict:
    """Her unit, its reading in hiragana, and where it is in the units' official order
    (`World.unit_of`); all None without the world setting, or if its sources could not
    agree on her unit."""
    unit, order = (world.unit_of(character.name) if world else None) or (None, None)
    reading = world.units[order].reading if world and order is not None else None
    return {"unit": unit, "unit_reading": reading, "unit_order": order}


def create_app(backend: Backend) -> FastAPI:
    """The API over `backend`, which the app closes when it shuts down."""
    store, characters, engine = backend.store, backend.characters, backend.engine
    memory, calls, admissions = backend.memories, backend.calls, backend.admissions
    gift_catalog, titles = backend.gift_catalog, backend.titles
    group_chat.ensure_schema(store._db)  # 群聊的表，和单聊分开

    def gift_of(event: ConversationEvent) -> dict:
        """For a gift record, which gift and its name, to show it by."""
        if event.gift_id is None:
            return {}
        return {"gift_id": event.gift_id, "gift_name": gift_catalog[event.gift_id].name}

    def last_shown(last: Recorded | None) -> dict | None:
        """The last thing in a conversation as a list shows it: a message without its
        delivery cues, marked `photo` if it is her reply with a photo she sent (not one being
        generated or not sent after all), or a conversation event, or her reach-out, shown as
        her message is, marked `reach_out`; None while it has none."""
        if last is None:
            return None
        if isinstance(last, ConversationEvent):
            return {"type": "event", "kind": last.kind, "created_at": last.created_at} | gift_of(
                last
            )
        if isinstance(last, ReachOut):
            said = last.without_cues()
            return {
                "role": "character",
                "text": None,
                "ja": said.ja,
                "zh": said.zh,
                "created_at": said.created_at,
                "reach_out": True,
            }
        shown = last.without_cues()
        return {
            "role": shown.role,
            "text": shown.text,
            "ja": shown.ja,
            "zh": shown.zh,
            "created_at": shown.created_at,
        } | ({"photo": True} if shown.photo_sent else {})

    def recorded(entry: Recorded) -> dict:
        """A message, conversation event or reach-out as the browser reads it."""
        if isinstance(entry, ConversationEvent):
            return entry.shown() | gift_of(entry)
        return entry.shown()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await backend.start()
        yield
        await backend.aclose()

    app = FastAPI(title="Eidolon-SC", lifespan=lifespan)
    app.state.backend = backend
    app.middleware("http")(session_middleware(backend.settings))

    @app.exception_handler(OverLimit)
    async def over_limit(_: Request, e: OverLimit) -> JSONResponse:
        """Refused for now, with when the user can go on (also as `Retry-After`); or, set
        to their own keys only, with no time to wait for."""
        if e.retry_at is None:
            return JSONResponse({"detail": str(e), "retry_at": None}, status_code=429)
        wait = max(1, math.ceil((e.retry_at - e.now).total_seconds()))
        return JSONResponse(
            {"detail": str(e), "retry_at": e.retry_at.isoformat()},
            status_code=429,
            headers={"Retry-After": str(wait)},
        )

    @app.exception_handler(CoolingDown)
    async def cooling_down(_: Request, e: CoolingDown) -> JSONResponse:
        """Refused while she is in a cooldown, with when it ends; told apart from the limit's
        429."""
        return JSONResponse(
            {"detail": str(e), "cooldown_until": e.cooldown_until.isoformat()}, status_code=409
        )

    @app.exception_handler(CallsUnavailable)
    async def calls_unavailable(_: Request, e: CallsUnavailable) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=503)

    @app.exception_handler(Throttled)
    async def throttled(_: Request, e: Throttled) -> JSONResponse:
        """Asked services on the internet too often: when to ask again, in words too."""
        wait = e.retry_after
        later = f"{wait} 秒" if wait < 120 else f"{math.ceil(wait / 60)} 分钟"
        return JSONResponse(
            {"detail": f"{e}，请 {later}后再试"}, status_code=429,
            headers={"Retry-After": str(wait)},
        )

    @app.exception_handler(UnknownService)
    async def unknown_service(_: Request, e: UnknownService) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=422)

    @app.exception_handler(CallNotFound)
    async def call_not_found(_: Request, e: CallNotFound) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=404)

    @app.exception_handler(CallFailed)
    async def call_failed(_: Request, e: CallFailed) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=500)

    def conversation_or_404(conversation_id: str, user_id: str) -> Conversation:
        conversation = store.get_conversation(conversation_id)
        if conversation is None or conversation.user_id != user_id:
            raise HTTPException(404, "对话不存在")
        if conversation.character_id not in characters:  # removed from the configuration
            raise HTTPException(404, "角色不存在")
        return conversation

    def refuse_if_ended(conversation: Conversation) -> None:
        """A call into a conversation she has ended (GLOSSARY.md「已结束的对话」) is refused,
        before anything is asked or spent; told apart from the limit's 429."""
        if store.ended(conversation.id) is not None:
            raise HTTPException(409, ENDED)

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True}

    add_account_routes(app, backend)
    add_admin_routes(app, backend)
    add_dashboard_routes(app, backend)
    add_admin_backup_routes(app, backend)
    add_admin_user_routes(app, backend)
    add_admin_config_routes(app, backend)
    add_admin_price_routes(app, backend)
    add_announcement_routes(app, backend)
    add_gift_routes(app, backend)
    add_photo_routes(app, backend)
    add_sponsor_routes(app, backend)
    add_data_package_routes(app, backend)
    add_stats_routes(app, backend)
    add_service_status_routes(app, backend.service_status)

    @app.get("/api/ice-servers")
    def ice_servers(user_id: UserId, own: str = "", conversation: str | None = None) -> dict:
        """What the browser needs before calling, if the call is to bring the user's own
        keys for the services `own` names (as `llm,tts,stt`), but none of the keys; also
        tells it when calls are unavailable, or, for a call into `conversation`, when she
        is in a cooldown or has ended it."""
        character_id = None
        if conversation:
            into = conversation_or_404(conversation, user_id)
            refuse_if_ended(into)
            character_id = into.character_id
        return calls.ice_servers(user_id, named_services(own), character_id)

    @app.post("/api/offer")
    async def offer(
        body: Offer, user_id: UserId, device_timezone: DeviceTimezone = None
    ) -> dict:
        """WebRTC signalling: a new call into one of the user's conversations, on the user's
        own keys it brings, or a renegotiation, which brings none."""
        description = SessionDescription(body.sdp, body.type)
        if body.pc_id is not None:  # renegotiating, possibly on a fresh peer connection
            return await calls.renegotiate(
                user_id, body.pc_id, description, bool(body.restart_pc)
            )
        if body.call is None:
            raise HTTPException(422, "缺少要通话的对话")
        conversation = conversation_or_404(body.call.conversation_id, user_id)
        refuse_if_ended(conversation)
        return await calls.start(
            user_id,
            conversation,
            characters[conversation.character_id],
            CallOptions(body.call.muted, body.call.quality, device_timezone),
            description,
            body.call.own_keys.brought() if body.call.own_keys else BroughtKeys(),
        )

    @app.patch("/api/offer")
    async def ice_candidates(body: IceCandidates, user_id: UserId) -> dict:
        candidates = [
            IceCandidate(c.candidate, c.sdp_mid, c.sdp_mline_index) for c in body.candidates
        ]
        await calls.add_candidates(user_id, body.pc_id, candidates)
        return {"ok": True}

    @app.get("/api/profile")
    def get_profile(user_id: UserId) -> dict:
        return asdict(store.get_profile(user_id))

    @app.put("/api/profile")
    def update_profile(body: ProfileInput, user_id: UserId) -> dict:
        birthday = body.birthday and Birthday(body.birthday.month, body.birthday.day)
        profile = ProducerProfile(body.form_of_address, body.gender, body.timezone, birthday)
        store.save_profile(user_id, profile)
        return asdict(profile)

    @app.get("/api/characters")
    def list_characters() -> list[dict]:
        """Who there is to talk to; shown before signing in too, and on the landing page.
        In the order of their idol numbers, those without one after them as configured."""
        listed = in_idol_order(characters.values())
        return [
            {
                "id": c.id,
                "name": c.name,
                "intro": c.intro,
                "avatar_url": f"/api/characters/{c.id}/avatar" if c.avatar else None,
                "scsp_avatar_url": f"/api/characters/{c.id}/scsp-avatar" if c.scsp_avatar else None,
                "color": c.color,
                "background_url": f"/api/characters/{c.id}/background" if c.background else None,
                "stand": _stand(c, backend.settings.stand_assets_url),
                "idol_id": c.idol_id,
                "name_zh": c.name_zh,
                "name_kana": c.name_kana,
                "name_romaji": c.name_romaji,
                **_unit(c, backend.world),
                "greeting": _greeting(c, backend.settings.stand_assets_url),
                "call_opener": _call_opener(c, backend.settings.stand_assets_url),
                "call_poster_url": _call_poster(c, backend.settings.stand_assets_url),
                "voxels_url": _voxels(c, backend.settings.stand_assets_url),
            }
            for c in listed
        ]

    @app.get("/api/landing")
    def landing() -> dict:
        """What the landing page shows that belongs to no one character: its act about memory
        as one picture, for visitors who see no animation (ADR-0003, 2026-10-16). Listed as a
        call poster is, without asking the bucket whether it is there (the page shows only the
        words instead); None unless the stand assets are served."""
        assets_url = backend.settings.stand_assets_url
        return {"memory_still_url": f"{assets_url}/{LANDING_MEMORY_STILL_PATH}" if assets_url else None}

    @app.get("/api/characters/{character_id}/avatar")
    def character_avatar(character_id: str, request: Request) -> Response:
        character = characters.get(character_id)
        if character is None or character.avatar is None:
            raise HTTPException(404, "没有这位角色的头像")
        return _picture(character.avatar, request)

    @app.get("/api/characters/{character_id}/scsp-avatar")
    def character_scsp_avatar(character_id: str, request: Request) -> Response:
        """Her avatar in SCSP's set, for users who switched to it in settings."""
        character = characters.get(character_id)
        if character is None or character.scsp_avatar is None:
            raise HTTPException(404, "没有这位角色的 SCSP 头像")
        return _picture(character.scsp_avatar, request)

    @app.get("/api/characters/{character_id}/background")
    def character_background(character_id: str, request: Request) -> Response:
        """The picture behind her call screen, served like her avatar."""
        character = characters.get(character_id)
        if character is None or character.background is None:
            raise HTTPException(404, "没有这位角色的通话背景图")
        return _picture(character.background, request)

    @app.get("/api/characters/{character_id}/affinity")
    def affinity(character_id: str, user_id: UserId) -> dict | None:
        """Her affinity with the user now, by tier only, and when the cooldown ends if she
        is in one; null while affinity is off."""
        if character_id not in characters:
            raise HTTPException(404, "角色不存在")
        status = backend.affinities.status(user_id, character_id)
        return status and status.shown()

    @app.get("/api/memories")
    async def list_memories(user_id: UserId) -> list[dict]:
        """Newest first, each with the name of the character it was told to and whether
        it has been superseded (ADR-0014)."""
        return [
            {
                "id": m.id,
                "text": m.text,
                "source_character_id": m.source_character_id,
                "source_character_name": m.source_character_name,
                "created_at": m.created_at,
                "superseded": m.superseded_by is not None,
            }
            for m in await memory.entries(user_id)
        ]

    @app.delete("/api/memories/{memory_id}", status_code=204)
    async def delete_memory(memory_id: int, user_id: UserId) -> None:
        if not await memory.delete(user_id, memory_id):
            raise HTTPException(404, "记忆不存在")

    @app.delete("/api/memories", status_code=204)
    async def clear_memories(user_id: UserId) -> None:
        await memory.clear(user_id)

    @app.post("/api/conversations", status_code=201)
    def create_conversation(body: NewConversation, user_id: UserId) -> dict:
        if body.character_id not in characters:
            raise HTTPException(404, "角色不存在")
        return asdict(store.create_conversation(user_id, body.character_id))

    @app.get("/api/conversations")
    async def list_conversations(character_id: str, user_id: UserId) -> list[dict]:
        """Her conversations with the user, newest first, the first her current one; each
        with its title, null if it has none, the last thing in it, as the conversation list
        shows it, and whether she has ended it. Those being named are waited for a while."""
        await titles.settle(user_id)
        listed = await asyncio.to_thread(store.list_conversations, user_id, character_id)
        return [
            asdict(c) | {"last_message": last_shown(last), "ended": ended}
            for c, last, ended in listed
        ]

    @app.get("/api/conversations/latest")
    def latest_conversations(user_id: UserId) -> list[dict]:
        """For the conversation list: each character's newest conversation with the user, the
        last thing in it, a message, a conversation event or a reach-out (null while it has
        none), and whether it has a reach-out the user has not seen, in one request."""
        return [
            {
                "character_id": c.character_id,
                "conversation_id": c.id,
                "created_at": c.created_at,
                "last_message": last_shown(last),
                "unseen_reach_out": unseen,
            }
            for c, last, unseen in store.latest_conversations(user_id)
        ]

    @app.delete("/api/conversations/{conversation_id}", status_code=204)
    def delete_conversation(conversation_id: str, user_id: UserId) -> None:
        """Delete a past conversation (GLOSSARY.md「历史对话」), for good; her current one is
        not deleted (409)."""
        try:
            store.delete_conversation(conversation_or_404(conversation_id, user_id))
        except IsCurrent:
            raise HTTPException(409, "当前对话不能删除") from None

    @app.get("/api/conversations/{conversation_id}/messages")
    def list_messages(conversation_id: str, user_id: UserId) -> list[dict]:
        """The conversation's record: its messages, conversation events and reach-outs, in
        order. The user reads it: its reach-outs are seen from now on."""
        conversation = conversation_or_404(conversation_id, user_id)
        entries = [recorded(entry) for entry in store.read_by_user(conversation.id)]
        with_images = store.user_image_message_ids(conversation.id)
        for entry in entries:  # a message the user sent a picture with says so, for the page
            if entry.get("id") in with_images:
                entry["image"] = True
        return entries

    @app.get("/api/messages/{message_id}/image")
    def message_image(message_id: int, user_id: UserId) -> Response:
        """The picture the user sent with that message; 404 for anyone else's."""
        found = store.user_image(message_id, user_id)
        if found is None:
            raise HTTPException(404, "没有这张图片")
        data, mime = found
        return Response(
            data, media_type=mime, headers={"Cache-Control": "private, max-age=31536000"}
        )

    @app.get("/api/messages/{message_id}/speech")
    async def message_speech(message_id: int, user_id: UserId) -> Response:
        """Her reply said aloud: voiced the first time the user asks for it, on the
        deployer's voice, and kept with the message so asking again voices nothing; 404 for
        anyone else's."""
        speaking = store.message_to_speak(message_id, user_id)
        if speaking is None:
            raise HTTPException(404, "没有这条消息")
        kept = store.message_speech(message_id)
        if kept is not None:
            data, mime = kept
            return Response(
                data, media_type=mime, headers={"Cache-Control": "private, max-age=31536000"}
            )
        voice = backend.config.voice
        if voice is None:
            raise HTTPException(503, "还没有配置语音合成")
        character_id, ja, conversation_id = speaking
        character = characters.get(character_id)
        voice_id = (character.voice_id if character else "").strip()
        if not voice_id:
            raise HTTPException(503, "她还没有声音")
        said = spoken_by(voiced(ja), voice.model)
        if not said.strip():
            raise HTTPException(503, "这条回复没有可以念出来的文字")
        try:
            data = await voice_line(voice, said, voice_id)
        except Exception as e:  # voiced or not, the reply itself stays
            raise HTTPException(502, f"语音合成失败了：{e}") from e
        store.keep_message_speech(message_id, conversation_id, "audio/mpeg", data)
        return Response(
            data, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=31536000"}
        )

    @app.get("/api/characters/{character_id}/call-opener/speech")
    async def call_opener_speech(character_id: str) -> Response:
        """Her call opener said aloud, so that answering a call she speaks first: voiced the
        first time it is asked for and kept on disk, since the opener does not change."""
        character = characters.get(character_id)
        if character is None or character.call_opener is None:
            raise HTTPException(404, "她没有开场白")
        voice = backend.config.voice
        if voice is None:
            raise HTTPException(503, "还没有配置语音合成")
        voice_id = (character.voice_id or "").strip()
        if not voice_id:
            raise HTTPException(503, "她还没有声音")
        ja = getattr(character.call_opener, "ja", "") or ""
        said = spoken_by(voiced(ja), voice.model)
        if not said.strip():
            raise HTTPException(503, "开场白没有可以念出来的文字")
        digest = hashlib.sha1(f"{voice.model}:{voice_id}:{said}".encode()).hexdigest()[:16]
        kept = Path("/data/speech") / f"opener-{character_id}-{digest}.mp3"
        if not kept.exists():
            try:
                data = await voice_line(voice, said, voice_id)
            except Exception as e:
                raise HTTPException(502, f"语音合成失败了：{e}") from e
            kept.parent.mkdir(parents=True, exist_ok=True)
            kept.write_bytes(data)
        return Response(
            kept.read_bytes(),
            media_type="audio/mpeg",
            headers={"Cache-Control": "private, max-age=31536000"},
        )

    @app.post("/api/voice")
    async def hear_voice(request: Request, user_id: UserId) -> dict[str, str]:
        """What a voice message says, so the composer can send it as text: 16 kHz 16-bit mono
        PCM in the body, transcribed on the deployer's speech recognition."""
        pcm = await request.body()
        if not pcm:
            raise HTTPException(422, "没有收到声音")
        if len(pcm) > MAX_VOICE_BYTES:
            raise HTTPException(413, "语音太长了，最多 8 MB")
        hearing = backend.config.hearing
        if hearing is None or hearing.provider != "bailian":
            raise HTTPException(503, "还没有配置语音识别")
        try:
            heard = await transcribe(
                api_key=hearing.api_key,
                model=backend.settings.bailian_stt_model,
                region=hearing.region,
                pcm=pcm,
            )
        except Exception as e:  # nothing was sent, so nothing is lost
            raise HTTPException(502, f"没能听清这段语音：{e}") from e
        if not heard.strip():
            raise HTTPException(422, "没有听清，再说一次？")
        return {"text": heard}

    class GroupInput(BaseModel):
        character_ids: list[str]
        title: str | None = None

    class GroupTurnInput(BaseModel):
        text: str
        target_id: str | None = None

    def group_or_404(group_id: str, user_id: str) -> group_chat.Group:
        group = group_chat.get_group(store, group_id)
        if group is None or group.user_id != user_id:
            raise HTTPException(404, "没有这个群")
        return group

    def group_json(group: group_chat.Group, last: group_chat.GroupMessage | None) -> dict:
        return {
            "id": group.id,
            "title": group.title,
            "participants": list(group.participants),
            "created_at": group.created_at,
            "last_message": message_json(last) if last else None,
        }

    def message_json(message: group_chat.GroupMessage | None) -> dict | None:
        if message is None:
            return None
        return {
            "id": message.id,
            "role": message.role,
            "speaker_id": message.speaker_id,
            "segments": [{"ja": s.ja, "zh": s.zh} for s in message.segments],
            "created_at": message.created_at,
        }

    @app.post("/api/groups")
    def make_group(body: GroupInput, user_id: UserId) -> dict:
        """建一个群，拉 2–4 个人。"""
        wanted = list(dict.fromkeys(body.character_ids))
        unknown = [c for c in wanted if c not in characters]
        if unknown:
            raise HTTPException(422, f"没有这些角色：{', '.join(unknown)}")
        if not group_chat.MIN_PARTICIPANTS <= len(wanted) <= group_chat.MAX_PARTICIPANTS:
            raise HTTPException(
                422,
                f"群聊要 {group_chat.MIN_PARTICIPANTS}–{group_chat.MAX_PARTICIPANTS} 个人",
            )
        group = group_chat.create_group(store, user_id, wanted, body.title)
        return group_json(group, None)

    @app.get("/api/groups")
    def my_groups(user_id: UserId) -> dict:
        return {
            "groups": [
                group_json(group, last) for group, last in group_chat.list_groups(store, user_id)
            ]
        }

    @app.get("/api/groups/{group_id}/messages")
    def group_messages(group_id: str, user_id: UserId) -> dict:
        group = group_or_404(group_id, user_id)
        return {
            "group": group_json(group, None),
            "messages": [message_json(m) for m in group_chat.messages_of(store, group.id)],
        }

    @app.patch("/api/groups/{group_id}")
    def rename_group(group_id: str, body: dict, user_id: UserId) -> dict:
        """给群改个名字；传空名字就回到「成员名」。"""
        group = group_or_404(group_id, user_id)
        title = (body.get("title") or "").strip() or None
        if title is not None and len(title) > 40:
            raise HTTPException(422, "名字太长了，40 个字以内")
        group_chat.rename_group(store, group.id, title)
        return group_json(group_chat.get_group(store, group.id) or group, None)

    @app.post("/api/groups/{group_id}/chatter")
    async def group_chatter(group_id: str, user_id: UserId) -> StreamingResponse:
        """没人说话时，让她们自己聊两三句。"""
        group = group_or_404(group_id, user_id)
        members = [characters[c] for c in group.participants if c in characters]
        if len(members) < 2:
            raise HTTPException(422, "群里至少要两个人")
        admission = admissions.turn(user_id, members[0].id, BroughtKeys())

        async def events() -> AsyncIterator[str]:
            def send(payload: dict) -> str:
                return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

            async for kind, speaker_id, message in group_chat.run_chatter(
                store, group, characters, admission.llms.chat, admission.llms.background
            ):
                if kind == "thinking":
                    yield send({"type": "thinking", "speaker_id": speaker_id})
                elif message is not None:
                    yield send({"type": "message", "message": message_json(message)})
            yield send({"type": "done"})

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.delete("/api/groups/{group_id}")
    def drop_group(group_id: str, user_id: UserId) -> dict:
        group = group_or_404(group_id, user_id)
        group_chat.delete_group(store, group.id)
        return {"deleted": group.id}

    @app.post("/api/groups/{group_id}/turns")
    async def group_turn(group_id: str, body: GroupTurnInput, user_id: UserId) -> StreamingResponse:
        """我发一句话，她们一条接一条地说。一条一条推给页面，别一起冒出来。

        事件：`{"type":"thinking","speaker_id":...}`（她想说话）、
        `{"type":"message","message":{...}}`（她说完了）、`{"type":"done"}`。"""
        group = group_or_404(group_id, user_id)
        text = body.text.strip()
        if not text:
            raise HTTPException(422, "说点什么吧")
        members = [characters[c] for c in group.participants if c in characters]
        if not members:
            raise HTTPException(422, "群里没有人")
        brought = BroughtKeys()
        if not backend.config.chat_model.text.configured and brought.llm is None:
            raise HTTPException(503, NO_CHAT_MODEL)
        admission = admissions.turn(user_id, members[0].id, brought)

        async def events() -> AsyncIterator[str]:
            def send(payload: dict) -> str:
                return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

            yield send({"type": "user", "segments": [{"ja": "", "zh": text}]})
            async for kind, speaker_id, message in group_chat.run_turn(
                store,
                group,
                characters,
                text,
                admission.llms.chat,
                admission.llms.background,
                forced=(body.target_id,) if body.target_id else (),
            ):
                if kind == "thinking":
                    yield send({"type": "thinking", "speaker_id": speaker_id})
                elif message is not None:
                    yield send({"type": "message", "message": message_json(message)})
            yield send({"type": "done"})

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/api/conversations/{conversation_id}/turns")
    def text_turn(
        conversation_id: str,
        body: TurnInput,
        user_id: UserId,
        device_timezone: DeviceTimezone = None,
    ) -> StreamingResponse:
        conversation = conversation_or_404(conversation_id, user_id)
        if body.own_llm is not None:  # transitional: see `TurnInput.own_llm`
            raise HTTPException(422, "页面已经更新，请刷新后再发")
        brought = body.own_keys.brought() if body.own_keys else BroughtKeys()
        if not backend.config.chat_model.text.configured and brought.llm is None:
            # A deployment still being set up (EIDOLON_CHAT_LLM_OPTIONAL=1) has no model of
            # its own: only a turn bringing its own LLM can run, and the rest are told why.
            raise HTTPException(503, NO_CHAT_MODEL)
        admission = admissions.turn(user_id, conversation.character_id, brought)
        outcome = TurnOutcome(admission.recording, admission.settle)
        picture = _decode_turn_image(body.image)

        async def events() -> AsyncIterator[str]:
            # Closed as the response is, so that the turn is no longer under way once the user
            # has gone (`Store.taking_turn`).
            with outcome.running():
                note = None
                is_her: bool | None = None
                other: str | None = None
                if picture is not None:  # what it shows, worked out before her prompt is built
                    # A background model of its own was chosen, so let it read the picture: a
                    # cheap or better-seeing one, leaving the model that plays her to talk.
                    reader = (
                        admission.llms.background
                        if backend.settings.background_llm_explicit
                        else admission.llms.chat
                    )
                    try:
                        note, is_her, other = await _describe_picture(
                            reader,
                            picture,
                            characters.get(conversation.character_id),
                            roster="；".join(
                                f"{c.name}（{c.photo_appearance}）" if c.photo_appearance else c.name
                                for c in characters.values()
                            ),
                        )
                        note = note or None
                    except Exception:  # her turn still runs, just without the picture told
                        log.exception("could not describe the picture in conversation %s", conversation.id)
                async with aclosing(engine.turn(
                    conversation, body.text, admission.llms, device_timezone,
                    own_image=brought.image, image=picture, picture_note=note,
                    picture_is_her=is_her, picture_other=other,
                )) as turn:
                    async for event in turn:
                        outcome.see(event)
                        if isinstance(event, SegmentDelta):
                            outcome.began_speaking()  # the reply's first words have appeared
                        if isinstance(event, SegmentComplete | SpeechDelta | TurnJudged):
                            continue
                        yield _sse(shown(event))
            if picture is not None:  # kept once the turn is on the record, to show it again
                store.keep_user_image(conversation.id, body.text, picture[1], picture[0])

        return _TurnResponse(events(), lambda: outcome.over(cut_short_by_failure=False))

    return app


class _TurnResponse(StreamingResponse):
    """A text turn's events, `end` called once they are sent or the page is gone, even
    gone before they began, when the stream of them never runs."""

    def __init__(self, events: AsyncIterator[str], end: Callable[[], None]) -> None:
        super().__init__(
            events,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
        self._end = end

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._end()


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
