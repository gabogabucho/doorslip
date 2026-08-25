"""Atomicity and concurrency invariants for one-time codes."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Barrier, Lock

import pytest

from doorslip.store import (
    InviteInvalid,
    KeyAlreadyRegistered,
    MAX_AGENTS_PER_HUMAN,
    SCOPE_SPEAK,
    Store,
    TooManyAgents,
    connect,
)


@pytest.fixture
def store():
    db = connect(":memory:")
    try:
        yield Store(db)
    finally:
        db.close()


class _BarrierCursor:
    """Pause after a qualifying read has captured its pre-race result."""

    def __init__(self, cursor, barrier):
        self._cursor = cursor
        self._barrier = barrier

    def fetchone(self):
        row = self._cursor.fetchone()
        self._barrier.wait()
        return row

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _BarrierConnection:
    """Expose stale-read races deterministically without production hooks.

    A real explicit transaction disables the barrier. In that case concurrent
    callers must serialize before the read instead of deadlocking inside it.
    """

    def __init__(self, connection, parties, *read_fragments):
        self._connection = connection
        self._barrier = Barrier(parties)
        self._read_fragments = read_fragments
        self._remaining_waits = parties * len(read_fragments)
        self._state_lock = Lock()

    def execute(self, sql, parameters=()):
        cursor = self._connection.execute(sql, parameters)
        matches = any(fragment in sql for fragment in self._read_fragments)
        with self._state_lock:
            if matches and self._connection.in_transaction:
                self._remaining_waits = 0
            should_wait = (
                not self._connection.in_transaction
                and matches
                and self._remaining_waits > 0
            )
            if should_wait:
                self._remaining_waits -= 1
        if should_wait:
            return _BarrierCursor(cursor, self._barrier)
        return cursor

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, *args):
        return self._connection.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def _register(store, name):
    return store.register_identity(
        handle=f"{name}@doorslip.test", pubkey=f"pubkey-{name}", label="test"
    )


def _run_concurrently(callables):
    with ThreadPoolExecutor(max_workers=len(callables)) as pool:
        futures = [pool.submit(call) for call in callables]

    successes = []
    failures = []
    for future in futures:
        try:
            successes.append(future.result())
        except Exception as exc:  # outcomes are asserted by exact type below
            failures.append(exc)
    return successes, failures


def _start_together(*callables):
    barrier = Barrier(len(callables))

    def ready(call):
        def run():
            barrier.wait()
            return call()

        return run

    return _run_concurrently([ready(call) for call in callables])


def test_concurrent_invite_redemption_has_one_winner_and_one_contact_pair(store):
    issuer = _register(store, "issuer")
    acceptors = [_register(store, f"acceptor-{number}") for number in range(6)]
    code = store.create_invite(issuer.id)
    store._db = _BarrierConnection(
        store._db, len(acceptors), "SELECT issuer_human_id FROM invite_code"
    )

    successes, failures = _run_concurrently(
        [
            lambda acceptor=acceptor: store.redeem_invite(code, acceptor.id)
            for acceptor in acceptors
        ]
    )

    assert len(successes) == 1
    assert len(failures) == len(acceptors) - 1
    assert all(isinstance(exc, InviteInvalid) for exc in failures)
    contacts = store._db.execute(
        "SELECT owner_human_id, peer_human_id FROM contact"
    ).fetchall()
    assert len(contacts) == 2
    redeemed_by = store._db.execute(
        "SELECT redeemed_by_human_id FROM invite_code WHERE code = ?", (code,)
    ).fetchone()[0]
    assert set(contacts) == {
        (issuer.id, redeemed_by),
        (redeemed_by, issuer.id),
    }
    assert store._db.execute(
        "SELECT COUNT(*) FROM event_log WHERE kind = 'accept'"
    ).fetchone() == (1,)


def test_concurrent_enrolment_code_redemption_has_one_winner(store):
    owner = _register(store, "owner")
    code = store.create_enroll_code(owner.id, SCOPE_SPEAK)
    contenders = 6
    store._db = _BarrierConnection(
        store._db,
        contenders,
        "SELECT human_id, scope FROM enroll_code",
        "SELECT COUNT(*) FROM agent WHERE human_id",
    )

    successes, failures = _run_concurrently(
        [
            lambda number=number: store.redeem_enroll_code(
                code, pubkey=f"contender-{number}", label=f"agent-{number}"
            )
            for number in range(contenders)
        ]
    )

    assert len(successes) == 1
    assert len(failures) == contenders - 1
    assert all(isinstance(exc, InviteInvalid) for exc in failures)
    assert store.count_active_agents(owner.id) == 2
    assert store._db.execute(
        "SELECT COUNT(*) FROM event_log WHERE kind = 'enroll'"
    ).fetchone() == (1,)
    assert store._db.execute(
        "SELECT scope FROM agent WHERE pubkey LIKE 'contender-%'"
    ).fetchone() == (SCOPE_SPEAK,)


def test_two_stores_sharing_one_connection_share_the_transaction_lock():
    db = connect(":memory:")
    try:
        first = Store(db)
        second = Store(db)
        issuer = _register(first, "issuer")
        acceptors = [_register(first, f"acceptor-{number}") for number in range(2)]
        code = first.create_invite(issuer.id)

        successes, failures = _start_together(
            lambda: first.redeem_invite(code, acceptors[0].id),
            lambda: second.redeem_invite(code, acceptors[1].id),
        )

        assert len(successes) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], InviteInvalid)
    finally:
        db.close()


def test_separate_connections_redeem_one_invitation_once(tmp_path):
    path = tmp_path / "shared.db"
    first_db, second_db = connect(path), connect(path)
    try:
        first, second = Store(first_db), Store(second_db)
        issuer = _register(first, "issuer")
        acceptors = [_register(first, f"acceptor-{number}") for number in range(2)]
        code = first.create_invite(issuer.id)

        successes, failures = _start_together(
            lambda: first.redeem_invite(code, acceptors[0].id),
            lambda: second.redeem_invite(code, acceptors[1].id),
        )

        assert len(successes) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], InviteInvalid)
        assert first._db.execute("SELECT COUNT(*) FROM contact").fetchone() == (2,)
    finally:
        first_db.close()
        second_db.close()


def test_distinct_enrolment_codes_compete_atomically_for_last_slot(store):
    owner = _register(store, "owner")
    for number in range(MAX_AGENTS_PER_HUMAN - 2):
        code = store.create_enroll_code(owner.id)
        store.redeem_enroll_code(code, pubkey=f"existing-{number}", label="existing")
    assert store.count_active_agents(owner.id) == MAX_AGENTS_PER_HUMAN - 1

    codes = [store.create_enroll_code(owner.id) for _ in range(2)]
    store._db = _BarrierConnection(
        store._db, len(codes), "SELECT COUNT(*) FROM agent WHERE human_id"
    )

    successes, failures = _run_concurrently(
        [
            lambda number=number, code=code: store.redeem_enroll_code(
                code, pubkey=f"last-slot-{number}", label="last"
            )
            for number, code in enumerate(codes)
        ]
    )

    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], TooManyAgents)
    assert store.count_active_agents(owner.id) == MAX_AGENTS_PER_HUMAN

    unused_code = store._db.execute(
        "SELECT code FROM enroll_code WHERE code IN (?, ?) AND redeemed_at IS NULL",
        tuple(codes),
    ).fetchone()[0]
    key_to_revoke = store._db.execute(
        "SELECT pubkey FROM agent"
        " WHERE human_id = ? AND pubkey != ? AND revoked_at IS NULL LIMIT 1",
        (owner.id, owner.canonical_pubkey),
    ).fetchone()[0]
    assert store.revoke_key(key_to_revoke)
    assert store.redeem_enroll_code(
        unused_code, pubkey="after-capacity-frees", label="retry"
    ) == owner
    assert store.count_active_agents(owner.id) == MAX_AGENTS_PER_HUMAN


def test_separate_connections_cannot_exceed_the_active_agent_limit(tmp_path):
    path = tmp_path / "shared.db"
    first_db, second_db = connect(path), connect(path)
    try:
        first, second = Store(first_db), Store(second_db)
        owner = _register(first, "owner")
        for number in range(MAX_AGENTS_PER_HUMAN - 2):
            code = first.create_enroll_code(owner.id)
            first.redeem_enroll_code(
                code, pubkey=f"separate-existing-{number}", label="existing"
            )
        codes = [first.create_enroll_code(owner.id) for _ in range(2)]

        successes, failures = _start_together(
            lambda: first.redeem_enroll_code(
                codes[0], pubkey="separate-last-first", label="last"
            ),
            lambda: second.redeem_enroll_code(
                codes[1], pubkey="separate-last-second", label="last"
            ),
        )

        assert len(successes) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], TooManyAgents)
        assert first.count_active_agents(owner.id) == MAX_AGENTS_PER_HUMAN
    finally:
        first_db.close()
        second_db.close()


def test_database_rejects_an_active_agent_beyond_the_limit(store):
    owner = _register(store, "owner")
    for number in range(MAX_AGENTS_PER_HUMAN - 1):
        code = store.create_enroll_code(owner.id)
        store.redeem_enroll_code(code, pubkey=f"existing-{number}", label="existing")

    with pytest.raises(sqlite3.IntegrityError, match="active agent limit"):
        store._db.execute(
            "INSERT INTO agent (id, human_id, label, pubkey, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("one-too-many", owner.id, "extra", "extra-pubkey", "2026-08-25T00:00:00Z"),
        )

    assert store.count_active_agents(owner.id) == MAX_AGENTS_PER_HUMAN


def test_invite_consume_and_bilateral_contacts_roll_back_together(store):
    issuer = _register(store, "issuer")
    acceptor = _register(store, "acceptor")
    code = store.create_invite(issuer.id)
    store._db.executescript(
        """
        CREATE TRIGGER fail_second_contact
        BEFORE INSERT ON contact
        WHEN (SELECT COUNT(*) FROM contact) = 1
        BEGIN
            SELECT RAISE(ABORT, 'forced second contact failure');
        END;
        """
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.redeem_invite(code, acceptor.id)

    assert store._db.execute(
        "SELECT redeemed_at FROM invite_code WHERE code = ?", (code,)
    ).fetchone() == (None,)
    assert store._db.execute("SELECT COUNT(*) FROM contact").fetchone() == (0,)
    assert store._db.execute(
        "SELECT COUNT(*) FROM event_log WHERE kind = 'accept'"
    ).fetchone() == (0,)

    store._db.execute("DROP TRIGGER fail_second_contact")
    assert store.redeem_invite(code, acceptor.id) == issuer
    assert store._db.execute("SELECT COUNT(*) FROM contact").fetchone() == (2,)


def test_enrolment_consume_and_agent_insert_roll_back_together(store):
    owner = _register(store, "owner")
    code = store.create_enroll_code(owner.id)
    store._db.executescript(
        """
        CREATE TRIGGER fail_agent_insert
        BEFORE INSERT ON agent
        WHEN NEW.pubkey = 'fault-pubkey'
        BEGIN
            SELECT RAISE(ABORT, 'forced agent failure');
        END;
        """
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.redeem_enroll_code(code, pubkey="fault-pubkey", label="fault")

    assert store._db.execute(
        "SELECT redeemed_at FROM enroll_code WHERE code = ?", (code,)
    ).fetchone() == (None,)
    assert store.find_agent("fault-pubkey") is None
    assert store._db.execute(
        "SELECT COUNT(*) FROM event_log WHERE kind = 'enroll'"
    ).fetchone() == (0,)

    store._db.execute("DROP TRIGGER fail_agent_insert")
    assert store.redeem_enroll_code(
        code, pubkey="fault-pubkey", label="fault"
    ) == owner


def test_duplicate_enrolment_pubkey_remains_key_already_registered(store):
    owner = _register(store, "owner")
    code = store.create_enroll_code(owner.id)

    with pytest.raises(KeyAlreadyRegistered):
        store.redeem_enroll_code(code, pubkey="pubkey-owner", label="duplicate")

    assert store._db.execute(
        "SELECT redeemed_at FROM enroll_code WHERE code = ?", (code,)
    ).fetchone() == (None,)


def test_redemption_logging_starts_only_after_commit(store, monkeypatch):
    issuer = _register(store, "issuer")
    acceptor = _register(store, "acceptor")
    code = store.create_invite(issuer.id)
    calls = []

    monkeypatch.setattr(
        store,
        "log",
        lambda kind, **fields: calls.append((kind, store._db.in_transaction, fields)),
    )

    store.redeem_invite(code, acceptor.id)

    assert [(kind, in_transaction) for kind, in_transaction, _ in calls] == [
        ("accept", False)
    ]
