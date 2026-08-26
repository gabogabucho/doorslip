"""Three people's agents deciding one thing, with a coordinator.

Somebody asked whether this is like giving agents a phone. It is not, and the
reason is worth more than the answer: a phone is a channel and this is a shared
state. What a channel leaves behind is a transcript somebody has to read. What
this leaves behind is one object you can ask a question.

The pattern below needs no group thread and no new verb — spec §11 refuses
group threads, because a reply belongs to whoever wrote it. Instead:

- one mailbox opens its inbox and becomes the coordinator;
- whoever writes to it is subscribed, and holds one thread with it;
- each specialist patches the shared state from their own side;
- when two of them contradict each other the protocol SAYS SO, rather than
  letting the later one quietly win;
- the coordinator settles it with `resolves`, and republishes.

The last two are the point. A chat room cannot tell you that two participants
hold incompatible positions. Reconstruction can, because every message names
its parent and two messages naming one parent is a fact rather than an opinion.

    uv run python demo-coordination.py
    uv run python demo-coordination.py --url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from doorslip.client import Agent
from doorslip.crypto import generate_keypair
from doorslip.state import reconstruct

SERVER = "doorslip.test"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def build_transport(url: str | None):
    if url:
        import httpx

        return httpx.Client(base_url=url, timeout=30.0)

    from fastapi.testclient import TestClient

    from doorslip.api import create_app
    from doorslip.store import Store, connect

    return TestClient(create_app(Store(connect(":memory:")),
                                 welcome_handle=f"welcome@{SERVER}"))


def rule(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m\n" + "─" * 72)


def said(who: str, prose: str, patch: dict | None = None) -> None:
    print(f"  \033[36m{who:<26}\033[0m {prose}")
    if patch:
        print(f"  {'':26} \033[90m{json.dumps(patch, ensure_ascii=False)}\033[0m")


# Every agent gets an outbox. The server files a message into the recipient's
# inbox only, so an agent that keeps none holds half of every thread it
# started — and `chain` has nowhere to record which thread belongs to whom.
ROOT = Path(tempfile.mkdtemp(prefix="doorslip-demo-"))


def person(http, name: str, label: str) -> Agent:
    agent = Agent(http, handle=f"{name}@{SERVER}", label=label,
                  keypair=generate_keypair(),
                  outbox_path=ROOT / name / "outbox.jsonl")
    agent.register()
    return agent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None)
    http = build_transport(parser.parse_args().url)

    rule("1. Four people, four agents, four keys")
    # A coordinator is not a role the protocol knows about. It is whoever
    # opened their mailbox and wrote first.
    coord = person(http, "arquitectura", "coordinator")
    backend = person(http, "backend", "claude")
    security = person(http, "seguridad", "codex")
    product = person(http, "producto", "hermes")
    for who in (coord, backend, security, product):
        print(f"  {who.handle:<26} key {who.pubkey[:14]}…  never leaves that machine")

    rule("2. The coordinator opens the mailbox and sets the order")
    coord.open_inbox(True)
    print("  arquitectura@ is open: writing to it is how you join")
    for who in (backend, security, product):
        who.send(to=coord.handle, state={"topic": "storage", "status": "joined"},
                 prose=f"{who.handle} joining")
    print(f"  subscribed: {', '.join(sorted(coord.contacts()))}")

    agenda = {
        "topic": "where session state lives",
        "status": "open",
        "options": ["postgres", "redis"],
        "decision": None,
    }
    opened = coord.broadcast(
        state=agenda, chain="storage",
        prose="One question: where does session state live? Say your position "
              "and what it costs. I will publish the reconciled view.",
    )
    said("arquitectura@ (coordinator)", "sets the agenda", agenda)
    print(f"  one thread each, opened for: {', '.join(sorted(opened['opened']))}")

    rule("3. Each specialist answers from their own side")
    positions = [
        (backend, {"backend": "redis", "why_backend": "sub-ms reads, we already run it"},
         "Redis. We already operate it and the read path is the hot one."),
        (security, {"security": "postgres", "why_security": "durability and audit trail"},
         "Postgres. Sessions are auditable and Redis loses them on restart."),
        (product, {"product": "no preference", "constraint": "ship before the 12th"},
         "No preference on the store. The date is the constraint."),
    ]
    threads, replied = {}, {}
    for who, patch, prose in positions:
        arrived = [m for m in who.inbox() if m["from"] == coord.handle][-1]
        threads[who.handle] = arrived["thread_id"]
        replied[who.handle] = who.send(
            to=coord.handle, state=patch, prose=prose,
            thread_id=arrived["thread_id"],
            parent_message_id=arrived["message_id"],
        )["message_id"]
        said(who.handle, prose, patch)

    rule("4. The coordinator sees the disagreement — by comparing, not by magic")
    # Everything the coordinator knows about this conversation: what it sent,
    # and what came back. Reconstruction is done on the agent side; the server
    # transported and interpreted nothing.
    replies = {m["from"]: m["envelope"] for m in coord.inbox()
               if m["thread_id"] in threads.values()}
    merged = dict(agenda)
    for envelope in replies.values():
        merged.update(envelope.get("state") or {})

    print(f"  backend says   {merged['backend']}")
    print(f"  security says  {merged['security']}")
    print("  \033[33mtwo positions on one question. Nothing here picks a winner.\033[0m")
    print("  a chat room would show two messages and no fact about them.")

    rule("5. Two sides write at once, and the thread says so")
    # A real race, not a staged one: the coordinator publishes the decision
    # onto the agenda message, and backend — who had not seen it — sends a
    # second thought onto that same agenda message. Two messages, one parent.
    contested = replied[backend.handle]
    late = backend.send(
        to=coord.handle, state={"backend": "redis, with a postgres fallback"},
        prose="Second thought: redis with a postgres fallback.",
        thread_id=threads[backend.handle], parent_message_id=contested,
    )
    racing = coord.send(
        to=backend.handle, state={"status": "deciding"},
        prose="Closing this now.",
        thread_id=threads[backend.handle], parent_message_id=contested,
    )["message_id"]
    split = backend.thread_state(threads[backend.handle])
    print(f"  backend added a second thought and the coordinator answered,")
    print(f"  both onto {contested[:8]}… without seeing each other")
    print(f"  \033[33mdiverged: {split.diverged}\033[0m — reported, not resolved for anybody")
    for d in split.open_divergences:
        print(f"  two children of {d.parent_id[:8]}…: "
              f"{', '.join(m[:8] + '…' for m in d.message_ids)}")
    print("  nobody had to notice this while scrolling. It is a fact about the thread.")

    rule("6. A human settles it, and says so in the message that carries it")
    decided = {
        "status": "decided",
        "decision": "postgres",
        "because": "durability wins over read latency at this size; revisit if "
                   "the read path becomes the bottleneck",
        "decided_by": "a human, after reading both",
        "constraint": merged["constraint"],
    }
    # `resolves` names what this supersedes. Without it the thread reports
    # diverged forever, and a warning that never clears teaches everybody to
    # scroll past it — including past the next real one.
    coord.send(to=backend.handle, state=decided,
               prose="A human read both and chose postgres. Redis stays on the "
                     "table for the read path.",
               thread_id=threads[backend.handle], parent_message_id=racing,
               resolves=[late["message_id"]])
    for who in (security, product):
        # The parent is THEIR reply, not our own last message. Answering our
        # own message would put this beside their reply — two children of one
        # parent — and manufacture the divergence we just finished settling.
        # This is what `broadcast --chain` resolves with `_tips()`, and writing
        # the demo by hand is how it got made here first.
        coord.send(to=who.handle, state=decided,
                   prose="A human read both and chose postgres.",
                   thread_id=threads[who.handle],
                   parent_message_id=replied[who.handle])
    said("arquitectura@ (coordinator)", "publishes the reconciled view", decided)
    settled = backend.thread_state(threads[backend.handle])
    print(f"  backend's thread now reports diverged={settled.diverged} — the record")
    print("  of the disagreement stays; only the alarm goes quiet.")

    rule("7. What each of them holds now")
    for who in (backend, security, product):
        thread = threads[who.handle]
        envelopes = [m["envelope"] for m in who.inbox() if m["thread_id"] == thread]
        envelopes += [e for e in who.sent() if e["thread_id"] == thread]
        result = reconstruct(list({e["message_id"]: e for e in envelopes}.values()))
        state = result.state
        print(f"  \033[36m{who.handle:<26}\033[0m {len(result.applied)} patches, "
              f"diverged={result.diverged}")
        print(f"  {'':26} \033[90mdecision={state.get('decision')!r} "
              f"status={state.get('status')!r}\033[0m")

    rule("What this is, and what it is not")
    print("  Not a room. Three separate threads, each one signed by one person,")
    print("  fanned out by a coordinator who is only a coordinator because they")
    print("  opened a mailbox and wrote first.")
    print()
    print("  What survives is not a transcript. It is one object each of them")
    print("  can ask, six weeks later, without reading anything:")
    print()
    print(f"    doorslip thread {list(threads.values())[0][:8]}…")
    print(f"    \033[90m→ {json.dumps({k: v for k, v in decided.items() if k != 'because'}, ensure_ascii=False)}\033[0m")
    print()
    print("  And the disagreement was a fact the protocol reported, not something")
    print("  somebody had to notice while scrolling.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
