"""Thread state reconstruction (spec §6.1).

This lives on the agent side, not the server's. The server transports and
verifies; it never interprets what a message means.

Each message carries a JSON Merge Patch (RFC 7386) that applies to the state
resulting from the message its `parent_message_id` names. Not "the previous
message by timestamp": the timestamp is written by the sender, and with two
agents working asynchronously there is no defined order, so two replays of the
same thread would produce different states — and the done-criterion of spec §2
depends on reconstructing it identically every time.

The parent pointer also makes divergence *detectable*. Two messages naming the
same parent mean both sides wrote at once. The agent finds out instead of one
silently overwriting the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ThreadBroken(Exception):
    """The thread cannot be reconstructed: a parent is missing or cyclic."""


@dataclass
class Divergence:
    """Two or more messages claiming the same parent."""

    parent_id: str | None
    message_ids: list[str]


@dataclass
class Reconstruction:
    state: dict[str, Any]
    applied: list[str] = field(default_factory=list)
    divergences: list[Divergence] = field(default_factory=list)

    @property
    def diverged(self) -> bool:
        return bool(self.divergences)


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386, with the two behaviours that surprise people made explicit.

    Arrays are replaced wholesale, never merged: to change one task you send
    the whole `tasks` array. And `null` DELETES a key — an agent meaning "I do
    not know the budget yet" must send a value, not null, or the field
    disappears from the thread.

    Both rules are written down because two implementations left to guess will
    guess differently, and the divergence would only surface once they talk.
    """
    if not isinstance(patch, dict):
        return patch
    if not isinstance(target, dict):
        target = {}

    result = dict(target)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def reconstruct(messages: list[dict[str, Any]]) -> Reconstruction:
    """Fold a thread's envelopes into one state, following parent pointers.

    `messages` are envelopes in any order — the parent chain defines the
    sequence, not the order they arrived in.
    """
    if not messages:
        return Reconstruction(state={})

    by_id = {message["message_id"]: message for message in messages}
    children: dict[str | None, list[str]] = {}
    for message in messages:
        children.setdefault(message.get("parent_message_id"), []).append(
            message["message_id"]
        )

    roots = children.get(None, [])
    if len(roots) != 1:
        raise ThreadBroken(f"a thread needs exactly one root, found {len(roots)}")

    orphans = [
        message["message_id"]
        for message in messages
        if message.get("parent_message_id")
        and message["parent_message_id"] not in by_id
    ]
    if orphans:
        raise ThreadBroken(f"parent missing for: {', '.join(orphans)}")

    # Every message must be reachable from the root. Anything else is either a
    # detached subgraph or a cycle — and a cycle is NEVER reachable from the
    # root, because every message inside one already has a parent. Walking
    # forward and watching for repeats would never see it; only reachability
    # does.
    reachable: set[str] = set()
    frontier = [roots[0]]
    while frontier:
        current_id = frontier.pop()
        if current_id in reachable:
            continue
        reachable.add(current_id)
        frontier.extend(children.get(current_id, []))

    unreachable = sorted(set(by_id) - reachable)
    if unreachable:
        raise ThreadBroken(f"unreachable from the root (detached or cyclic): {', '.join(unreachable)}")

    result = Reconstruction(state={})
    for parent_id, siblings in children.items():
        if len(siblings) > 1:
            result.divergences.append(
                Divergence(parent_id=parent_id, message_ids=sorted(siblings))
            )

    # Walk the chain. On divergence the first child by message_id wins, purely
    # so the walk terminates — the caller is told it happened and decides what
    # to do. Resolving divergence is out of scope for v0 by design.
    current: str | None = roots[0]
    seen: set[str] = set()
    while current is not None:
        if current in seen:
            raise ThreadBroken(f"cycle at {current}")
        seen.add(current)

        result.state = merge_patch(result.state, by_id[current].get("state") or {})
        result.applied.append(current)

        next_ids = sorted(children.get(current, []))
        current = next_ids[0] if next_ids else None

    return result
