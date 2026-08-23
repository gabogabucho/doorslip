"""Byte discipline of the envelope (spec §5.1, §7.9)."""

import json

import pytest

from doorslip.crypto import generate_keypair, verify
from doorslip.envelope import (
    MAX_ENVELOPE_BYTES,
    MAX_PROSE_CHARS,
    MAX_STATE_DEPTH,
    EnvelopeError,
    build,
    parse,
    seal,
)


def _envelope(**extra):
    base = dict(
        sender_handle="gabo@doorslip.test",
        sender_agent="hermes",
        sender_pubkey="cGs=",
        to="tomas@doorslip.test",
        state={"topic": "saturday barbecue", "status": "proposed"},
        prose="Throwing out a date.",
    )
    base.update(extra)
    return build(**base)


def test_build_returns_bytes_not_a_dict():
    """A dict would eventually get re-serialized on its way out the door."""
    assert isinstance(_envelope(), bytes)


def test_what_was_built_can_be_read_back():
    envelope = parse(_envelope())

    assert envelope["version"] == "0.1"
    assert envelope["from"]["handle"] == "gabo@doorslip.test"
    assert envelope["to"] == "tomas@doorslip.test"
    assert envelope["parent_message_id"] is None


def test_reserializing_changes_the_bytes():
    """Why the `message` table stores `envelope_raw` and not the parsed JSON.

    Both documents represent the same object and both are equally valid JSON.
    But they are different bytes, and Ed25519 signs bytes. Verifying against a
    re-serialization fails even though nobody tampered with anything — the bug
    that costs an afternoon.
    """
    raw = _envelope()

    reserialized = json.dumps(parse(raw)).encode("utf-8")

    assert reserialized != raw
    assert json.loads(reserialized) == json.loads(raw)


def test_signature_only_verifies_against_the_original_bytes():
    keypair = generate_keypair()
    raw = _envelope()
    sealed = seal(raw, keypair.private_key)

    reserialized = json.dumps(parse(raw)).encode("utf-8")

    assert verify(sealed.raw, sealed.signature, keypair.public_key)
    assert not verify(reserialized, sealed.signature, keypair.public_key)


def test_signature_covers_the_recipient():
    """Without this, a message can be replayed into another conversation."""
    keypair = generate_keypair()
    original = seal(_envelope(to="tomas@doorslip.test"), keypair.private_key)

    redirected = _envelope(to="nanton@doorslip.test")

    assert not verify(redirected, original.signature, keypair.public_key)


def test_a_new_thread_gets_its_own_id():
    first = parse(_envelope())
    second = parse(_envelope())

    assert first["thread_id"] != second["thread_id"]


def test_a_reply_declares_its_parent():
    root = parse(_envelope())

    reply = parse(
        _envelope(thread_id=root["thread_id"], parent_message_id=root["message_id"])
    )

    assert reply["thread_id"] == root["thread_id"]
    assert reply["parent_message_id"] == root["message_id"]


def test_a_parent_without_a_thread_is_incoherent():
    """A patch needs to know which thread it applies to, not just which message."""
    with pytest.raises(EnvelopeError):
        _envelope(parent_message_id="some-uuid")


def test_invalid_disclosure_is_rejected():
    with pytest.raises(EnvelopeError):
        _envelope(disclosure="public")


def test_prose_has_a_ceiling():
    with pytest.raises(EnvelopeError):
        _envelope(prose="x" * (MAX_PROSE_CHARS + 1))


def test_the_envelope_has_a_ceiling():
    """Spec §7.9: the limit protects the recipient, who spends tokens reading."""
    with pytest.raises(EnvelopeError):
        _envelope(state={"filler": "x" * MAX_ENVELOPE_BYTES})


def test_deeply_nested_state_is_rejected():
    bomb = cursor = {}
    for _ in range(MAX_STATE_DEPTH + 2):
        cursor["level"] = {}
        cursor = cursor["level"]

    with pytest.raises(EnvelopeError):
        _envelope(state=bomb)


@pytest.mark.parametrize(
    "garbage",
    [b"not json", b"[1,2,3]", b'{"version":"0.1"}', b"\xff\xfe"],
    ids=["not-json", "not-an-object", "missing-fields", "not-utf8"],
)
def test_invalid_envelopes_are_rejected(garbage):
    with pytest.raises(EnvelopeError):
        parse(garbage)


def test_a_sender_without_a_pubkey_is_rejected():
    """With no pubkey there is nothing to verify against: malformed, not 401."""
    incomplete = json.dumps(
        {
            "version": "0.1",
            "message_id": "m",
            "thread_id": "t",
            "from": {"handle": "gabo@doorslip.test"},
            "to": "tomas@doorslip.test",
            "timestamp": "2026-08-23T12:00:00+00:00",
        }
    ).encode("utf-8")

    with pytest.raises(EnvelopeError):
        parse(incomplete)
