"""HTTP surface for Doorslip (spec §7).

One rule dominates every handler that deals with a signature: the body is read
as **raw bytes** with `await request.body()` and verified as such. Never a
parsed Pydantic model, never `json.loads` output re-serialized. The moment a
handler verifies against anything but the bytes that arrived, signatures start
failing between implementations for reasons nobody can see.

That is why the request bodies here are not declared as Pydantic models even
though FastAPI would happily do it: declaring one invites the next person to
verify against it.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from doorslip.crypto import verify
from doorslip.store import HandleTaken, KeyAlreadyRegistered, Store

SIGNATURE_HEADER = "X-Doorslip-Signature"


def create_app(store: Store) -> FastAPI:
    """Build the app around an open store.

    A factory rather than a module-level app so tests get an isolated
    in-memory database instead of sharing global state.
    """
    app = FastAPI(title="Doorslip", version="0.1")

    @app.get("/nonce")
    def issue_nonce(pubkey: str) -> Response:
        """Mint a single-use nonce bound to `pubkey` (spec §7.1).

        Deliberately open to unregistered keys: `POST /register` needs a nonce
        before the key exists in the directory.
        """
        nonce = store.issue_nonce(pubkey)
        return JSONResponse(
            {"nonce": nonce.value, "expires_at": nonce.expires_at.isoformat()}
        )

    @app.post("/register")
    async def register(request: Request) -> Response:
        """Create an identity and its first agent (spec §7.2).

        Proof of possession: the body is signed with the private half of the
        very key being registered. Without it anyone could register a key they
        do not hold and poison the directory.
        """
        raw = await request.body()
        signature = request.headers.get(SIGNATURE_HEADER)
        if not signature:
            return _error(401, f"missing {SIGNATURE_HEADER}")

        try:
            body = _decode_body(raw)
            handle = _required(body, "handle")
            pubkey = _required(body, "pubkey")
            label = _required(body, "label")
            nonce = _required(body, "nonce")
        except ValueError as exc:
            return _error(400, str(exc))

        if "enroll_code" in body:
            # Spec §7.3. Not in this slice: enrolling extra keys for one human
            # is not exercised by the done-criterion, which needs two agents
            # belonging to *different* people.
            return _error(501, "enrolling additional agents is not implemented yet")

        # Signature first, nonce second. A bad signature must not burn a valid
        # nonce — otherwise anyone who can observe a request can grief the
        # sender by replaying it with one byte flipped.
        if not verify(raw, signature, pubkey):
            return _error(401, "signature does not verify against the given pubkey")

        if not store.consume_nonce(nonce, pubkey):
            return _error(401, "nonce is unknown, expired, already used, or another key's")

        try:
            human = store.register_identity(handle=handle, pubkey=pubkey, label=label)
        except HandleTaken:
            return _error(409, f"handle already registered: {handle}")
        except KeyAlreadyRegistered:
            return _error(409, "pubkey already registered")

        return JSONResponse(
            {"handle": human.handle, "human_id": human.id, "agent_label": label},
            status_code=201,
        )

    return app


def _decode_body(raw: bytes) -> dict[str, Any]:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"body is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    return body


def _required(body: dict[str, Any], field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing or empty field: {field}")
    return value


def _error(status: int, detail: str) -> Response:
    return JSONResponse({"detail": detail}, status_code=status)
