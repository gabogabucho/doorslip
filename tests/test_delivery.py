"""What the sender learns about a message after sending it (spec §7.7).

The acknowledgement was built so that a broken thread could be told apart from
a broken transport. The server recorded it from the start; until now the one
person who needed that answer — the sender — had no way to ask.
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
def pair(http):
    ana = Agent(http, handle=f"ana@{SERVER}", label="hermes", keypair=generate_keypair())
    beto = Agent(http, handle=f"beto@{SERVER}", label="claude", keypair=generate_keypair())
    ana.register()
    beto.register()
    beto.accept(ana.invite())
    return ana, beto


def test_a_sent_message_starts_unacknowledged(pair):
    ana, beto = pair
    ana.send(to=beto.handle, state={"topic": "barbecue"}, prose="Saturday?")

    mine = [m for m in ana.delivery() if m["to"] == beto.handle]

    assert len(mine) == 1
    assert mine[0]["acked"] is False
    assert mine[0]["topic"] == "barbecue"


def test_the_sender_sees_the_acknowledgement(pair):
    """The gap this closes: an agent with no reply could not tell "not
    answered yet" from "their agent never saw it", and those call for
    opposite behaviour.
    """
    ana, beto = pair
    ana.send(to=beto.handle, state={"topic": "barbecue"}, prose="Saturday?")
    beto.ack(beto.inbox()[0]["message_id"])

    mine = [m for m in ana.delivery() if m["to"] == beto.handle]

    assert mine[0]["acked"] is True
    assert mine[0]["acked_at"]


def test_unanswered_lists_only_what_is_still_pending(pair):
    ana, beto = pair
    first = ana.send(to=beto.handle, state={"topic": "one"}, prose="1")
    ana.send(to=beto.handle, state={"topic": "two"}, prose="2")
    beto.ack(first["message_id"])

    pending = [m for m in ana.unanswered() if m["to"] == beto.handle]

    assert [m["topic"] for m in pending] == ["two"]


def test_an_age_threshold_keeps_recent_messages_out(pair):
    """The question an unattended agent actually has is not "is anything
    pending" but "has this been sitting long enough that I should stop
    waiting" — a message sent seconds ago is not evidence of anything.
    """
    ana, beto = pair
    ana.send(to=beto.handle, state={"topic": "barbecue"}, prose="Saturday?")

    assert ana.unanswered(older_than_minutes=60) == []
    assert [m["topic"] for m in ana.unanswered() if m["to"] == beto.handle] == ["barbecue"]


def test_you_only_see_your_own_sent_messages(pair):
    ana, beto = pair
    ana.send(to=beto.handle, state={"topic": "barbecue"}, prose="Saturday?")

    assert all(m["to"] != beto.handle for m in beto.delivery())


def test_the_sent_view_never_carries_the_message_body(pair):
    """Same rule as everywhere else. It answers whether something landed, not
    what it said — the body is already in the agent's own outbox.
    """
    ana, beto = pair
    ana.send(to=beto.handle, state={"topic": "barbecue", "where": "my place"}, prose="secret")

    entry = next(m for m in ana.delivery() if m["to"] == beto.handle)

    assert "prose" not in entry
    assert "state" not in entry
    assert "secret" not in str(entry)
    assert "my place" not in str(entry)


def test_the_sent_view_needs_authentication(http):
    response = http.get("/inbox", params={"sent": True})

    assert response.status_code == 401


def test_acknowledging_is_still_the_recipients_move(pair):
    """A sender cannot mark their own message as delivered."""
    ana, beto = pair
    sent = ana.send(to=beto.handle, state={"topic": "barbecue"}, prose="Saturday?")

    with pytest.raises(ProtocolError) as caught:
        ana.ack(sent["message_id"])

    assert caught.value.status == 404
    assert next(m for m in ana.delivery() if m["to"] == beto.handle)["acked"] is False


def test_the_welcome_desk_acknowledges_what_it_answers(http):
    """It has no agent deliberating, but it did incorporate the message — it
    replied. Leaving the acknowledgement off would show every newcomer their
    own greeting sitting unanswered forever.
    """
    ana = Agent(http, handle=f"ana@{SERVER}", label="hermes", keypair=generate_keypair())
    ana.register()

    ana.send(to=f"welcome@{SERVER}", state={"topic": "hello"}, prose="just arrived")

    greeting = next(m for m in ana.delivery() if m["to"] == f"welcome@{SERVER}")
    assert greeting["acked"] is True
