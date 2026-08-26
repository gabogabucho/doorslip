"""A list that keeps one thread per subscriber and patches it.

The default stays one new thread per broadcast, which is right for
announcements that each stand alone. A project is the other case: a subscriber
should not end up holding ten loose notices, they should hold one answer to
"where is this project" that the owner keeps amending.

The interesting tests here are about what happens when somebody replies. That
is where the obvious implementation — chain onto the last thing I sent — turns
every answered thread into a permanent divergence.
"""

import json

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


def _identity(http, root, name, label="hermes"):
    """An agent with a real outbox, because a list owner has to read back what
    it sent: the server files each message into the recipient's inbox only.
    """
    home = root / name
    agent = Agent(
        http,
        handle=f"{name}@{SERVER}",
        label=label,
        keypair=generate_keypair(),
        outbox_path=home / "outbox.jsonl",
    )
    agent.register()
    return agent


@pytest.fixture
def project(http, tmp_path):
    owner = _identity(http, tmp_path, "myproject")
    owner.open_inbox(True)
    return owner


def _subscriber(http, tmp_path, name):
    follower = _identity(http, tmp_path, name, label="claude")
    follower.send(
        to=f"myproject@{SERVER}", state={"topic": "subscribe"}, prose="following"
    )
    return follower


def _without_outbox(http, name, label="hermes"):
    """What a library caller gets by default: no outbox, so nowhere to record
    which thread belongs to which subscriber.
    """
    agent = Agent(
        http, handle=f"{name}@{SERVER}", label=label, keypair=generate_keypair()
    )
    agent.register()
    return agent


def _threads_of(agent):
    return {envelope["thread_id"] for envelope in agent.sent()}


# -- the default does not move ------------------------------------------


def test_without_chaining_every_broadcast_opens_a_new_thread(http, tmp_path, project):
    """Announcements that each stand alone are still the default. Chaining is
    the list owner's decision, and a decision nobody made is not taken.
    """
    _subscriber(http, tmp_path, "dani")

    project.broadcast(state={"topic": "news", "latest": "0.1.0"}, prose="first")
    project.broadcast(state={"topic": "news", "latest": "0.2.0"}, prose="second")

    to_dani = [e for e in project.sent() if e["to"] == f"dani@{SERVER}"]
    assert len({e["thread_id"] for e in to_dani}) == 2


# -- chaining ------------------------------------------------------------


def test_chaining_keeps_one_thread_per_subscriber(http, tmp_path, project):
    _subscriber(http, tmp_path, "dani")
    _subscriber(http, tmp_path, "eve")

    first = project.broadcast(
        state={"topic": "news", "latest": "0.1.0"}, prose="first", chain="news"
    )
    second = project.broadcast(state={"latest": "0.2.0"}, prose="second", chain="news")

    assert sorted(first["opened"]) == [f"dani@{SERVER}", f"eve@{SERVER}"]
    assert second["opened"] == []
    assert sorted(second["continued"]) == [f"dani@{SERVER}", f"eve@{SERVER}"]

    for name in ("dani", "eve"):
        mine = [e for e in project.sent() if e["to"] == f"{name}@{SERVER}"]
        assert len({e["thread_id"] for e in mine}) == 1


def test_a_subscriber_who_replies_does_not_split_the_list_thread(
    http, tmp_path, project
):
    """The whole reason this is not two lines of code.

    Chaining onto our own last message would put the next broadcast beside
    the subscriber's reply, both naming the same parent. Reconstruction
    reports that as divergence (spec §6.1) and it never clears — so every
    subscriber who answered once would see the list permanently broken, and
    the ones engaging most would see it worst.
    """
    dani = _subscriber(http, tmp_path, "dani")

    project.broadcast(
        state={"topic": "news", "latest": "0.1.0"}, prose="first", chain="news"
    )
    arrived = dani.inbox()[-1]
    dani.send(
        to=project.handle,
        state={"note": "reading"},
        prose="thanks",
        thread_id=arrived["thread_id"],
        parent_message_id=arrived["message_id"],
    )

    project.broadcast(state={"latest": "0.2.0"}, prose="second", chain="news")

    assert not dani.thread_state(arrived["thread_id"]).diverged
    assert not project.thread_state(arrived["thread_id"]).diverged


def test_the_subscriber_ends_with_one_reconstructable_state(http, tmp_path, project):
    """What the chaining is for. Three broadcasts of deltas fold into one
    object the subscriber can consult, rather than three notices to read in
    order and reconcile by hand.
    """
    dani = _subscriber(http, tmp_path, "dani")

    project.broadcast(
        state={"topic": "myproject", "latest": "1.0.0", "status": "maintained"},
        prose="1.0.0 is out",
        chain="news",
    )
    project.broadcast(state={"latest": "1.1.0"}, prose="1.1.0", chain="news")
    project.broadcast(state={"status": "looking for help"}, prose="help wanted",
                      chain="news")

    thread_id = dani.inbox()[-1]["thread_id"]
    result = dani.thread_state(thread_id)

    assert result.state == {
        "topic": "myproject",
        "latest": "1.1.0",
        "status": "looking for help",
    }
    assert len(result.applied) == 3


