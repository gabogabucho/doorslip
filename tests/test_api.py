"""HTTP surface: nonce issuance and registration (spec §7.1, §7.2)."""

import json

import pytest
from fastapi.testclient import TestClient

from doorslip.api import SIGNATURE_HEADER, create_app
from doorslip.crypto import generate_keypair, sign
from doorslip.store import Store, connect


@pytest.fixture
def client():
    db = connect(":memory:")
    try:
        yield TestClient(create_app(Store(db)))
    finally:
        db.close()


def _signed_registration(client, keypair, *, handle="gabo@doorslip.test", label="hermes"):
    """Build a registration exactly as an agent would.

    Note what happens here and nowhere else: the body is serialized ONCE into
    bytes, those bytes are signed, and those same bytes are posted with
    `content=`. Using `json=` would let httpx re-serialize and the signature
    would no longer match what the server reads.
    """
    nonce = client.get("/nonce", params={"pubkey": keypair.public_key}).json()["nonce"]
    raw = json.dumps(
        {
            "handle": handle,
            "pubkey": keypair.public_key,
            "label": label,
            "nonce": nonce,
        }
    ).encode("utf-8")
    return raw, sign(raw, keypair.private_key)


def test_a_nonce_is_issued_for_the_requested_key(client):
    keypair = generate_keypair()

    response = client.get("/nonce", params={"pubkey": keypair.public_key})

    assert response.status_code == 200
    assert response.json()["nonce"]
    assert response.json()["expires_at"]


def test_registration_creates_the_identity(client):
    keypair = generate_keypair()
    raw, signature = _signed_registration(client, keypair)

    response = client.post("/register", content=raw, headers={SIGNATURE_HEADER: signature})

    assert response.status_code == 201
    assert response.json()["handle"] == "gabo@doorslip.test"
    assert response.json()["agent_label"] == "hermes"


def test_registration_without_a_signature_is_refused(client):
    keypair = generate_keypair()
    raw, _ = _signed_registration(client, keypair)

    response = client.post("/register", content=raw)

    assert response.status_code == 401


def test_registration_signed_by_a_different_key_is_refused(client):
    """Proof of possession: the body must be signed by the key being registered.

    Without this check anyone could register a public key they do not hold and
    poison the directory with an identity nobody can use.
    """
    keypair = generate_keypair()
    impostor = generate_keypair()
    raw, _ = _signed_registration(client, keypair)

    response = client.post(
        "/register",
        content=raw,
        headers={SIGNATURE_HEADER: sign(raw, impostor.private_key)},
    )

    assert response.status_code == 401


def test_a_body_altered_after_signing_is_refused(client):
    keypair = generate_keypair()
    raw, signature = _signed_registration(client, keypair)

    tampered = raw.replace(b"gabo@", b"tomas@")

    response = client.post(
        "/register", content=tampered, headers={SIGNATURE_HEADER: signature}
    )

    assert response.status_code == 401


def test_a_nonce_cannot_be_reused(client):
    keypair = generate_keypair()
    raw, signature = _signed_registration(client, keypair)
    client.post("/register", content=raw, headers={SIGNATURE_HEADER: signature})

    replayed = client.post(
        "/register", content=raw, headers={SIGNATURE_HEADER: signature}
    )

    assert replayed.status_code == 401


def test_a_nonce_issued_for_another_key_is_refused(client):
    """The server must check the binding, not just that the nonce exists."""
    keypair = generate_keypair()
    other = generate_keypair()
    foreign_nonce = client.get("/nonce", params={"pubkey": other.public_key}).json()["nonce"]
    raw = json.dumps(
        {
            "handle": "gabo@doorslip.test",
            "pubkey": keypair.public_key,
            "label": "hermes",
            "nonce": foreign_nonce,
        }
    ).encode("utf-8")

    response = client.post(
        "/register", content=raw, headers={SIGNATURE_HEADER: sign(raw, keypair.private_key)}
    )

    assert response.status_code == 401


def test_a_bad_signature_does_not_burn_the_nonce(client):
    """Otherwise anyone observing a request can grief the sender by replaying
    it with one byte flipped, spending the nonce before the real attempt lands.
    """
    keypair = generate_keypair()
    impostor = generate_keypair()
    raw, signature = _signed_registration(client, keypair)

    client.post(
        "/register", content=raw, headers={SIGNATURE_HEADER: sign(raw, impostor.private_key)}
    )
    retried = client.post(
        "/register", content=raw, headers={SIGNATURE_HEADER: signature}
    )

    assert retried.status_code == 201


def test_a_taken_handle_is_refused(client):
    first = generate_keypair()
    raw, signature = _signed_registration(client, first)
    client.post("/register", content=raw, headers={SIGNATURE_HEADER: signature})

    second = generate_keypair()
    raw, signature = _signed_registration(client, second, label="claude")

    response = client.post("/register", content=raw, headers={SIGNATURE_HEADER: signature})

    assert response.status_code == 409


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[1,2,3]", b'{"handle":"gabo@doorslip.test"}', b"{}"],
    ids=["not-json", "not-an-object", "missing-fields", "empty-object"],
)
def test_malformed_registration_bodies_are_refused(client, body):
    keypair = generate_keypair()

    response = client.post(
        "/register",
        content=body,
        headers={SIGNATURE_HEADER: sign(body, keypair.private_key)},
    )

    assert response.status_code == 400
