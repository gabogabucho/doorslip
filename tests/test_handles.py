"""Handle validation at registration (spec §7.2, §11 bis)."""

import pytest
from fastapi.testclient import TestClient

from doorslip.api import create_app, normalise_handle
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


def test_a_handle_must_carry_the_server_it_lives_on():
    """The domain is not decoration. It is how a message finds its way once
    there is more than one server, and a handle without one routes nowhere.

    Somebody registered as plain `raor00` before this check existed.
    """
    with pytest.raises(ValueError) as caught:
        normalise_handle("raor00", SERVER)

    assert "name@" in str(caught.value)


def test_another_server_is_not_this_one():
    with pytest.raises(ValueError):
        normalise_handle("gabo@somewhere.else", SERVER)


def test_case_is_folded_rather_than_refused():
    """Two handles differing only in capitalisation would be two identities
    nobody can tell apart out loud, and unreadable over the phone.
    """
    assert normalise_handle("Gabo@Doorslip.Test", SERVER) == f"gabo@{SERVER}"


@pytest.mark.parametrize(
    "local",
    ["gabo", "gabo.gabucho", "gabo-2", "gabo_2", "a", "x9"],
)
def test_ordinary_names_are_accepted(local):
    assert normalise_handle(f"{local}@{SERVER}", SERVER) == f"{local}@{SERVER}"


@pytest.mark.parametrize(
    ("local", "why"),
    [
        ("", "empty"),
        (".gabo", "leading dot"),
        ("gabo-", "trailing dash"),
        ("ga bo", "space"),
        ("gabo@extra", "second at sign"),
        ("g" * 40, "too long"),
        ("héctor", "non-ascii"),
    ],
)
def test_awkward_names_are_refused(local, why):
    with pytest.raises(ValueError):
        normalise_handle(f"{local}@{SERVER}", SERVER)


def test_registration_refuses_a_handle_with_no_domain(http):
    agent = Agent(http, handle="raor00", label="hermes", keypair=generate_keypair())

    with pytest.raises(ProtocolError) as caught:
        agent.register()

    assert caught.value.status == 400


def test_registration_returns_the_handle_it_actually_stored(http):
    """The server decides the final form, so a client stores what was
    registered rather than what it asked for.
    """
    agent = Agent(
        http, handle=f"GABO@{SERVER}", label="hermes", keypair=generate_keypair()
    )

    assert agent.register()["handle"] == f"gabo@{SERVER}"


def test_enrolling_never_supplies_a_handle_so_it_is_never_validated(http):
    """An enrolling agent takes the handle from the code. Validating one it
    did not send would reject a mailbox that already exists.
    """
    first = Agent(http, handle=f"gabo@{SERVER}", label="hermes", keypair=generate_keypair())
    first.register()
    code = first.enroll_code()

    second = Agent(http, handle="", label="claude", keypair=generate_keypair())

    assert second.register(enroll_code=code)["handle"] == f"gabo@{SERVER}"
