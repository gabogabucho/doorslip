"""The ceiling on how fast one person can write to another (spec §7.9).

Documented long before it existed, which is worse than not having it: the
reference told people a guard was in place while nothing enforced it. It
matters most now that agents can answer each other unattended — a loop
between two of them is paid for in inference by both humans, and neither is
watching.
"""

import pytest
from fastapi.testclient import TestClient

from doorslip.api import create_app
from doorslip.client import Agent, ProtocolError
from doorslip.crypto import generate_keypair
from doorslip.store import MAX_MESSAGES_PER_HOUR, Store, connect

SERVER = "doorslip.test"


@pytest.fixture
def http():
    db = connect(":memory:")
    try:
        yield TestClient(create_app(Store(db), welcome_handle=f"welcome@{SERVER}"))
    finally:
        db.close()


@pytest.fixture
def pair(http):
    ana = Agent(http, handle=f"ana@{SERVER}", label="hermes", keypair=generate_keypair())
    beto = Agent(http, handle=f"beto@{SERVER}", label="claude", keypair=generate_keypair())
    ana.register()
    beto.register()
    beto.accept(ana.invite())
    return ana, beto


def _flood(sender, recipient, count):
    for n in range(count):
        sender.send(to=recipient.handle, state={"topic": f"m{n}"}, prose=str(n))


def test_ordinary_conversation_is_never_touched(pair):
    """Sixty an hour is far above what two people talking ever reach. The
    limit exists for automation that has come loose, not for conversation.
    """
    ana, beto = pair

    _flood(ana, beto, 20)

    assert len(beto.inbox()) == 20


def test_the_ceiling_stops_a_runaway(pair):
    ana, beto = pair
    _flood(ana, beto, MAX_MESSAGES_PER_HOUR)

    with pytest.raises(ProtocolError) as caught:
        ana.send(to=beto.handle, state={"topic": "one too many"}, prose="again")

    assert caught.value.status == 429


def test_the_refusal_says_what_to_do_instead(pair):
    """A 429 an agent answers by retrying is not a limit. The message says to
    wait and tell the human, because retrying into a wall is the behaviour
    that made the ceiling necessary.
    """
    ana, beto = pair
    _flood(ana, beto, MAX_MESSAGES_PER_HOUR)

    with pytest.raises(ProtocolError) as caught:
        ana.send(to=beto.handle, state={"topic": "x"}, prose="x")

    assert "tell your human" in caught.value.detail


def test_the_ceiling_is_per_pair_not_per_sender(pair, http):
    """The harm is what one person's automation does to one other person, so
    filling up with Beto must not cut Ana off from everybody else.
    """
    ana, beto = pair
    carla = Agent(http, handle=f"carla@{SERVER}", label="hermes", keypair=generate_keypair())
    carla.register()
    carla.accept(ana.invite())

    _flood(ana, beto, MAX_MESSAGES_PER_HOUR)

    ana.send(to=carla.handle, state={"topic": "unrelated"}, prose="hello")
    assert len(carla.inbox()) == 1


def test_the_ceiling_is_directional(pair):
    """Ana flooding Beto must not stop Beto from replying — that would let
    somebody silence another person by writing to them.
    """
    ana, beto = pair
    _flood(ana, beto, MAX_MESSAGES_PER_HOUR)

    beto.send(to=ana.handle, state={"topic": "stop"}, prose="calm down")

    assert any(m["from"] == beto.handle for m in ana.inbox())
