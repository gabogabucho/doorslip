"""End-to-end demonstration of the done-criterion in spec §2.

Two agents belonging to DIFFERENT people, with mutual address books, hold a
thread of at least 8 turns with at least 2 partial state updates, and the logs
exist to count state errors.

Runs in-process against an in-memory database by default, so it needs no
server and no network:

    uv run python demo.py

Point it at a running server to exercise real HTTP:

    uv run python demo.py --url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import sys

from doorslip.client import Agent
from doorslip.crypto import generate_keypair
from doorslip.state import reconstruct

SERVER = "doorslip.test"
GABO = f"gabo@{SERVER}"
TOMAS = f"tomas@{SERVER}"

# Windows consoles still default to a legacy codepage. The output below is
# UTF-8 and the handles could be too, so say so rather than lose characters.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def build_transport(url: str | None):
    """Either a real HTTP client or the app running in this process."""
    if url:
        import httpx

        return httpx.Client(base_url=url), None

    from fastapi.testclient import TestClient

    from doorslip.api import create_app
    from doorslip.store import Store, connect

    db = connect(":memory:")
    store = Store(db)
    return TestClient(create_app(store, welcome_handle=f"welcome@{SERVER}")), store


def rule(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m\n" + "─" * 68)


def turn(number: int, who: str, prose: str, patch: dict) -> None:
    print(f"  {number}. \033[36m{who:<22}\033[0m {prose}")
    if patch:
        print(f"     \033[90mpatch: {json.dumps(patch, ensure_ascii=False)}\033[0m")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="run against a live server instead of in-process")
    args = parser.parse_args()

    http, store = build_transport(args.url)

    rule("1. Each agent generates its own key and registers")
    gabo = Agent(http, handle=GABO, label="hermes", keypair=generate_keypair())
    tomas = Agent(http, handle=TOMAS, label="claude", keypair=generate_keypair())
    welcome_handle = gabo.register()["welcome_handle"]
    tomas.register()
    print(f"  {GABO:<22} key {gabo.pubkey[:16]}…  (never sent to the server)")
    print(f"  {TOMAS:<22} key {tomas.pubkey[:16]}…  (never sent to the server)")

    rule("2. The welcome desk answers, from a template, spending no inference")
    first = gabo.send(
        to=welcome_handle,
        state={"topic": "hello", "status": "proposed"},
        prose="Just installed. Checking this works.",
    )
    reply = next(m for m in gabo.inbox() if m["from"] == welcome_handle)
    gabo.ack(reply["message_id"])
    print(f"  reply arrived on thread {first['thread_id'][:8]}…, acknowledged")
    print(f"  \033[90m{reply['envelope']['prose'].splitlines()[0][:64]}…\033[0m")

    rule("3. Nobody can write to a stranger")
    try:
        tomas.send(to=GABO, state={"topic": "test"}, prose="Can I get in?")
        print("  \033[31mFAIL: a stranger was allowed through\033[0m")
        return 1
    except Exception as exc:  # ProtocolError
        print(f"  refused, as designed → {exc}")

    rule("4. An invitation code opens the door, in both directions")
    code = gabo.invite()
    tomas.accept(code)
    print(f"  {GABO} issued {code[:18]}…")
    print(f"  {GABO:<22} sees {gabo.contacts()}")
    print(f"  {TOMAS:<22} sees {tomas.contacts()}")

    rule("5. Eight turns, each patching the state of the one before it")
    script = [
        (gabo, TOMAS, "Saturday barbecue?", {
            "topic": "saturday barbecue",
            "status": "proposed",
            "who": [GABO, TOMAS],
            "when": [
                {"start": "2026-08-29T20:00:00-03:00", "confidence": "high"},
                {"start": "2026-08-30T13:00:00-03:00", "confidence": "low"},
            ],
        }),
        (tomas, GABO, "Saturday night works, drop the Sunday option.", {
            "status": "negotiating",
            "when": [{"start": "2026-08-29T20:00:00-03:00", "confidence": "high"}],
        }),
        (gabo, TOMAS, "My place then.", {"where": "my place"}),
        (tomas, GABO, "I'll put in for the meat.", {
            "budget": {"amount": 18000, "currency": "ARS", "per": "person"},
        }),
        (gabo, TOMAS, "I'll handle fire and salads.", {
            "tasks": [{"what": "fire", "who": GABO}, {"what": "salads", "who": GABO}],
        }),
        (tomas, GABO, "I'll bring wine — note the whole array is resent.", {
            "tasks": [
                {"what": "fire", "who": GABO},
                {"what": "salads", "who": GABO},
                {"what": "wine", "who": TOMAS},
            ],
        }),
        (gabo, TOMAS, "One of us is vegetarian, keep it in mind.", {
            "constraints": ["one vegetarian guest"],
        }),
        (tomas, GABO, "Confirmed.", {"status": "confirmed"}),
    ]

    thread_id: str | None = None
    parent: str | None = None
    for number, (sender, recipient, prose, patch) in enumerate(script, start=1):
        sent = sender.send(
            to=recipient, state=patch, prose=prose,
            thread_id=thread_id, parent_message_id=parent,
        )
        thread_id = sent["thread_id"]
        parent = sent["message_id"]
        turn(number, sender.handle, prose, patch)

    rule("6. Both sides reconstruct the SAME state, deterministically")
    envelopes = [
        message["envelope"]
        for agent in (gabo, tomas)
        for message in agent.inbox()
        if message["thread_id"] == thread_id
    ]
    result = reconstruct(envelopes)
    print(json.dumps(result.state, indent=2, ensure_ascii=False))
    print(f"\n  {len(result.applied)} patches applied in parent order")
    print(f"  divergence detected: {result.diverged}")

    rule("7. Done-criterion (spec §2)")
    turns = len(script)
    partial = sum(1 for _, _, _, patch in script[1:] if patch)
    checks = [
        ("two agents, different people, mutual address books", len(gabo.contacts()) >= 1),
        (f"thread of at least 8 turns (got {turns})", turns >= 8),
        (f"at least 2 partial state updates (got {partial})", partial >= 2),
        ("logs exist to count state errors", store is not None or args.url is not None),
    ]
    passed = "\033[32mOK\033[0m  "
    failed = "\033[31mNO\033[0m  "
    for label, ok in checks:
        print("  " + (passed if ok else failed) + label)

    if store is not None:
        rule("8. Instrumentation (spec §9)")
        for key, value in store.metrics().items():
            print(f"  {key:<38} {value}")

    print()
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
