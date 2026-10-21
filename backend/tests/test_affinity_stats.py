"""Stats (GLOSSARY.md「统计」), the characters by her affinity with the user: in each of the
five tiers, from cold to intimate, those in it, in the idols' order, by tier only, never by
value, so that none in a tier reads higher than another; one in a cooldown is in cold all
the same. Where affinity is off, there are no tiers, and no gifts either. Driven through the
backend's external API, with what is kept in the database set directly."""

from dataclasses import replace

import pytest

from eidolon.config import Settings
from tests.conftest import ClientFactory
from tests.fakes import FAKE_JUDGE, START, FakeClock, FakeLLM
from tests.test_affinity import keep
from tests.test_stats import meguru, stats  # noqa: F401 (a fixture)


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """With the judge model, and affinity on in `.env`."""
    return replace(settings, judge=FAKE_JUDGE, affinity_and_gifts=True)


async def test_each_tier_has_those_in_it_in_the_idols_order_by_tier_alone(
    make_client: ClientFactory, settings: Settings, meguru: None
):
    client = make_client(FakeLLM(), clock=FakeClock())
    database = settings.database_path
    # Kogane highest of the three, and configured before Meguru, but last in the idols' order.
    keep(database, "producer", 70, START)
    keep(database, "producer", 66, START, character="meguru")
    keep(database, "producer", 80, START, character="kogane")

    got = await stats(client)

    assert got["affinity"] == [
        {"tier": "cold", "characters": []},
        {"tier": "distant", "characters": []},
        {"tier": "trust", "characters": []},
        {"tier": "close", "characters": ["mano", "meguru", "kogane"]},
        {"tier": "intimate", "characters": []},
        {"tier": "love", "characters": []},
    ]


async def test_one_in_a_cooldown_is_in_cold_and_one_never_spoken_to_in_trust(
    make_client: ClientFactory, settings: Settings, meguru: None
):
    client = make_client(FakeLLM(), clock=FakeClock())
    keep(settings.database_path, "producer", 0, START, cooling=True)
    keep(settings.database_path, "producer", 90, START, character="kogane")

    tiers = {group["tier"]: group["characters"] for group in (await stats(client))["affinity"]}

    assert tiers == {
        "cold": ["mano"], "distant": [], "trust": ["meguru"], "close": [],
        "intimate": ["kogane"], "love": [],
    }


async def test_each_user_has_their_own(make_client: ClientFactory, settings: Settings):
    client = make_client(FakeLLM(), clock=FakeClock())
    other = make_client(FakeLLM(), "other", clock=FakeClock())
    keep(settings.database_path, "producer", 30, START)

    mine = {g["tier"]: g["characters"] for g in (await stats(client))["affinity"]}
    theirs = {g["tier"]: g["characters"] for g in (await stats(other))["affinity"]}

    assert mine["distant"] == ["mano"] and mine["trust"] == ["kogane"]
    assert theirs["distant"] == [] and theirs["trust"] == ["mano", "kogane"]


async def test_where_affinity_is_off_there_are_no_tiers_and_no_gifts(
    make_client: ClientFactory, settings: Settings
):
    client = make_client(
        FakeLLM(), settings=replace(settings, affinity_and_gifts=False), clock=FakeClock()
    )
    keep(settings.database_path, "producer", 90, START)

    got = await stats(client)

    assert got["affinity"] is None
    assert got["range"]["gifts"] is None
    assert got["range"]["turns"] == 0  # the rest as ever
