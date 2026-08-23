"""SQLite storage for the Doorslip directory and mailboxes (spec §4).

Five tables, plain `sqlite3`, no ORM. With this few tables an ORM costs more
attention than it saves, and the spec is explicit that the stack is not a
strategic decision.

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

SCHEMA = """
CREATE TABLE IF NOT EXISTS human (
    id                  TEXT PRIMARY KEY,
    handle              TEXT NOT NULL UNIQUE,
    canonical_pubkey    TEXT NOT NULL,
    accepts_unsolicited INTEGER NOT NULL DEFAULT 0,
    credit_balance      INTEGER NOT NULL DEFAULT 0,
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

CREATE INDEX IF NOT EXISTS idx_message_recipient ON message(to_human_id, created_at);
CREATE INDEX IF NOT EXISTS idx_message_thread    ON message(thread_id);
CREATE INDEX IF NOT EXISTS idx_agent_human       ON agent(human_id);
"""


class StoreError(Exception):
    """Base for storage failures the caller is expected to handle."""


class HandleTaken(StoreError):
    """That handle already belongs to someone. Spec §7.2: first come, first served."""


class KeyAlreadyRegistered(StoreError):
    """That pubkey is already in the directory, possibly under another human."""


class TooManyAgents(StoreError):
    """The human already has MAX_AGENTS_PER_HUMAN active keys."""


@dataclass(frozen=True)
class Human:
    id: str
    handle: str
    canonical_pubkey: str


@dataclass(frozen=True)
class Nonce:
    value: str
    pubkey: str
    expires_at: datetime


class Store:
    """The directory. Every method is synchronous and short.

    Held deliberately dumb: it stores and retrieves, and knows nothing about
    signatures. Deciding whether a sender is who they claim belongs to
    `identity.verify_sender`, which this class feeds through `find_agent`.
    """

    def __init__(self, connection: sqlite3.Connection):
        self._db = connection

    # -- identity ---------------------------------------------------------

    def register_identity(self, *, handle: str, pubkey: str, label: str) -> Human:
        """Create a human and their first agent (spec §7.2, no-code branch).

        The first key registered becomes `canonical_pubkey`. It is unused in
        v0 and exists so that identity can outlive the handle later (§11 bis).
        """
        if self.find_human(handle) is not None:
            raise HandleTaken(handle)
        if self.find_agent(pubkey) is not None:
            raise KeyAlreadyRegistered(pubkey)

        human = Human(id=str(uuid.uuid4()), handle=handle, canonical_pubkey=pubkey)
        moment = _now_text()
        with self._db:
            self._db.execute(
                "INSERT INTO human (id, handle, canonical_pubkey, created_at)"
                " VALUES (?, ?, ?, ?)",
                (human.id, human.handle, human.canonical_pubkey, moment),
            )
            self._db.execute(
                "INSERT INTO agent (id, human_id, label, pubkey, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), human.id, label, pubkey, moment),
            )
        return human

    def find_human(self, handle: str) -> Human | None:
        row = self._db.execute(
            "SELECT id, handle, canonical_pubkey FROM human WHERE handle = ?",
            (handle,),
        ).fetchone()
        return Human(*row) if row else None

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

    def count_active_agents(self, human_id: str) -> int:
        return self._db.execute(
            "SELECT COUNT(*) FROM agent WHERE human_id = ? AND revoked_at IS NULL",
            (human_id,),
        ).fetchone()[0]

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

    # -- introspection ----------------------------------------------------

    def message_envelope(self, message_id: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT envelope FROM message WHERE id = ?", (message_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_text() -> str:
    return _now().isoformat()
