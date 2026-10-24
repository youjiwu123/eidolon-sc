"""Voicing the greetings and putting them where browsers hear them: `eidolon greeting upload`.

Each greeting the character configuration has (`Character.greeting`, GLOSSARY.md「招呼」)
is voiced once in her voice, delivery cues and all, over Fish Audio's HTTP API as mp3, and
put in the stands' bucket under `greetings/<id>-<hash>.mp3` (`greeting_path`). Each call
opener (`Character.call_opener`, GLOSSARY.md「开场白」) is too, under
`call-openers/<id>-<hash>.mp3` (`call_opener_path`): apart from the greetings, so neither
ever takes the other's place or is deleted for it. All said below of greetings holds of
call openers too, each kind by its own addresses.

The hash is of what she says, cues and all, of her voice, and of the readings it uses
(`readings`, each name as written and its phonemes; with none, the hash is of the first
two alone). So the server says where it is without asking the bucket; a greeting whose
words, voice or readings changed is at an address no browser has cached; and one the
bucket already has is not voiced again. Every one is voiced with all the readings as its
pronunciation dictionary, as calls are. Once hers is up, her greetings at other addresses
are deleted, and so are those of characters who no longer have one. Clearing the bucket
takes them down along with the stands (ADR-0003, revised 2026-10-10 and 2026-10-12).

They are voiced on the deployer's key, as `.env` has it (`FISH_AUDIO_API_KEY`,
`FISH_AUDIO_MODEL`; not what the admin console may have changed it to), and are no turn:
nothing is recorded of them and they count against no one's limit. Visitors only ever
download what was voiced here; nothing is voiced for them.
"""

import hashlib
import os
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

import httpx

from eidolon.characters import Character, ScriptedLine
from eidolon.config import DEFAULT_FISH_AUDIO_MODEL, FishAudioModel, fish_audio_model_from_env
from eidolon.delivery_cues import spoken_by, voiced
from eidolon.readings import Reading, pronunciation_dictionary
from eidolon.s3 import Bucket, S3Error, S3Settings

GREETINGS_DIR = "greetings"
CALL_OPENERS_DIR = "call-openers"
GREETING_CACHE = "public, max-age=31536000, immutable"
"""A greeting's or call opener's audio never changes at its address: new words or a new
voice are a new one."""
FISH_TTS_HTTP_URL = "https://api.fish.audio/v1/tts"
_HASH_LENGTH = 12
_TIMEOUT = httpx.Timeout(60, connect=10)
"""A greeting is a few seconds of audio, voiced in a few seconds more."""

SOME_FAILED = 3
"""The exit code when some greetings or call openers could not be voiced or put; 2 is the
bucket failing to be listed, so that nothing was."""


@dataclass(frozen=True)
class _Kind:
    """Greetings, or call openers: where in the bucket they go, which line of hers it is,
    and what the deployer calls it."""

    directory: str
    of: Callable[[Character], ScriptedLine | None]
    called: str

    def path(self, character: Character) -> str | None:
        """Where her line of this kind is in the bucket, as the command puts it there; None
        without one, or without a voice to say it in."""
        line = self.of(character)
        if line is None or not character.voice_id.strip():
            return None
        said = f"{character.voice_id}\n{line.text}"
        said += "".join(f"\n{r.written}\t{r.phonemes}" for r in line.readings)
        digest = hashlib.sha256(said.encode()).hexdigest()
        return f"{self.directory}/{character.id}-{digest[:_HASH_LENGTH]}.mp3"

    def hers(self, character: Character) -> re.Pattern[str]:
        """Her paths of this kind, whatever she said: not those of another character whose
        identifier starts with hers (`mano-2`)."""
        return re.compile(rf"{self.directory}/{re.escape(character.id)}-[0-9a-f]{{{_HASH_LENGTH}}}\.mp3")

    def anyones(self) -> re.Pattern[str]:
        """A path of this kind, whoever's it is."""
        return re.compile(rf"{self.directory}/.+-[0-9a-f]{{{_HASH_LENGTH}}}\.mp3")


_GREETINGS = _Kind(GREETINGS_DIR, lambda c: c.greeting, "招呼")
_CALL_OPENERS = _Kind(CALL_OPENERS_DIR, lambda c: c.call_opener, "开场白")
_KINDS = (_GREETINGS, _CALL_OPENERS)


def greeting_path(character: Character) -> str | None:
    """Where her greeting's audio is in the bucket, as the command puts it there; None
    without a greeting, or without a voice to say it in."""
    return _GREETINGS.path(character)


def call_opener_path(character: Character) -> str | None:
    """Where her call opener's audio is in the bucket, as the command puts it there; None
    without a call opener, or without a voice to say it in."""
    return _CALL_OPENERS.path(character)


def call_poster_path(character: Character) -> str:
    """Where her card for the landing page's incoming call is in the bucket. The deployer puts
    it there by hand (docs/deploy.md「来电卡图」); this command does not touch it."""
    return f"call-posters/{character.id}.webp"


def voxels_path(character: Character) -> str:
    """Where her voxel figure for the landing page's act about memory is in the bucket. The
    deployer puts it there by hand (docs/deploy.md「体素小人」); this command does not touch it."""
    return f"landing-voxels/{character.id}.json"


LANDING_MEMORY_STILL_PATH = "landing-memory/still.webp"
"""Where the landing page's act about memory, rendered as one picture, is in the bucket. The
deployer puts it there by hand (docs/deploy.md「第 5 幕的静止图」); this command does not touch it."""


