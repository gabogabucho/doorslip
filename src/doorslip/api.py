"""HTTP surface for Doorslip (spec §7).

One rule dominates every handler that deals with a signature: the body is read
as **raw bytes** with `await request.body()` and verified as such. Never a
parsed Pydantic model, never `json.loads` output re-serialized. The moment a
handler verifies against anything but the bytes that arrived, signatures start
failing between implementations for reasons nobody can see.

That is why request bodies here are not declared as Pydantic models even though
FastAPI would happily do it: declaring one invites the next person to verify
against it.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from doorslip.auth import AUTH_HEADER, parse_credential, signature_holds
from doorslip.crypto import generate_keypair, verify
from doorslip.envelope import EnvelopeError, parse
from doorslip.identity import Rejection, VerifiedSender, verify_sender
from doorslip.store import (
    ENROLL_PREFIX,
    INVITE_PREFIX,
    MAX_AGENTS_PER_HUMAN,
    HandleTaken,
    Human,
    InviteInvalid,
    KeyAlreadyRegistered,
    Store,
    TooManyAgents,
)
from doorslip.welcome import WELCOME_LABEL, WelcomeAgent

SIGNATURE_HEADER = "X-Doorslip-Signature"
DEFAULT_WELCOME_HANDLE = "welcome@doorslip.test"


def create_app(store: Store, *, welcome_handle: str = DEFAULT_WELCOME_HANDLE) -> FastAPI:
    """Build the app around an open store.

    A factory rather than a module-level app so tests get an isolated
    in-memory database instead of sharing global state.
    """
    app = FastAPI(title="Doorslip", version="0.1")
    welcome = _ensure_welcome_agent(store, welcome_handle)
    app.state.store = store
    app.state.welcome = welcome

    def authenticate(request: Request) -> Human | Response:
        """Spend a nonce and resolve the caller to a human (spec §7.1)."""
        credential = parse_credential(request.headers.get(AUTH_HEADER))
        if credential is None:
            return _error(401, f"missing or malformed {AUTH_HEADER}")
        if not signature_holds(credential):
            return _error(401, "auth signature does not verify")

        record = store.find_agent(credential.pubkey)
        if record is None:
            return _error(401, "key is not registered")
        if record.revoked:
            return _error(401, "key is revoked")
        if not store.consume_nonce(credential.nonce, credential.pubkey):
            return _error(401, "nonce is unknown, expired, already used, or another key's")

        human = store.find_human(record.handle)
        assert human is not None  # the join in find_agent guarantees it
        return human

    # -- identity ---------------------------------------------------------

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
            pubkey = _required(body, "pubkey")
            label = _required(body, "label")
            nonce = _required(body, "nonce")
            enrolling = "enroll_code" in body
            code = _required(body, "enroll_code") if enrolling else None
            handle = None if enrolling else _required(body, "handle")
        except ValueError as exc:
            return _error(400, str(exc))

        # Signature first, nonce second. A bad signature must not burn a valid
        # nonce — otherwise anyone who can observe a request can grief the
        # sender by replaying it with one byte flipped.
        if not verify(raw, signature, pubkey):
            return _error(401, "signature does not verify against the given pubkey")

        if not store.consume_nonce(nonce, pubkey):
            return _error(401, "nonce is unknown, expired, already used, or another key's")

        if enrolling:
            assert code is not None
            if code.startswith(INVITE_PREFIX):
                # The prefixes exist so this is catchable. Redeeming an
                # invitation here would enrol another human as your own agent.
                return _error(400, "that is an invitation code, not an enrolment code")
            try:
                human = store.redeem_enroll_code(code, pubkey=pubkey, label=label)
            except TooManyAgents as exc:
                return _error(409, str(exc))
            except KeyAlreadyRegistered:
                return _error(409, "pubkey already registered")
            except InviteInvalid as exc:
                return _error(400, str(exc))

            _notify_enrolment(store, welcome, human, label)
            return JSONResponse(
                {
                    "handle": human.handle,
                    "human_id": human.id,
                    "agent_label": label,
                    "welcome_handle": welcome.handle,
                    "enrolled": True,
                    "active_agents": store.active_agent_labels(human.id),
                },
                status_code=201,
            )

        assert handle is not None
        try:
            human = store.register_identity(handle=handle, pubkey=pubkey, label=label)
        except HandleTaken:
            return _error(409, f"handle already registered: {handle}")
        except KeyAlreadyRegistered:
            return _error(409, "pubkey already registered")

        return JSONResponse(
            {
                "handle": human.handle,
                "human_id": human.id,
                "agent_label": label,
                "welcome_handle": welcome.handle,
            },
            status_code=201,
        )

    @app.post("/enroll-code")
    def enroll_code(request: Request) -> Response:
        """Mint a code to attach another agent to your own identity (spec §7.3).

        Any active key may enrol, and any may revoke. No hierarchy on purpose:
        once an agent is compromised the damage is already total, so
        restricting who may enrol buys nothing real and adds edge cases.
        """
        caller = authenticate(request)
        if isinstance(caller, Response):
            return caller
        if store.count_active_agents(caller.id) >= MAX_AGENTS_PER_HUMAN:
            return _error(409, f"already at {MAX_AGENTS_PER_HUMAN} active agents")
        return JSONResponse({"code": store.create_enroll_code(caller.id)}, status_code=201)

    @app.post("/revoke-key")
    async def revoke_key(request: Request) -> Response:
        """Revoke one agent key (spec §7.6). Any active key may revoke any other.

        No hierarchy on purpose: once an agent is compromised the damage is
        already total — it reads the inbox, signs as the identity, sees the
        address book — so restricting who may revoke buys nothing real and
        adds edge cases.
        """
        caller = authenticate(request)
        if isinstance(caller, Response):
            return caller

        try:
            body = _decode_body(await request.body())
            target = _required(body, "pubkey")
        except ValueError as exc:
            return _error(400, str(exc))

        record = store.find_agent(target)
        if record is None or record.handle != caller.handle:
            return _error(403, "that key does not belong to your identity")
        if not store.revoke_key(target):
            return _error(409, "key is already revoked")
        return JSONResponse({"revoked": target})

    # -- address book -----------------------------------------------------

    @app.post("/invite")
    def invite(request: Request) -> Response:
        """Mint an invitation code to hand to someone out of band (spec §7.4)."""
        caller = authenticate(request)
        if isinstance(caller, Response):
            return caller
        return JSONResponse({"code": store.create_invite(caller.id)}, status_code=201)

    @app.post("/accept")
    async def accept(request: Request) -> Response:
        """Redeem an invitation code. Creates BOTH contact rows (spec §4)."""
        caller = authenticate(request)
        if isinstance(caller, Response):
            return caller

        try:
            body = _decode_body(await request.body())
            code = _required(body, "code")
        except ValueError as exc:
            return _error(400, str(exc))

        if code.startswith(ENROLL_PREFIX):
            # The two prefixes exist so this mistake is catchable. An enrolment
            # code attaches a key to YOUR identity; accepting one here would
            # mean adding a stranger as your own agent.
            return _error(400, "that is an enrolment code, not an invitation")

        try:
            issuer = store.redeem_invite(code, caller.id)
        except InviteInvalid as exc:
            return _error(400, str(exc))
        return JSONResponse({"contact": issuer.handle}, status_code=201)

    @app.get("/contacts")
    def contacts(request: Request) -> Response:
        caller = authenticate(request)
        if isinstance(caller, Response):
            return caller
        return JSONResponse(
            {
                "handle": caller.handle,
                "contacts": [
                    {"handle": c.handle, "disclosure": c.disclosure}
                    for c in store.list_contacts(caller.id)
                ],
            }
        )

    # -- mailbox ----------------------------------------------------------

    @app.post("/inbox")
    async def deposit(request: Request) -> Response:
        """Deposit a message (spec §7.5).

        The recipient travels in the envelope only, never in the path. Putting
        it in both would create a mismatch to validate; with one source there
        is nothing to disagree with, and the signature already covers it.
        """
        raw = await request.body()
        signature = request.headers.get(SIGNATURE_HEADER)
        if not signature:
            return _error(401, f"missing {SIGNATURE_HEADER}")

        outcome = verify_sender(raw, signature, store.find_agent)
        if isinstance(outcome, Rejection):
            return _error(outcome.http_status, outcome.name.lower())

        envelope = outcome.envelope
        sender = store.find_human(outcome.handle)
        assert sender is not None

        recipient = store.find_human(envelope["to"])
        if recipient is None:
            return _error(404, "no such handle")

        # The address book IS the anti-spam of v0, and it is enough. The
        # welcome desk is the one exception (spec §8).
        if not recipient.is_welcome and not store.is_contact(recipient.id, sender.id):
            return _error(403, "the recipient has not accepted you")

        if store.message_exists(envelope["message_id"]):
            return _error(409, "duplicate message_id")

        error = _check_parent(store, envelope)
        if error is not None:
            return error

        agent_id = store.agent_id_for(outcome.pubkey)
        assert agent_id is not None
        store.store_message(
            envelope=envelope,
            raw=raw,
            signature=signature,
            from_human_id=sender.id,
            from_agent_id=agent_id,
            to_human_id=recipient.id,
        )
        if outcome.label_mismatch:
            store.log("label_mismatch", pubkey=outcome.pubkey, detail=envelope["message_id"])

        if recipient.is_welcome:
            _welcome_reply(store, welcome, envelope, sender.id)

        return JSONResponse({"accepted": envelope["message_id"]}, status_code=202)

    @app.get("/inbox")
    def read_inbox(request: Request, unacked_only: bool = False) -> Response:
        """Read your own messages. No side effects — acknowledging is a POST."""
        caller = authenticate(request)
        if isinstance(caller, Response):
            return caller
        return JSONResponse(
            {
                "handle": caller.handle,
                "messages": [
                    {
                        "message_id": m.id,
                        "thread_id": m.thread_id,
                        "parent_message_id": m.parent_message_id,
                        "from": m.sender_handle,
                        "envelope": m.envelope,
                        "acked": m.acked,
                        "received_at": m.created_at,
                    }
                    for m in store.fetch_inbox(caller.id, unacked_only=unacked_only)
                ],
            }
        )

    @app.post("/ack")
    async def acknowledge(request: Request) -> Response:
        """Confirm the message was INCORPORATED, not merely received (spec §7.7).

        A POST and not a GET query parameter: an acknowledgement is a mutation,
        and GETs get cached, retried and fired by prefetchers on their own.
        """
        caller = authenticate(request)
        if isinstance(caller, Response):
            return caller

        try:
            body = _decode_body(await request.body())
            message_id = _required(body, "message_id")
        except ValueError as exc:
            return _error(400, str(exc))

        if not store.ack_message(message_id, caller.id):
            return _error(404, "no such message in your inbox, or already acknowledged")
        return JSONResponse({"acked": message_id})

    # -- instrumentation --------------------------------------------------

    @app.get("/metrics")
    def metrics() -> Response:
        """Spec §9. Internal: not part of the protocol, not for agents."""
        return JSONResponse(store.metrics())

    return app


def _ensure_welcome_agent(store: Store, handle: str) -> WelcomeAgent:
    """Create the welcome identity if it is not there yet (spec §8)."""
    existing = store.find_human(handle)
    if existing is not None:
        # A restart loses the in-memory key. Fine for v0: the desk only ever
        # signs replies it generates now, and nothing verifies its old ones
        # after the fact.
        keypair = generate_keypair()
        return WelcomeAgent(handle=handle, human_id=existing.id, keypair=keypair)

    keypair = generate_keypair()
    human = store.register_identity(
        handle=handle, pubkey=keypair.public_key, label=WELCOME_LABEL, is_welcome=True
    )
    return WelcomeAgent(handle=handle, human_id=human.id, keypair=keypair)


def _welcome_reply(
    store: Store, welcome: WelcomeAgent, envelope: dict[str, Any], sender_human_id: str
) -> None:
    """Answer a newcomer and put them in each other's address books."""
    store.add_contact_pair(welcome.human_id, sender_human_id)
    raw, signature = welcome.reply_to(envelope)
    reply = parse(raw)
    agent_id = store.agent_id_for(welcome.keypair.public_key)
    if agent_id is None:
        return
    store.store_message(
        envelope=reply,
        raw=raw,
        signature=signature,
        from_human_id=welcome.human_id,
        from_agent_id=agent_id,
        to_human_id=sender_human_id,
    )


