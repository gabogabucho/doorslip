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
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from typing import Any

INTERVALS = {"15m": 900, "30m": 1800, "60m": 3600}


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


def notify(summary: dict[str, Any]) -> None:
    """Best-effort desktop notification. Never fatal if unavailable.

    A watcher that dies because a notification daemon is missing is worse than
    one that quietly keeps watching.
    """
    sender = summary.get("from") or "someone"
    topic = summary.get("topic")
    title = "New Doorslip slip"
    body = f"from {sender}" + (f" — {topic}" if topic else "")

    try:
        if sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e", f'display notification "{body}" with title "{title}"'],
                check=False,
                capture_output=True,
                timeout=10,
            )
        elif sys.platform.startswith("linux"):
            subprocess.run(
                ["notify-send", title, body], check=False, capture_output=True, timeout=10
            )
        elif sys.platform == "win32":
            script = (
                "[reflection.assembly]::loadwithpartialname('System.Windows.Forms')"
                "|Out-Null;$n=New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;"
                f"$n.ShowBalloonTip(8000,'{title}','{body}',"
                "[System.Windows.Forms.ToolTipIcon]::Info);Start-Sleep -s 6"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                check=False,
                capture_output=True,
                timeout=20,
            )
    except Exception:
        # Notification is a convenience. Losing it must not stop the watch.
        pass


def watch(agent: Any, *, every: int, use_notifications: bool = True) -> None:
    """Poll until interrupted, printing one JSON line per newly seen slip.

    JSON on stdout rather than prose, because the reader is a program: an
    agent tails this and turns it into a sentence for its human.
    """
    announced: set[str] = set()
    print(
        json.dumps({"event": "watching", "handle": agent.handle, "every_seconds": every}),
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

        time.sleep(every)
