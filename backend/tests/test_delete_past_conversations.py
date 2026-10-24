"""Deleting a past conversation (GLOSSARY.md「历史对话」): it and its record, messages,
conversation events and her reach-outs alike, and when she was to reach out in it, go for good; the memories written in it stay, saying where no
longer, even those written after it was deleted; her current one, someone else's, or
one not there is not deleted. Driven through the backend's external API."""

import asyncio
import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path


from eidolon.config import Settings
from tests.conftest import ClientFactory
from tests.fakes import (
    FakeBackgroundLLM,
    FakeEmbedder,
    FakeJudge,
    FakeLLM,
    FakeRandom,
    FakeReranker,
)
from tests.test_conversation_events import hang_up
from tests.test_conversation_titles import TITLE, listed_titles
from tests.test_delete_account import rows_mentioning
from tests.test_memory_management import memories, memory_client
from tests.test_past_conversations import hers
from tests.test_reach_outs import MIDDLE, REACH_OUT, backend_of, last_contact, talked
from tests.test_shared_memory import CAT, REPLY, memorized, turn
from tests.test_text_turn import send_turn, start_conversation


def memory_sources(database: Path) -> list[str | None]:
    """The conversation each memory was written in, as kept."""
    with sqlite3.connect(database) as db:
        return [c for (c,) in db.execute("SELECT conversation_id FROM memories ORDER BY id")]


async def test_a_past_conversation_and_its_record_are_deleted_and_its_memories_kept(
    make_client: ClientFactory, settings: Settings
):
    client = memory_client(make_client, FakeLLM(REPLY, REPLY), CAT)
    past = await start_conversation(client)
    await turn(client, past, "我家的猫叫小白哦")
    hang_up(settings.database_path, past)
    current = await start_conversation(client)
    await turn(client, current, "早上好")

    res = await client.delete(f"/api/conversations/{past}")

    assert res.status_code == 204
    assert [c["id"] for c in await hers(client)] == [current]
    assert (await client.get(f"/api/conversations/{past}/messages")).status_code == 404
    assert rows_mentioning(settings.database_path, past) == {}
    assert [m["text"] for m in await memories(client)] == [CAT]
    assert memory_sources(settings.database_path) == [None]
    # The current conversation is untouched.
    messages = (await client.get(f"/api/conversations/{current}/messages")).json()
    assert [m["text"] for m in messages if m["role"] == "user"] == ["早上好"]


async def test_her_reach_outs_in_it_and_when_she_was_to_come_go_with_it(
    make_client: ClientFactory, settings: Settings
):
    client, _, clock, past = await talked(
        make_client, REACH_OUT,
        settings=replace(settings, reach_outs=True), random=FakeRandom(MIDDLE),
    )
    last_contact(settings.database_path, past, clock.now - timedelta(hours=18))
    await backend_of(client).reach_outs.run_due()
    assert set(rows_mentioning(settings.database_path, past)) >= {
        "reach_outs", "reach_out_schedules",
    }
    await start_conversation(client)

    assert (await client.delete(f"/api/conversations/{past}")).status_code == 204

    assert rows_mentioning(settings.database_path, past) == {}


async def test_her_current_conversation_is_not_deleted(make_client: ClientFactory):
    client = make_client(FakeLLM(REPLY))
    await start_conversation(client)
    current = await start_conversation(client)
    await turn(client, current, "早上好")
    other = await start_conversation(client, "kogane")  # someone else's current is not hers

    res = await client.delete(f"/api/conversations/{current}")

    assert res.status_code == 409
    assert res.json()["detail"] == "当前对话不能删除"
    assert (await client.delete(f"/api/conversations/{other}")).status_code == 409
    assert len(await hers(client)) == 2


async def test_only_their_own_conversations_that_are_there_are_deleted(
    make_client: ClientFactory,
):
    producer = make_client(FakeLLM())
    friend = make_client(FakeLLM(), user="friend")
    past = await start_conversation(producer)
    await start_conversation(producer)

    assert (await friend.delete(f"/api/conversations/{past}")).status_code == 404
    assert len(await hers(producer)) == 2
    assert (await producer.delete(f"/api/conversations/{past}")).status_code == 204
    res = await producer.delete(f"/api/conversations/{past}")
    assert res.status_code == 404
    assert res.json()["detail"] == "对话不存在"


async def test_deleting_a_conversation_needs_a_session(make_client: ClientFactory):
    client = make_client(FakeLLM(), user=None)

    assert (await client.delete("/api/conversations/whatever")).status_code == 401


async def test_a_memory_written_after_its_conversation_was_deleted_is_kept(
    make_client: ClientFactory, settings: Settings
):
    gate = asyncio.Event()
    client = make_client(
        FakeLLM(REPLY), retrieval_judge=FakeJudge(), embedder=FakeEmbedder(),
        reranker=FakeReranker("制作人"),
        background_llm=FakeBackgroundLLM(memorized(CAT), gate=gate),
    )
    past = await start_conversation(client)
    await turn(client, past, "我家的猫叫小白哦")  # done with the model still writing it down
    await start_conversation(client)

    assert (await client.delete(f"/api/conversations/{past}")).status_code == 204
    gate.set()

    assert [m["text"] for m in await memories(client)] == [CAT]
    assert memory_sources(settings.database_path) == [None]


async def test_a_conversation_deleted_while_being_named_stays_deleted(
    make_client: ClientFactory, settings: Settings
):
    gate = asyncio.Event()
    client = make_client(
        FakeLLM(*[REPLY] * 3), background_llm=FakeBackgroundLLM(TITLE, gate=gate)
    )
    past = await start_conversation(client)
    for i in range(3):
        assert (await send_turn(client, past, f"第{i}句"))[-1]["type"] == "done"
    await start_conversation(client)

    assert (await client.delete(f"/api/conversations/{past}")).status_code == 204
    gate.set()

    assert await listed_titles(client) == [None]
    assert rows_mentioning(settings.database_path, past) == {}
