"""Agent-side client (spec §13).

This is the layer an MCP server or a CLI wraps. It exists as a library first,
on purpose: the protocol must be usable by anything that speaks HTTP, and any
convenience built on top has to be a thin shell over something that already
works without it.

**The private key never leaves this object.** It is loaded from a local file
and used only to sign, at the moment of signing. It is never returned, never
logged, never placed anywhere an agent's context could pick it up — some
harnesses synchronise agent memory to the cloud, and a key that reaches memory
has effectively been published.

The handle and the address book are different: those are what an agent needs
in front of it to work, and they are safe to keep in context.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Protocol

from doorslip.auth import AUTH_HEADER, AUTH_V1, build_credential
from doorslip.crypto import KeyPair, generate_keypair
from doorslip.envelope import build, seal
from doorslip.state import Reconstruction, ThreadBroken, reconstruct

# Which thread belongs to which subscriber, for a list that chains. Beside the
# outbox rather than inside one agent's directory, for the same reason the
# outbox is: every agent of this person acts for the same mailbox, and a list
# one of them started must be continuable by another.
LISTS_NAME = "lists.json"


class HttpClient(Protocol):
    """Whatever speaks httpx's shape: a real client or a TestClient."""

    def get(self, url: str, **kwargs: Any) -> Any: ...
    def post(self, url: str, **kwargs: Any) -> Any: ...
    def build_request(self, method: str, url: str, **kwargs: Any) -> Any: ...
    def send(self, request: Any) -> Any: ...


