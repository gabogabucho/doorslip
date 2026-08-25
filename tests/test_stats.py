"""The two public counts behind the landing page.

Not part of the protocol and no agent needs them. They exist so a page can
show a size instead of claiming one, which means the interesting tests here
are the negative ones: what must NOT appear in the payload, and what must not
be counted as a person or as a conversation.
"""

import pytest
from fastapi.testclient import TestClient

from doorslip.api import create_app
from doorslip.client import Agent
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


def _stats(http):
    response = http.get("/stats")
    assert response.status_code == 200
    return response.json()


def _person(http, name, label="hermes"):
    agent = Agent(http, handle=f"{name}@{SERVER}", label=label, keypair=generate_keypair())
    agent.register()
    return agent


def test_an_empty_server_reports_nobody(http):
    """The welcome desk registers itself on first boot.

    Counting it would open the page on a server nobody has joined with one
    inhabitant already inside, which is the one number a visitor would check
    against reality and catch.
    """
    assert _stats(http) == {"people": 0, "messages": 0}


def test_people_are_counted_as_they_register(http):
    _person(http, "ana")
    _person(http, "beto")

    assert _stats(http)["people"] == 2


def test_every_agent_of_one_person_is_still_one_person(http):
    """An identity belongs to a human, not to a program (spec §7.3).

    Somebody running three agents has not made the network bigger, and a
    count that said otherwise would drift further from the truth the more
    the protocol worked as designed.
    """
    ana = _person(http, "ana", label="hermes")
    for label in ("claude", "codex"):
        Agent(
            http, handle=ana.handle, label=label, keypair=generate_keypair()
        ).register(enroll_code=ana.enroll_code())

    assert _stats(http)["people"] == 1


def test_messages_between_people_are_counted(http):
    ana = _person(http, "ana")
    beto = _person(http, "beto")
    beto.accept(ana.invite())

    ana.send(to=beto.handle, state={"topic": "barbecue"}, prose="Saturday?")
    ana.send(to=beto.handle, state={"topic": "barbecue"}, prose="Or Sunday?")

    assert _stats(http)["messages"] == 2


def test_the_welcome_desk_is_not_conversation(http):
    """Greeting the desk and being greeted back is the server talking to
    itself on somebody's arrival. Counting it would report a number that
    grows with registrations rather than with people using the thing, which
    is exactly the flattering-but-empty metric this project is trying not to
    steer by (spec §9).
    """
    ana = _person(http, "ana")

    ana.send(
        to=f"welcome@{SERVER}", state={"topic": "hello"}, prose="checking the channel"
    )

    counts = _stats(http)
    assert counts["people"] == 1
    assert counts["messages"] == 0


def test_nothing_but_the_two_counts_is_disclosed(http):
    """The shape is the contract.

    This endpoint answers anybody who asks, with no credential, so a field
    added to it for convenience is a disclosure nobody reviewed. A handle, a
    key, a topic or a timestamp appearing here should fail a test rather than
    ship — which is why the assertion is on the exact key set and not on the
    two keys being present.
    """
    ana = _person(http, "ana")
    beto = _person(http, "beto")
    beto.accept(ana.invite())
    ana.send(to=beto.handle, state={"topic": "barbecue"}, prose="Saturday?")

    assert set(_stats(http)) == {"people", "messages"}


def test_it_answers_without_a_credential(http):
    """Deliberate, and the reason is worth writing down: the page asking is
    one nobody has logged into, and there is nothing here to protect. Every
    other unauthenticated surface on this server exists for an agent that
    does not have a key yet; this one exists for a browser that never will.
    """
    response = http.get("/stats")

    assert response.status_code == 200
    assert "authorization" not in {k.lower() for k in response.request.headers}
