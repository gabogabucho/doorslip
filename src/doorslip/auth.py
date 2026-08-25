"""Request authentication for ``doorslip-auth-v1``.

The credential proves possession of an Ed25519 key and binds that proof to the
method, untouched ASGI target components, nonce, and exact body bytes.  During
the 0.28.0 compatibility window the server also accepts the old nonce-only
signature; updated clients never emit it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass

from doorslip.crypto import sign, verify

AUTH_HEADER = "X-Doorslip-Auth"
AUTH_V1 = "doorslip-auth-v1"
AUTH_NONCE_ONLY = "nonce-only"
ACCEPTED_AUTH_SCHEMES = (AUTH_V1, AUTH_NONCE_ONLY)

# The compatibility window is exactly the release that introduces v1.  This
# name is both machine-advertised and tested so removing legacy verification is
# a scheduled 0.29.0 change, not a deprecation that can silently become permanent.
LEGACY_AUTH_REMOVAL_RELEASE = "0.29.0"

_PUBLIC_KEY = re.compile(r"^[A-Za-z0-9+/]{43}=$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_SIGNATURE = re.compile(r"^[A-Za-z0-9+/]{86}==$")
_METHOD = re.compile(rb"^[!#$%&'*+\-.^_`|~0-9A-Z]+$")


@dataclass(frozen=True)
class Credential:
    """A strictly parsed ``<pubkey>.<nonce>.<signature>`` header value."""

    pubkey: str
    nonce: str
    signature: str


def parse_credential(header: str | None) -> Credential | None:
    """Parse only the one canonical encoding allowed for each component."""
    if not header:
        return None
    parts = header.split(".")
    if len(parts) != 3:
        return None
    pubkey, nonce, signature = parts
    if not _canonical_standard(pubkey, _PUBLIC_KEY, 32):
        return None
    if not _canonical_nonce(nonce):
        return None
    if not _canonical_standard(signature, _SIGNATURE, 64):
        return None
    return Credential(pubkey=pubkey, nonce=nonce, signature=signature)


def build_auth_frame(
    method: str,
    raw_path: bytes,
    query_string: bytes,
    nonce: str,
    body: bytes,
) -> bytes:
    """Build the five-line ASCII frame from the actual request components."""
    try:
        method_bytes = method.encode("ascii")
    except (AttributeError, UnicodeEncodeError) as exc:
        raise ValueError("method must be uppercase ASCII") from exc
    if not _METHOD.fullmatch(method_bytes):
        raise ValueError("method must be an uppercase ASCII HTTP token")
    if (
        not isinstance(raw_path, bytes)
        or not raw_path.startswith(b"/")
        or b"?" in raw_path
    ):
        raise ValueError("raw_path must be origin-form bytes beginning with /")
    if not isinstance(query_string, bytes):
        raise TypeError("query_string must be bytes")
    target = raw_path + (b"?" + query_string if query_string else b"")
    if (
        not target
        or b"#" in target
        or any(octet < 0x21 or octet > 0x7E for octet in target)
    ):
        raise ValueError("target must be visible ASCII origin-form without a fragment")
    if not _canonical_nonce(nonce):
        raise ValueError("nonce must be 32-byte unpadded base64url")
    if not isinstance(body, bytes):
        raise TypeError("body must be bytes")

    digest = hashlib.sha256(body).hexdigest().encode("ascii")
    return b"\n".join(
        (b"doorslip-auth-v1", method_bytes, target, nonce.encode("ascii"), digest)
    )


def build_credential(
    pubkey: str,
    nonce: str,
    private_key: str,
    *,
    method: str,
    raw_path: bytes,
    query_string: bytes,
    body: bytes,
) -> str:
    """Sign a v1 frame and format the authentication header value."""
    if not _canonical_standard(pubkey, _PUBLIC_KEY, 32):
        raise ValueError("pubkey must be 32-byte padded standard base64")
    frame = build_auth_frame(method, raw_path, query_string, nonce, body)
    return f"{pubkey}.{nonce}.{sign(frame, private_key)}"


def v1_signature_holds(
    credential: Credential,
    *,
    method: str,
    raw_path: bytes,
    query_string: bytes,
    body: bytes,
) -> bool:
    """Verify v1 against components rebuilt from the received request."""
    try:
        frame = build_auth_frame(
            method, raw_path, query_string, credential.nonce, body
        )
    except ValueError:
        return False
    return verify(frame, credential.signature, credential.pubkey)


def nonce_only_signature_holds(credential: Credential) -> bool:
    """The bounded 0.28.0 compatibility verifier; remove in 0.29.0."""
    return verify(
        credential.nonce.encode("ascii"), credential.signature, credential.pubkey
    )


def _canonical_standard(value: str, pattern: re.Pattern[str], length: int) -> bool:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        return False
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return False
    return len(raw) == length and base64.b64encode(raw).decode("ascii") == value


def _canonical_nonce(value: str) -> bool:
    if not isinstance(value, str) or _NONCE.fullmatch(value) is None:
        return False
    try:
        raw = base64.b64decode(value + "=", altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        return False
    return (
        len(raw) == 32
        and base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") == value
    )
