"""An admin sets a user's affinity with a character by hand (the admin console's users page):
the value from the lowest to the highest, out of any cooldown; the user still sees only the
tier, never the value."""

from dataclasses import replace

import httpx
import pytest

from eidolon.config import Settings
from tests.conftest import ClientFactory
from tests.fakes import FAKE_JUDGE, START, FakeIdentityProvider, FakeLLM
from tests.test_admin_users import admin_client, github_user
from tests.test_affinity import keep, status

ADMIN = ("github", "9001")


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """With an admin, the judge model, and affinity on in `.env`."""
    return replace(
        settings,
        admins=frozenset({ADMIN}),
        judge=FAKE_JUDGE,
        affinity_and_gifts=True,
    )


async def user_id(admin: httpx.AsyncClient, name: str) -> str:
    res = await admin.get("/api/admin/users", params={"q": name})
    assert res.status_code == 200, res.text
    [found] = [u for u in res.json()["users"] if u["name"] == name]
    return found["id"]


async def test_an_admin_sets_affinity_and_the_user_sees_only_the_tier(
    make_client: ClientFactory, github: FakeIdentityProvider
):
    admin = await admin_client(make_client, github)
    octo = await github_user(make_client, github, "1001", "octo", "octo@example.com")
    uid = await user_id(admin, "octo")

    listed = await admin.get(f"/api/admin/users/{uid}/affinity")
    assert listed.status_code == 200, listed.text
    [mano] = [a for a in listed.json()["affinities"] if a["character_id"] == "mano"]
    assert mano == {"character_id": "mano", "value": 50.0, "tier": "trust"}

    set_it = await admin.put(
        f"/api/admin/users/{uid}/affinity", json={"character_id": "mano", "value": 120}
    )
    assert set_it.status_code == 200, set_it.text
    assert set_it.json() == {"character_id": "mano", "value": 120.0, "tier": "love"}

    seen = await status(octo)
    assert seen.json() == {"tier": "love", "cooldown_until": None}


async def test_setting_it_lifts_a_cooldown(
    make_client: ClientFactory,
    github: FakeIdentityProvider,
    settings: Settings,
):
    admin = await admin_client(make_client, github)
    octo = await github_user(make_client, github, "1001", "octo", "octo@example.com")
    uid = await user_id(admin, "octo")
    keep(settings.database_path, "octo", 30, START, cooling=True)

    res = await admin.put(
        f"/api/admin/users/{uid}/affinity", json={"character_id": "mano", "value": 60}
    )

    assert res.json() == {"character_id": "mano", "value": 60.0, "tier": "trust"}
    assert (await status(octo)).json() == {"tier": "trust", "cooldown_until": None}


async def test_it_is_held_at_the_lowest_and_the_highest(
    make_client: ClientFactory, github: FakeIdentityProvider
):
    admin = await admin_client(make_client, github)
    await github_user(make_client, github, "1001", "octo", "octo@example.com")
    uid = await user_id(admin, "octo")

    low = await admin.put(
        f"/api/admin/users/{uid}/affinity", json={"character_id": "mano", "value": 0}
    )
    high = await admin.put(
        f"/api/admin/users/{uid}/affinity", json={"character_id": "mano", "value": 120}
    )

    assert (low.json()["tier"], high.json()["tier"]) == ("cold", "love")


async def test_an_unknown_user_character_or_value_is_refused(
    make_client: ClientFactory, github: FakeIdentityProvider
):
    admin = await admin_client(make_client, github)
    await github_user(make_client, github, "1001", "octo", "octo@example.com")
    uid = await user_id(admin, "octo")

    assert (
        await admin.get("/api/admin/users/nope/affinity")
    ).status_code == 404
    assert (
        await admin.put(
            f"/api/admin/users/{uid}/affinity",
            json={"character_id": "nope", "value": 60},
        )
    ).status_code == 404
    assert (
        await admin.put(
            f"/api/admin/users/{uid}/affinity",
            json={"character_id": "mano", "value": 121},
        )
    ).status_code == 422


async def test_without_being_an_admin_it_is_not_there(
    make_client: ClientFactory, github: FakeIdentityProvider
):
    await admin_client(make_client, github)
    octo = await github_user(make_client, github, "1001", "octo", "octo@example.com")

    res = await octo.get("/api/admin/users/whatever/affinity")

    assert res.status_code == 404
