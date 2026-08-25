"""SQLite storage for the Doorslip directory and mailboxes (spec §4).

Six tables, plain `sqlite3`, no ORM. With this few tables an ORM costs more
attention than it saves, and the spec is explicit that the stack is not a
strategic decision.

Spec §4 lists five; `invite_code` is the sixth. The spec describes codes as
single-use with a TTL but never says where they live, and single-use with a
TTL is precisely a row that gets written once and updated once.

Two SQLite defaults are wrong for us and are corrected on every connection:

- Foreign keys are OFF by default. Without the pragma every `REFERENCES`
  clause in the schema is decoration and orphan rows appear silently.
- The default journal mode blocks readers during a write. WAL lets an agent
  poll its inbox while another message is being deposited.

Timestamps are stored as ISO-8601 UTC text. SQLite has no date type, and the
implicit datetime adapters are deprecated — being explicit costs one call and
survives the interpreter upgrade.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, RLock
from typing import Any
from weakref import WeakValueDictionary

from doorslip.identity import AgentRecord

# Hardcoded, per spec §4. Not a setting: a limit that can be raised by editing
# a config file is not a limit.
MAX_AGENTS_PER_HUMAN = 5

# What a key is allowed to do. Two, not a permission system.
#
# The split is between moving messages and changing who may reach you. An
# agent that publishes needs the first and none of the second, and the
# difference is not a matter of degree: dropping a subscriber, admitting a
# stranger or revoking the owner's key are things a human decides, and an
# agent given them can undo the human's ability to take them back.
#
# Eight named permissions was the other design. It is a permission system,
# and a permission system is a thing people misconfigure. One bit is a
# decision somebody makes once, at the moment they choose to add an agent.
SCOPE_FULL = "full"
SCOPE_SPEAK = "speak"
SCOPES = (SCOPE_FULL, SCOPE_SPEAK)

# Spec §7.1. Short enough that a stolen nonce is useless, long enough for a
# round trip over a bad connection.
NONCE_TTL_SECONDS = 60

# Not specified by the spec. An invite travels out of band — a chat message,
# an email — and the person on the other side may take a day to paste it.
INVITE_TTL_DAYS = 7

# Spec §7.3. Much shorter than an invitation because it grants far more: an
# enrolment code hands over the inbox, the address book and the ability to sign
# as that person. It must not survive being pasted into the wrong window.
ENROLL_TTL_MINUTES = 20

# Spec §7.9. The ceiling that matters most now that agents can answer each
# other unattended: a loop between two of them costs both humans money, and
# neither is watching. Per pair rather than per sender, because the harm is
# what one person's automation does to one other person.
MAX_MESSAGES_PER_HOUR = 60

INVITE_PREFIX = "ds_inv_"
ENROLL_PREFIX = "ds_enr_"

# Spec §6. Not enforced anywhere — `state` is free and a message is never
# rejected for ignoring this. It exists so metric 3 can measure whether agents
# converge on a shared vocabulary on their own.
RECOMMENDED_STATE_KEYS = frozenset(
    {"topic", "status", "when", "where", "who", "budget", "constraints", "tasks"}
)

# The active-agent triggers are installed once per database. The Python constant
# feeds new databases and the matching application check; changing the limit in
# a future release also requires an explicit trigger migration for existing DBs.
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS human (
    id                  TEXT PRIMARY KEY,
    handle              TEXT NOT NULL UNIQUE,
    canonical_pubkey    TEXT NOT NULL,
    accepts_unsolicited INTEGER NOT NULL DEFAULT 0,
    credit_balance      INTEGER NOT NULL DEFAULT 0,
    is_welcome          INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent (
    id         TEXT PRIMARY KEY,
    human_id   TEXT NOT NULL REFERENCES human(id),
    label      TEXT NOT NULL,
    pubkey     TEXT NOT NULL UNIQUE,
    scope      TEXT NOT NULL DEFAULT 'full',
    revoked_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contact (
    id             TEXT PRIMARY KEY,
    owner_human_id TEXT NOT NULL REFERENCES human(id),
    peer_human_id  TEXT NOT NULL REFERENCES human(id),
    disclosure     TEXT NOT NULL DEFAULT 'basic',
    created_at     TEXT NOT NULL,
    UNIQUE (owner_human_id, peer_human_id)
);

CREATE TABLE IF NOT EXISTS message (
    id                TEXT PRIMARY KEY,
    thread_id         TEXT NOT NULL,
    parent_message_id TEXT REFERENCES message(id),
    from_human_id     TEXT NOT NULL REFERENCES human(id),
    from_agent_id     TEXT NOT NULL REFERENCES agent(id),
    to_human_id       TEXT NOT NULL REFERENCES human(id),
    envelope_raw      BLOB NOT NULL,
    envelope          TEXT NOT NULL,
    signature         TEXT NOT NULL,
    ack_at            TEXT,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nonce (
    value      TEXT PRIMARY KEY,
    pubkey     TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT
);

CREATE TABLE IF NOT EXISTS invite_code (
    code                 TEXT PRIMARY KEY,
    issuer_human_id      TEXT NOT NULL REFERENCES human(id),
    expires_at           TEXT NOT NULL,
    redeemed_at          TEXT,
    redeemed_by_human_id TEXT REFERENCES human(id),
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS enroll_code (
    code        TEXT PRIMARY KEY,
    human_id    TEXT NOT NULL REFERENCES human(id),
    scope       TEXT NOT NULL DEFAULT 'full',
    expires_at  TEXT NOT NULL,
    redeemed_at TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_log (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    pubkey     TEXT,
    human_id   TEXT,
    detail     TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_message_recipient ON message(to_human_id, created_at);
CREATE INDEX IF NOT EXISTS idx_message_thread    ON message(thread_id);
CREATE INDEX IF NOT EXISTS idx_agent_human       ON agent(human_id);
CREATE INDEX IF NOT EXISTS idx_event_kind        ON event_log(kind, created_at);

CREATE TRIGGER IF NOT EXISTS agent_active_limit_insert
BEFORE INSERT ON agent
WHEN NEW.revoked_at IS NULL
 AND NOT EXISTS (SELECT 1 FROM human WHERE id = NEW.human_id AND is_welcome = 1)
 AND (SELECT COUNT(*) FROM agent
       WHERE human_id = NEW.human_id AND revoked_at IS NULL) >= {MAX_AGENTS_PER_HUMAN}
BEGIN
    SELECT RAISE(ABORT, 'active agent limit exceeded');
END;

CREATE TRIGGER IF NOT EXISTS agent_active_limit_update
BEFORE UPDATE OF human_id, revoked_at ON agent
WHEN NEW.revoked_at IS NULL
 AND (OLD.revoked_at IS NOT NULL OR OLD.human_id != NEW.human_id)
 AND NOT EXISTS (SELECT 1 FROM human WHERE id = NEW.human_id AND is_welcome = 1)
 AND (SELECT COUNT(*) FROM agent
       WHERE human_id = NEW.human_id AND revoked_at IS NULL) >= {MAX_AGENTS_PER_HUMAN}
BEGIN
    SELECT RAISE(ABORT, 'active agent limit exceeded');
END;
"""


