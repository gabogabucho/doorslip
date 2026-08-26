"""Keeping the watcher alive across reboots.

Nothing here is protocol. The watcher is a local poll and this module writes
the file this machine's init system wants so it starts again after a reboot —
because the alternative was every person inventing it, and most of them not.

**It writes the definition and does not enable it.** The same rule the skill
applies to installing the package applies here: a background process that
starts itself on every login is the human's decision, and a tool that arranges
one without being told is doing what this protocol refuses to do elsewhere.
So the file is written and the one command to enable it is printed.

The generated definition always pins an identity with `--as`. A watcher that
relied on discovery works until the day a second mailbox is set up on the same
machine, and then `discover_home` correctly refuses to guess and the service
dies quietly — the failure being quiet is what makes it worth spending a flag
on now.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

LAUNCHD_LABEL = "org.doorslip.watch"


def _runner() -> list[str]:
    """How to invoke this installation from a file written today.

    `python -m doorslip` rather than whatever `doorslip` resolves to on PATH:
    a console script installed by uvx or a virtualenv may not be on the PATH
    a service manager builds, and `sys.executable` is the interpreter that is
    demonstrably running this code.
    """
    return [sys.executable, "-m", "doorslip"]


def systemd_unit(handle: str, extra: list[str]) -> str:
    command = " ".join(_runner() + ["--as", handle, "watch"] + extra)
    return f"""[Unit]
Description=Doorslip watcher for {handle}
Documentation=https://doorslip.org/reference.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={command}
# A watcher that stays down after one bad night is a person who stops hearing
# from anybody and is never told. Restarting is the whole point of the file.
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
"""


def launchd_plist(handle: str, extra: list[str]) -> str:
    arguments = "".join(
        f"\n        <string>{part}</string>"
        for part in _runner() + ["--as", handle, "watch"] + extra
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>{arguments}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
"""


def plan(handle: str, extra: list[str] | None = None) -> dict[str, object]:
    """Where the definition goes, what goes in it, and how to turn it on.

    Returns a plan rather than performing one, so the caller can print it,
    write it, or undo it without this module deciding which.
    """
    extra = extra or []
    home = Path.home()

    if sys.platform.startswith("linux"):
        path = home / ".config" / "systemd" / "user" / "doorslip-watch.service"
        return {
            "manager": "systemd (user)",
            "path": path,
            "contents": systemd_unit(handle, extra),
            "enable": [
                "systemctl --user daemon-reload",
                "systemctl --user enable --now doorslip-watch",
            ],
            "disable": ["systemctl --user disable --now doorslip-watch"],
            "logs": "journalctl --user -u doorslip-watch -f",
            # Without this a user service stops at logout and starts at login,
            # which is not what somebody asking for a watcher meant.
            "note": "for it to run while you are logged out: "
            f"sudo loginctl enable-linger {home.name}",
        }

    if sys.platform == "darwin":
        path = home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        return {
            "manager": "launchd",
            "path": path,
            "contents": launchd_plist(handle, extra),
            "enable": [f"launchctl load -w {path}"],
            "disable": [f"launchctl unload -w {path}"],
            "logs": "log stream --predicate 'process == \"python\"' --info",
            "note": "launchd starts this at login and restarts it if it exits",
        }

    if sys.platform == "win32":
        # Only the interpreter path needs quoting, and cmd.exe wants the inner
        # quotes backslash-escaped inside /tr. Quoting every argument produced
        # a command with nested bare quotes that schtasks rejects, which is
        # the kind of thing that looks fine in a JSON blob and fails on paste.
        exe, *rest = _runner()
        inner = " ".join([f'\\"{exe}\\"', *rest, "--as", handle, "watch", *extra])
        command = f'schtasks /create /tn "Doorslip watcher" /sc onlogon /tr "{inner}" /f'
        return {
            "manager": "Task Scheduler",
            "path": None,
            "contents": None,
            # Nothing is written here: Task Scheduler's store is not a file to
            # drop, and generating XML to import is more moving parts than the
            # one command it replaces.
            "enable": [command],
            "disable": ['schtasks /delete /tn "Doorslip watcher" /f'],
            "logs": "run `doorslip watch` in a terminal to see it working first",
            "note": "Task Scheduler starts it at logon; it does not restart it "
            "if it exits, which systemd and launchd both do",
        }

    return {
        "manager": None,
        "error": f"no service definition for {sys.platform}",
        "do": "run `doorslip watch` under whatever keeps processes alive here",
    }


def install(handle: str, extra: list[str] | None = None) -> dict[str, object]:
    """Write the definition. Enabling it stays the human's move."""
    made = plan(handle, extra)
    if made.get("error"):
        return made

    path, contents = made.get("path"), made.get("contents")
    if path and contents:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(contents), encoding="utf-8")

    return {
        "manager": made["manager"],
        "wrote": str(path) if path else None,
        "handle": handle,
        "now_run": made["enable"],
        "to_stop": made["disable"],
        "logs": made["logs"],
        "note": made.get("note"),
        "not_enabled": "written, not started — a background process that runs "
        "at every login is your decision, not this tool's",
    }


def uninstall(handle: str) -> dict[str, object]:
    made = plan(handle)
    if made.get("error"):
        return made
    path = made.get("path")
    removed = False
    if path and Path(path).exists():
        Path(path).unlink()
        removed = True
    return {
        "manager": made["manager"],
        "removed": str(path) if removed else None,
        "now_run": made["disable"],
        "note": "the definition is gone; stop the running one with the command "
        "above if it is still going",
    }


def manager_available() -> bool:
    """Whether the thing that would run this is actually here."""
    if sys.platform.startswith("linux"):
        return shutil.which("systemctl") is not None
    if sys.platform == "darwin":
        return shutil.which("launchctl") is not None
    if sys.platform == "win32":
        return shutil.which("schtasks") is not None
    return False
