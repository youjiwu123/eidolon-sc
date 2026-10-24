"""群聊：几个人一个对话，各自用自己的口吻说话（`.scratch/group-chat/spec.md`）。

和单聊完全分开：自己的表、自己的回合、自己的提示词，单聊的代码一行不改。
群聊里不判定、也不改变任何人的好感度。
"""

from __future__ import annotations

import asyncio
import json
import re
import logging
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from eidolon.characters import Character
from eidolon.reply_format import ReplyParser, Segment, SegmentComplete, SegmentDelta
from eidolon.store import Store

if TYPE_CHECKING:  # 只用来标注，不引入运行期依赖
    from eidolon.llm import ChatMessage, ChatService

log = logging.getLogger(__name__)

MAX_PARTICIPANTS = 4
MIN_PARTICIPANTS = 2
MAX_SPEAKERS = 1
MAX_EXCHANGES = 1
"""一句用户发言之后，她们之间最多再多接几次话，免得自说自话停不下来。"""
REACTION_PAUSE = 0.8
"""她们接别人的话之前的一小段停顿（秒），读起来像在想。"""
"""一次用户发言最多几个人接话，免得一句问话刷出四段回复。"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS group_conversations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    title TEXT,
    participants TEXT NOT NULL,
    auto_chat INTEGER NOT NULL DEFAULT 0,
    auto_chat_until TEXT
);
CREATE TABLE IF NOT EXISTS group_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL REFERENCES group_conversations(id),
    role TEXT NOT NULL CHECK (role IN ('user', 'character', 'system')),
    speaker_id TEXT,
    segments TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS group_messages_by_group ON group_messages(group_id, id);
CREATE INDEX IF NOT EXISTS groups_by_user ON group_conversations(user_id);
CREATE TABLE IF NOT EXISTS group_message_speech (
    message_id INTEGER PRIMARY KEY REFERENCES group_messages(id),
    mime TEXT NOT NULL,
    audio BLOB NOT NULL
);
"""

_MIGRATE_SYSTEM = """
PRAGMA foreign_keys = OFF;
BEGIN;
CREATE TABLE group_messages_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL REFERENCES group_conversations(id),
    role TEXT NOT NULL CHECK (role IN ('user', 'character', 'system')),
    speaker_id TEXT,
    segments TEXT NOT NULL,
    created_at TEXT NOT NULL
);
INSERT INTO group_messages_new SELECT id, group_id, role, speaker_id, segments, created_at FROM group_messages;
DROP TABLE group_messages;
ALTER TABLE group_messages_new RENAME TO group_messages;
CREATE INDEX IF NOT EXISTS group_messages_by_group ON group_messages(group_id, id);
COMMIT;
"""

