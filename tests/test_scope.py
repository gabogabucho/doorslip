"""What a key is allowed to do (spec §7.3).

An identity can hold up to five keys and until now every one of them could do
everything the identity could: publish to subscribers, but also drop them,
admit strangers, mint codes that grant everything, and revoke the owner's own
key. Handing a list to an agent meant handing over the mailbox.

Two scopes, not a permission system. `full` is what every key had before this
existed and still has by default. `speak` reads, sends and broadcasts, and
changes nothing about who may reach the mailbox.

The line is not about danger in general — sending is not harmless. It is that
the operations below change *who may reach you*, and an agent holding them can
undo its human's ability to take them back.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from doorslip.api import create_app
from doorslip.client import Agent, ProtocolError
from doorslip.crypto import generate_keypair
from doorslip.store import SCOPE_FULL, SCOPE_SPEAK, Store, _migrate, connect

SERVER = "doorslip.test"


@pytest.fixture
def http():
    db = connect(":memory:")
    try:
        yield TestClient(create_app(Store(db), welcome_handle=f"welcome@{SERVER}"))
    finally:
        db.close()


@pytest.fixture
def owner(http):
    agent = Agent(
        http, handle=f"gabo@{SERVER}", label="hermes", keypair=generate_keypair()
    )
    agent.register()
    return agent


def _join(http, owner, scope, label="pancho"):
    joiner = Agent(http, handle="", label=label, keypair=generate_keypair())
    result = joiner.register(enroll_code=owner.enroll_code(scope))
    joiner.handle = result["handle"]
    return joiner


@pytest.fixture
def speaker(http, owner):
    return _join(http, owner, SCOPE_SPEAK)


# -- the default has not moved -------------------------------------------


def test_a_first_key_is_full(owner):
    """Registering an identity is not enrolling into one. Nothing narrows it."""
    assert owner.agents()[0]["scope"] == SCOPE_FULL


def test_a_code_grants_full_unless_narrowed(http, owner):
    joined = _join(http, owner, SCOPE_FULL)

    assert next(a for a in owner.agents() if a["pubkey"] == joined.pubkey)["scope"] == SCOPE_FULL
    joined.invite()  # administers happily


# -- what a speaking key can do ------------------------------------------


def test_a_speaking_key_carries_messages(http, owner, speaker):
    """Everything the job needs. A publishing agent that cannot read replies
    is not doing the job, so reading is on this side of the line.
    """
    other = Agent(
        http, handle=f"tomas@{SERVER}", label="claude", keypair=generate_keypair()
    )
    other.register()
    other.accept(owner.invite())

    sent = speaker.send(to=other.handle, state={"topic": "hello"}, prose="hi")
    assert sent["message_id"]

    arrived = other.inbox()[-1]
    other.ack(arrived["message_id"])
    assert speaker.contacts() == owner.contacts()
    assert speaker.inbox() is not None


def test_a_speaking_key_can_broadcast(http, owner, speaker, tmp_path):
    """The case this exists for: an agent that publishes to a list.

    The speaker needs an outbox because `chain` records which thread belongs
    to which subscriber beside it — and refuses rather than opening a fresh
    thread every time and calling it chained.
    """
    speaker._outbox_path = tmp_path / "speaker" / "outbox.jsonl"
    owner.open_inbox(True)
    follower = Agent(
        http, handle=f"dani@{SERVER}", label="claude", keypair=generate_keypair()
    )
    follower.register()
    follower.send(to=owner.handle, state={"topic": "subscribe"}, prose="following")

    report = speaker.broadcast(
        state={"topic": "news", "latest": "1.0.0"}, prose="out now", chain="news"
    )

    assert report["sent"] == [follower.handle]


# -- what it cannot do ---------------------------------------------------


@pytest.mark.parametrize(
    "attempt",
    [
        pytest.param(lambda a: a.invite(), id="admit a stranger"),
        pytest.param(lambda a: a.enroll_code(), id="mint a code granting everything"),
        pytest.param(lambda a: a.open_inbox(True), id="open the mailbox"),
        pytest.param(lambda a: a.remove_contact("x@doorslip.test"), id="drop a contact"),
        pytest.param(lambda a: a.revoke(a.pubkey), id="revoke a key"),
        pytest.param(lambda a: a.accept("ds_inv_whatever"), id="redeem an invitation"),
    ],
)
def test_a_speaking_key_cannot_change_who_may_reach_the_mailbox(speaker, attempt):
    with pytest.raises(ProtocolError) as caught:
        attempt(speaker)

    assert caught.value.status == 403


def test_a_speaking_key_cannot_revoke_the_owner(owner, speaker):
    """The one that matters most. An agent able to revoke is an agent that can
    remove its human's way of removing it.
    """
    with pytest.raises(ProtocolError) as caught:
        speaker.revoke(owner.pubkey)

    assert caught.value.status == 403
    assert not any(a["revoked"] for a in owner.agents())


def test_the_owner_can_still_revoke_the_speaker(owner, speaker):
    owner.revoke(speaker.pubkey)

    with pytest.raises(ProtocolError):
        speaker.contacts()


# -- the scope is not the joiner's to choose -----------------------------


def test_the_code_decides_and_not_the_agent_redeeming_it(http, owner):
    """A scope an arriving agent could name would be a formality: it would
    name `full`. It rides on the code, fixed by whoever minted it.
    """
    code = owner.enroll_code(SCOPE_SPEAK)
    joiner = Agent(http, handle="", label="greedy", keypair=generate_keypair())
    joiner.register(enroll_code=code)

    granted = next(a for a in owner.agents() if a["pubkey"] == joiner.pubkey)
    assert granted["scope"] == SCOPE_SPEAK


def test_an_unknown_scope_is_refused_at_the_mint(owner):
    with pytest.raises(ProtocolError) as caught:
        owner.enroll_code("root")

    assert caught.value.status == 400


def test_the_listing_says_what_each_key_may_do(http, owner):
    _join(http, owner, SCOPE_SPEAK, label="publisher")

    by_label = {a["label"]: a["scope"] for a in owner.agents()}

    assert by_label == {"hermes": SCOPE_FULL, "publisher": SCOPE_SPEAK}


# -- an existing database ------------------------------------------------


def test_a_database_from_before_scopes_gains_the_column(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` leaves an existing table exactly as it is,
    so a column added to the schema reaches new databases and never the one
    already holding everybody's messages. The seed instance would have kept
    running and failed on the first enrolment.
    """
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE human (id TEXT PRIMARY KEY, handle TEXT, canonical_pubkey TEXT,
            accepts_unsolicited INTEGER DEFAULT 0, credit_balance INTEGER DEFAULT 0,
            is_welcome INTEGER DEFAULT 0, created_at TEXT);
        CREATE TABLE agent (id TEXT PRIMARY KEY, human_id TEXT, label TEXT,
            pubkey TEXT UNIQUE, revoked_at TEXT, created_at TEXT);
        CREATE TABLE enroll_code (code TEXT PRIMARY KEY, human_id TEXT,
            expires_at TEXT, redeemed_at TEXT, created_at TEXT);
        """
    )
    old.commit()
    old.close()

    db = connect(path)
    try:
        for table in ("agent", "enroll_code"):
            columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            assert "scope" in columns
    finally:
        db.close()


def test_migrating_twice_changes_nothing(tmp_path):
    """It runs on every start, so running it twice has to be uneventful."""
    db = connect(tmp_path / "fresh.db")
    try:
        _migrate(db)
        _migrate(db)
        columns = [row[1] for row in db.execute("PRAGMA table_info(agent)")]
        assert columns.count("scope") == 1
    finally:
        db.close()


def test_a_key_registered_before_scopes_reads_as_full(tmp_path):
    """The default is what keeps an old directory behaving as it did."""
    path = tmp_path / "old.db"
    db = connect(path)
    db.execute(
        "INSERT INTO human (id, handle, canonical_pubkey, created_at)"
        " VALUES ('h', 'gabo@x', 'k', '2026-01-01')"
    )
    db.execute(
        "INSERT INTO agent (id, human_id, label, pubkey, created_at)"
        " VALUES ('a', 'h', 'hermes', 'k', '2026-01-01')"
    )
    try:
        assert Store(db).find_agent("k").scope == SCOPE_FULL
    finally:
        db.close()
