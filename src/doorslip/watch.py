"""Local watcher: notice new slips without asking the server for anything.

A small process on the human's own machine, polling their own mailbox. The
server learns nothing it would not learn anyway, and no endpoint exists for
push — which is deliberate. Notifying somebody requires knowing how to reach
them, and a phone number or an email address is exactly the personal data this
protocol is worth using for not holding.

**It reports metadata, never content.** Who wrote, what the topic is, which
thread. Not the prose, not the state. Two reasons: a background process that
writes message bodies into a log or a desktop toast is a leak nobody asked
for, and the decision to open a slip belongs to the human. The agent announces
that something arrived; the human decides to read it.

`--unacked` is the cursor, and it needs no bookkeeping of its own: a message
stays unacknowledged until an agent has genuinely incorporated it (spec §7.7).
"Anything I have not processed" is a better question than "anything since
timestamp X", and it survives a machine being off for a week.

With `--on-slip` the watcher can wake an agent instead of only telling one.
That is what turns Doorslip into something two agents can use unattended — and
it is why the brakes below live here rather than in the agent's instructions.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import Counter
from typing import Any

INTERVALS = {"15m": 900, "30m": 1800, "60m": 3600}

# A thread carrying one of these has finished negotiating. Announce it, but do
# not wake an agent to answer: there is nothing left to agree.
TERMINAL_STATUS = {"confirmed", "declined", "cancelled", "done"}

# Conversations between humans end because humans get bored. Two agents do not,
# so the ceiling is enforced out here where an enthusiastic model cannot talk
# its way past it.
DEFAULT_MAX_TURNS = 8

# A thread about Saturday still running on Sunday is not coordinating anything
# any more. Hand it back to the human rather than keep paying for it.
DEFAULT_MAX_AGE_HOURS = 48.0


def interval_seconds(setting: str) -> int | None:
    """Translate a stored preference. `manual` means do not run at all."""
    return INTERVALS.get(setting)


def summarise(message: dict[str, Any]) -> dict[str, Any]:
    """Metadata only. Deliberately drops `prose` and the body of `state`."""
    envelope = message.get("envelope", {})
    state = envelope.get("state") or {}
    return {
        "event": "slip",
        "from": message.get("from"),
        "topic": state.get("topic"),
        "status": state.get("status"),
        "message_id": message.get("message_id"),
        "thread_id": message.get("thread_id"),
    }


def should_wake(
    summary: dict[str, Any],
    seen: Counter,
    max_turns: int,
    *,
    we_also_settled: bool = False,
    thread_age_hours: float | None = None,
    max_age_hours: float | None = None,
) -> tuple[bool, str]:
    """Whether to run the hook for this slip, and why not when the answer is no.

    Refusing still leaves the slip announced. Stopping the automation is not
    the same as hiding the message: the human should always find out, even
    when their agent is no longer allowed to answer on its own.
    """
    status = (summary.get("status") or "").lower()
    if status in TERMINAL_STATUS:
        # One side declaring the matter closed is a proposal, not a
        # conclusion. Stopping on it would let either agent end a negotiation
        # unilaterally, and the other's human would never learn their side was
        # never actually agreed to.
        if we_also_settled:
            return False, f"both sides reached {status}"
        return True, ""

    thread = summary.get("thread_id")
    if thread and seen[thread] >= max_turns:
        return False, f"thread hit the {max_turns}-turn ceiling"

    if max_age_hours is not None and thread_age_hours is not None:
        if thread_age_hours > max_age_hours:
            # A thread about Saturday still running on Sunday is not
            # coordinating anything any more.
            return False, f"thread is older than {max_age_hours}h"

    return True, ""


def settled_by_us(sent: list[dict[str, Any]], thread_id: str | None) -> bool:
    """Whether this agent already declared the thread closed."""
    if not thread_id:
        return False
    return any(
        envelope.get("thread_id") == thread_id
        and str((envelope.get("state") or {}).get("status", "")).lower() in TERMINAL_STATUS
        for envelope in sent
    )


def thread_age_hours(envelopes: list[dict[str, Any]], thread_id: str | None) -> float | None:
    """Hours since the oldest message this agent has seen in the thread."""
    from datetime import datetime, timezone

    if not thread_id:
        return None
    stamps = []
    for envelope in envelopes:
        if envelope.get("thread_id") != thread_id:
            continue
        try:
            stamps.append(datetime.fromisoformat(envelope["timestamp"]))
        except (KeyError, ValueError):
            continue
    if not stamps:
        return None
    oldest = min(stamps)
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - oldest).total_seconds() / 3600


def run_hook(command: str, summary: dict[str, Any]) -> dict[str, Any]:
    """Run the configured command, with the slip's metadata in the environment.

    Metadata only, same as everywhere else: a hook that received the message
    body would put a private conversation into a process table and any log the
    command happens to write.
    """
    env = dict(os.environ)
    for key in ("from", "topic", "status", "message_id", "thread_id"):
        env[f"DOORSLIP_{key.upper()}"] = str(summary.get(key) or "")

    try:
        finished = subprocess.run(
            command, shell=True, env=env, capture_output=True, text=True, timeout=900
        )
        return {"event": "hook", "exit_code": finished.returncode}
    except subprocess.TimeoutExpired:
        return {"event": "hook", "error": "timed out after 15 minutes"}
    except Exception as exc:
        # A broken hook must not take the watcher down with it. Losing the
        # automation is recoverable; losing the notifications is not.
        return {"event": "hook", "error": str(exc)}


NOTIFY_LIMIT = 120

# The AppleScript is a constant with a `run` handler, and the text arrives as
# arguments. Building the statement with the text inside it — which is what
# this used to do — let a sender whose topic contained a quotation mark close
# the string and continue in AppleScript, on the machine of anyone running the
# watcher. There is no escaping scheme here to get subtly wrong: the script
# never varies.
_OSASCRIPT = [
    "osascript",
    "-e", "on run argv",
    "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
    "-e", "end run",
]

# Same rule on Windows. The script is fixed and the text travels in the
# environment, where PowerShell reads it as a value and never as source.
_POWERSHELL = (
    "[reflection.assembly]::loadwithpartialname('System.Windows.Forms')"
    "|Out-Null;$n=New-Object System.Windows.Forms.NotifyIcon;"
    "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;"
    "$n.ShowBalloonTip(8000,$env:DOORSLIP_NOTIFY_TITLE,$env:DOORSLIP_NOTIFY_BODY,"
    "[System.Windows.Forms.ToolTipIcon]::Info);Start-Sleep -s 6"
)


def plain(text: str, limit: int = NOTIFY_LIMIT) -> str:
    """Reduce remote text to something inert before it leaves this process.

    Not the defence — passing text as data rather than as source is the
    defence, and it holds without this. This is the second layer, for the
    parts of a notification stack we do not control: newlines that split a
    record, terminal escapes that repaint a line, markup some daemons render,
    and lengths that turn one message into a wall.
    """
    cleaned = "".join(" " if ord(c) < 32 or ord(c) == 127 else c for c in str(text))
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit].rstrip() + "…" if len(cleaned) > limit else cleaned


def notify(summary: dict[str, Any]) -> None:
    """Best-effort desktop notification. Never fatal if unavailable.

    A watcher that dies because a notification daemon is missing is worse than
    one that quietly keeps watching — and after this carried remote text into
    a shell, one that dies on a hostile message would be worse still.
    """
    sender = plain(summary.get("from") or "someone", 64)
    topic = plain(summary.get("topic") or "")
    title = "New Doorslip slip"
    body = f"from {sender}" + (f" — {topic}" if topic else "")

    try:
        if sys.platform == "darwin":
            subprocess.run(
                [*_OSASCRIPT, body, title],
                check=False,
                capture_output=True,
                timeout=10,
            )
        elif sys.platform.startswith("linux"):
            # `--` so a body starting with a dash is text and not an option.
            subprocess.run(
                ["notify-send", "--", title, body],
                check=False,
                capture_output=True,
                timeout=10,
            )
        elif sys.platform == "win32":
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", _POWERSHELL],
                check=False,
                capture_output=True,
                timeout=20,
                env={
                    **os.environ,
                    "DOORSLIP_NOTIFY_TITLE": title,
                    "DOORSLIP_NOTIFY_BODY": body,
                },
            )
    except Exception:
        # Notification is a convenience. Losing it must not stop the watch.
        pass


def watch(
    agent: Any,
    *,
    every: int,
    use_notifications: bool = True,
    on_slip: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_age_hours: float | None = DEFAULT_MAX_AGE_HOURS,
) -> None:
    """Poll until interrupted, printing one JSON line per newly seen slip.

    JSON on stdout rather than prose, because the reader is a program: an
    agent tails this and turns it into a sentence for its human.
    """
    announced: set[str] = set()
    per_thread: Counter = Counter()
    print(
        json.dumps(
            {
                "event": "watching",
                "handle": agent.handle,
                "every_seconds": every,
                "hook": bool(on_slip),
                "max_turns": max_turns if on_slip else None,
            }
        ),
        flush=True,
    )
    while True:
        try:
            pending = agent.inbox(unacked_only=True)
        except Exception as exc:
            # The network goes away, laptops sleep, servers restart. Say so and
            # keep going; a watcher that exits on the first blip is useless.
            print(json.dumps({"event": "error", "detail": str(exc)}), flush=True)
            time.sleep(every)
            continue

        for message in pending:
            message_id = message.get("message_id")
            if not message_id or message_id in announced:
                continue
            announced.add(message_id)
            summary = summarise(message)
            print(json.dumps(summary, ensure_ascii=False), flush=True)
            if use_notifications:
                notify(summary)

            if not on_slip:
                continue

            outgoing = agent.sent()
            incoming = [m.get("envelope", {}) for m in pending]
            allowed, reason = should_wake(
                summary,
                per_thread,
                max_turns,
                we_also_settled=settled_by_us(outgoing, summary.get("thread_id")),
                thread_age_hours=thread_age_hours(
                    outgoing + incoming, summary.get("thread_id")
                ),
                max_age_hours=max_age_hours,
            )
            if not allowed:
                print(
                    json.dumps({"event": "hook-skipped", "reason": reason}),
                    flush=True,
                )
                continue
            if summary.get("thread_id"):
                per_thread[summary["thread_id"]] += 1
            print(json.dumps(run_hook(on_slip, summary)), flush=True)

        time.sleep(every)
