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
from typing import Any

from doorslip.identity import AgentRecord

# Hardcoded, per spec §4. Not a setting: a limit that can be raised by editing
# a config file is not a limit.
MAX_AGENTS_PER_HUMAN = 5

# Spec §7.1. Short enough that a stolen nonce is useless, long enough for a
# round trip over a bad connection.
NONCE_TTL_SECONDS = 60

# Not specified by the spec. An invite travels out of band — a chat message,
# an email — and the person on the other side may take a day to paste it.
INVITE_TTL_DAYS = 7

INVITE_PREFIX = "ds_inv_"
ENROLL_PREFIX = "ds_enr_"

# Spec §6. Not enforced anywhere — `state` is free and a message is never
# rejected for ignoring this. It exists so metric 3 can measure whether agents
# converge on a shared vocabulary on their own.
RECOMMENDED_STATE_KEYS = frozenset(
    {"topic", "status", "when", "where", "who", "budget", "constraints", "tasks"}
)

SCHEMA = """
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
class StoredMessage:
    id: str
    thread_id: str
    parent_message_id: str | None
    sender_handle: str
    envelope: dict[str, Any]
    signature: str
    acked: bool
    created_at: str


class Store:
    """The directory and the mailboxes.

    Held deliberately dumb: it stores and retrieves, and knows nothing about
    signatures. Deciding whether a sender is who they claim belongs to
    `identity.verify_sender`, which this class feeds through `find_agent`.
    """

    def __init__(self, connection: sqlite3.Connection):
        self._db = connection

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
            "SELECT a.pubkey, h.handle, a.label, a.revoked_at"
            " FROM agent a JOIN human h ON h.id = a.human_id"
            " WHERE a.pubkey = ?",
            (pubkey,),
        ).fetchone()
        if row is None:
            return None
        pubkey_, handle, label, revoked_at = row
        return AgentRecord(
            pubkey=pubkey_, handle=handle, label=label, revoked=revoked_at is not None
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

        row = self._db.execute(
            "SELECT issuer_human_id FROM invite_code"
            " WHERE code = ? AND redeemed_at IS NULL AND expires_at > ?",
            (code, _now_text()),
        ).fetchone()
        if row is None:
            raise InviteInvalid("unknown, expired or already redeemed")

        issuer_id = row[0]
        if issuer_id == accepting_human_id:
            raise InviteInvalid("cannot redeem your own invitation")

        moment = _now_text()
        with self._db:
            self._db.execute(
                "UPDATE invite_code SET redeemed_at = ?, redeemed_by_human_id = ?"
                " WHERE code = ? AND redeemed_at IS NULL",
                (moment, accepting_human_id, code),
            )
            for owner, peer in ((issuer_id, accepting_human_id), (accepting_human_id, issuer_id)):
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
    return db


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
