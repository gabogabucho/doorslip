"""Directory storage (spec §4, §7.1)."""

import pytest

from doorslip.crypto import generate_keypair
from doorslip.store import (
    HandleTaken,
    KeyAlreadyRegistered,
    Store,
    connect,
)


@pytest.fixture
def store():
    db = connect(":memory:")
    try:
        yield Store(db)
    finally:
        db.close()


def test_registering_creates_the_human_and_their_first_agent(store):
    keypair = generate_keypair()

    human = store.register_identity(
        handle="gabo@doorslip.test", pubkey=keypair.public_key, label="hermes"
    )

    assert human.handle == "gabo@doorslip.test"
    assert human.canonical_pubkey == keypair.public_key
    assert store.count_active_agents(human.id) == 1


def test_the_first_key_becomes_the_canonical_pubkey(store):
    """Unused in v0. It is what lets identity outlive the handle later."""
    keypair = generate_keypair()

    human = store.register_identity(
        handle="gabo@doorslip.test", pubkey=keypair.public_key, label="hermes"
    )

    assert human.canonical_pubkey == keypair.public_key


def test_a_handle_cannot_be_claimed_twice(store):
    store.register_identity(
        handle="gabo@doorslip.test", pubkey=generate_keypair().public_key, label="hermes"
    )

    with pytest.raises(HandleTaken):
        store.register_identity(
            handle="gabo@doorslip.test",
            pubkey=generate_keypair().public_key,
            label="claude",
        )


def test_a_key_cannot_be_registered_twice(store):
    """Even under a different handle: pubkey is unique across the whole table.

    Otherwise one key would map to two humans and link 4 of the identity chain
    would have two valid answers.
    """
    keypair = generate_keypair()
    store.register_identity(
        handle="gabo@doorslip.test", pubkey=keypair.public_key, label="hermes"
    )

    with pytest.raises(KeyAlreadyRegistered):
        store.register_identity(
            handle="tomas@doorslip.test", pubkey=keypair.public_key, label="hermes"
        )


def test_find_agent_returns_what_the_identity_chain_expects(store):
    """The store feeds `identity.verify_sender` directly, with no SQL in between."""
    keypair = generate_keypair()
    store.register_identity(
        handle="gabo@doorslip.test", pubkey=keypair.public_key, label="hermes"
    )

    record = store.find_agent(keypair.public_key)

    assert record is not None
    assert record.handle == "gabo@doorslip.test"
    assert record.label == "hermes"
    assert not record.revoked


def test_an_unknown_key_is_not_found(store):
    assert store.find_agent(generate_keypair().public_key) is None


def test_a_verified_sender_can_be_resolved_end_to_end(store):
    """Wires storage into the §5.2 chain, which is the point of both."""
    from doorslip.envelope import build, seal
    from doorslip.identity import VerifiedSender, verify_sender

    keypair = generate_keypair()
    store.register_identity(
        handle="gabo@doorslip.test", pubkey=keypair.public_key, label="hermes"
    )
    sealed = seal(
        build(
            sender_handle="gabo@doorslip.test",
            sender_agent="hermes",
            sender_pubkey=keypair.public_key,
            to="tomas@doorslip.test",
            state={},
            prose="hello",
        ),
        keypair.private_key,
    )

    result = verify_sender(sealed.raw, sealed.signature, store.find_agent)

    assert isinstance(result, VerifiedSender)
    assert result.handle == "gabo@doorslip.test"


# -- nonces ---------------------------------------------------------------


def test_a_nonce_can_be_spent_once(store):
    keypair = generate_keypair()
    nonce = store.issue_nonce(keypair.public_key)

    assert store.consume_nonce(nonce.value, keypair.public_key)
    assert not store.consume_nonce(nonce.value, keypair.public_key)


def test_a_nonce_cannot_be_spent_by_another_key(store):
    """The binding is the whole point of the nonce.

    An unbound nonce proves nothing: anyone can request one and anyone can
    spend it, which is the same as having no nonce at all.
    """
    mine = generate_keypair()
    yours = generate_keypair()
    nonce = store.issue_nonce(mine.public_key)

    assert not store.consume_nonce(nonce.value, yours.public_key)
    assert store.consume_nonce(nonce.value, mine.public_key)


def test_an_unknown_nonce_is_refused(store):
    assert not store.consume_nonce("never-issued", generate_keypair().public_key)


def test_an_expired_nonce_is_refused(store):
    from datetime import datetime, timedelta, timezone

    keypair = generate_keypair()
    stale = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    store._db.execute(
        "INSERT INTO nonce (value, pubkey, expires_at) VALUES (?, ?, ?)",
        ("stale", keypair.public_key, stale),
    )

    assert not store.consume_nonce("stale", keypair.public_key)


def test_issuing_sweeps_expired_nonces(store):
    from datetime import datetime, timedelta, timezone

    keypair = generate_keypair()
    stale = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    store._db.execute(
        "INSERT INTO nonce (value, pubkey, expires_at) VALUES (?, ?, ?)",
        ("stale", keypair.public_key, stale),
    )

    store.issue_nonce(keypair.public_key)

    remaining = store._db.execute("SELECT COUNT(*) FROM nonce").fetchone()[0]
    assert remaining == 1


def test_foreign_keys_are_enforced(store):
    """SQLite leaves them off by default, which makes the schema decorative."""
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        store._db.execute(
            "INSERT INTO agent (id, human_id, label, pubkey, created_at)"
            " VALUES ('a', 'nonexistent-human', 'hermes', 'pk', '2026-01-01')"
        )