GROUP_RULES = """\
# 这不是一对一的对话
现在和你说话的除了制作人，还有别的偶像在场。你只听得到在场的每个人说的话，
不要在回复里替任何人说话，也不要描写别人的台词和动作——只写你自己的。
- 你可以回应制作人，也可以接在场某个人的话。
- 一轮通常一两句；别人说完，你可以只回一句反应。
- 不要每句都点别人的名字，也不要每次都要总结。
- 称呼按你人设里的叫法来；不确定该怎么称呼的人，就用「名字＋さん」。

关系不是均匀的：你和制作人之间有多亲近，按你读到的关系来写；别人和制作人更亲近时，
你会有自己的反应（在意、打趣、装作没看见都行），但不要说出「好感」「档位」这种词。
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Group:
    """一个群，`participants` 是角色的 id，按建群时的顺序。"""

    id: str
    user_id: str
    created_at: str
    title: str | None
    participants: tuple[str, ...]
    auto_chat: bool = False


@dataclass(frozen=True)
class GroupMessage:
    id: int
    group_id: str
    role: str
    speaker_id: str | None
    segments: tuple[Segment, ...]
    created_at: str


# --------------------------------------------------------------------------- 存储


def ensure_schema(db) -> None:
    """建群聊的表；启动时调一次即可。老的 `group_messages` 只认 user/character，
    这里换成也认 system 的那张（成员加入的提示要用）。"""
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'group_messages'"
    ).fetchone()
    if row and "'system'" not in (row[0] or ""):
        db.executescript(_MIGRATE_SYSTEM)
    db.executescript(_SCHEMA)
    columns = {r[1] for r in db.execute("pragma table_info(group_conversations)")}
    if "auto_chat" not in columns:
        db.execute("ALTER TABLE group_conversations ADD COLUMN auto_chat INTEGER NOT NULL DEFAULT 0")
    if "auto_chat_until" not in columns:
        db.execute("ALTER TABLE group_conversations ADD COLUMN auto_chat_until TEXT")
    db.commit()


def _row_to_group(row: Mapping) -> Group:
    return Group(
        row["id"],
        row["user_id"],
        row["created_at"],
        row["title"],
        tuple(json.loads(row["participants"])),
        bool(row.get("auto_chat")) if hasattr(row, "get") else False,
    )


def create_group(
    store: Store, user_id: str, character_ids: Sequence[str], title: str | None = None
) -> Group:
    group = Group(uuid.uuid4().hex, user_id, _now(), title, tuple(character_ids))
    with store._lock, store._db:
        store._db.execute(
            "INSERT INTO group_conversations (id, user_id, created_at, title, participants)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                group.id,
                group.user_id,
                group.created_at,
                group.title,
                json.dumps(list(group.participants), ensure_ascii=False),
            ),
        )
    return group


def get_group(store: Store, group_id: str) -> Group | None:
    with store._lock:
        row = store._db.execute(
            "SELECT * FROM group_conversations WHERE id = ?", (group_id,)
        ).fetchone()
    return _row_to_group(row) if row else None


def list_groups(store: Store, user_id: str) -> list[tuple[Group, GroupMessage | None]]:
    """用户的群，新的在前，各带最后一条消息。"""
    with store._lock:
        rows = store._db.execute(
            "SELECT * FROM group_conversations WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        groups = [_row_to_group(r) for r in rows]
        latest = {g.id: _last_message(store, g.id) for g in groups}
    return [(g, latest[g.id]) for g in groups]


def rename_group(store: Store, group_id: str, title: str | None) -> None:
    with store._lock, store._db:
        store._db.execute(
            "UPDATE group_conversations SET title = ? WHERE id = ?", (title, group_id)
        )


def speech_of(store: Store, message_id: int) -> tuple[bytes, str] | None:
    with store._lock:
        row = store._db.execute(
            "SELECT mime, audio FROM group_message_speech WHERE message_id = ?", (message_id,)
        ).fetchone()
    return (row["audio"], row["mime"]) if row else None


def keep_speech(store: Store, message_id: int, mime: str, audio: bytes) -> None:
    with store._lock, store._db:
        store._db.execute(
            "INSERT OR REPLACE INTO group_message_speech (message_id, mime, audio) VALUES (?, ?, ?)",
            (message_id, mime, audio),
        )


def message_by_id(store: Store, group_id: str, message_id: int) -> GroupMessage | None:
    with store._lock:
        row = store._db.execute(
            "SELECT * FROM group_messages WHERE id = ? AND group_id = ?", (message_id, group_id)
        ).fetchone()
    return _row_to_message(row) if row else None


def join_notice(store: Store, group_id: str, character: Character) -> GroupMessage:
    """「某某 加入了群聊」这条系统消息，`speaker_id` 是加进来的人。"""
    segments = (Segment("", f"{character.name_zh or character.name} 加入了群聊"),)
    return append_message(store, group_id, "system", segments, character.id)


def set_auto_chat(store: Store, group_id: str, until: str | None) -> None:
    """`until` 为空就是关掉；否则开着，到这个时间自动停（服务器说了算，关页面也照旧）。"""
    with store._lock, store._db:
        store._db.execute(
            "UPDATE group_conversations SET auto_chat = ?, auto_chat_until = ? WHERE id = ?",
            (1 if until else 0, until, group_id),
        )


def auto_chat_groups(store: Store, now: str) -> list[Group]:
    """现在该让她们自己聊的群：开着，且还没到时间。"""
    with store._lock:
        rows = store._db.execute(
            "SELECT * FROM group_conversations WHERE auto_chat = 1 AND auto_chat_until > ?"
            " ORDER BY created_at",
            (now,),
        ).fetchall()
    return [_row_to_group(r) for r in rows]


def stop_expired_auto_chat(store: Store, now: str) -> None:
    """到点的自动关掉。"""
    with store._lock, store._db:
        store._db.execute(
            "UPDATE group_conversations SET auto_chat = 0, auto_chat_until = NULL"
            " WHERE auto_chat = 1 AND (auto_chat_until IS NULL OR auto_chat_until <= ?)",
            (now,),
        )


def add_member(store: Store, group_id: str, character_id: str) -> Group:
    """把一个人拉进群；返回新的群。"""
    group = get_group(store, group_id)
    if group is None:
        raise KeyError(group_id)
    people = list(group.participants)
    if character_id in people:
        return group
    people.append(character_id)
    with store._lock, store._db:
        store._db.execute(
            "UPDATE group_conversations SET participants = ? WHERE id = ?",
            (json.dumps(people, ensure_ascii=False), group_id),
        )
    return Group(group.id, group.user_id, group.created_at, group.title, tuple(people))


def delete_group(store: Store, group_id: str) -> None:
    with store._lock, store._db:
        store._db.execute("DELETE FROM group_messages WHERE group_id = ?", (group_id,))
        store._db.execute("DELETE FROM group_conversations WHERE id = ?", (group_id,))


def append_message(
    store: Store,
    group_id: str,
    role: str,
    segments: Sequence[Segment],
    speaker_id: str | None = None,
) -> GroupMessage:
    payload = json.dumps(
        [{"ja": s.ja, "zh": s.zh} for s in segments], ensure_ascii=False
    )
    with store._lock, store._db:
        cursor = store._db.execute(
            "INSERT INTO group_messages (group_id, role, speaker_id, segments, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (group_id, role, speaker_id, payload, _now()),
        )
        message_id = int(cursor.lastrowid)
    message = GroupMessage(message_id, group_id, role, speaker_id, tuple(segments), _now())
    return message


def messages_of(store: Store, group_id: str, limit: int = 200) -> list[GroupMessage]:
    with store._lock:
        rows = store._db.execute(
            "SELECT * FROM group_messages WHERE group_id = ? ORDER BY id DESC LIMIT ?",
            (group_id, limit),
        ).fetchall()
    return [_row_to_message(r) for r in reversed(rows)]


def _row_to_message(row: Mapping) -> GroupMessage:
    segments = tuple(
        Segment(s.get("ja", ""), s.get("zh", "")) for s in json.loads(row["segments"])
    )
    return GroupMessage(
        row["id"],
        row["group_id"],
        row["role"],
        row["speaker_id"],
        segments,
        row["created_at"],
    )


def _last_message(store: Store, group_id: str) -> GroupMessage | None:
    row = store._db.execute(
        "SELECT * FROM group_messages WHERE group_id = ? ORDER BY id DESC LIMIT 1",
        (group_id,),
    ).fetchone()
    return _row_to_message(row) if row else None


# --------------------------------------------------------------------------- 提示词


def _participants_line(members: Sequence[Character]) -> str:
    return "、".join(m.name_zh or m.name for m in members)


def _history_lines(history: Sequence[GroupMessage], roster: Mapping[str, Character]) -> list[str]:
    """群历史，写成「谁：说了什么」，让模型知道每句是谁说的。"""
    lines: list[str] = []
    for message in history:
        if message.role == "user":
            lines.append(f"制作人：{_plain(message.segments)}")
        elif message.speaker_id:
            who = roster.get(message.speaker_id)
            name = (who.name_zh or who.name) if who else message.speaker_id
            lines.append(f"{name}：{_plain(message.segments)}")
    return lines


def _plain(segments: Sequence[Segment]) -> str:
    """历史里给她看日语原文：看到中文，她下一句就会用中文答。"""
    return " ".join(s.ja or s.zh for s in segments if (s.ja or s.zh))


RELATIONSHIP_RULE = (
    "群里还有一层关系：制作人和每个人之间的亲近程度不一样。你只知道自己和制作人"
    "现在是什么关系；如果看出制作人和别人比和你更亲近，你会有反应——在意、好奇、"
    "起哄、别扭都行。但不要说出「好感度」「档位」「数值」这类词，也不要给别人排名。"
)


def _relationship_lines(
    speaker: Character, members: Sequence[Character], tiers: Mapping[str, str] | None
) -> list[str]:
    """她这一轮要读到的人际关系：自己的档位人设，以及别人的亲近程度。"""
    if not tiers:
        return []
    from eidolon.affinity import tier_prompt, tier_rank

    lines: list[str] = []
    mine = tiers.get(speaker.id)
    if mine:
        lines.append(f"# 你和制作人之间的关系\n{tier_prompt(mine)}")
    closer: list[str] = []
    for other in members:
        if other.id == speaker.id:
            continue
        theirs = tiers.get(other.id)
        if not theirs or not mine:
            continue
        if tier_rank(theirs) > tier_rank(mine) and tier_rank(theirs) >= 3:  # 亲近及以上
            closer.append(other.name_zh or other.name)
    if closer:
        lines.append(
            "# 你看得出来的事\n制作人和"
            + "、".join(closer)
            + "之间的气氛，比和你更亲近一些。你自己心里有数就好，不一定要说出来。"
        )
    return lines


def group_prompt(
    speaker: Character,
    members: Sequence[Character],
    history: Sequence[GroupMessage],
    text: str,
    tiers: Mapping[str, str] | None = None,
) -> list["ChatMessage"]:
    """轮到 `speaker` 时给模型的完整提示词。

    `members` 是群里所有人（含她自己）；`history` 是群历史，最后一条是刚写下的用户消息。
    """
    from eidolon.llm import ChatMessage  # 延迟导入，避免循环依赖
    from eidolon.reply_format import format_instructions

    others = [m for m in members if m.id != speaker.id]
    system = "\n\n".join(
        [
            speaker.sheet,
            format_instructions(None),  # 文字回合：日语原文＋中文字幕
            GROUP_RULES,
            f"现在在场的人：制作人、你（{speaker.name_zh or speaker.name}）、{_participants_line(others)}。",
            *_relationship_lines(speaker, members, tiers),
        ]
    )
    conversation: list[ChatMessage] = [ChatMessage("system", system)]
    lines = _history_lines(history, {m.id: m for m in members})
    if lines:
        conversation.append(ChatMessage("system", "# 刚才的对话\n" + "\n".join(lines[:-1])))
    conversation.append(ChatMessage("user", text))
    return conversation


OPENING = (
        "（没人说话。你可以主动开口：说说你最近的事、问问身边的谁，或者提起工作上的事。"
        "一两句就好，不要总结，不要问「大家都在吗」这种空话。）"
)


async def speak(
    speaker: Character,
    members: Sequence[Character],
    history: Sequence[GroupMessage],
    text: str,
    chat: "ChatService",
    opening: bool = False,
    hint: str = "",
    tiers: Mapping[str, str] | None = None,
) -> list[Segment]:
    """让 `speaker` 回一句，解析成段落；解析不出内容时返回空表。

    `opening` 是真的一句用户发言都没有时的开口，她得自己挑起话头。"""
    prompt = group_prompt(speaker, members, history, text or OPENING, tiers)
    if hint:
        from eidolon.llm import ChatMessage

        prompt.append(ChatMessage("system", hint))
    parser = ReplyParser()
    parts: dict[int, dict[str, str]] = {}
    events = []

    async def feed(chunk: str) -> None:
        events.extend(parser.feed(chunk))

    stream = chat.stream(prompt, (), forbid_calls=True)
    try:
        async for item in stream:
            if isinstance(item, str):
                await feed(item)
    finally:
        close = getattr(stream, "aclose", None)
        if close is not None:
            await close()
    events.extend(parser.finish())

    for event in events:
        if isinstance(event, SegmentDelta):
            bucket = parts.setdefault(event.segment, {"ja": "", "zh": ""})
            bucket[event.lang] += event.text
        elif isinstance(event, SegmentComplete):
            parts.setdefault(event.segment, {"ja": "", "zh": ""})
    return [Segment(parts[i]["ja"], parts[i]["zh"]) for i in sorted(parts)]


def _invites_a_reply(message: GroupMessage) -> bool:
    """刚才那句是不是在等别人接话：问句，或者带「？」的那种语气。"""
    said = " ".join(s.ja or s.zh for s in message.segments)
    return "？" in said or "?" in said or "どう" in said


def _fallback_speakers(
    members: Sequence[Character], history: Sequence[GroupMessage]
) -> list[Character]:
    """判断模型没给结果时的退路：上一轮没说话的先说。"""
    spoken = {m.speaker_id for m in history if m.role == "character"}
    ordered = [m for m in members if m.id not in spoken] + [
        m for m in members if m.id in spoken
    ]
    return ordered[: min(2, len(ordered))]


def _chooser_prompt(members: Sequence[Character], history: Sequence[GroupMessage], text: str) -> str:
    roster = "\n".join(f"- {m.id}：{m.name_zh or m.name}——{(m.intro or '').strip()}" for m in members)
    recent = "\n".join(_history_lines(history[-8:], {m.id: m for m in members}))
    return (
        "这是一段多人聊天。判断制作人这句话之后，谁最该接话。\n\n"
        f"在场的人：\n{roster}\n\n"
        f"最近几句：\n{recent}\n\n"
        f"制作人刚说：{text}\n\n"
        "只输出一个 JSON 对象，形如 {\"speakers\": [\"id1\", \"id2\"]}，不要别的文字。规则：\n"
        "- 1 到 2 个人，按该说话的先后排序。\n"
        "- 制作人点名了谁，谁必须排第一。\n"
        "- 被问到的事只有某个人知道，就只选她。\n"
        "- 上一轮已经说过话的人，这一轮往后排（被点名的除外），让每个人都有机会开口。\n"
        "- 制作人在说自己累、难过、开心、要走这类自己的事时，可以选 2 个人，让群里更像在关心他。\n"
        "- 没有人适合接话时输出空数组。"
    )


async def _ask(picker: "ChatService", prompt: str) -> str:
    from eidolon.llm import ChatMessage

    said = ""
    stream = picker.stream([ChatMessage("system", "你负责判断多人聊天里谁该接话。"), ChatMessage("user", prompt)], (), forbid_calls=True)
    try:
        async for item in stream:
            if isinstance(item, str):
                said += item
    finally:
        close = getattr(stream, "aclose", None)
        if close is not None:
            await close()
    return said


async def choose_speakers(
    members: Sequence[Character],
    history: Sequence[GroupMessage],
    text: str,
    picker: "ChatService | None",
) -> list[Character]:
    """这一轮由谁接话：先问判断模型，问不出结果就按上一轮没说话的排。"""
    if not members:
        return []
    if picker is None:
        return _fallback_speakers(members, history)
    try:
        said = await _ask(picker, _chooser_prompt(members, history, text))
        match = re.search(r"\{.*\}", said, re.S)
        ids = json.loads(match.group(0)).get("speakers", []) if match else []
    except Exception:
        log.exception("group: could not choose who speaks")
        return _fallback_speakers(members, history)
    picked = [m for m in members if m.id in ids][:MAX_SPEAKERS]
    picked.sort(key=lambda m: ids.index(m.id))
    return picked


async def choose_reactor(
    members: Sequence[Character],
    history: Sequence[GroupMessage],
    picker: "ChatService | None",
    last_speaker: str | None,
) -> Character | None:
    """她们之间接话：刚才那句话之后，还有谁想补一句（没有就到此为止）。"""
    candidates = [m for m in members if m.id != last_speaker]
    if picker is None or len(candidates) < 2:
        return None
    roster = "\n".join(f"- {m.id}：{m.name_zh or m.name}" for m in candidates)
    recent = "\n".join(_history_lines(history[-4:], {m.id: m for m in members}))
    prompt = (
        "几个人在聊天。刚才的对话如下：\n\n"
        f"{recent}\n\n"
        f"在场的人：\n{roster}\n\n"
        "谁最想对最后那句话再补一句？只输出 JSON：{\"speaker\": \"id\"}；"
        "如果大家都已经把话说完、再补一句只会显得多余，输出 {\"speaker\": null}。"
    )
    try:
        said = await _ask(picker, prompt)
        match = re.search(r"\{.*\}", said, re.S)
        who = json.loads(match.group(0)).get("speaker") if match else None
    except Exception:
        log.exception("group: could not choose a reactor")
        return None
    return next((m for m in candidates if m.id == who), None)


async def run_chatter(
    store: Store,
    group: Group,
    roster: Mapping[str, Character],
    chat: "ChatService",
    picker: "ChatService | None" = None,
    tiers: Mapping[str, str] | None = None,
) -> AsyncIterator[tuple[str, str, GroupMessage | None]]:
    """没人说话的时候，让她们自己聊两三句。和回合一样，一句一条地交出去。"""
    members = [roster[cid] for cid in group.participants if cid in roster]
    if len(members) < 2:
        return
    history = messages_of(store, group.id)
    opener = await choose_speakers(members, history, OPENING, picker)
    if not opener:
        opener = members[:1]
    first = opener[0]
    yield ("thinking", first.id, None)
    try:
        segments = await speak(first, members, history, "", chat, opening=True, tiers=tiers)
    except Exception:
        log.exception("group %s: %s could not open", group.id, first.id)
        return
    if not segments:
        return
    message = append_message(store, group.id, "character", segments, first.id)
    history.append(message)
    yield ("message", first.id, message)

    reactor = await choose_reactor(members, history, picker, first.id)
    if reactor is None:
        return
    await asyncio.sleep(REACTION_PAUSE)
    yield ("thinking", reactor.id, None)
    try:
        segments = await speak(reactor, members, history, "", chat, tiers=tiers)
    except Exception:
        log.exception("group %s: %s could not react", group.id, reactor.id)
        return
    if segments:
        message = append_message(store, group.id, "character", segments, reactor.id)
        yield ("message", reactor.id, message)


WELCOME_HINT = (
    "（{name} 刚加入这个群。请大家自然地打个招呼——一两句就好，"
    "不要列队欢迎，也不要说「欢迎加入」这种客套话太多遍。）"
)


async def run_welcome(
    store: Store,
    group: Group,
    roster: Mapping[str, Character],
    joiner: Character,
    chat: "ChatService",
    picker: "ChatService | None" = None,
    tiers: Mapping[str, str] | None = None,
) -> AsyncIterator[tuple[str, str, GroupMessage | None]]:
    """有人刚进群：她先开口打个招呼，另一个人接一句。"""
    members = [roster[cid] for cid in group.participants if cid in roster]
    history = messages_of(store, group.id)
    name = joiner.name_zh or joiner.name

    yield ("thinking", joiner.id, None)
    try:
        segments = await speak(
            joiner, members, history, "", chat, hint=WELCOME_HINT.format(name=name), tiers=tiers
        )
    except Exception:
        log.exception("group %s: %s could not say hello", group.id, joiner.id)
        return
    if segments:
        message = append_message(store, group.id, "character", segments, joiner.id)
        history.append(message)
        yield ("message", joiner.id, message)

    reactor = await choose_reactor(members, history, picker, joiner.id)
    if reactor is None:
        return
    await asyncio.sleep(REACTION_PAUSE)
    yield ("thinking", reactor.id, None)
    try:
        segments = await speak(
            reactor, members, history, "", chat, hint=WELCOME_HINT.format(name=name), tiers=tiers
        )
    except Exception:
        log.exception("group %s: %s could not greet", group.id, reactor.id)
        return
    if segments:
        message = append_message(store, group.id, "character", segments, reactor.id)
        yield ("message", reactor.id, message)


async def run_turn(
    store: Store,
    group: Group,
    roster: Mapping[str, Character],
    text: str,
    chat: "ChatService",
    picker: "ChatService | None" = None,
    forced: Sequence[str] = (),
    tiers: Mapping[str, str] | None = None,
) -> AsyncIterator[tuple[str, str, GroupMessage | None]]:
    """用户发言后，一个个角色接话，产出写好的群消息。"""
    members = [roster[cid] for cid in group.participants if cid in roster]
    user_message = append_message(store, group.id, "user", (Segment("", text),))
    history = [*messages_of(store, group.id), user_message]
    if forced:
        # 用户在输入框里用 /名字 指定了听的人：先由她接，别人还能再补一句。
        speakers = [m for m in members if m.id in forced][:MAX_SPEAKERS]
        if not speakers:
            speakers = await choose_speakers(members, history, text, picker)
    else:
        speakers = await choose_speakers(members, history, text, picker)
    if not speakers:
        return
    # 几个人同时想，谁能先说就先出现：一起发出去，再按顺序交给页面，
    # 整轮等的时间是一句的时间，不是几句相加。
    tasks = [
        asyncio.create_task(speak(speaker, members, history, text, chat, tiers=tiers))
        for speaker in speakers
    ]
    last_message: GroupMessage | None = None
    try:
        for index, (speaker, task) in enumerate(zip(speakers, tasks, strict=True)):
            if index:
                await asyncio.sleep(REACTION_PAUSE)  # 像在想怎么接
            yield ("thinking", speaker.id, None)
            try:
                segments = await task
            except Exception:
                log.exception("group %s: %s could not reply", group.id, speaker.id)
                continue
            if not segments:
                continue
            message = append_message(store, group.id, "character", segments, speaker.id)
            history.append(message)
            last_message = message
            yield ("message", speaker.id, message)
    finally:
        for task in tasks:
            task.cancel()

    if forced:
        return  # 点名了给谁说，就只让她回，别人不插话
    if last_message is None or MAX_EXCHANGES < 1:
        return
    # 别人接一句：每次都试一试，判断模型觉得没话可说才停。
    reactor = await choose_reactor(members, history, picker, last_message.speaker_id)
    if reactor is None:
        return
    await asyncio.sleep(REACTION_PAUSE)
    yield ("thinking", reactor.id, None)
    try:
        segments = await speak(reactor, members, history, text, chat, tiers=tiers)
    except Exception:
        log.exception("group %s: %s could not react", group.id, reactor.id)
        return
    if segments:
        message = append_message(store, group.id, "character", segments, reactor.id)
        yield ("message", reactor.id, message)
