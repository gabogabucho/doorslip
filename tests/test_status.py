"""One answer to "did anything happen?" (feedback, August 2026).

A first user said the three things they could not see were receipt, reply and
subscription. None of them were missing — they were a join away, across
`inbox`, `sent` and `contacts`, and somebody had to hold the result in their
head.

The distinction the tests below defend is that a sent slip has three states
and not two. Not delivered yet, taken in and unanswered, and answered each
call for different behaviour from the agent reading them, so collapsing them
into "sent" loses the only thing that says what to do next.
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


def _person(http, name, label="hermes"):
    agent = Agent(
        http, handle=f"{name}@{SERVER}", label=label, keypair=generate_keypair()
    )
    agent.register()
    return agent


def _read_everything(agent):
    """Clear the inbox so a test measures what it put there.

    Accepting an invitation makes the welcome desk write to the inviter, and
    that notice is a real unread message — see the test that says so.
    """
    for message in agent.inbox():
        if not message.get("acked"):
            agent.ack(message["message_id"])


@pytest.fixture
def pair(http):
    gabo = _person(http, "gabo")
    tomas = _person(http, "tomas", label="claude")
    tomas.accept(gabo.invite())
    _read_everything(gabo)
    _read_everything(tomas)
    return gabo, tomas


# -- the three states of something you sent ------------------------------


def test_a_slip_nobody_has_taken_in_yet(pair):
    gabo, tomas = pair
    gabo.send(to=tomas.handle, state={"topic": "barbecue"}, prose="Saturday?")

    sent = gabo.status()["you_sent"]

    assert [s["topic"] for s in sent["not_delivered_yet"]] == ["barbecue"]
    assert sent["taken_in_awaiting_reply"] == []
    assert sent["answered"] == []


def test_taken_in_is_not_answered(pair):
    """The state that used to be invisible, and the one that says keep
    waiting: their agent has it and has not written back.
    """
    gabo, tomas = pair
    gabo.send(to=tomas.handle, state={"topic": "barbecue"}, prose="Saturday?")
    tomas.ack(tomas.inbox()[-1]["message_id"])

    sent = gabo.status()["you_sent"]

    assert sent["not_delivered_yet"] == []
    assert [s["topic"] for s in sent["taken_in_awaiting_reply"]] == ["barbecue"]
    assert sent["answered"] == []


def test_a_reply_moves_it_to_answered(pair):
    gabo, tomas = pair
    first = gabo.send(to=tomas.handle, state={"topic": "barbecue"}, prose="Saturday?")
    arrived = tomas.inbox()[-1]
    tomas.send(
        to=gabo.handle,
        state={"status": "confirmed"},
        prose="Saturday works",
        thread_id=arrived["thread_id"],
        parent_message_id=arrived["message_id"],
    )

    sent = gabo.status()["you_sent"]

    assert [s["topic"] for s in sent["answered"]] == ["barbecue"]
    assert sent["taken_in_awaiting_reply"] == []
    assert sent["not_delivered_yet"] == []


def test_a_reply_counts_only_when_it_answers_ours(pair):
    """Precision, not "something newer in the thread".

    Our own follow-up is newer and is not an answer. Counting by recency
    would report a conversation as answered because we kept talking into it.
    """
    gabo, tomas = pair
    first = gabo.send(to=tomas.handle, state={"topic": "barbecue"}, prose="Saturday?")
    gabo.send(
        to=tomas.handle,
        state={"note": "or Sunday"},
        prose="or Sunday",
        thread_id=first["thread_id"],
        parent_message_id=first["message_id"],
    )

    sent = gabo.status()["you_sent"]

    assert sent["answered"] == []
    assert len(sent["not_delivered_yet"]) == 2


# -- what arrived for you -------------------------------------------------


def test_unread_counts_what_is_waiting_and_says_who_from(pair):
    gabo, tomas = pair
    tomas.send(to=gabo.handle, state={"topic": "hello"}, prose="hi")

    for_you = gabo.status()["for_you"]

    assert for_you["unread"] == 1
    assert for_you["from"] == [tomas.handle]
    assert "inbox" in for_you["next"]


def test_nothing_waiting_offers_no_next_step(pair):
    """A command to run when there is nothing to run it for is noise, and an
    agent that reads JSON will suggest it to its human anyway.
    """
    gabo, _ = pair

    assert gabo.status()["for_you"]["next"] is None


def test_acking_clears_it(pair):
    gabo, tomas = pair
    tomas.send(to=gabo.handle, state={"topic": "hello"}, prose="hi")
    gabo.ack(gabo.inbox()[-1]["message_id"])

    assert gabo.status()["for_you"]["unread"] == 0


# -- the mailbox itself ---------------------------------------------------


def test_a_notice_from_the_server_is_unread_like_anything_else(http):
    """The desk telling you a second key was enrolled on your mailbox is
    something to read, and filtering it out of the count to make the number
    look tidier would hide the one message nobody chose to send you.
    """
    gabo = _person(http, "gabo")
    tomas = _person(http, "tomas", label="claude")
    tomas.accept(gabo.invite())

    for_you = gabo.status()["for_you"]

    assert for_you["unread"] == 1
    assert for_you["from"] == [f"welcome@{SERVER}"]


def test_the_mailbox_reports_whether_it_is_open(pair):
    """The third thing the feedback named. Whether strangers can subscribe is
    a fact about your mailbox that nothing put next to the rest.
    """
    gabo, _ = pair

    assert gabo.status()["mailbox"]["open_to_strangers"] is False

    gabo.open_inbox(True)
    after = gabo.status()["mailbox"]

    assert after["open_to_strangers"] is True
    assert after["contacts"] == 1
    assert after["keys"] == 1


# -- the wording ----------------------------------------------------------


def test_it_says_what_taken_in_means(pair):
    """An agent reporting "they read it" is inventing something nobody told
    it. The distinction ships with the answer rather than in a document the
    reader has not opened.
    """
    gabo, _ = pair

    note = gabo.status()["what_taken_in_means"]

    assert "agent incorporated" in note
    assert "never that their human read it" in note
