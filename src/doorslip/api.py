"""HTTP surface for Doorslip (spec §7).

One rule dominates every handler that deals with a signature: the body is read
as **raw bytes** and verified as such. Authenticated requests are streamed
through a bounded reader, then those exact bytes are cached for the handler.
Never a parsed Pydantic model, never `json.loads` output re-serialized. The
moment a handler verifies against anything but the bytes that arrived,
signatures start failing between implementations for reasons nobody can see.

That is why request bodies here are not declared as Pydantic models even though
FastAPI would happily do it: declaring one invites the next person to verify
against it.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from doorslip.auth import (
    ACCEPTED_AUTH_SCHEMES,
    AUTH_HEADER,
    AUTH_NONCE_ONLY,
    AUTH_V1,
    LEGACY_AUTH_REMOVAL_RELEASE,
    build_auth_frame,
    nonce_only_signature_holds,
    parse_credential,
    v1_signature_holds,
)
from doorslip.crypto import KeyPair, generate_keypair, verify
from doorslip.envelope import VERSION as ENVELOPE_VERSION
from doorslip.envelope import EnvelopeError, parse
from doorslip.identity import Rejection, VerifiedSender, verify_sender
from doorslip.store import (
    ENROLL_PREFIX,
    INVITE_PREFIX,
    MAX_AGENTS_PER_HUMAN,
    MAX_MESSAGES_PER_HOUR,
    SCOPE_FULL,
    SCOPES,
    HandleTaken,
    Human,
    InviteInvalid,
    KeyAlreadyRegistered,
    Store,
    TooManyAgents,
)
from doorslip.welcome import WELCOME_LABEL, WelcomeAgent

# A handle is `local@domain`, and the domain is the server it lives on. That
# is not decoration: it is how a message finds its way once there is more than
# one server (spec §11 bis), and a handle without one has nowhere to route to.
# Somebody registered as plain `raor00` before this existed.
_LOCAL_PART = re.compile(r"^[a-z0-9]([a-z0-9._-]{0,30}[a-z0-9])?$")


def normalise_handle(handle: str, server_domain: str) -> str:
    """Lowercase and check a requested handle, or explain what is wrong.

    Case is folded rather than rejected: two handles differing only in
    capitalisation would be two identities nobody can tell apart out loud, and
    a person reading one back over the phone cannot hear the difference.

    The server decides the final form and returns it, so the client stores
    what was actually registered instead of what it asked for.
    """
    candidate = handle.strip().lower()
    if candidate.count("@") != 1:
        raise ValueError(f"a handle looks like name@{server_domain}")

    local, domain = candidate.split("@")
    if domain != server_domain.lower():
        raise ValueError(f"handles on this server end in @{server_domain}")
    if not _LOCAL_PART.match(local):
        raise ValueError(
            "the part before @ may use letters, digits, dot, dash and underscore, "
            "must start and end with a letter or digit, and stops at 32 characters"
        )
    return candidate


SIGNATURE_HEADER = "X-Doorslip-Signature"
DEFAULT_WELCOME_HANDLE = "welcome@doorslip.test"
AUTHENTICATED_BODY_MAX_BYTES = 64 * 1024


class _AuthenticatedBodyTooLarge(Exception):
    pass


async def _read_authenticated_body(request: Request) -> bytes:
    """Read no more than the operational pre-authentication body limit."""
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > AUTHENTICATED_BODY_MAX_BYTES:
            raise _AuthenticatedBodyTooLarge
        chunks.append(chunk)

    body = b"".join(chunks)
    # Starlette's Request.body() uses this private cache. The stream has now
    # been consumed, so preserving the exact accepted bytes here is what lets
    # endpoint handlers call request.body() without reading or rebuilding them.
    request._body = body
    return body


@dataclass(frozen=True)
class Caller:
    """Who is calling and what their key may do.

    Proxies the human's fields so every handler that already asked for
    `caller.id` or `caller.handle` keeps reading the same way. The scope is
    the addition, and it comes from the same lookup as the handle rather than
    a second query, so the two cannot answer about different keys.
    """

    human: Human
    scope: str = SCOPE_FULL

    @property
    def id(self) -> str:
        return self.human.id

    @property
    def handle(self) -> str:
        return self.human.handle

    @property
    def is_welcome(self) -> bool:
        return self.human.is_welcome


def create_app(
    store: Store,
    *,
    welcome_handle: str = DEFAULT_WELCOME_HANDLE,
    welcome_key_path: str | Path | None = None,
) -> FastAPI:
    """Build the app around an open store.

    A factory rather than a module-level app so tests get an isolated
    in-memory database instead of sharing global state.
    """
    app = FastAPI(title="Doorslip", version="0.1")
    welcome = _ensure_welcome_agent(store, welcome_handle, welcome_key_path)
    app.state.store = store
    app.state.welcome = welcome

    async def authenticate(request: Request) -> Caller | Response:
        """Spend a nonce and resolve the caller to a human and a scope (§7.1).

        Both halves matter. The handle says whose mailbox this is; the scope
        says what this particular key was given leave to do with it, and the
        two answers come from the same row so they cannot drift apart.
        """
        credential = parse_credential(request.headers.get(AUTH_HEADER))
        if credential is None:
            return _error(401, f"missing or malformed {AUTH_HEADER}")
        try:
            body = await _read_authenticated_body(request)
        except _AuthenticatedBodyTooLarge:
            return _error(
                413,
                "authenticated request body exceeds the 65536-byte "
                "pre-authentication limit",
            )
        raw_path = request.scope.get("raw_path")
        query_string = request.scope.get("query_string", b"")
        if not isinstance(raw_path, bytes) or not isinstance(query_string, bytes):
            return _error(401, "server did not receive the raw request target")

        try:
            build_auth_frame(
                request.method, raw_path, query_string, credential.nonce, body
            )
        except ValueError:
            return _error(401, "request is outside the authentication profile")

        if v1_signature_holds(
            credential,
            method=request.method,
            raw_path=raw_path,
            query_string=query_string,
            body=body,
        ):
            scheme = AUTH_V1
        elif nonce_only_signature_holds(credential):
            scheme = AUTH_NONCE_ONLY
        else:
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
        if scheme == AUTH_NONCE_ONLY:
            # The row is operational migration evidence, not a credential log.
            # In particular, neither nonce nor signature material belongs here.
            store.log(
                "auth_legacy",
                pubkey=credential.pubkey,
                human_id=human.id,
                detail=AUTH_NONCE_ONLY,
            )
        return Caller(human=human, scope=record.scope)

    def _owner_only(caller: Caller) -> Response | None:
        """Refuse a key that was enrolled to speak and not to administer.

        These are the operations that change who may reach this mailbox, and
        an agent holding them can undo its human's ability to take them back:
        revoking the owner's own key, minting a code that grants everything,
        admitting a stranger, or dropping a subscriber.
        """
        if caller.scope == SCOPE_FULL:
            return None
        return _error(
            403,
            "this key was enrolled to send and read, not to change who may "
            "reach this mailbox; ask your human to run this from a full key",
        )

    # -- identity ---------------------------------------------------------

    @app.get("/nonce")
    def issue_nonce(request: Request, pubkey: str) -> Response:
        """Mint a single-use nonce bound to `pubkey` (spec §7.1).

        Deliberately open to unregistered keys: `POST /register` needs a nonce
        before the key exists in the directory.

        The reply also carries what this server speaks. Every authenticated
        command passes through here, so it is the one place a client is
        guaranteed to look — and a client that never learns to look cannot be
        told later that it is out of date. Adding the field now is what keeps
        that door open; it can stay ignored for as long as nothing needs it.
        """
        nonce = store.issue_nonce(pubkey)
        return JSONResponse(
            {
                "nonce": nonce.value,
                "expires_at": nonce.expires_at.isoformat(),
                "server": _server_info(request),
            }
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
            handle = normalise_handle(handle, welcome_handle.split("@", 1)[-1])
        except ValueError as exc:
            return _error(400, str(exc))

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
    async def enroll_code(request: Request) -> Response:
        """Mint a code to attach another agent to your own identity (spec §7.3).

        Any active key may enrol, and any may revoke. No hierarchy on purpose:
        once an agent is compromised the damage is already total, so
        restricting who may enrol buys nothing real and adds edge cases.
        """
        caller = await authenticate(request)
        if isinstance(caller, Response):
            return caller
        refusal = _owner_only(caller)
        if refusal is not None:
            return refusal
        if store.count_active_agents(caller.id) >= MAX_AGENTS_PER_HUMAN:
            return _error(409, f"already at {MAX_AGENTS_PER_HUMAN} active agents")

        # The scope travels on the code, decided by whoever mints it. Letting
        # the joining agent name its own would make the whole thing a
        # formality: an agent asking for `full` would get it.
        raw = await request.body()
        try:
            scope = str(_decode_body(raw).get("scope") or SCOPE_FULL) if raw else SCOPE_FULL
        except ValueError:
            return _error(400, "body is not valid JSON")
        if scope not in SCOPES:
            return _error(400, f"unknown scope {scope!r}; expected one of {list(SCOPES)}")

        return JSONResponse(
            {"code": store.create_enroll_code(caller.id, scope), "scope": scope},
            status_code=201,
        )

    @app.post("/revoke-key")
    async def revoke_key(request: Request) -> Response:
        """Revoke one agent key (spec §7.6). Any active key may revoke any other.

        No hierarchy on purpose: once an agent is compromised the damage is
        already total — it reads the inbox, signs as the identity, sees the
        address book — so restricting who may revoke buys nothing real and
        adds edge cases.
        """
        caller = await authenticate(request)
        if isinstance(caller, Response):
            return caller
        refusal = _owner_only(caller)
        if refusal is not None:
            return refusal

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
    async def invite(request: Request) -> Response:
        """Mint an invitation code to hand to someone out of band (spec §7.4)."""
        caller = await authenticate(request)
        if isinstance(caller, Response):
            return caller
        refusal = _owner_only(caller)
        if refusal is not None:
            return refusal
        return JSONResponse({"code": store.create_invite(caller.id)}, status_code=201)

    @app.post("/accept")
    async def accept(request: Request) -> Response:
        """Redeem an invitation code. Creates BOTH contact rows (spec §4)."""
        caller = await authenticate(request)
        if isinstance(caller, Response):
            return caller
        refusal = _owner_only(caller)
        if refusal is not None:
            return refusal

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

        # The person accepting learns who they added from this very reply. The
        # person who invited them would otherwise learn nothing at all, so they
        # get a slip — which is also what gives a local watcher something to
        # ring on.
        _notify_invitation_accepted(store, welcome, issuer, caller)
        return JSONResponse({"contact": issuer.handle}, status_code=201)

    @app.post("/contacts")
    async def change_contacts(request: Request) -> Response:
        """Manage your own address book: open the mailbox, or drop somebody.

        Both live here rather than on new endpoints because both are edits to
        one thing — who may write to you — and the list of endpoints is closed.
        """
        caller = await authenticate(request)
        if isinstance(caller, Response):
            return caller
        refusal = _owner_only(caller)
        if refusal is not None:
            return refusal

        try:
            body = _decode_body(await request.body())
        except ValueError as exc:
            return _error(400, str(exc))

        if "open" in body:
            if not isinstance(body["open"], bool):
                return _error(400, "'open' must be true or false")
            store.set_open_inbox(caller.id, body["open"])
            return JSONResponse({"handle": caller.handle, "open": body["open"]})

        if "remove" in body:
            handle = body["remove"]
            if not isinstance(handle, str) or not handle:
                return _error(400, "'remove' must be a handle")
            if not store.remove_contact(caller.id, handle):
                return _error(404, f"{handle} is not in your address book")
            return JSONResponse({"removed": handle})

        return _error(400, "say either 'open' or 'remove'")

    @app.get("/contacts")
    async def contacts(request: Request) -> Response:
        caller = await authenticate(request)
        if isinstance(caller, Response):
            return caller
        return JSONResponse(
            {
                "handle": caller.handle,
                "open": store.is_open_inbox(caller.id),
                "contacts": [
                    {"handle": c.handle, "disclosure": c.disclosure}
                    for c in store.list_contacts(caller.id)
                ],
                # The client has read this field since revocation was added
                # and the server never sent it, so `doorslip agents` returned
                # an empty list and the documented way to revoke an enrolled
                # key had no way to name one. Only the owner's own keys: this
                # endpoint is authenticated, and a caller sees their identity
                # and nobody else's.
                "agents": store.list_agents(caller.id),
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

        # The address book IS the anti-spam of v0, and it is enough. Two
        # exceptions: the welcome desk (spec §8), and a mailbox its owner
        # deliberately opened — a list somebody subscribes to.
        subscribing = False
        if not store.is_contact(recipient.id, sender.id):
            if recipient.is_welcome:
                pass
            elif sender.is_welcome:
                # The server may always tell somebody something, and does not
                # become their contact for doing it. Notices used to buy the
                # right by writing the pair themselves, which put the desk in
                # the address book of everyone who ever enrolled an agent —
                # and made it a subscriber of every mailbox that was a list.
                # Being able to reach you and being in your book are different
                # things, and only the first one the server needs.
                pass
            elif store.is_open_inbox(recipient.id):
                # Writing to an open mailbox is how you subscribe to it. The
                # pair is created below, once the message is known to be good.
                subscribing = True
            else:
                return _error(403, "the recipient has not accepted you")

        if store.message_exists(envelope["message_id"]):
            return _error(409, "duplicate message_id")

        # Enforced here rather than trusted to the sender. Two agents answering
        # each other unattended will not stop on their own, and the cost of a
        # loop is paid in inference by both humans, neither of whom is looking.
        if store.messages_in_last_hour(sender.id, recipient.id) >= MAX_MESSAGES_PER_HOUR:
            return _error(
                429,
                f"more than {MAX_MESSAGES_PER_HOUR} messages to this person in an "
                "hour; wait, and tell your human rather than retrying",
            )

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
        elif subscribing:
            store.add_contact_pair(recipient.id, sender.id)

        return JSONResponse(
            {"accepted": envelope["message_id"], "subscribed": subscribing or None},
            status_code=202,
        )

    @app.get("/inbox")
    async def read_inbox(
        request: Request, unacked_only: bool = False, sent: bool = False
    ) -> Response:
        """Read your own mailbox. No side effects — acknowledging is a POST.

        `sent=true` shows the other half of the same mailbox: what you sent and
        whether it was acknowledged. Not a tenth endpoint, the second view of
        one — a mailbox has always had an outgoing side.

        An agent with no reply cannot otherwise tell "they have not answered
        yet" from "their agent never saw it", and those call for opposite
        behaviour: wait, or tell your human the other side is not listening.
        """
        caller = await authenticate(request)
        if isinstance(caller, Response):
            return caller

        if sent:
            return JSONResponse(
                {
                    "handle": caller.handle,
                    "sent": [
                        {
                            "message_id": m.id,
                            "thread_id": m.thread_id,
                            "to": m.recipient_handle,
                            "topic": m.topic,
                            "acked": m.acked_at is not None,
                            "acked_at": m.acked_at,
                            "sent_at": m.created_at,
                        }
                        for m in store.fetch_sent(caller.id)
                    ],
                }
            )

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
        caller = await authenticate(request)
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

    @app.get("/stats")
    def stats() -> Response:
        """How many people are here and how many slips they have sent.

        Not part of the protocol either, and no agent needs it: it exists so
        the landing page can show two numbers instead of claiming a size it
        does not have.

        Unauthenticated on purpose. It is two integers about a whole server
        and names nobody — no handle, no key, no message, no time series. The
        rule for this endpoint is that adding a field to it is a disclosure
        decision, not a convenience, which is why a test asserts the shape is
        exactly these two keys.
        """
        return JSONResponse(store.public_counts())

    return app


def _server_info(request: Request) -> dict[str, Any]:
    """What this server speaks, and where the current documents live.

    Three separate things get confused as "the version", so they are named
    apart:

    - `protocol` is the wire format, the same string that goes in every
      envelope. It is what lets a receiver handle old and new side by side.
    - `client` is the package release this server was built from. A client
      comparing it against its own tells its human there is an upgrade —
      nothing is enforced, because refusing service over a version number
      would strand people mid-conversation.
    - `skill` is where the current instructions live. An agent read them once
      and its understanding froze there; this is the only pointer back.
    """
    try:
        from importlib.metadata import version as package_version

        release = package_version("doorslip")
    except Exception:
        release = "unknown"

    base = str(request.base_url).rstrip("/")
    return {
        "protocol": ENVELOPE_VERSION,
        "client": release,
        "skill": f"{base}/skill.md",
        "auth": list(ACCEPTED_AUTH_SCHEMES),
        "nonce_only_removal": LEGACY_AUTH_REMOVAL_RELEASE,
    }


def _ensure_welcome_agent(
    store: Store, handle: str, key_path: str | Path | None = None
) -> WelcomeAgent:
    """Create or reload the welcome identity (spec §8).

    Its key must survive a restart. Generating a fresh one leaves the desk
    holding a key that is not in the `agent` table, so every notice it tries
    to send — the greeting, an enrolment warning, an accepted invitation —
    fails the lookup and is dropped without a word. That is invisible in tests
    because tests always start from an empty database, and only shows up on a
    real server the second time it is restarted.

    Without a path the key is ephemeral, which is correct for an in-memory
    database: both vanish together.
    """
    stored_key = Path(key_path) if key_path else None
    existing = store.find_human(handle)

    if stored_key is not None and stored_key.exists():
        saved = json.loads(stored_key.read_text(encoding="utf-8"))
        keypair = KeyPair(private_key=saved["private_key"], public_key=saved["public_key"])
    else:
        keypair = generate_keypair()
        if stored_key is not None:
            stored_key.parent.mkdir(parents=True, exist_ok=True)
            stored_key.write_text(
                json.dumps(
                    {"private_key": keypair.private_key, "public_key": keypair.public_key}
                ),
                encoding="utf-8",
            )
            os.chmod(stored_key, stat.S_IRUSR | stat.S_IWUSR)

    if existing is not None:
        # The identity outlived its key file, or the file was replaced. Attach
        # the current key so the desk can sign again instead of failing mutely.
        if store.find_agent(keypair.public_key) is None:
            store.redeem_or_attach_welcome_key(existing.id, keypair.public_key)
        return WelcomeAgent(handle=handle, human_id=existing.id, keypair=keypair)

    human = store.register_identity(
        handle=handle, pubkey=keypair.public_key, label=WELCOME_LABEL, is_welcome=True
    )
    return WelcomeAgent(handle=handle, human_id=human.id, keypair=keypair)


def _welcome_reply(
    store: Store, welcome: WelcomeAgent, envelope: dict[str, Any], sender_human_id: str
) -> None:
    """Answer a newcomer and put them in each other's address books.

    The desk acknowledges what it answers. It has no agent deliberating, but
    it did incorporate the message — it replied to it — and leaving the
    acknowledgement off would show every newcomer their own greeting sitting
    unanswered forever in `doorslip sent`.
    """
    store.ack_message(envelope["message_id"], welcome.human_id)
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


def _notify_invitation_accepted(
    store: Store, welcome: WelcomeAgent, inviter: Human, acceptor: Human
) -> None:
    """Deliver the acceptance notice to whoever issued the code."""
    raw, signature = welcome.notify_invitation_accepted(inviter.handle, acceptor.handle)
    agent_id = store.agent_id_for(welcome.keypair.public_key)
    if agent_id is None:
        return
    store.store_message(
        envelope=parse(raw),
        raw=raw,
        signature=signature,
        from_human_id=welcome.human_id,
        from_agent_id=agent_id,
        to_human_id=inviter.id,
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