class ProtocolError(Exception):
    """The server refused. Carries the status so callers can branch on it."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


def load_or_create_keypair(path: str | Path) -> KeyPair:
    """Read the key from disk, generating it on first run.

    Written with owner-only permissions. On Windows `chmod` cannot express
    that fully, so this is a best effort there — the real protection on that
    platform is the user profile directory, not the file mode.
    """
    key_path = Path(path)
    if key_path.exists():
        stored = json.loads(key_path.read_text(encoding="utf-8"))
        return KeyPair(private_key=stored["private_key"], public_key=stored["public_key"])

    keypair = generate_keypair()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(
        json.dumps({"private_key": keypair.private_key, "public_key": keypair.public_key}),
        encoding="utf-8",
    )
    os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
    return keypair


class Agent:
    """One agent key operating one human's mailbox."""

    def __init__(
        self,
        http: HttpClient,
        *,
        handle: str,
        label: str,
        keypair: KeyPair,
        outbox_path: str | Path | None = None,
    ):
        self._http = http
        self.handle = handle
        self.label = label
        self._keypair = keypair
        # An agent must remember what it said. The server files each message
        # into exactly one inbox — the recipient's — so an agent reading only
        # its inbox sees half a conversation and cannot reconstruct a thread it
        # started. Keeping our own outbox is also the honest split: the server
        # transports, the agent interprets.
        self._outbox_path = Path(outbox_path) if outbox_path else None
        self._sent: list[dict[str, Any]] = []
        self.server_info: dict[str, Any] = {}

    @property
    def pubkey(self) -> str:
        return self._keypair.public_key

    # -- plumbing ---------------------------------------------------------

    def _nonce(self) -> str:
        payload = _unwrap(self._http.get("/nonce", params={"pubkey": self.pubkey}))
        # Every authenticated command passes through here, so this is where a
        # client is guaranteed to hear what the server speaks. Stored, never
        # acted on: an upgrade is the human's decision, not ours.
        self.server_info = payload.get("server") or {}
        return payload["nonce"]

    def update_notice(self) -> dict[str, Any] | None:
        """Whether this client is behind what the server was built from.

        Advisory only. Refusing to work over a version number would strand
        people in the middle of a conversation for something that is usually
        cosmetic; a message their agent can pass on is enough.
        """
        offered = (self.server_info or {}).get("client")
        if not offered or offered == "unknown":
            return None
        try:
            from importlib.metadata import version as package_version

            installed = package_version("doorslip")
        except Exception:
            return None
        if _release(offered) <= _release(installed):
            return None
        return {
            "installed": installed,
            "available": offered,
            "skill": (self.server_info or {}).get("skill"),
        }

    def _authenticated_request(
        self, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Prepare once, sign the prepared components, and send that request.

        In particular, the target does not come from ``path``: it comes back
        from the HTTP library after it has encoded the URL and parameters.  The
        bytes signed here are therefore the bytes the transport will send.
        """
        nonce = self._nonce()
        schemes = (self.server_info or {}).get("auth")
        if not isinstance(schemes, list) or AUTH_V1 not in schemes:
            raise ProtocolError(
                426,
                f"server does not advertise required authentication scheme {AUTH_V1}",
            )

        request = self._http.build_request(method, path, **kwargs)
        raw_target = request.url.raw_path
        query_string = request.url.query
        if query_string:
            suffix = b"?" + query_string
            if not raw_target.endswith(suffix):
                raise ProtocolError(400, "HTTP client exposed inconsistent target components")
            raw_path = raw_target[: -len(suffix)]
        else:
            raw_path = raw_target

        try:
            body = request.content
        except Exception as exc:
            raise ProtocolError(400, "authenticated request body is not buffered") from exc
        if not isinstance(body, bytes):
            raise ProtocolError(400, "authenticated request body must be bytes")

        request.headers[AUTH_HEADER] = build_credential(
            self.pubkey,
            nonce,
            self._keypair.private_key,
            method=request.method,
            raw_path=raw_path,
            query_string=query_string,
            body=body,
        )
        # One send, with no legacy retry after any response.  A fallback here
        # would turn a rejection into a downgrade oracle.
        return _unwrap(self._http.send(request))

    def _signed_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Serialize ONCE, sign those bytes, send those bytes.

        Never `json=`: letting the HTTP library serialize means signing one
        byte string and sending another.
        """
        raw = json.dumps(payload).encode("utf-8")
        from doorslip.crypto import sign

        return _unwrap(
            self._http.post(
                path,
                content=raw,
                headers={"X-Doorslip-Signature": sign(raw, self._keypair.private_key)},
            )
        )

    # -- identity ---------------------------------------------------------

    def register(self, enroll_code: str | None = None) -> dict[str, Any]:
        """Register this key.

        Without a code this claims a new handle. With one it attaches this key
        to a mailbox that already exists, and the handle comes from the code —
        which is why it is not sent.
        """
        payload: dict[str, Any] = {
            "pubkey": self.pubkey,
            "label": self.label,
            "nonce": self._nonce(),
        }
        if enroll_code:
            payload["enroll_code"] = enroll_code
        else:
            payload["handle"] = self.handle
        return self._signed_post("/register", payload)

    def enroll_code(self, scope: str = "full") -> str:
        """Mint a code so another agent can join THIS mailbox (spec §7.3).

        Twenty minutes, single use. A `full` code grants everything this
        identity has — the inbox, the address book, and the ability to sign as
        this person — so hand it over directly and never through a channel you
        do not control.

        `speak` grants reading, sending and broadcasting and nothing that
        changes who may reach this mailbox. It is the one to mint for an agent
        that publishes on your behalf: it cannot drop a subscriber, admit a
        stranger, or revoke your own key.

        The scope is fixed here, when you decide to add the agent. The joining
        agent never asks for one.
        """
        return self._authenticated_request(
            "POST", "/enroll-code", json={"scope": scope}
        )["code"]

    def revoke(self, pubkey: str) -> dict[str, Any]:
        """Stop a key of this identity from sending anything further.

        Server side, because the server is the only party that can refuse a
        signature. Deleting a key file removes your own ability to use it and
        nothing else: anybody holding a copy keeps signing as this identity
        until the directory is told to stop accepting it.

        Not retroactive. Messages already delivered stay valid, their
        signatures having been checked when they arrived (spec §7.6).
        """
        return self._authenticated_request(
            "POST", "/revoke-key", json={"pubkey": pubkey}
        )

    def agents(self) -> list[dict[str, Any]]:
        """The keys registered to this identity, so one can be named to revoke."""
        payload = self._authenticated_request("GET", "/contacts")
        return payload.get("agents", [])

    # -- address book -----------------------------------------------------

    def invite(self) -> str:
        return self._authenticated_request("POST", "/invite")["code"]

    def accept(self, code: str) -> str:
        return self._authenticated_request(
            "POST", "/accept", json={"code": code}
        )["contact"]

    def open_inbox(self, open_to_strangers: bool) -> dict[str, Any]:
        """Let anyone write to this mailbox, or stop letting them.

        For a list people subscribe to, not for a personal mailbox. Opening a
        personal one hands your inbox to whoever finds the handle, and there
        is no cost attached to writing to a stranger yet (spec §11 ter) to
        make that survivable.
        """
        return self._authenticated_request(
            "POST", "/contacts", json={"open": open_to_strangers}
        )

    def remove_contact(self, handle: str) -> dict[str, Any]:
        """Stop somebody writing to you. Your side only.

        They keep you in their book: you can decide who reaches you, not who
        remembers you.
        """
        return self._authenticated_request(
            "POST", "/contacts", json={"remove": handle}
        )

    def broadcast(
        self, *, state: dict[str, Any], prose: str, chain: str | None = None
    ) -> dict[str, Any]:
        """Send the same slip to everybody in this address book.

        One thread per subscriber rather than one shared thread, either way: a
        reply belongs to the person who wrote it, and a group thread is not
        something this protocol does (spec §11).

        What `chain` changes is whether those threads are reused. Without it
        every broadcast opens a new one, which is right for announcements that
        each stand alone. With it the subscriber keeps a single thread for the
        whole list and every slip patches the one before, so what they hold is
        one reconstructable answer to "where is this project" instead of ten
        loose notices.

        That is the shape a project wants and a feed of announcements does
        not, which is why it is the list owner's decision and not a default.
        It only pays off if the slips carry deltas — `{"latest": "0.21.0"}`
        rather than the whole object every time. Resending the full state
        chains threads together and buys nothing.
        """
        threads = self._list_threads(chain) if chain else {}
        tips = self._tips(set(threads.values()))

        sent: list[str] = []
        failed: dict[str, str] = {}
        opened: list[str] = []

        for handle in self.contacts():
            thread_id = threads.get(handle)
            parent = tips.get(thread_id) if thread_id else None
            if thread_id is not None and parent is None:
                # A thread we recorded but cannot find the end of. Sending
                # into it without a parent would add a second root and make
                # the thread unreconstructable for both sides; a fresh thread
                # loses the accumulated state but stays readable.
                thread_id = None

            try:
                receipt = self.send(
                    to=handle,
                    state=dict(state),
                    prose=prose,
                    thread_id=thread_id,
                    parent_message_id=parent,
                )
            except ProtocolError as exc:
                # One unreachable subscriber must not stop the rest.
                failed[handle] = exc.detail
                continue

            sent.append(handle)
            if chain and thread_id is None:
                threads[handle] = receipt["thread_id"]
                opened.append(handle)

        if chain and opened:
            self._save_list_threads(chain, threads)

        report: dict[str, Any] = {"sent": sent, "failed": failed}
        if chain:
            report["list"] = chain
            report["opened"] = opened
            report["continued"] = [h for h in sent if h not in opened]
        return report

    def _tips(self, thread_ids: set[str]) -> dict[str, str]:
        """Where each thread currently ends.

        The end of the reconstructed chain, not the last thing we sent. Those
        differ the moment a subscriber answers, and chaining onto our own
        message instead would place the next slip beside their reply — two
        messages naming one parent, which is a divergence (spec §6.1). It
        would be reported for good, on every thread anybody ever replied to,
        and the list would look broken to the people using it most.

        Resolved for the whole batch in one pass. Asking `thread_state` per
        subscriber would re-download the same inbox once per person.
        """
        if not thread_ids:
            return {}

        by_thread: dict[str, dict[str, dict[str, Any]]] = {}
        for message in self.inbox():
            if message["thread_id"] in thread_ids:
                envelope = message["envelope"]
                by_thread.setdefault(message["thread_id"], {})[
                    envelope["message_id"]
                ] = envelope
        for envelope in self.sent():
            if envelope["thread_id"] in thread_ids:
                by_thread.setdefault(envelope["thread_id"], {})[
                    envelope["message_id"]
                ] = envelope

        tips: dict[str, str] = {}
        for thread_id, envelopes in by_thread.items():
            try:
                applied = reconstruct(list(envelopes.values())).applied
            except ThreadBroken:
                # This machine is missing part of the thread. Falling back to
                # the newest message we do know puts the slip somewhere
                # readable, and if it lands beside something we never saw the
                # result is a reported divergence rather than silence.
                #
                # Timestamps are the sender's and are not an ordering anywhere
                # else in this protocol. They are used here because the chain
                # that would have answered the question is the broken thing.
                newest = max(envelopes.values(), key=lambda e: e.get("timestamp") or "")
                applied = [newest["message_id"]]
            if applied:
                tips[thread_id] = applied[-1]
        return tips

    # -- list bookkeeping -------------------------------------------------

    def _lists_path(self) -> Path | None:
        if self._outbox_path is None:
            return None
        return self._outbox_path.parent / LISTS_NAME

    def _read_lists(self) -> dict[str, dict[str, str]]:
        path = self._lists_path()
        if path is None or not path.exists():
            return {}
        try:
            book = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # An unreadable book costs the threading, never the broadcast:
            # every subscriber gets a fresh thread and the list keeps working.
            return {}
        return book if isinstance(book, dict) else {}

    def _list_threads(self, name: str | None) -> dict[str, str]:
        if name is None:
            return {}
        threads = self._read_lists().get(name)
        return dict(threads) if isinstance(threads, dict) else {}

    def _save_list_threads(self, name: str, threads: dict[str, str]) -> None:
        path = self._lists_path()
        if path is None:
            return
        book = self._read_lists()
        book[name] = threads
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(book, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def contacts(self) -> list[str]:
        payload = self._authenticated_request("GET", "/contacts")
        return [contact["handle"] for contact in payload["contacts"]]

    # -- mailbox ----------------------------------------------------------

    def send(
        self,
        *,
        to: str,
        state: dict[str, Any],
        prose: str,
        thread_id: str | None = None,
        parent_message_id: str | None = None,
        resolves: list[str] | None = None,
    ) -> dict[str, str]:
        """Build, sign and deposit one message.

        Returns the ids so a caller can chain the next reply onto this one —
        which is what keeps the thread reconstructable (spec §6.1).
        """
        raw = build(
            sender_handle=self.handle,
            sender_agent=self.label,
            sender_pubkey=self.pubkey,
            to=to,
            state=state,
            prose=prose,
            thread_id=thread_id,
            parent_message_id=parent_message_id,
            resolves=resolves,
        )
        sealed = seal(raw, self._keypair.private_key)
        _unwrap(
            self._http.post(
                "/inbox",
                content=sealed.raw,
                headers={"X-Doorslip-Signature": sealed.signature},
            )
        )
        envelope = json.loads(raw)
        self._remember(envelope)
        return {"message_id": envelope["message_id"], "thread_id": envelope["thread_id"]}

    def _remember(self, envelope: dict[str, Any]) -> None:
        """File a sent envelope into our own outbox."""
        self._sent.append(envelope)
        if self._outbox_path is not None:
            self._outbox_path.parent.mkdir(parents=True, exist_ok=True)
            with self._outbox_path.open("a", encoding="utf-8") as outbox:
                outbox.write(json.dumps(envelope) + "\n")

    def sent(self) -> list[dict[str, Any]]:
        """Envelopes sent from this machine, from memory and from disk.

        Reads any outbox left in an agent's own directory as well as the
        shared one. Earlier versions filed each agent's sent messages
        separately, and a thread started before an upgrade would otherwise
        come back unreadable.
        """
        stored: list[dict[str, Any]] = []
        for path in self._outbox_files():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        stored.append(json.loads(line))
                    except json.JSONDecodeError:
                        # One unreadable line must not cost the whole history.
                        continue

        merged = {envelope["message_id"]: envelope for envelope in stored}
        for envelope in self._sent:
            merged.setdefault(envelope["message_id"], envelope)
        return list(merged.values())

    def _outbox_files(self) -> list[Path]:
        if self._outbox_path is None:
            return []
        candidates = [self._outbox_path]
        # Where earlier versions put it: inside each agent's own directory.
        legacy_root = self._outbox_path.parent
        if legacy_root.is_dir():
            candidates += sorted(
                d / self._outbox_path.name
                for d in legacy_root.iterdir()
                if d.is_dir()
            )
        return [p for p in candidates if p.exists()]

    def inbox(self, *, unacked_only: bool = False) -> list[dict[str, Any]]:
        payload = self._authenticated_request(
            "GET", "/inbox", params={"unacked_only": unacked_only}
        )
        return payload["messages"]

    def delivery(self) -> list[dict[str, Any]]:
        """What became of the messages this agent sent.

        `acked` says the recipient's AGENT incorporated the message, never
        that their human read it. That is what separates "waiting for an
        answer" from "nobody is listening over there", and those two call for
        opposite behaviour.
        """
        payload = self._authenticated_request("GET", "/inbox", params={"sent": True})
        return payload["sent"]

    def unanswered(self, older_than_minutes: int = 0) -> list[dict[str, Any]]:
        """Messages sent that nobody has acknowledged yet.

        With `older_than_minutes` this is the question an unattended agent
        actually needs answered: has this been sitting unread long enough that
        I should stop waiting and tell my human?
        """
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
        pending = []
        for message in self.delivery():
            if message.get("acked"):
                continue
            try:
                sent_at = datetime.fromisoformat(message["sent_at"])
            except (KeyError, ValueError):
                pending.append(message)
                continue
            if sent_at <= cutoff:
                pending.append(message)
        return pending

    def ack(self, message_id: str) -> None:
        self._authenticated_request("POST", "/ack", json={"message_id": message_id})

    def thread_messages(self, thread_id: str) -> list[dict[str, Any]]:
        """The whole conversation, in the order the parent chain defines.

        Both halves again: what arrived and what this agent wrote. Reading a
        thread from the inbox alone shows one side talking to itself.

        Messages on a branch that reconstruction did not follow are marked
        rather than hidden. After an exchange nobody supervised, the discarded
        branch is often the most interesting thing on the page — it is where
        the two sides were about to disagree.
        """
        received = {
            m["message_id"]: {
                "message_id": m["message_id"],
                "from": m["from"],
                "envelope": m["envelope"],
                "direction": "in",
                "acked": m.get("acked"),
            }
            for m in self.inbox()
            if m["thread_id"] == thread_id
        }
        for envelope in self.sent():
            if envelope.get("thread_id") != thread_id:
                continue
            received.setdefault(
                envelope["message_id"],
                {
                    "message_id": envelope["message_id"],
                    "from": envelope["from"]["handle"],
                    "envelope": envelope,
                    "direction": "out",
                    "acked": None,
                },
            )

        if not received:
            return []

        result = reconstruct([m["envelope"] for m in received.values()])
        applied = list(result.applied)
        ordered = [received[mid] for mid in applied if mid in received]
        for message in ordered:
            message["on_main_branch"] = True

        leftover = [m for mid, m in received.items() if mid not in set(applied)]
        for message in leftover:
            message["on_main_branch"] = False

        for message in ordered + leftover:
            envelope = message["envelope"]
            message["prose"] = envelope.get("prose")
            message["state"] = envelope.get("state")
            message["timestamp"] = envelope.get("timestamp")
            message["parent_message_id"] = envelope.get("parent_message_id")
            del message["envelope"]

        return ordered + leftover

    def thread_state(self, thread_id: str) -> Reconstruction:
        """Fold a whole thread into one state — what we received AND what we sent.

        Both halves are required. The server files each message into the
        recipient's inbox only, so an agent that consulted its inbox alone
        would be missing every message it wrote itself — including the root of
        any thread it started, which makes reconstruction impossible.
        """
        envelopes = [
            message["envelope"]
            for message in self.inbox()
            if message["thread_id"] == thread_id
        ]
        envelopes += [
            envelope for envelope in self.sent() if envelope["thread_id"] == thread_id
        ]
        unique = {envelope["message_id"]: envelope for envelope in envelopes}
        return reconstruct(list(unique.values()))


def _release(text: str) -> tuple[int, ...]:
    """Compare releases without pulling in a parser.

    Anything unparseable sorts lowest, so a malformed version on either
    side produces no advice rather than a wrong one.
    """
    parts: list[int] = []
    for chunk in text.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _unwrap(response: Any) -> dict[str, Any]:
    if response.status_code >= 400:
        detail = response.json().get("detail", response.text)
        raise ProtocolError(response.status_code, detail)
    return response.json()