class StoreError(Exception):
    """Base for storage failures the caller is expected to handle."""


class HandleTaken(StoreError):
    """That handle already belongs to someone. Spec §7.2: first come, first served."""


class KeyAlreadyRegistered(StoreError):
    """That pubkey is already in the directory, possibly under another human."""


class TooManyAgents(StoreError):
    """The human already has MAX_AGENTS_PER_HUMAN active keys."""


class InviteInvalid(StoreError):
    """Unknown, expired, already redeemed, or the issuer's own code."""


@dataclass(frozen=True)
class Human:
    id: str
    handle: str
    canonical_pubkey: str
    is_welcome: bool = False


@dataclass(frozen=True)
class Nonce:
    value: str
    pubkey: str
    expires_at: datetime


@dataclass(frozen=True)
class Contact:
    handle: str
    disclosure: str


@dataclass(frozen=True)
class SentMessage:
    """A message you sent, and what became of it.

    `acked_at` is the only thing the recipient's side ever reveals, and it
    says their AGENT incorporated the message — not that their human read it.
    That distinction is the whole reason it is safe to show: it reports
    liveness, not attention, and it is what tells a sender apart the case of
    "not answered yet" from "never even seen".
    """

    id: str
    thread_id: str
    recipient_handle: str
    topic: str | None
    acked_at: str | None
    created_at: str


@dataclass(frozen=True)
class StoredMessage:
    id: str
    thread_id: str
    parent_message_id: str | None
    sender_handle: str
    envelope: dict[str, Any]
    signature: str
    acked: bool
    created_at: str


class _SerializedConnection:
    """Serialize access to one connection shared by request threads."""

    def __init__(self, connection: sqlite3.Connection, lock: RLock):
        self._connection = connection
        self._lock = lock

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._connection.execute(sql, parameters)

    def executescript(self, sql_script: str) -> sqlite3.Cursor:
        with self._lock:
            return self._connection.executescript(sql_script)

    def __enter__(self) -> _SerializedConnection:
        self._lock.acquire()
        try:
            self._connection.__enter__()
        except BaseException:
            self._lock.release()
            raise
        return self

    def __exit__(self, *args: Any) -> bool | None:
        try:
            return self._connection.__exit__(*args)
        finally:
            self._lock.release()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


