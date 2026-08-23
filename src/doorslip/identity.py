"""Sender identity verification chain (spec §5.2).

This is the central check of the protocol, and the one the first draft of the
spec left implicit.

**A signature alone does not prove identity: it proves control of a key.**
Anyone can generate an Ed25519 pair, put its public half in the `from` field
and sign with it. That signature verifies perfectly and means nothing at all.

What binds identity is the full chain. Drop any link and `from.handle` becomes
decorative — anyone can claim to be anyone.

The module is pure: it takes an `AgentLookup` instead of touching the database.
That keeps the chain testable without SQLite, and when federation arrives
(spec §11 bis) swapping "look it up in the `agent` table" for "look it up in
the sending domain's .well-known" means replacing the lookup and nothing else.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from doorslip.crypto import verify
from doorslip.envelope import EnvelopeError, parse


class Rejection(Enum):
    """Rejection reasons, carrying the HTTP status from spec §7.5.

    They are deliberately distinguishable: the agent on the other side reacts
    differently to each one, and a blanket 401 hides whether the problem is
    theirs or the server's.
    """

    MALFORMED = 400
    BAD_SIGNATURE = 401
    UNKNOWN_KEY = 401
    REVOKED_KEY = 401
    HANDLE_MISMATCH = 401

    @property
    def http_status(self) -> int:
        return self.value


@dataclass(frozen=True)
class AgentRecord:
    """What the directory knows about one key.

    `handle` is that of the **human who owns the key**, not of the agent. That
    distinction is link 4 of the chain.
    """

    pubkey: str
    handle: str
    label: str
    revoked: bool = False


@dataclass(frozen=True)
class VerifiedSender:
    """A sender whose identity is proven end to end."""

    handle: str
    label: str
    pubkey: str
    envelope: dict[str, Any]
    raw: bytes
    label_mismatch: bool = False


AgentLookup = Callable[[str], AgentRecord | None]


def verify_sender(
    raw: bytes,
    signature: str,
    lookup: AgentLookup,
) -> VerifiedSender | Rejection:
    """Run the spec §5.2 chain over a received envelope.

    Returns a `VerifiedSender` if every link holds, or the `Rejection` for the
    first link that fails. Never raises on network input.
    """
    # Link 0 — the envelope must parse before its `from` field can be read.
    try:
        envelope = parse(raw)
    except EnvelopeError:
        return Rejection.MALFORMED

    sender = envelope["from"]
    pubkey = sender["pubkey"]
    handle = sender["handle"]

    # Link 1 — the signature verifies against the pubkey the envelope declares.
    #
    # This runs before touching the directory, for two reasons. Verifying
    # Ed25519 costs microseconds, and hitting the database first would leak
    # through response timing which pubkeys are registered — handing anyone a
    # free oracle to enumerate identities without ever sending a message.
    #
    # Passing this link proves nothing yet. It is the easiest one to fake:
    # generating a fresh keypair is enough.
    if not verify(raw, signature, pubkey):
        return Rejection.BAD_SIGNATURE

    # Link 2 — the key must exist in the directory.
    record = lookup(pubkey)
    if record is None:
        return Rejection.UNKNOWN_KEY

    # Link 3 — and must not be revoked.
    #
    # Revocation stops NEW messages. Already-received ones remain valid: their
    # signature was verified on arrival and that fact was recorded (spec §7.6).
    # Retroactive revocation would break every historical thread.
    if record.revoked:
        return Rejection.REVOKED_KEY

    # Link 4 — the key must belong to the human the envelope names.
    #
    # THIS is the link that stops impersonation. Without it, anyone holding a
    # registered key — that is, anyone who signed up — can sign an envelope
    # claiming to come from someone else and clear all three previous links.
    if record.handle != handle:
        return Rejection.HANDLE_MISMATCH

    # Link 5 — the agent label is informational. A mismatch is logged, not
    # rejected: it is a human-facing tag, not part of the trust model.
    declared_label = sender.get("agent")
    return VerifiedSender(
        handle=record.handle,
        label=record.label,
        pubkey=pubkey,
        envelope=envelope,
        raw=raw,
        label_mismatch=declared_label != record.label,
    )
