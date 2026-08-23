"""Ed25519 signatures over raw bytes.

Doorslip signs the exact bytes that travel on the wire (spec §5.1).

This module knows nothing about JSON, and that ignorance is deliberate: it
takes `bytes` and returns `bytes`. If it accepted dictionaries it would have to
serialize them, which reintroduces the very problem the spec removed when it
dropped JCS — two serializations of the same object producing different
signatures.

Callers are responsible for building the bytes once and sending those same
bytes. See `envelope.py`.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

# Ed25519: 32-byte keys, 64-byte signatures. Fixed, not negotiable.
_KEY_LEN = 32
_SIG_LEN = 64


@dataclass(frozen=True)
class KeyPair:
    """An Ed25519 pair, base64 encoded.

    The private key is never persisted from here. It belongs in a local file
    with restricted permissions, not in the agent's memory (spec §13).
    """

    private_key: str
    public_key: str


def generate_keypair() -> KeyPair:
    """Generate a fresh pair. Always local, never on the server (spec §3.1)."""
    private = ed25519.Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return KeyPair(
        private_key=base64.b64encode(private_raw).decode("ascii"),
        public_key=base64.b64encode(public_raw).decode("ascii"),
    )


def sign(payload: bytes, private_key: str) -> str:
    """Sign raw bytes. Returns the signature, base64 encoded.

    `payload` is the exact byte string that will be transmitted. It is not
    normalized, reordered or touched in any way.
    """
    if not isinstance(payload, bytes):
        raise TypeError(
            "sign() takes bytes, not str or dict. "
            "Serializing here would break the guarantee in spec §5.1."
        )
    key = ed25519.Ed25519PrivateKey.from_private_bytes(_decode(private_key, _KEY_LEN))
    return base64.b64encode(key.sign(payload)).decode("ascii")


def verify(payload: bytes, signature: str, public_key: str) -> bool:
    """Verify a signature against the bytes as received.

    Returns False on any failure — bad signature, malformed key, invalid
    base64. It never raises on attacker-controlled input: callers handle data
    straight off the network and should not have to wrap this in try/except.
    """
    if not isinstance(payload, bytes):
        raise TypeError("verify() takes bytes, not str or dict.")
    try:
        key = ed25519.Ed25519PublicKey.from_public_bytes(_decode(public_key, _KEY_LEN))
        key.verify(_decode(signature, _SIG_LEN), payload)
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


def _decode(value: str, expected_len: int) -> bytes:
    """base64 -> bytes, validating length.

    Length is checked here rather than by the caller because a key of the wrong
    size is malformed input, not a signature that fails to verify. Those are
    different failures and the caller reports them differently.
    """
    raw = base64.b64decode(value, validate=True)
    if len(raw) != expected_len:
        raise ValueError(f"expected {expected_len} bytes, got {len(raw)}")
    return raw
