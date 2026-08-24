"""Request authentication: signature over a nonce (spec §7.1).

Doorslip has no session tokens. A token is a bearer secret: whoever copies it
is you until it expires. Signing a server-issued nonce proves possession of the
private key on every single request, and there is nothing worth stealing in
transit — the nonce is already spent by the time anyone could replay it.

The cost is one extra round trip per authenticated request. At v0 scale that is
irrelevant, and it buys independence from clock synchronisation, which the
single-round-trip alternative (signing method + path + body hash + timestamp,
RFC 9421) would require.
"""

from __future__ import annotations

from dataclasses import dataclass

from doorslip.crypto import verify

AUTH_HEADER = "X-Doorslip-Auth"


@dataclass(frozen=True)
class Credential:
    """A parsed `X-Doorslip-Auth` value: `<pubkey>.<nonce>.<signature>`."""

    pubkey: str
    nonce: str
    signature: str


def parse_credential(header: str | None) -> Credential | None:
    if not header:
        return None
    parts = header.split(".")
    if len(parts) != 3 or not all(parts):
        return None
    return Credential(pubkey=parts[0], nonce=parts[1], signature=parts[2])


def build_credential(pubkey: str, nonce: str, private_key: str) -> str:
    """Client side: sign the nonce and format the header value."""
    from doorslip.crypto import sign

    return f"{pubkey}.{nonce}.{sign(nonce.encode('ascii'), private_key)}"


def signature_holds(credential: Credential) -> bool:
    """Check the signature covers the nonce, before any database work.

    Same ordering rule as the identity chain: cheap cryptography first, so a
    forged credential never reaches the store and response timing never leaks
    which nonces or keys exist.
    """
    return verify(credential.nonce.encode("ascii"), credential.signature, credential.pubkey)
