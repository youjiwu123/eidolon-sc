"""The user's own switch for reach-outs (GLOSSARY.md「搭话」): on by default, kept on the server
so that it holds on every device; the account says it where the deployer turns reach-outs on,
nothing where not, and it cannot be set then. One who switches it off is reached out to by
none of the characters, their times left as they are, and is again once it is back on.

Driven through the backend's external API, with the reach-outs' ticks taken by hand
(`ReachOuts.run_due`), as in `test_reach_outs`."""

from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from eidolon.config import Settings
from tests.conftest import ClientFactory
from tests.fakes import FakeRandom
from tests.test_reach_outs import (
    REACH_OUT,
    REPLY,
    backend_of,
    last_contact,
    reach_outs,
    record,
    talked,
)
from tests.test_text_turn import send_turn, start_conversation

SWITCH = "/api/account/reach-outs"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """With reach-outs on in `.env`."""
    return replace(settings, reach_outs=True)


async def switched_on(client: httpx.AsyncClient) -> bool | None:
    res = await client.get("/api/account")
    assert res.status_code == 200
    return res.json()["reach_outs"]


async def switch(client: httpx.AsyncClient, on: bool) -> None:
    res = await client.put(SWITCH, json={"on": on})
    assert res.status_code == 204, res.text


async def test_the_account_says_nothing_where_the_deployer_has_them_off_else_the_users_switch(
    make_client: ClientFactory, settings: Settings
):
    client, *_ = await talked(make_client, settings=replace(settings, reach_outs=False))
    on, *_ = await talked(make_client, settings=replace(settings, reach_outs=True))
    by_default = await switched_on(on)

    await switch(on, False)

    assert (await switched_on(client), by_default, await switched_on(on)) == (None, True, False)


async def test_it_cannot_be_set_where_the_deployer_has_them_off_nor_to_what_is_not_on_or_off(
    make_client: ClientFactory, settings: Settings
):
    off, *_ = await talked(make_client, settings=replace(settings, reach_outs=False))
    on, *_ = await talked(make_client, settings=replace(settings, reach_outs=True))

    refused = (await off.put(SWITCH, json={"on": False})).status_code
    invalid = (await on.put(SWITCH, json={"on": "no"})).status_code

    assert (refused, invalid) == (409, 422)
    assert (await switched_on(on)) is True


async def test_switched_off_none_of_them_comes_and_switched_on_again_she_comes_when_her_time_is(
    make_client: ClientFactory, settings: Settings
):
    client, _, clock, conversation = await talked(
        make_client, REPLY, REACH_OUT, REACH_OUT, random=FakeRandom(0, 0)
    )
    other = await start_conversation(client, "kogane")
    await send_turn(client, other, "我去练习了")
    for each in (conversation, other):  # trust: 12–24 hours, at its start, as drawn once on
        last_contact(settings.database_path, each, clock.now - timedelta(hours=12))
    reach_out = backend_of(client).reach_outs

    await switch(client, False)
    await reach_out.run_due()
    while_off = [reach_outs(await record(client, each)) for each in (conversation, other)]
    await switch(client, True)
    await reach_out.run_due()

    assert while_off == [[], []]
    assert [len(reach_outs(await record(client, each))) for each in (conversation, other)] == [1, 1]

