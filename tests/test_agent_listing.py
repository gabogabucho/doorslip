"""Naming a key in order to revoke it (spec §7.3, §7.6).

Reported as DS-04 in a coordinated review. `Agent.agents()` read an `agents`
field from `/contacts` and the server never sent one, so it always returned an
empty list. Revocation was documented, the endpoint worked, and the owner had
no way to find out what to revoke — the enrolment notice named a label the new
agent had chosen for itself, and labels are neither unique nor trustworthy.

The test that matters is the whole loop: list, identify, revoke, and watch the
old key stop working.
"""

import pytest
from fastapi.testclient import TestClient

from doorslip.api import create_app
from doorslip.client import Agent, ProtocolError
from doorslip.crypto import generate_keypair
from doorslip.store import Store, connect

SERVER = "doorslip.test"


@pytest.fixture
def http():
    db = connect(":memory:")
    try:
        yield TestClient(create_app(Store(db), welcome_handle=f"welcome@{SERVER}"))
    finally:
        db.close()


@pytest.fixture
def gabo(http):
    agent = Agent(
        http, handle=f"gabo@{SERVER}", label="hermes", keypair=generate_keypair()
    )
    agent.register()
    return agent


def _enrol(http, owner, label):
    joiner = Agent(http, handle="", label=label, keypair=generate_keypair())
    result = joiner.register(enroll_code=owner.enroll_code())
    joiner.handle = result["handle"]
    return joiner


def test_an_owner_can_see_their_own_key(gabo):
    """It returned an empty list for every caller before this."""
    listed = gabo.agents()

    assert [a["pubkey"] for a in listed] == [gabo.pubkey]
    assert listed[0]["label"] == "hermes"
    assert listed[0]["revoked"] is False
    assert listed[0]["created_at"]


def test_an_enrolled_key_shows_up(gabo, http):
    joined = _enrol(http, gabo, "claude")

    assert {a["pubkey"] for a in gabo.agents()} == {gabo.pubkey, joined.pubkey}


def test_list_identify_revoke_and_the_key_stops_working(gabo, http):
    """The loop the documentation promised and could not complete."""
    joined = _enrol(http, gabo, "claude")

    target = next(a for a in gabo.agents() if a["pubkey"] != gabo.pubkey)
    gabo.revoke(target["pubkey"])

    with pytest.raises(ProtocolError):
        joined.invite()
    assert next(a for a in gabo.agents() if a["pubkey"] == joined.pubkey)["revoked"]


def test_a_revoked_key_stays_listed(gabo, http):
    """Disappearing looks exactly like never having been there, which is the
    wrong thing to show somebody who just revoked a key and wants to be sure.
    """
    joined = _enrol(http, gabo, "claude")
    gabo.revoke(joined.pubkey)

    assert len(gabo.agents()) == 2


def test_two_agents_sharing_a_label_are_still_separable(gabo, http):
    """The reason a label cannot be the identifier.

    Nothing stops a second agent enrolling under the name of the first, and
    an owner told only "claude enrolled" could not say which one to remove.
    """
    first = _enrol(http, gabo, "claude")
    second = _enrol(http, gabo, "claude")

    keys = {a["pubkey"] for a in gabo.agents() if a["label"] == "claude"}

    assert keys == {first.pubkey, second.pubkey}
    gabo.revoke(second.pubkey)
    first.invite()  # the other one is untouched


def test_a_caller_sees_no_keys_but_their_own(gabo, http):
    """Authenticated and scoped. Another identity's keys are not listed here,
    and the pubkey is the thing revocation consumes.
    """
    other = Agent(
        http, handle=f"tomas@{SERVER}", label="codex", keypair=generate_keypair()
    )
    other.register()

    assert {a["pubkey"] for a in gabo.agents()} == {gabo.pubkey}
    assert {a["pubkey"] for a in other.agents()} == {other.pubkey}


def test_the_listing_needs_a_credential(gabo, http):
    assert http.get("/contacts").status_code == 401