_CONNECTIONS_LOCK = Lock()
_SERIALIZED_CONNECTIONS: WeakValueDictionary[int, _SerializedConnection] = (
    WeakValueDictionary()
)


def _serialized(connection: sqlite3.Connection | _SerializedConnection) -> _SerializedConnection:
    """Return the one synchronization boundary for a raw SQLite connection.

    More than one ``Store`` may wrap the same connection during an in-process
    restart.  A lock owned by each Store would let those wrappers interleave
    transactions on the same connection, so wrappers are shared by connection
    identity while any Store still holds one.
    """
    if isinstance(connection, _SerializedConnection):
        return connection
    key = id(connection)
    with _CONNECTIONS_LOCK:
        current = _SERIALIZED_CONNECTIONS.get(key)
        if current is not None and current._connection is connection:
            return current
        current = _SerializedConnection(connection, RLock())
        _SERIALIZED_CONNECTIONS[key] = current
        return current


class Store:
    """The directory and the mailboxes.

    Held deliberately dumb: it stores and retrieves, and knows nothing about
    signatures. Deciding whether a sender is who they claim belongs to
    `identity.verify_sender`, which this class feeds through `find_agent`.
    """

    def __init__(self, connection: sqlite3.Connection | _SerializedConnection):
        self._db = _serialized(connection)
        self._transaction_lock = self._db._lock

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Run an explicit transaction without shared-connection interleaving."""
        with self._transaction_lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._db.execute("COMMIT")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    # -- identity ---------------------------------------------------------

    def register_identity(
        self, *, handle: str, pubkey: str, label: str, is_welcome: bool = False
    ) -> Human:
        """Create a human and their first agent (spec §7.2, no-code branch).

        The first key registered becomes `canonical_pubkey`. It is unused in
        v0 and exists so that identity can outlive the handle later (§11 bis).
        """
        if self.find_human(handle) is not None:
            raise HandleTaken(handle)
        if self.find_agent(pubkey) is not None:
            raise KeyAlreadyRegistered(pubkey)

        human = Human(
            id=str(uuid.uuid4()),
            handle=handle,
            canonical_pubkey=pubkey,
            is_welcome=is_welcome,
        )
        moment = _now_text()
        with self._db:
            self._db.execute(
                "INSERT INTO human (id, handle, canonical_pubkey, is_welcome, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (human.id, human.handle, pubkey, int(is_welcome), moment),
            )
            self._db.execute(
                "INSERT INTO agent (id, human_id, label, pubkey, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), human.id, label, pubkey, moment),
            )
        self.log("register", pubkey=pubkey, human_id=human.id, detail=handle)
        return human

    def find_human(self, handle: str) -> Human | None:
        row = self._db.execute(
            "SELECT id, handle, canonical_pubkey, is_welcome FROM human WHERE handle = ?",
            (handle,),
        ).fetchone()
        return _as_human(row)

    def human_by_id(self, human_id: str) -> Human | None:
        row = self._db.execute(
            "SELECT id, handle, canonical_pubkey, is_welcome FROM human WHERE id = ?",
            (human_id,),
        ).fetchone()
        return _as_human(row)

    def find_agent(self, pubkey: str) -> AgentRecord | None:
        """The `AgentLookup` that `identity.verify_sender` runs on.

        Returning the dataclass the chain already expects is what keeps the
        verification logic free of SQL — and what makes swapping this for a
        `.well-known` fetch a one-line change when federation arrives.
        """
        row = self._db.execute(
            "SELECT a.pubkey, h.handle, a.label, a.revoked_at, a.scope"
            " FROM agent a JOIN human h ON h.id = a.human_id"
            " WHERE a.pubkey = ?",
            (pubkey,),
        ).fetchone()
        if row is None:
            return None
        pubkey_, handle, label, revoked_at, scope = row
        return AgentRecord(
            pubkey=pubkey_,
            handle=handle,
            label=label,
            revoked=revoked_at is not None,
            scope=scope or SCOPE_FULL,
        )

    def agent_id_for(self, pubkey: str) -> str | None:
        row = self._db.execute(
            "SELECT id FROM agent WHERE pubkey = ?", (pubkey,)
        ).fetchone()
        return row[0] if row else None

    def count_active_agents(self, human_id: str) -> int:
        return self._db.execute(
            "SELECT COUNT(*) FROM agent WHERE human_id = ? AND revoked_at IS NULL",
            (human_id,),
        ).fetchone()[0]

    def revoke_key(self, pubkey: str) -> bool:
        """Stop NEW messages from this key.

        Not retroactive (spec §7.6): messages already received stay valid,
        because their signature was verified on arrival and that fact is
        recorded. Retroactive revocation would break every historical thread.
        """
        with self._db:
            changed = self._db.execute(
                "UPDATE agent SET revoked_at = ? WHERE pubkey = ? AND revoked_at IS NULL",
                (_now_text(), pubkey),
            ).rowcount
        if changed:
            self.log("revoke", pubkey=pubkey)
        return changed == 1

    def create_enroll_code(self, human_id: str, scope: str = SCOPE_FULL) -> str:
        """Mint a code that attaches ANOTHER KEY TO THIS SAME IDENTITY (spec §7.3).

        Twenty minutes, single use, and a prefix that cannot be mistaken for an
        invitation. The short life is the mitigation that matters: this code
        grants everything — inbox, address book, the ability to sign as this
        person — so it must not survive being pasted into the wrong window.
        """
        if scope not in SCOPES:
            raise ValueError(f"unknown scope: {scope!r}")
        code = ENROLL_PREFIX + secrets.token_urlsafe(18)
        with self._db:
            self._db.execute(
                "INSERT INTO enroll_code (code, human_id, scope, expires_at, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    code,
                    human_id,
                    scope,
                    (_now() + timedelta(minutes=ENROLL_TTL_MINUTES)).isoformat(),
                    _now_text(),
                ),
            )
        self.log("enroll_code", human_id=human_id, detail=scope)
        return code

    def redeem_enroll_code(self, code: str, *, pubkey: str, label: str) -> Human:
        """Attach a new agent key to the identity that issued the code."""
        if not code.startswith(ENROLL_PREFIX):
            raise InviteInvalid("not an enrolment code")

        try:
            with self._transaction():
                if self.find_agent(pubkey) is not None:
                    raise KeyAlreadyRegistered(pubkey)

                now = _now_text()
                row = self._db.execute(
                    "SELECT human_id, scope FROM enroll_code"
                    " WHERE code = ? AND redeemed_at IS NULL AND expires_at > ?",
                    (code, now),
                ).fetchone()
                if row is None:
                    raise InviteInvalid("unknown, expired or already redeemed")

                # The scope rides on the code, so the joining agent cannot ask
                # for more than the person who minted it decided to hand over.
                human_id, scope = row[0], row[1] or SCOPE_FULL
                if self.count_active_agents(human_id) >= MAX_AGENTS_PER_HUMAN:
                    raise TooManyAgents(
                        f"already at {MAX_AGENTS_PER_HUMAN} active agents"
                    )

                moment = _now_text()
                changed = self._db.execute(
                    "UPDATE enroll_code SET redeemed_at = ?"
                    " WHERE code = ? AND redeemed_at IS NULL AND expires_at > ?",
                    (moment, code, moment),
                ).rowcount
                if changed != 1:
                    raise InviteInvalid("unknown, expired or already redeemed")
                self._db.execute(
                    "INSERT INTO agent (id, human_id, label, pubkey, scope, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), human_id, label, pubkey, scope, moment),
                )
        except sqlite3.IntegrityError as exc:
            if "active agent limit exceeded" in str(exc):
                raise TooManyAgents(
                    f"already at {MAX_AGENTS_PER_HUMAN} active agents"
                ) from exc
            raise

        self.log(
            "enroll", pubkey=pubkey, human_id=human_id, detail=f"{label} ({scope})"
        )
        human = self.human_by_id(human_id)
        assert human is not None  # FK guarantees it
        return human

    def everyone_but_the_desk(self) -> list[Human]:
        """Every registered person, for an operator announcement.

        The welcome desk is excluded: it would be the server writing to
        itself, and its mailbox is nobody's to read.
        """
        rows = self._db.execute(
            "SELECT id, handle, canonical_pubkey, is_welcome FROM human"
            " WHERE is_welcome = 0 ORDER BY created_at"
        ).fetchall()
        return [h for h in (_as_human(row) for row in rows) if h is not None]

    def set_open_inbox(self, human_id: str, open_to_strangers: bool) -> None:
        """Let anyone write to this mailbox, or stop letting them.

        The column has been reserved since the beginning for the open mode of
        spec §11 ter, and this is its first honest use — not that design in
        full, which needs a cost attached to writing to strangers, but the one
        case where an open mailbox is the point: a list somebody subscribes to.
        """
        with self._db:
            self._db.execute(
                "UPDATE human SET accepts_unsolicited = ? WHERE id = ?",
                (int(open_to_strangers), human_id),
            )
        self.log("open_inbox" if open_to_strangers else "close_inbox", human_id=human_id)

    def is_open_inbox(self, human_id: str) -> bool:
        row = self._db.execute(
            "SELECT accepts_unsolicited FROM human WHERE id = ?", (human_id,)
        ).fetchone()
        return bool(row and row[0])

    def remove_contact(self, owner_human_id: str, peer_handle: str) -> bool:
        """Take somebody out of an address book — one side only.

        Deliberately not symmetric, unlike accepting. Removing them from both
        would let anybody sever a relationship they are only half of: you can
        stop somebody writing to you, and you cannot decide on their behalf
        that they no longer know you.
        """
        peer = self.find_human(peer_handle)
        if peer is None:
            return False
        with self._db:
            changed = self._db.execute(
                "DELETE FROM contact WHERE owner_human_id = ? AND peer_human_id = ?",
                (owner_human_id, peer.id),
            ).rowcount
        if changed:
            self.log("remove_contact", human_id=owner_human_id, detail=peer_handle)
        return changed > 0

    def redeem_or_attach_welcome_key(self, human_id: str, pubkey: str) -> None:
        """Attach a key to the welcome desk after a key file was lost.

        Not an enrolment: no code, no notification, no five-agent ceiling. The
        desk is the server's own identity and this is how it recovers the
        ability to sign after its key file went missing, rather than failing
        every notice in silence.
        """
        with self._db:
            self._db.execute(
                "INSERT INTO agent (id, human_id, label, pubkey, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), human_id, "welcome", pubkey, _now_text()),
            )
        self.log("welcome_key_attached", pubkey=pubkey, human_id=human_id)

    def list_agents(self, human_id: str) -> list[dict[str, Any]]:
        """Every key of this identity, revoked ones included.

        Revoking needs a pubkey and until now nothing would tell an owner what
        theirs were: the enrolment notice named a label the new agent chose for
        itself, which is not something to revoke by. Labels are also not
        unique, so a second agent could enrol as the same word as the first and
        leave the owner unable to say which one to remove.

        The revoked stay listed. An owner who has just revoked a key needs to
        see that it took, and a key vanishing from the list looks the same as
        one that was never there.
        """
        return [
            {
                "pubkey": row[0],
                "label": row[1],
                "created_at": row[2],
                "revoked": row[3] is not None,
                "scope": row[4] or SCOPE_FULL,
            }
            for row in self._db.execute(
                "SELECT pubkey, label, created_at, revoked_at, scope FROM agent"
                " WHERE human_id = ? ORDER BY created_at",
                (human_id,),
            ).fetchall()
        ]

    def active_agent_labels(self, human_id: str) -> list[str]:
        return [
            row[0]
            for row in self._db.execute(
                "SELECT label FROM agent WHERE human_id = ? AND revoked_at IS NULL"
                " ORDER BY created_at",
                (human_id,),
            ).fetchall()
        ]

    # -- nonces -----------------------------------------------------------

    def issue_nonce(self, pubkey: str) -> Nonce:
        """Mint a single-use nonce bound to one pubkey (spec §7.1).

        The binding is the point. A nonce anyone can request and anyone can
        spend proves nothing about who spent it.

        The key need not be registered: `POST /register` needs a nonce before
        the key exists anywhere. Accepted consequence — anyone can make the
        table grow. Expired rows are swept here, and with a 60s TTL the
        steady state stays small.
        """
        self._sweep_expired_nonces()
        issued = Nonce(
            value=secrets.token_urlsafe(32),
            pubkey=pubkey,
            expires_at=_now() + timedelta(seconds=NONCE_TTL_SECONDS),
        )
        with self._db:
            self._db.execute(
                "INSERT INTO nonce (value, pubkey, expires_at) VALUES (?, ?, ?)",
                (issued.value, issued.pubkey, issued.expires_at.isoformat()),
            )
        return issued

    def consume_nonce(self, value: str, pubkey: str) -> bool:
        """Spend a nonce. False if missing, expired, already used, or another key's.

        The UPDATE carries every condition so that two simultaneous requests
        cannot both spend the same nonce: SQLite reports one row changed and
        the loser sees zero.
        """
        with self._db:
            changed = self._db.execute(
                "UPDATE nonce SET used_at = ?"
                " WHERE value = ? AND pubkey = ? AND used_at IS NULL AND expires_at > ?",
                (_now_text(), value, pubkey, _now_text()),
            ).rowcount
        return changed == 1

    def _sweep_expired_nonces(self) -> None:
        with self._db:
            self._db.execute("DELETE FROM nonce WHERE expires_at <= ?", (_now_text(),))

    # -- address book -----------------------------------------------------

    def create_invite(self, issuer_human_id: str) -> str:
        """Mint an invitation code (spec §7.4).

        Deliberately prefixed so it cannot be confused with an enrolment code.
        One adds a stranger to your address book; the other adds a key to your
        own identity. Pasting the wrong one into the wrong endpoint would
        enrol another human as your own agent, so the two are distinguishable
        at a glance and the endpoints reject each other's prefix.
        """
        code = INVITE_PREFIX + secrets.token_urlsafe(18)
        with self._db:
            self._db.execute(
                "INSERT INTO invite_code (code, issuer_human_id, expires_at, created_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    code,
                    issuer_human_id,
                    (_now() + timedelta(days=INVITE_TTL_DAYS)).isoformat(),
                    _now_text(),
                ),
            )
        self.log("invite", human_id=issuer_human_id)
        return code

    def redeem_invite(self, code: str, accepting_human_id: str) -> Human:
        """Redeem a code and create BOTH contact rows (spec §4).

        The address book is symmetric on purpose: accepting an invitation is
        an agreement to converse, and a one-sided one would let the inviter
        write while the invitee could not reply.
        """
        if not code.startswith(INVITE_PREFIX):
            raise InviteInvalid("not an invitation code")

        with self._transaction():
            now = _now_text()
            row = self._db.execute(
                "SELECT issuer_human_id FROM invite_code"
                " WHERE code = ? AND redeemed_at IS NULL AND expires_at > ?",
                (code, now),
            ).fetchone()
            if row is None:
                raise InviteInvalid("unknown, expired or already redeemed")

            issuer_id = row[0]
            if issuer_id == accepting_human_id:
                raise InviteInvalid("cannot redeem your own invitation")

            moment = _now_text()
            changed = self._db.execute(
                "UPDATE invite_code SET redeemed_at = ?, redeemed_by_human_id = ?"
                " WHERE code = ? AND redeemed_at IS NULL AND expires_at > ?",
                (moment, accepting_human_id, code, moment),
            ).rowcount
            if changed != 1:
                raise InviteInvalid("unknown, expired or already redeemed")
            for owner, peer in (
                (issuer_id, accepting_human_id),
                (accepting_human_id, issuer_id),
            ):
                self._db.execute(
                    "INSERT OR IGNORE INTO contact"
                    " (id, owner_human_id, peer_human_id, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (str(uuid.uuid4()), owner, peer, moment),
                )
        self.log("accept", human_id=accepting_human_id, detail=issuer_id)
        issuer = self.human_by_id(issuer_id)
        assert issuer is not None  # FK guarantees it
        return issuer

    def add_contact_pair(self, a_human_id: str, b_human_id: str) -> None:
        """Create the two rows directly. Used by the welcome agent (spec §8)."""
        moment = _now_text()
        with self._db:
            for owner, peer in ((a_human_id, b_human_id), (b_human_id, a_human_id)):
                self._db.execute(
                    "INSERT OR IGNORE INTO contact"
                    " (id, owner_human_id, peer_human_id, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (str(uuid.uuid4()), owner, peer, moment),
                )

    def list_contacts(self, human_id: str) -> list[Contact]:
        rows = self._db.execute(
            "SELECT h.handle, c.disclosure FROM contact c"
            " JOIN human h ON h.id = c.peer_human_id"
            " WHERE c.owner_human_id = ? ORDER BY h.handle",
            (human_id,),
        ).fetchall()
        return [Contact(handle=handle, disclosure=disclosure) for handle, disclosure in rows]

    def is_contact(self, owner_human_id: str, peer_human_id: str) -> bool:
        return (
            self._db.execute(
                "SELECT 1 FROM contact WHERE owner_human_id = ? AND peer_human_id = ?",
                (owner_human_id, peer_human_id),
            ).fetchone()
            is not None
        )

    # -- mailbox ----------------------------------------------------------

    def store_message(
        self,
        *,
        envelope: dict[str, Any],
        raw: bytes,
        signature: str,
        from_human_id: str,
        from_agent_id: str,
        to_human_id: str,
    ) -> str:
        """Persist a message exactly as it arrived.

        `envelope_raw` holds the bytes the signature was verified against.
        The parsed `envelope` column is derived and exists to be queried —
        never to verify against.
        """
        message_id = envelope["message_id"]
        with self._db:
            self._db.execute(
                "INSERT INTO message (id, thread_id, parent_message_id, from_human_id,"
                " from_agent_id, to_human_id, envelope_raw, envelope, signature, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message_id,
                    envelope["thread_id"],
                    envelope.get("parent_message_id"),
                    from_human_id,
                    from_agent_id,
                    to_human_id,
                    raw,
                    json.dumps(envelope),
                    signature,
                    _now_text(),
                ),
            )
        self.log("message", human_id=from_human_id, detail=envelope["thread_id"])
        return message_id

    def messages_in_last_hour(self, from_human_id: str, to_human_id: str) -> int:
        """How many messages went one way between these two in the last hour."""
        since = (_now() - timedelta(hours=1)).isoformat()
        return self._db.execute(
            "SELECT COUNT(*) FROM message"
            " WHERE from_human_id = ? AND to_human_id = ? AND created_at > ?",
            (from_human_id, to_human_id, since),
        ).fetchone()[0]

    def message_envelope(self, message_id: str) -> dict[str, Any] | None:
        """The parsed envelope of one message, for parent-chain checks."""
        row = self._db.execute(
            "SELECT envelope FROM message WHERE id = ?", (message_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def message_exists(self, message_id: str) -> bool:
        return (
            self._db.execute(
                "SELECT 1 FROM message WHERE id = ?", (message_id,)
            ).fetchone()
            is not None
        )

    def fetch_inbox(self, human_id: str, *, unacked_only: bool = False) -> list[StoredMessage]:
        query = (
            "SELECT m.id, m.thread_id, m.parent_message_id, h.handle, m.envelope,"
            " m.signature, m.ack_at, m.created_at"
            " FROM message m JOIN human h ON h.id = m.from_human_id"
            " WHERE m.to_human_id = ?"
        )
        if unacked_only:
            query += " AND m.ack_at IS NULL"
        query += " ORDER BY m.created_at"
        return [
            StoredMessage(
                id=row[0],
                thread_id=row[1],
                parent_message_id=row[2],
                sender_handle=row[3],
                envelope=json.loads(row[4]),
                signature=row[5],
                acked=row[6] is not None,
                created_at=row[7],
            )
            for row in self._db.execute(query, (human_id,)).fetchall()
        ]

    def fetch_sent(self, human_id: str) -> list[SentMessage]:
        """What this human sent, and whether it was acknowledged.

        Spec §7.7 put the acknowledgement there so that a broken thread could
        be told apart from a broken transport. The server has recorded it all
        along; without this the one person who needs that answer — the sender
        — had no way to ask for it.
        """
        rows = self._db.execute(
            "SELECT m.id, m.thread_id, h.handle,"
            " json_extract(m.envelope, '$.state.topic'), m.ack_at, m.created_at"
            " FROM message m JOIN human h ON h.id = m.to_human_id"
            " WHERE m.from_human_id = ? ORDER BY m.created_at",
            (human_id,),
        ).fetchall()
        return [
            SentMessage(
                id=row[0],
                thread_id=row[1],
                recipient_handle=row[2],
                topic=row[3],
                acked_at=row[4],
                created_at=row[5],
            )
            for row in rows
        ]

    def thread_messages(self, thread_id: str) -> list[StoredMessage]:
        rows = self._db.execute(
            "SELECT m.id, m.thread_id, m.parent_message_id, h.handle, m.envelope,"
            " m.signature, m.ack_at, m.created_at"
            " FROM message m JOIN human h ON h.id = m.from_human_id"
            " WHERE m.thread_id = ? ORDER BY m.created_at",
            (thread_id,),
        ).fetchall()
        return [
            StoredMessage(
                id=row[0],
                thread_id=row[1],
                parent_message_id=row[2],
                sender_handle=row[3],
                envelope=json.loads(row[4]),
                signature=row[5],
                acked=row[6] is not None,
                created_at=row[7],
            )
            for row in rows
        ]

    def ack_message(self, message_id: str, human_id: str) -> bool:
        """Record that the recipient INCORPORATED the message, not just got it.

        Spec §7.7 makes this mandatory: without it, when a thread breaks there
        is no way to tell whether transport failed or the agent did.
        """
        with self._db:
            changed = self._db.execute(
                "UPDATE message SET ack_at = ?"
                " WHERE id = ? AND to_human_id = ? AND ack_at IS NULL",
                (_now_text(), message_id, human_id),
            ).rowcount
        if changed:
            self.log("ack", human_id=human_id, detail=message_id)
        return changed == 1

    # -- instrumentation (spec §9) ----------------------------------------

    def log(
        self,
        kind: str,
        *,
        pubkey: str | None = None,
        human_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO event_log (id, kind, pubkey, human_id, detail, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), kind, pubkey, human_id, detail, _now_text()),
            )

    def public_counts(self) -> dict[str, int]:
        """Two aggregates, for the landing page and nothing else.

        Deliberately not `metrics()`. That one answers research questions the
        operator asks about how the protocol is being used, and one of them
        reads the `state` of every message to do it. Those numbers are not a
        thing to hand to anybody who asks — this is, because it is two
        integers with nobody inside them.

        Neither count includes the welcome desk. It is not a person: it
        registers itself on first boot, so counting it would report one
        inhabitant on an empty server, and it replies to every arrival, so
        counting its greetings would report traffic the server generated for
        itself as conversation between people.
        """
        people = self._db.execute(
            "SELECT COUNT(*) FROM human WHERE is_welcome = 0"
        ).fetchone()[0]
        messages = self._db.execute(
            """
            SELECT COUNT(*) FROM message
             WHERE from_human_id NOT IN (SELECT id FROM human WHERE is_welcome = 1)
               AND to_human_id   NOT IN (SELECT id FROM human WHERE is_welcome = 1)
            """
        ).fetchone()[0]
        return {"people": people, "messages": messages}

    def metrics(self) -> dict[str, Any]:
        """The four metrics of spec §9, with the operational definitions.

        Metric 1 is the one that matters. Registrations and mentions mean
        nothing; a second conversation between the same two people does.
        """
        pairs_with_second = self._db.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT t.a, t.b FROM (
                    SELECT MIN(from_human_id, to_human_id) AS a,
                           MAX(from_human_id, to_human_id) AS b,
                           thread_id
                    FROM message GROUP BY a, b, thread_id
                    HAVING COUNT(DISTINCT from_human_id) = 2
                ) t GROUP BY t.a, t.b HAVING COUNT(*) >= 2
            )
            """
        ).fetchone()[0]

        # A turn is a change of speaker, not a message. Eight messages from
        # one person are not eight turns, and the done-criterion of spec §2
        # would otherwise be satisfiable by an agent talking to itself.
        speakers = self._db.execute(
            "SELECT thread_id, from_human_id FROM message ORDER BY thread_id, created_at"
        ).fetchall()
        turns_by_thread: dict[str, int] = {}
        previous: dict[str, str] = {}
        for thread_id, speaker in speakers:
            if previous.get(thread_id) != speaker:
                turns_by_thread[thread_id] = turns_by_thread.get(thread_id, 0) + 1
                previous[thread_id] = speaker
        turns = (
            sum(turns_by_thread.values()) / len(turns_by_thread) if turns_by_thread else 0
        )

        # Off-shape means using vocabulary outside the recommended shape — NOT
        # being a partial patch. A patch that touches one recommended key is
        # exactly what §6 asks for; counting it as a miss would make the
        # metric punish correct behaviour.
        states = self._db.execute(
            "SELECT json_extract(envelope, '$.state') FROM message"
        ).fetchall()
        total = len(states)
        off_shape = 0
        for (raw_state,) in states:
            parsed = json.loads(raw_state) if raw_state else {}
            if isinstance(parsed, dict) and set(parsed) - RECOMMENDED_STATE_KEYS:
                off_shape += 1

        moved_disclosure = self._db.execute(
            "SELECT COUNT(*) FROM contact WHERE disclosure != 'basic'"
        ).fetchone()[0]

        return {
            "pairs_with_second_conversation": pairs_with_second,
            "average_turns_per_thread": round(turns or 0, 2),
            "state_off_recommended_shape": round(off_shape / total, 3) if total else 0.0,
            "contacts_off_default_disclosure": moved_disclosure,
            "messages_total": total,
        }


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    """Open a connection with the pragmas this project depends on.

    `check_same_thread=False` is required because the server handles requests
    on a threadpool while holding one connection. SQLite itself serializes
    access under its default threading mode, so the guard Python adds on top
    is what would break us, not the database.

    The accepted consequence is that one connection is shared across requests.
    At v0 scale — ten people, sub-millisecond queries — that is not a
    bottleneck. If it ever becomes one, the fix is a connection per request,
    not a bigger machine.
    """
    db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    db.executescript(SCHEMA)
    _migrate(db)
    return db


def _migrate(db: sqlite3.Connection) -> None:
    """Bring a database made by an older release up to the current schema.

    `CREATE TABLE IF NOT EXISTS` leaves an existing table exactly as it is, so
    a column added to SCHEMA reaches new databases and never the one already
    holding everybody's messages. Found the day scopes were added: the seed
    instance would have kept running and failed on the first enrolment.

    Every step is a column addition with a default, which is the only kind of
    change that is safe to run on every start and safe to run twice.
    """
    for table, column, definition in (
        ("enroll_code", "scope", "TEXT NOT NULL DEFAULT 'full'"),
        ("agent", "scope", "TEXT NOT NULL DEFAULT 'full'"),
    ):
        existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


@contextmanager
def open_store(path: str | Path = ":memory:") -> Iterator[Store]:
    db = connect(path)
    try:
        yield Store(db)
    finally:
        db.close()


def _as_human(row: Any) -> Human | None:
    if row is None:
        return None
    return Human(id=row[0], handle=row[1], canonical_pubkey=row[2], is_welcome=bool(row[3]))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_text() -> str:
    return _now().isoformat()
