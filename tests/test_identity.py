"""Sender identity verification chain (spec §5.2).

These are the tests that define the protocol. A valid signature passing proves
nothing; what proves the design is everything that gets rejected while holding
a cryptographically flawless signature.
"""

import pytest

from doorslip.crypto import generate_keypair
from doorslip.envelope import build, seal
from doorslip.identity import AgentRecord, Rejection, VerifiedSender, verify_sender


class Directory:
    """The `agent` table, in memory. The real server runs the same query."""

    def __init__(self, *records: AgentRecord):
        self._by_pubkey = {r.pubkey: r for r in records}

    def __call__(self, pubkey: str) -> AgentRecord | None:
        return self._by_pubkey.get(pubkey)


@pytest.fixture
def hermes():
    return generate_keypair()


@pytest.fixture
def gabo_envelope(hermes):
    def _make(**extra):
        fields = dict(
            sender_handle="gabo@doorslip.test",
            sender_agent="hermes",
            sender_pubkey=hermes.public_key,
            to="tomas@doorslip.test",
            state={"topic": "saturday barbecue"},
            prose="Throwing out a date.",
        )
        fields.update(extra)
        return seal(build(**fields), hermes.private_key)

    return _make


def test_a_legitimate_sender_passes_the_whole_chain(hermes, gabo_envelope):
    directory = Directory(
        AgentRecord(pubkey=hermes.public_key, handle="gabo@doorslip.test", label="hermes")
    )
    sealed = gabo_envelope()

    result = verify_sender(sealed.raw, sealed.signature, directory)

    assert isinstance(result, VerifiedSender)
    assert result.handle == "gabo@doorslip.test"
    assert result.label == "hermes"
    assert not result.label_mismatch


def test_a_valid_signature_from_an_unregistered_key_is_rejected(hermes, gabo_envelope):
    """THE test of the protocol.

    The signature is flawless: a real Ed25519 pair, correctly signed. If this
    were accepted, `from.handle` would be decorative and anyone could register
    as anyone else just by generating a key.

    This is the link the first draft of the spec left implicit.
    """
    directory = Directory()  # empty
    sealed = gabo_envelope()

    assert verify_sender(sealed.raw, sealed.signature, directory) is Rejection.UNKNOWN_KEY


def test_impersonation_with_an_own_registered_key_is_rejected(gabo_envelope):
    """Nanton is registered. He signs an envelope claiming to come from Gabo.

    It clears the first three links: the signature verifies, the key exists and
    is not revoked. The only thing stopping it is link 4 — that the key belong
    to the human the envelope names.
    """
    nanton = generate_keypair()
    directory = Directory(
        AgentRecord(pubkey=nanton.public_key, handle="nanton@doorslip.test", label="claude")
    )

    forged = seal(
        build(
            sender_handle="gabo@doorslip.test",  # the lie
            sender_agent="hermes",
            sender_pubkey=nanton.public_key,  # but signed with HIS key
            to="tomas@doorslip.test",
            state={},
            prose="Hey, lend me some money.",
        ),
        nanton.private_key,
    )

    assert verify_sender(forged.raw, forged.signature, directory) is Rejection.HANDLE_MISMATCH


def test_a_revoked_key_is_rejected(hermes, gabo_envelope):
    directory = Directory(
        AgentRecord(
            pubkey=hermes.public_key,
            handle="gabo@doorslip.test",
            label="hermes",
            revoked=True,
        )
    )
    sealed = gabo_envelope()

    assert verify_sender(sealed.raw, sealed.signature, directory) is Rejection.REVOKED_KEY


def test_a_body_tampered_with_in_transit_is_rejected(hermes, gabo_envelope):
    directory = Directory(
        AgentRecord(pubkey=hermes.public_key, handle="gabo@doorslip.test", label="hermes")
    )
    sealed = gabo_envelope()

    tampered = sealed.raw.replace(b"saturday barbecue", b"sunday barbecue")

    assert verify_sender(tampered, sealed.signature, directory) is Rejection.BAD_SIGNATURE


def test_a_signature_replayed_on_another_envelope_is_rejected(hermes, gabo_envelope):
    """The signature covers the whole envelope, `to` and `thread_id` included."""
    directory = Directory(
        AgentRecord(pubkey=hermes.public_key, handle="gabo@doorslip.test", label="hermes")
    )
    original = gabo_envelope(to="tomas@doorslip.test")
    other = gabo_envelope(to="nanton@doorslip.test")

    assert verify_sender(other.raw, original.signature, directory) is Rejection.BAD_SIGNATURE


def test_a_malformed_envelope_never_reaches_the_signature_check(hermes):
    directory = Directory(
        AgentRecord(pubkey=hermes.public_key, handle="gabo@doorslip.test", label="hermes")
    )

    assert verify_sender(b"{broken", "signature", directory) is Rejection.MALFORMED


def test_a_label_mismatch_passes_but_is_flagged(hermes, gabo_envelope):
    """The label is a human-facing tag, not part of the trust model."""
    directory = Directory(
        AgentRecord(pubkey=hermes.public_key, handle="gabo@doorslip.test", label="claude")
    )
    sealed = gabo_envelope(sender_agent="hermes")

    result = verify_sender(sealed.raw, sealed.signature, directory)

    assert isinstance(result, VerifiedSender)
    assert result.label_mismatch


def test_rejections_carry_their_http_status():
    """Spec §7.5: the agent on the other side reacts differently to each one."""
    assert Rejection.MALFORMED.http_status == 400
    assert Rejection.UNKNOWN_KEY.http_status == 401
    assert Rejection.HANDLE_MISMATCH.http_status == 401