def _notify_enrolment(
    store: Store, welcome: WelcomeAgent, human: Human, new_label: str
) -> None:
    """Tell every active key that another one was just added (spec §7.3).

    The notice is signed by the SERVER's own identity, never by the key that
    made the change. If the compromised agent signed its own announcement it
    would control the warning too, and the alert would be worth nothing.

    One message reaches every agent of this person because they share a single
    inbox — which is the same property that makes the address book shared.
    """
    raw, signature = welcome.notify_enrolment(human.handle, new_label)
    agent_id = store.agent_id_for(welcome.keypair.public_key)
    if agent_id is None:
        return
    store.add_contact_pair(welcome.human_id, human.id)
    store.store_message(
        envelope=parse(raw),
        raw=raw,
        signature=signature,
        from_human_id=welcome.human_id,
        from_agent_id=agent_id,
        to_human_id=human.id,
    )


def _check_parent(store: Store, envelope: dict[str, Any]) -> Response | None:
    """A patch must name a parent that exists in the same thread (spec §6.1)."""
    parent_id = envelope.get("parent_message_id")
    if parent_id is None:
        return None
    parent = store.message_envelope(parent_id)
    if parent is None:
        return _error(400, "parent_message_id refers to a message that does not exist")
    if parent["thread_id"] != envelope["thread_id"]:
        return _error(400, "parent_message_id belongs to a different thread")
    return None


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


__all__ = ["create_app", "SIGNATURE_HEADER", "EnvelopeError", "VerifiedSender"]
