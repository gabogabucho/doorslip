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
    resolved_by: str | None = None

    @property
    def resolved(self) -> bool:
        return self.resolved_by is not None


@dataclass
class Reconstruction:
    state: dict[str, Any]
    applied: list[str] = field(default_factory=list)
    divergences: list[Divergence] = field(default_factory=list)

    @property
    def diverged(self) -> bool:
        """Whether anything is still unsettled.

        A divergence a human already settled must stop being reported, or the
        warning outlives the disagreement and people learn to scroll past it.
        The record stays in `divergences`; only the alarm goes quiet.
        """
        return any(not d.resolved for d in self.divergences)

    @property
    def open_divergences(self) -> list[Divergence]:
        return [d for d in self.divergences if not d.resolved]


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
    # A later message may name the ones it supersedes. Somebody's human looked
    # at both versions and decided; the structure should say so rather than
    # leaving the prose to carry it alone.
    resolutions: dict[str, str] = {}
    for message in messages:
        for superseded in message.get("resolves") or []:
            resolutions[superseded] = message["message_id"]

    for parent_id, siblings in children.items():
        if len(siblings) > 1:
            settled = next(
                (resolutions[m] for m in sorted(siblings) if m in resolutions), None
            )
            result.divergences.append(
                Divergence(
                    parent_id=parent_id,
                    message_ids=sorted(siblings),
                    resolved_by=settled,
                )
            )

    # Walk the chain. At a divergence, follow the branch the conversation
    # actually continued along — the one with the most messages hanging off it.
    #
    # The first version picked the lowest message_id, chosen arbitrarily so the
    # walk would terminate. A real thread showed what that costs: two agents
    # wrote at once, one message went unanswered while the other carried four
    # more turns and the agreed conclusion, and reconstruction reported the
    # dead stub as the state and the actual ending as a discarded branch. The
    # final status read `autonomous` on a thread that had closed as `done`.
    #
    # Depth is not a resolution of the disagreement and does not pretend to be
    # one — the divergence is still reported. It is the observation that a
    # branch nobody replied to is not where the conversation went.
    depth = _depth_of(children, by_id)

    current: str | None = roots[0]
    seen: set[str] = set()
    while current is not None:
        if current in seen:
            raise ThreadBroken(f"cycle at {current}")
        seen.add(current)

        result.state = merge_patch(result.state, by_id[current].get("state") or {})
        result.applied.append(current)

        next_ids = children.get(current, [])
        # Deepest wins; message_id breaks a tie so the answer never depends on
        # the order messages happened to arrive in.
        current = max(sorted(next_ids), key=lambda mid: depth[mid], default=None)

    return result


def _depth_of(
    children: dict[str | None, list[str]], by_id: dict[str, Any]
) -> dict[str, int]:
    """How many messages follow each one, at its deepest.

    Computed bottom-up rather than by recursing, so a long thread cannot
    exhaust the stack on somebody's machine.
    """
    depth: dict[str, int] = {}
    order: list[str] = []
    stack = list(children.get(None, []))
    while stack:
        node = stack.pop()
        order.append(node)
        stack.extend(children.get(node, []))

    for node in reversed(order):
        kids = children.get(node, [])
        depth[node] = 1 + max((depth.get(k, 0) for k in kids), default=0)
    for node in by_id:
        depth.setdefault(node, 1)
    return depth
