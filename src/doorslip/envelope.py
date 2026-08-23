"""Building and reading the Doorslip envelope.

The rule that governs this module: **the bytes are built once, and those same
bytes are what gets signed and what gets sent.**

That is why `build()` returns `bytes` and not a dictionary. If it returned a
dict, sooner or later someone would pass it to `httpx.post(json=...)` or run it
through `json.dumps()` again, and that second serialization can differ from the
first — different key order, different spacing, different unicode escaping. The
signature would stop verifying, and the bug only surfaces once two independent
implementations talk to each other.

There is no API here that hands you a re-serializable dict before signing. That
is on purpose.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

VERSION = "0.1"

# Hard limits from spec §7.9. They protect the recipient, who spends inference
# tokens evaluating whatever arrives — not the server, which handles far more.
MAX_ENVELOPE_BYTES = 64 * 1024
MAX_PROSE_CHARS = 8_000
MAX_STATE_DEPTH = 8

_REQUIRED = ("version", "message_id", "thread_id", "from", "to", "timestamp")
_DISCLOSURE = ("full", "basic", "minimal")


class EnvelopeError(ValueError):
    """Malformed or over-limit envelope. Maps to HTTP 400/413."""


@dataclass(frozen=True)
class SignedEnvelope:
    """The exact bytes plus their signature. This is what is sent and stored.

    `raw` is the source of truth. The parsed `envelope` column in the `message`
    table is derived: it exists to be queried, never to verify against.
    """

    raw: bytes
    signature: str


def build(
    *,
    sender_handle: str,
    sender_agent: str,
    sender_pubkey: str,
    to: str,
    state: dict[str, Any],
    prose: str,
    thread_id: str | None = None,
    parent_message_id: str | None = None,
    message_id: str | None = None,
    disclosure: str = "basic",
    timestamp: datetime | None = None,
) -> bytes:
    """Assemble an envelope and return the bytes to sign and send.

    A new thread leaves both `thread_id` and `parent_message_id` as None: the
    thread id is generated and the message becomes the root. A reply passes
    both — the parent defines which state the merge patch applies to (spec §6.1).
    """
    if parent_message_id is not None and thread_id is None:
        raise EnvelopeError(
            "a message with a parent belongs to an existing thread; "
            "thread_id is missing"
        )
    if disclosure not in _DISCLOSURE:
        raise EnvelopeError(f"invalid disclosure: {disclosure!r}")
    if len(prose) > MAX_PROSE_CHARS:
        raise EnvelopeError(f"prose exceeds {MAX_PROSE_CHARS} characters")
    _check_depth(state)

    moment = timestamp or datetime.now(timezone.utc)
    envelope = {
        "version": VERSION,
        "message_id": message_id or str(uuid.uuid4()),
        "thread_id": thread_id or str(uuid.uuid4()),
        "parent_message_id": parent_message_id,
        "from": {
            "handle": sender_handle,
            "agent": sender_agent,
            "pubkey": sender_pubkey,
        },
        "to": to,
        "timestamp": moment.isoformat(),
        "disclosure": disclosure,
        "state": state,
        "prose": prose,
    }

    raw = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise EnvelopeError(f"envelope exceeds {MAX_ENVELOPE_BYTES} bytes")
    return raw


def parse(raw: bytes) -> dict[str, Any]:
    """Read a received envelope. Does NOT verify the signature — see `identity`.

    Call this on the bytes as they arrived, and only to inspect the contents.
    What gets stored and what gets verified is still `raw`.
    """
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise EnvelopeError(f"envelope exceeds {MAX_ENVELOPE_BYTES} bytes")
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnvelopeError(f"not valid UTF-8 JSON: {exc}") from exc

    if not isinstance(envelope, dict):
        raise EnvelopeError("envelope must be a JSON object")
    missing = [field for field in _REQUIRED if field not in envelope]
    if missing:
        raise EnvelopeError(f"missing required fields: {', '.join(missing)}")

    sender = envelope["from"]
    if not isinstance(sender, dict) or "handle" not in sender or "pubkey" not in sender:
        raise EnvelopeError("'from' must carry handle and pubkey")

    return envelope


def seal(raw: bytes, private_key: str) -> SignedEnvelope:
    """Sign already-built bytes.

    Note it takes `raw`, not the individual fields. What gets signed cannot
    diverge from what gets sent, because they are the same byte object.
    """
    from doorslip.crypto import sign

    return SignedEnvelope(raw=raw, signature=sign(raw, private_key))


def _check_depth(value: Any, level: int = 1) -> None:
    """Reject deep nesting (spec §7.9): guards against structure bombs."""
    if level > MAX_STATE_DEPTH:
        raise EnvelopeError(f"state exceeds {MAX_STATE_DEPTH} levels of nesting")
    if isinstance(value, dict):
        for item in value.values():
            _check_depth(item, level + 1)
    elif isinstance(value, list):
        for item in value:
            _check_depth(item, level + 1)
