"""doorslip-auth-v1 interoperability, binding, and migration contract."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from doorslip import api as api_module
from doorslip import auth
from doorslip.api import create_app
from doorslip.client import Agent, ProtocolError
from doorslip.crypto import KeyPair, generate_keypair, sign, verify
from doorslip.store import Store, connect

PRIVATE_KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
PUBLIC_KEY = "A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg="
NONCE = "ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8"
AUTH_BODY_LIMIT = 64 * 1024


def _contract_frame(
    method: str,
    raw_path: bytes,
    query_string: bytes,
    nonce: str,
    body: bytes,
) -> bytes:
    """Independent transcription of SPEC-AUTH.md, not a production helper."""
    target = raw_path + (b"?" + query_string if query_string else b"")
    return b"\n".join(
        (
            b"doorslip-auth-v1",
            method.encode("ascii"),
            target,
            nonce.encode("ascii"),
            hashlib.sha256(body).hexdigest().encode("ascii"),
        )
    )


@pytest.mark.parametrize(
    ("method", "raw_path", "query", "body", "expected_signature"),
    [
        (
            "GET",
            b"/contacts",
            b"",
            b"",
            "qks9hJNIFbOZvcWnlfC99EueI+/JUV5mT281ShDSh7VvjhJHQ5vA0DA20zLivU0YZfG4NiGUqImVmLpyZT1tBQ==",
        ),
        (
            "GET",
            b"/inbox",
            b"unacked_only=true",
            b"",
            "d2TkukoqWrOyPylwUoFbbwsP7c2dwkTq2lv/hiEuCd3smCtJfpWGYynUfihTJ4VPJyVmfcRmhRK4B6DCFrjzCQ==",
        ),
        (
            "GET",
            b"/x/%2F",
            b"",
            b"",
            "TI5PgXU82IBApCoc9MAJKxnrYmSrBoLhbM8U7rILKHogLVgphfAGkWOkRUXCAD6iT76DuVVB9IFL6xU1tgVdAw==",
        ),
        (
            "GET",
            b"/x/%2f",
            b"",
            b"",
            "X9hdtQMOjPn0KFUJOgdPPj3zOE9MaSxlLs+99o3dWbsXhikcfXOlA9cov9dL0R5APOgwVwf9yvBK/ItIHRgyAQ==",
        ),
        (
            "GET",
            b"/inbox",
            b"a=1&a=2",
            b"",
            "QtFqFko6aQxRUe7C7loSQt5mwtTV5nkGO+ToC4hgXXxXfqlcdAVFbpGLZJFlNeZeyGRurFZuzCHP6eceZ1RCDw==",
        ),
        (
            "POST",
            b"/revoke-key",
            b"",
            b'{"pubkey":"AAA="}',
            "qzEBBWLpOfciawQcDS/JRq8Hv/ZbSte+xxb7TgUTgdkh+Qq6Oq6NA6bJahKOuPhbW9nHwbohtyNDZiUdt53wDg==",
        ),
    ],
    ids=[
        "get-no-query",
        "get-query",
        "uppercase-percent-escape",
        "lowercase-percent-escape",
        "ordered-repeated-query",
        "post-body",
    ],
)
def test_every_published_interoperability_vector(
    method, raw_path, query, body, expected_signature
):
    independently_derived = _contract_frame(method, raw_path, query, NONCE, body)

    assert auth.build_auth_frame(method, raw_path, query, NONCE, body) == independently_derived
    assert sign(independently_derived, PRIVATE_KEY) == expected_signature
    assert verify(independently_derived, expected_signature, PUBLIC_KEY)


def test_the_published_seed_and_nonce_have_the_stated_bytes():
    assert base64.b64decode(PRIVATE_KEY, validate=True) == bytes(range(0x20))
    assert base64.urlsafe_b64decode(NONCE + "=") == bytes(range(0x20, 0x40))


def test_the_frame_has_five_lines_and_no_trailing_lf():
    frame = auth.build_auth_frame("GET", b"/contacts", b"", NONCE, b"")

    assert frame.count(b"\n") == 4
    assert not frame.endswith(b"\n")


@pytest.mark.parametrize(
    ("method", "raw_path", "query"),
    [
        ("get", b"/contacts", b""),
        ("GET\nPOST", b"/contacts", b""),
        ("GET", b"/has space", b""),
        ("GET", b"/contacts?", b""),
        ("GET", b"/x#frag", b""),
        ("GET", b"/x", b"a=1#frag"),
        ("GET", b"/contacts", b"x=one\ntwo"),
        ("GET", b"/caf\xc3\xa9", b""),
    ],
)
def test_out_of_profile_frame_components_are_rejected(method, raw_path, query):
    with pytest.raises(ValueError):
        auth.build_auth_frame(method, raw_path, query, NONCE, b"")


@pytest.mark.parametrize(
    ("raw_path", "query", "expected_target"),
    [
        (b"/x%23frag", b"", b"/x%23frag"),
        (b"/x", b"a=1%23frag", b"/x?a=1%23frag"),
    ],
)
def test_percent_encoded_fragments_remain_in_profile(raw_path, query, expected_target):
    frame = auth.build_auth_frame("GET", raw_path, query, NONCE, b"")

    assert frame.split(b"\n")[2] == expected_target


def _valid_header() -> str:
    signature = sign(_contract_frame("GET", b"/contacts", b"", NONCE, b""), PRIVATE_KEY)
    return f"{PUBLIC_KEY}.{NONCE}.{signature}"


@pytest.mark.parametrize(
    "header",
    [
        f"{PUBLIC_KEY.rstrip('=')}.{NONCE}.{'A' * 86}==",
        f"{PUBLIC_KEY.replace('/', '_')}.{NONCE}.{'A' * 86}==",
        f"{PUBLIC_KEY}.{NONCE}=.{'A' * 86}==",
        f"{PUBLIC_KEY}.{base64.b64encode(bytes([251]) * 32).decode('ascii').rstrip('=')}.{'A' * 86}==",
        f"{PUBLIC_KEY}.{NONCE}.{'A' * 86}",
        f"{PUBLIC_KEY}.{NONCE}.{'A' * 85}_==",
        _valid_header() + ".extra",
    ],
    ids=[
        "unpadded-public-key",
        "base64url-public-key",
        "padded-nonce",
        "standard-base64-nonce",
        "unpadded-signature",
        "base64url-signature",
        "extra-component",
    ],
)
def test_component_encodings_are_strict(header):
    assert auth.parse_credential(header) is None


@pytest.fixture
def http_and_store():
    db = connect(":memory:")
    store = Store(db)
    try:
        yield TestClient(create_app(store)), store
    finally:
        db.close()


@pytest.fixture
def registered(http_and_store):
    http, _ = http_and_store
    agent = Agent(
        http,
        handle="gabo@doorslip.test",
        label="hermes",
        keypair=generate_keypair(),
    )
    agent.register()
    return agent


def _nonce(http: TestClient, pubkey: str) -> str:
    return http.get("/nonce", params={"pubkey": pubkey}).json()["nonce"]


def _asgi_request(
    app,
    *,
    method: str,
    raw_path: bytes,
    query: bytes = b"",
    headers: dict[str, str] | None = None,
    body_chunks: list[bytes] | None = None,
) -> tuple[int, bytes, int]:
    async def request() -> tuple[int, bytes, int]:
        sent = []
        pending = list(body_chunks if body_chunks is not None else [b""])
        chunks_read = 0

        async def receive():
            nonlocal chunks_read
            if pending:
                chunk = pending.pop(0)
                chunks_read += 1
                return {
                    "type": "http.request",
                    "body": chunk,
                    "more_body": bool(pending),
                }
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": method,
                "scheme": "http",
                "path": raw_path.decode("ascii"),
                "raw_path": raw_path,
                "query_string": query,
                "root_path": "",
                "headers": [
                    (name.lower().encode("ascii"), value.encode("ascii"))
                    for name, value in (headers or {}).items()
                ],
                "client": ("testclient", 50000),
                "server": ("testserver", 80),
            },
            receive,
            send,
        )
        status = next(
            message["status"]
            for message in sent
            if message["type"] == "http.response.start"
        )
        response_body = b"".join(
            message.get("body", b"")
            for message in sent
            if message["type"] == "http.response.body"
        )
        return status, response_body, chunks_read

    return asyncio.run(request())


def _v1_header(
    agent: Agent,
    nonce: str,
    *,
    method: str,
    raw_path: bytes,
    query: bytes = b"",
    body: bytes = b"",
) -> dict[str, str]:
    return {
        auth.AUTH_HEADER: auth.build_credential(
            agent.pubkey,
            nonce,
            agent._keypair.private_key,
            method=method,
            raw_path=raw_path,
            query_string=query,
            body=body,
        )
    }


def test_changing_the_method_fails_authentication(http_and_store, registered):
    http, _ = http_and_store
    nonce = _nonce(http, registered.pubkey)
    headers = _v1_header(registered, nonce, method="GET", raw_path=b"/contacts")

    response = http.post("/contacts", content=b"", headers=headers)

    assert response.status_code == 401


def test_changing_the_raw_target_fails_authentication(http_and_store, registered):
    http, _ = http_and_store
    nonce = _nonce(http, registered.pubkey)
    headers = _v1_header(registered, nonce, method="GET", raw_path=b"/contacts")

    response = http.get("/contacts?extra=1", headers=headers)

    assert response.status_code == 401


@pytest.mark.parametrize("actual_query", [b"a=2&a=1", b"a=2", b"a=1"])
def test_query_order_and_multiplicity_are_bound(
    http_and_store, registered, actual_query
):
    http, _ = http_and_store
    nonce = _nonce(http, registered.pubkey)
    headers = _v1_header(
        registered,
        nonce,
        method="GET",
        raw_path=b"/contacts",
        query=b"a=1&a=2",
    )

    response = http.get(
        (b"/contacts?" + actual_query).decode("ascii"), headers=headers
    )

    assert response.status_code == 401


def test_percent_escape_case_is_bound(http_and_store, registered):
    http, _ = http_and_store
    nonce = _nonce(http, registered.pubkey)
    headers = _v1_header(
        registered,
        nonce,
        method="GET",
        raw_path=b"/contacts",
        query=b"slash=%2F",
    )

    response = http.get("/contacts?slash=%2f", headers=headers)

    assert response.status_code == 401


def test_changing_body_bytes_fails_authentication(http_and_store, registered):
    http, _ = http_and_store
    signed_body = b'{"open":true}'
    actual_body = b'{"open":false}'
    nonce = _nonce(http, registered.pubkey)
    headers = _v1_header(
        registered,
        nonce,
        method="POST",
        raw_path=b"/contacts",
        body=signed_body,
    )

    response = http.post("/contacts", content=actual_body, headers=headers)

    assert response.status_code == 401


def test_changing_the_nonce_component_fails_authentication(http_and_store, registered):
    http, _ = http_and_store
    signed_nonce = _nonce(http, registered.pubkey)
    substituted_nonce = _nonce(http, registered.pubkey)
    header = _v1_header(
        registered,
        signed_nonce,
        method="GET",
        raw_path=b"/contacts",
    )[auth.AUTH_HEADER]
    pubkey, _, signature = header.split(".")

    response = http.get(
        "/contacts",
        headers={auth.AUTH_HEADER: f"{pubkey}.{substituted_nonce}.{signature}"},
    )

    assert response.status_code == 401


def test_changing_the_key_component_fails_authentication(http_and_store, registered):
    http, _ = http_and_store
    nonce = _nonce(http, registered.pubkey)
    header = _v1_header(
        registered, nonce, method="GET", raw_path=b"/contacts"
    )[auth.AUTH_HEADER]
    _, nonce_component, signature = header.split(".")
    other = generate_keypair()

    response = http.get(
        "/contacts",
        headers={auth.AUTH_HEADER: f"{other.public_key}.{nonce_component}.{signature}"},
    )

    assert response.status_code == 401


def test_changing_the_signature_component_fails_authentication(
    http_and_store, registered
):
    http, _ = http_and_store
    nonce = _nonce(http, registered.pubkey)
    header = _v1_header(
        registered, nonce, method="GET", raw_path=b"/contacts"
    )[auth.AUTH_HEADER]
    pubkey, nonce_component, signature = header.split(".")
    replacement = "A" if signature[0] != "A" else "B"

    response = http.get(
        "/contacts",
        headers={
            auth.AUTH_HEADER: f"{pubkey}.{nonce_component}.{replacement}{signature[1:]}"
        },
    )

    assert response.status_code == 401


def test_a_v1_credential_is_single_use(http_and_store, registered):
    http, _ = http_and_store
    nonce = _nonce(http, registered.pubkey)
    headers = _v1_header(registered, nonce, method="GET", raw_path=b"/contacts")

    assert http.get("/contacts", headers=headers).status_code == 200
    assert http.get("/contacts", headers=headers).status_code == 401


def test_an_expired_v1_credential_is_refused(http_and_store, registered):
    http, store = http_and_store
    nonce = _nonce(http, registered.pubkey)
    store._db.execute(
        "UPDATE nonce SET expires_at = ? WHERE value = ?",
        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), nonce),
    )
    headers = _v1_header(registered, nonce, method="GET", raw_path=b"/contacts")

    assert http.get("/contacts", headers=headers).status_code == 401


def test_a_bad_v1_signature_does_not_burn_the_nonce(http_and_store, registered):
    http, _ = http_and_store
    nonce = _nonce(http, registered.pubkey)
    correct = _v1_header(registered, nonce, method="GET", raw_path=b"/contacts")
    impostor = generate_keypair()
    bad = {
        auth.AUTH_HEADER: auth.build_credential(
            registered.pubkey,
            nonce,
            impostor.private_key,
            method="GET",
            raw_path=b"/contacts",
            query_string=b"",
            body=b"",
        )
    }

    assert http.get("/contacts", headers=bad).status_code == 401
    assert http.get("/contacts", headers=correct).status_code == 200


def test_oversized_invalid_credential_stops_reading_when_limit_is_crossed(
    http_and_store, registered, monkeypatch
):
    http, _ = http_and_store
    nonce = _nonce(http, registered.pubkey)
    impostor = generate_keypair()
    chunks = [
        b"a" * (AUTH_BODY_LIMIT // 2),
        b"b" * (AUTH_BODY_LIMIT // 2 + 1),
        b"this chunk must not be read",
    ]
    body = b"".join(chunks)
    headers = {
        auth.AUTH_HEADER: auth.build_credential(
            registered.pubkey,
            nonce,
            impostor.private_key,
            method="POST",
            raw_path=b"/contacts",
            query_string=b"",
            body=body,
        ),
        "Content-Length": "1",
    }
    verification_attempts = []

    def track_verification(*args, **kwargs):
        verification_attempts.append((args, kwargs))
        return False

    monkeypatch.setattr(api_module, "v1_signature_holds", track_verification)
    monkeypatch.setattr(api_module, "nonce_only_signature_holds", track_verification)

    status, _, chunks_read = _asgi_request(
        http.app,
        method="POST",
        raw_path=b"/contacts",
        headers=headers,
        body_chunks=chunks,
    )

    assert (status, chunks_read, verification_attempts) == (413, 2, [])


def test_oversized_valid_request_does_not_consume_its_nonce(
    http_and_store, registered, monkeypatch
):
    http, store = http_and_store
    nonce = _nonce(http, registered.pubkey)
    prefix = b'{"open":true,"padding":"'
    suffix = b'"}'
    oversized_body = prefix + b"x" * (
        AUTH_BODY_LIMIT + 1 - len(prefix) - len(suffix)
    ) + suffix
    oversized_headers = _v1_header(
        registered,
        nonce,
        method="POST",
        raw_path=b"/contacts",
        body=oversized_body,
    )
    oversized_headers["Content-Length"] = "1"
    contact_changes = []
    original_set_open_inbox = store.set_open_inbox

    def track_contact_change(human_id, is_open):
        contact_changes.append(is_open)
        return original_set_open_inbox(human_id, is_open)

    monkeypatch.setattr(store, "set_open_inbox", track_contact_change)

    oversized_status, _, _ = _asgi_request(
        http.app,
        method="POST",
        raw_path=b"/contacts",
        headers=oversized_headers,
        body_chunks=[oversized_body[:32768], oversized_body[32768:]],
    )

    accepted_body = b'{ "open" : true }'
    accepted_headers = _v1_header(
        registered,
        nonce,
        method="POST",
        raw_path=b"/contacts",
        body=accepted_body,
    )
    retried = http.post("/contacts", content=accepted_body, headers=accepted_headers)

    assert (oversized_status, retried.status_code, contact_changes) == (
        413,
        200,
        [True],
    )


def test_in_limit_body_is_cached_unchanged_for_the_handler(
    http_and_store, registered, monkeypatch
):
    http, _ = http_and_store
    nonce = _nonce(http, registered.pubkey)
    prefix = b'{\n  "open" : true,\n  "padding" : "'
    suffix = b'"\n}'
    body = prefix + b"x" * (AUTH_BODY_LIMIT - len(prefix) - len(suffix)) + suffix
    headers = _v1_header(
        registered,
        nonce,
        method="POST",
        raw_path=b"/contacts",
        body=body,
    )
    bodies_seen_by_handler = []
    original_decode_body = api_module._decode_body

    def capture_handler_body(raw):
        bodies_seen_by_handler.append(raw)
        return original_decode_body(raw)

    monkeypatch.setattr(api_module, "_decode_body", capture_handler_body)

    status, response_body, chunks_read = _asgi_request(
        http.app,
        method="POST",
        raw_path=b"/contacts",
        headers=headers,
        body_chunks=[body[:32768], body[32768:]],
    )

    assert (status, chunks_read) == (200, 2)
    assert b'"open":true' in response_body
    assert bodies_seen_by_handler == [body]


@pytest.mark.parametrize("credential", [None, "not-a-canonical-credential"])
def test_bad_auth_header_is_rejected_without_reading_the_body(
    http_and_store, credential
):
    http, _ = http_and_store
    headers = {} if credential is None else {auth.AUTH_HEADER: credential}

    status, _, chunks_read = _asgi_request(
        http.app,
        method="POST",
        raw_path=b"/contacts",
        headers=headers,
        body_chunks=[b"x" * (AUTH_BODY_LIMIT + 1)],
    )

    assert (status, chunks_read) == (401, 0)


def test_nonce_only_is_accepted_for_one_release_and_logged_without_credentials(
    http_and_store, registered
):
    http, store = http_and_store
    nonce = _nonce(http, registered.pubkey)
    signature = sign(nonce.encode("ascii"), registered._keypair.private_key)
    header = f"{registered.pubkey}.{nonce}.{signature}"

    response = http.get("/contacts", headers={auth.AUTH_HEADER: header})

    assert response.status_code == 200
    row = store._db.execute(
        "SELECT kind, pubkey, human_id, detail FROM event_log"
        " WHERE kind = 'auth_legacy' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row[1] == registered.pubkey
    assert row[3] == auth.AUTH_NONCE_ONLY
    assert nonce not in " ".join(value or "" for value in row)
    assert signature not in " ".join(value or "" for value in row)


def test_out_of_profile_request_does_not_fall_back_or_consume_legacy_nonce(
    http_and_store, registered, monkeypatch
):
    http, _ = http_and_store
    nonce = _nonce(http, registered.pubkey)
    signature = sign(nonce.encode("ascii"), registered._keypair.private_key)
    headers = {
        auth.AUTH_HEADER: f"{registered.pubkey}.{nonce}.{signature}"
    }
    legacy_attempts = []
    original_legacy_verifier = api_module.nonce_only_signature_holds

    def track_legacy_attempt(credential):
        legacy_attempts.append(credential)
        return original_legacy_verifier(credential)

    monkeypatch.setattr(api_module, "nonce_only_signature_holds", track_legacy_attempt)
    rejected_status, _, _ = _asgi_request(
        http.app,
        method="GET",
        raw_path=b"/contacts",
        query=b"a=1#frag",
        headers=headers,
    )
    attempts_after_rejection = len(legacy_attempts)

    retried_in_profile = http.get("/contacts", headers=headers)

    assert (
        rejected_status,
        attempts_after_rejection,
        retried_in_profile.status_code,
        len(legacy_attempts),
    ) == (401, 0, 200, 1)


class _RejectingServer:
    """A tiny transport spy: advertise v1, then reject the signed request."""

    def __init__(self, keypair: KeyPair, schemes: list[str]):
        self.keypair = keypair
        self.schemes = schemes
        self.authenticated_requests: list[httpx.Request] = []

    def get(self, url: str, **kwargs):
        if url == "/nonce":
            request = httpx.Request("GET", "http://test/nonce")
            return httpx.Response(
                200,
                json={
                    "nonce": NONCE,
                    "expires_at": "2099-01-01T00:00:00+00:00",
                    "server": {"auth": self.schemes},
                },
                request=request,
            )
        request = self.build_request("GET", url, **kwargs)
        return self.send(request)

    def post(self, url: str, **kwargs):
        request = self.build_request("POST", url, **kwargs)
        return self.send(request)

    def build_request(self, method: str, url: str, **kwargs):
        return httpx.Request(method, "http://test" + url, **kwargs)

    def send(self, request: httpx.Request):
        self.authenticated_requests.append(request)
        return httpx.Response(401, json={"detail": "rejected"}, request=request)


def test_an_updated_client_uses_v1_and_never_retries_legacy_after_rejection():
    keypair = KeyPair(private_key=PRIVATE_KEY, public_key=PUBLIC_KEY)
    transport = _RejectingServer(keypair, [auth.AUTH_V1, auth.AUTH_NONCE_ONLY])
    agent = Agent(transport, handle="gabo@test", label="hermes", keypair=keypair)

    with pytest.raises(ProtocolError):
        agent.contacts()

    assert len(transport.authenticated_requests) == 1
    request = transport.authenticated_requests[0]
    credential = auth.parse_credential(request.headers[auth.AUTH_HEADER])
    assert credential is not None
    frame = _contract_frame("GET", b"/contacts", b"", NONCE, b"")
    assert verify(frame, credential.signature, credential.pubkey)


def test_an_updated_client_refuses_a_server_that_does_not_advertise_v1():
    keypair = KeyPair(private_key=PRIVATE_KEY, public_key=PUBLIC_KEY)
    transport = _RejectingServer(keypair, [auth.AUTH_NONCE_ONLY])
    agent = Agent(transport, handle="gabo@test", label="hermes", keypair=keypair)

    with pytest.raises(ProtocolError) as caught:
        agent.contacts()

    assert "doorslip-auth-v1" in caught.value.detail
    assert transport.authenticated_requests == []


def test_an_updated_client_never_emits_a_trailing_question_mark():
    keypair = KeyPair(private_key=PRIVATE_KEY, public_key=PUBLIC_KEY)
    transport = _RejectingServer(keypair, [auth.AUTH_V1, auth.AUTH_NONCE_ONLY])
    agent = Agent(transport, handle="gabo@test", label="hermes", keypair=keypair)

    with pytest.raises(ValueError, match="raw_path"):
        agent._authenticated_request("GET", "/contacts?")

    assert transport.authenticated_requests == []
