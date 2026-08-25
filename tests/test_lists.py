"""Mailboxes anybody may write to, and taking somebody back out.

Not a special case for one account. Any identity can be a list somebody
subscribes to — a project announcing its releases, a person publishing notes —
and the same identity can stop being one.
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
    store = Store(db)
    try:
        client = TestClient(create_app(store, welcome_handle=f"welcome@{SERVER}"))
        # Kept on the client so a test can act as the welcome desk, which is
        # the one identity no Agent can hold.
        client.store = store
        yield client
    finally:
        db.close()


def _identity(http, name, label="hermes"):
    agent = Agent(
        http, handle=f"{name}@{SERVER}", label=label, keypair=generate_keypair()
    )
    agent.register()
    return agent


def test_a_closed_mailbox_still_refuses_strangers(http):
    """The default does not move. Opening one is a deliberate act."""
    owner = _identity(http, "gabo")
    stranger = _identity(http, "someone", label="claude")

    with pytest.raises(ProtocolError) as caught:
        stranger.send(to=owner.handle, state={"topic": "hello"}, prose="hi")

    assert caught.value.status == 403


def test_writing_to_an_open_mailbox_is_how_you_subscribe(http):
    """No separate subscribe endpoint. The first message is the subscription,
    which means a list needs nothing an ordinary conversation does not have.
    """
    project = _identity(http, "myproject")
    project.open_inbox(True)
    follower = _identity(http, "dani", label="claude")

    follower.send(to=project.handle, state={"topic": "subscribe"}, prose="following")

    assert follower.handle in project.contacts()
    assert project.handle in follower.contacts()


def test_a_list_reaches_everybody_who_subscribed(http):
    project = _identity(http, "myproject")
    project.open_inbox(True)
    followers = [_identity(http, name, label="claude") for name in ("dani", "hash")]
    for follower in followers:
        follower.send(to=project.handle, state={"topic": "subscribe"}, prose="hi")

    result = project.broadcast(
        state={"topic": "release 1.0", "status": "done"}, prose="Shipped."
    )

    assert sorted(result["sent"]) == sorted(f.handle for f in followers)
    for follower in followers:
        assert any(m["envelope"]["prose"] == "Shipped." for m in follower.inbox())


def test_one_unreachable_subscriber_does_not_stop_the_rest(http):
    """A list of a hundred cannot fail because one of them is gone."""
    project = _identity(http, "myproject")
    project.open_inbox(True)
    good = _identity(http, "dani", label="claude")
    good.send(to=project.handle, state={"topic": "subscribe"}, prose="hi")
    # Somebody in the book who no longer accepts this list.
    gone = _identity(http, "hash", label="claude")
    gone.send(to=project.handle, state={"topic": "subscribe"}, prose="hi")
    gone.remove_contact(project.handle)

    result = project.broadcast(state={"topic": "release"}, prose="Shipped.")

    assert good.handle in result["sent"]
    assert gone.handle in result["failed"]


def test_closing_the_mailbox_stops_new_subscribers(http):
    """Closing does not evict anybody. It stops the next stranger."""
    project = _identity(http, "myproject")
    project.open_inbox(True)
    early = _identity(http, "dani", label="claude")
    early.send(to=project.handle, state={"topic": "subscribe"}, prose="hi")

    project.open_inbox(False)
    late = _identity(http, "hash", label="claude")

    with pytest.raises(ProtocolError) as caught:
        late.send(to=project.handle, state={"topic": "subscribe"}, prose="hi")
    assert caught.value.status == 403
    assert early.handle in project.contacts()


def test_removing_a_contact_stops_them_writing(http):
    owner = _identity(http, "gabo")
    other = _identity(http, "dani", label="claude")
    other.accept(owner.invite())

    owner.remove_contact(other.handle)

    with pytest.raises(ProtocolError) as caught:
        other.send(to=owner.handle, state={"topic": "x"}, prose="still here?")
    assert caught.value.status == 403


def test_removing_is_one_sided(http):
    """You decide who reaches you, not who remembers you. Removing both rows
    would let anybody sever a relationship they are only half of.
    """
    owner = _identity(http, "gabo")
    other = _identity(http, "dani", label="claude")
    other.accept(owner.invite())

    owner.remove_contact(other.handle)

    assert other.handle not in owner.contacts()
    assert owner.handle in other.contacts()


def test_removing_somebody_who_is_not_there_says_so(http):
    owner = _identity(http, "gabo")

    with pytest.raises(ProtocolError) as caught:
        owner.remove_contact(f"nobody@{SERVER}")

    assert caught.value.status == 404


def test_the_address_book_reports_whether_it_is_open(http):
    project = _identity(http, "myproject")

    assert not _unwrap_open(project)
    project.open_inbox(True)
    assert _unwrap_open(project)


def _unwrap_open(agent):
    from doorslip.client import _unwrap

    return _unwrap(agent._http.get("/contacts", headers=agent._auth_headers()))["open"]


def test_the_welcome_desk_does_not_subscribe_to_a_list(http):
    """The desk is the server, not somebody who can follow anything.

    `announce` reaches every registered identity, and when one of them held
    an open mailbox the notice was read as a subscription: the server landed
    in a user's address book, every later broadcast wrote to the desk, and
    the desk greeted it back. Deliver the notice, create nothing.
    """
    from doorslip.api import _ensure_welcome_agent

    project = _identity(http, "myproject")
    project.open_inbox(True)

    desk = _ensure_welcome_agent(http.store, f"welcome@{SERVER}")
    raw, signature = desk.announce(project.handle, "notice", "something changed")
    response = http.post(
        "/inbox", content=raw, headers={"X-Doorslip-Signature": signature}
    )

    assert response.status_code == 202
    assert response.json()["subscribed"] is None
    assert f"welcome@{SERVER}" not in project.contacts()


def test_a_person_still_subscribes_by_writing(http):
    """The rule above must not have closed the door it was narrowing."""
    project = _identity(http, "myproject")
    project.open_inbox(True)
    follower = _identity(http, "dani", label="claude")

    follower.send(to=project.handle, state={"topic": "subscribe"}, prose="following")

    assert follower.handle in project.contacts()
