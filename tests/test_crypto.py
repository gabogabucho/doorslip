"""Ed25519 signatures over raw bytes (spec §5.1)."""

import pytest

from doorslip.crypto import generate_keypair, sign, verify


def test_signs_and_verifies_the_same_bytes():
    keypair = generate_keypair()
    payload = b'{"hello":"world"}'

    signature = sign(payload, keypair.private_key)

    assert verify(payload, signature, keypair.public_key)


def test_a_single_changed_byte_invalidates_the_signature():
    keypair = generate_keypair()
    signature = sign(b'{"amount":100}', keypair.private_key)

    assert not verify(b'{"amount":900}', signature, keypair.public_key)


def test_signature_does_not_verify_against_another_key():
    sender = generate_keypair()
    impostor = generate_keypair()
    payload = b"whatever"

    signature = sign(payload, sender.private_key)

    assert not verify(payload, signature, impostor.public_key)


def test_each_generated_pair_is_unique():
    assert generate_keypair().public_key != generate_keypair().public_key


@pytest.mark.parametrize(
    "garbage",
    ["not-base64!!", "", "YWJj", "////"],
    ids=["invalid", "empty", "too-short", "wrong-length"],
)
def test_malformed_input_returns_false_instead_of_raising(garbage):
    """verify() handles data straight off the network. It must never raise.

    If it did, every call site would need its own try/except, and the day
    someone forgets one a malformed envelope takes down the process.
    """
    keypair = generate_keypair()
    payload = b"x"
    signature = sign(payload, keypair.private_key)

    assert not verify(payload, garbage, keypair.public_key)
    assert not verify(payload, signature, garbage)


def test_signing_a_str_is_a_programming_error():
    """Accepting str would force an encoding choice inside this function.

    That is exactly where a second serialization creeps in and signatures stop
    verifying across implementations. Better to fail loudly at the call site.
    """
    keypair = generate_keypair()

    with pytest.raises(TypeError):
        sign('{"hello":"world"}', keypair.private_key)

    with pytest.raises(TypeError):
        verify('{"hello":"world"}', "x", keypair.public_key)