@dataclass(frozen=True)
class FishAudioSettings:
    """The deployer's key for Fish Audio, and the model it voices with."""

    api_key: str
    model: FishAudioModel = DEFAULT_FISH_AUDIO_MODEL

    @classmethod
    def from_env(cls) -> "FishAudioSettings":
        api_key = (os.environ.get("FISH_AUDIO_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError("合成招呼和开场白要用部署者的语音合成 key，环境变量 FISH_AUDIO_API_KEY 未设置")
        return cls(api_key, fish_audio_model_from_env())


class _Failed(RuntimeError):
    """Her greeting or call opener could not be voiced, put, or have its old versions deleted."""


@dataclass
class GreetingReport:
    voiced: list[str] = field(default_factory=list)
    """The names of the characters whose greetings were voiced and put this time, then
    those whose call openers were, as `<name>的开场白`."""
    unchanged: int = 0
    """Greetings and call openers the bucket already had, not voiced again."""
    deleted: list[str] = field(default_factory=list)
    """Old versions deleted."""
    failed: list[tuple[str, str]] = field(default_factory=list)
    """The name of each character whose greeting failed (`<name>的开场白` for her call
    opener), and why."""

    @property
    def exit_code(self) -> int:
        return SOME_FAILED if self.failed else 0


async def upload_greetings(
    characters: Iterable[Character],
    bucket: S3Settings,
    fish: FishAudioSettings,
    readings: Sequence[Reading],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> GreetingReport:
    """Voice what greetings and call openers of `characters` the bucket lacks, with
    `readings` (the whole file's) as the pronunciation dictionary, put them in it, and
    delete their old versions. One that fails is reported and the rest go on; raises
    S3Error, having voiced nothing, if the bucket cannot be listed."""
    characters = list(characters)
    dictionary = pronunciation_dictionary(readings)
    report = GreetingReport()
    storage = Bucket(bucket, transport=transport)
    try:
        async with httpx.AsyncClient(transport=transport, timeout=_TIMEOUT) as client:
            keys = {kind: set(await storage.keys(f"{kind.directory}/")) for kind in _KINDS}
            for kind in _KINDS:
                having = [c for c in characters if kind.of(c) is not None]
                for character in having:
                    try:
                        await _upload(kind, character, keys[kind], storage, client, fish, dictionary, report)
                    except _Failed as e:
                        report.failed.append((_whose(kind, character), str(e)))
                await _delete_unclaimed(kind, keys[kind], having, storage, report)
    finally:
        await storage.aclose()
    return report


def _whose(kind: _Kind, character: Character) -> str:
    """Her line of this kind, as the report names it: her name for her greeting,
    `<name>的开场白` for her call opener."""
    return character.name if kind is _GREETINGS else f"{character.name}的{kind.called}"


async def _delete_unclaimed(
    kind: _Kind, keys: set[str], having: list[Character], storage: Bucket, report: GreetingReport
) -> None:
    """Delete the lines of this kind of characters who no longer have one, or are no longer
    configured: nothing points at them, and the bucket is public."""
    hers = [kind.hers(c) for c in having]
    anyones = kind.anyones()
    for key in sorted(keys):
        if not anyones.fullmatch(key) or any(p.fullmatch(key) for p in hers):
            continue
        try:
            await storage.delete(key)
        except S3Error as e:
            report.failed.append((key, f"配置里已经没有这句{kind.called}，它没删掉（下次运行会再删）：{e}"))
        else:
            report.deleted.append(key)


async def _upload(
    kind: _Kind,
    character: Character,
    keys: set[str],
    storage: Bucket,
    client: httpx.AsyncClient,
    fish: FishAudioSettings,
    dictionary: list[dict] | None,
    report: GreetingReport,
) -> None:
    line = kind.of(character)
    assert line is not None
    path = kind.path(character)
    if path is None:
        raise _Failed("没有音色：characters.toml 里她的 voice_id 是空的")
    if path in keys:
        report.unchanged += 1
    else:
        said = spoken_by(voiced(line.text), fish.model)
        audio = await _voice(client, fish, said, character.voice_id, dictionary)
        try:
            await storage.put(path, audio, "audio/mpeg", GREETING_CACHE)
        except S3Error as e:
            raise _Failed(f"上传失败：{e}") from None
        report.voiced.append(_whose(kind, character))
    old = sorted(key for key in keys if key != path and kind.hers(character).fullmatch(key))
    for key in old:
        try:
            await storage.delete(key)
        except S3Error as e:
            raise _Failed(f"新的已上传，旧版本 {key} 没删掉（下次运行会再删）：{e}") from None
        report.deleted.append(key)


async def voice_line(fish: FishAudioSettings, text: str, voice_id: str) -> bytes:
    """`text` said in the voice `voice_id`, as mp3: what a reply's speaker beside the bubble
    asks for, voiced then and kept with the message (`message_speech`)."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        return await _voice(client, fish, text, voice_id, None)


async def _voice(
    client: httpx.AsyncClient,
    fish: FishAudioSettings,
    text: str,
    voice_id: str,
    dictionary: list[dict] | None,
) -> bytes:
    """`text` said in the voice `voice_id`, with the pronunciation dictionary `dictionary`,
    as mp3."""
    body: dict[str, object] = {"text": text, "reference_id": voice_id, "format": "mp3"}
    if dictionary is not None:
        body["pronunciation_dictionary"] = dictionary
    try:
        response = await client.post(
            FISH_TTS_HTTP_URL,
            headers={"Authorization": f"Bearer {fish.api_key}", "model": fish.model},
            json=body,
        )
    except httpx.HTTPError as e:
        raise _Failed(f"连不上 Fish Audio：{e!r}") from None
    if response.is_error:
        raise _Failed(f"Fish Audio 没能合成：{response.status_code} {response.text[:200]}")
    if not response.content:
        raise _Failed("Fish Audio 没有返回音频")
    return response.content