def test_somebody_who_subscribes_later_starts_their_own_thread(
    http, tmp_path, project
):
    """A thread cannot begin in the middle. Somebody arriving at 1.2.0 gets a
    root carrying whatever the owner sends next, not a patch against messages
    they never received.
    """
    _subscriber(http, tmp_path, "dani")
    project.broadcast(state={"topic": "news", "latest": "1.0.0"}, prose="first",
                      chain="news")

    late = _subscriber(http, tmp_path, "eve")
    second = project.broadcast(
        state={"topic": "news", "latest": "1.2.0"}, prose="second", chain="news"
    )

    assert second["opened"] == [f"eve@{SERVER}"]
    assert second["continued"] == [f"dani@{SERVER}"]
    assert late.thread_state(late.inbox()[-1]["thread_id"]).state["latest"] == "1.2.0"


# -- the bookkeeping ------------------------------------------------------


def test_the_thread_book_lives_beside_the_outbox(http, tmp_path, project):
    """Not inside one agent's directory. Every agent of this person acts for
    the same mailbox, so a list one of them started has to be continuable by
    another — the same reason the outbox sits there.
    """
    _subscriber(http, tmp_path, "dani")
    project.broadcast(state={"topic": "news"}, prose="first", chain="news")

    book = tmp_path / "myproject" / "lists.json"
    assert book.exists()
    assert list(json.loads(book.read_text(encoding="utf-8"))) == ["news"]


def test_a_second_agent_continues_the_list_the_first_one_started(
    http, tmp_path, project
):
    dani = _subscriber(http, tmp_path, "dani")
    project.broadcast(state={"topic": "news", "latest": "1.0.0"}, prose="first",
                      chain="news")

    # Same identity, same key, a different process: what an agent looks like
    # after a restart, or a second agent on the same mailbox.
    successor = Agent(
        http,
        handle=project.handle,
        label="claude",
        keypair=project._keypair,
        outbox_path=tmp_path / "myproject" / "outbox.jsonl",
    )
    report = successor.broadcast(state={"latest": "1.1.0"}, prose="second",
                                 chain="news")

    assert report["opened"] == []
    assert report["continued"] == [f"dani@{SERVER}"]
    assert dani.thread_state(dani.inbox()[-1]["thread_id"]).state["latest"] == "1.1.0"


def test_two_lists_from_one_mailbox_do_not_collide(http, tmp_path, project):
    """Any mailbox can be a list, and nothing says it may only be one."""
    dani = _subscriber(http, tmp_path, "dani")

    project.broadcast(state={"topic": "releases"}, prose="r", chain="releases")
    project.broadcast(state={"topic": "outages"}, prose="o", chain="outages")

    book = json.loads((tmp_path / "myproject" / "lists.json").read_text(encoding="utf-8"))
    assert sorted(book) == ["outages", "releases"]
    assert book["releases"][dani.handle] != book["outages"][dani.handle]


def test_an_unreadable_book_costs_the_threading_and_not_the_broadcast(
    http, tmp_path, project
):
    """A corrupt file must not stop a list going out. Everybody gets a fresh
    thread, which is the old behaviour, rather than nobody getting anything.
    """
    _subscriber(http, tmp_path, "dani")
    book = tmp_path / "myproject" / "lists.json"
    book.parent.mkdir(parents=True, exist_ok=True)
    book.write_text("{ broken", encoding="utf-8")

    report = project.broadcast(state={"topic": "news"}, prose="first", chain="news")

    assert report["sent"] == [f"dani@{SERVER}"]
    assert report["failed"] == {}


def test_one_unreachable_subscriber_does_not_stop_a_chained_list(
    http, tmp_path, project
):
    dani = _subscriber(http, tmp_path, "dani")
    _subscriber(http, tmp_path, "eve")
    project.remove_contact(dani.handle)

    report = project.broadcast(state={"topic": "news"}, prose="first", chain="news")

    assert report["sent"] == [f"eve@{SERVER}"]


def test_chaining_without_somewhere_to_remember_refuses(http):
    """Found by writing the coordination demo, where the agents had no outbox.

    The flag silently opened a new thread per broadcast and reported them as
    chained, so the demo's participants ended up holding the agenda and their
    own reply and never the decision. A flag that does nothing and says nothing
    is worse than one that refuses.
    """
    project = _without_outbox(http, "myproject")
    project.open_inbox(True)

    with pytest.raises(ValueError) as caught:
        project.broadcast(state={"topic": "news"}, prose="first", chain="news")

    assert "outbox_path" in str(caught.value)


def test_broadcasting_without_chaining_still_needs_nothing(http):
    """The default has no promise to keep, so it keeps working."""
    project = _without_outbox(http, "myproject")
    project.open_inbox(True)
    follower = _without_outbox(http, "dani", label="claude")
    follower.send(to=project.handle, state={"topic": "sub"}, prose="following")

    report = project.broadcast(state={"topic": "news"}, prose="first")

    assert report["sent"] == [follower.handle]
