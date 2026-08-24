"""Command line interface.

Two audiences, one tool. A person runs `doorslip serve`; an agent runs
everything else — which is why **every command prints JSON**. The reader is a
program, and a program should not have to scrape prose.

This is also the no-MCP path. An agent with a shell can onboard and converse
using nothing but this command, so the MCP server stays an improvement rather
than a prerequisite. Making the MCP mandatory would put a config-file edit and
a restart between a new person and their first message, and that is where
onboarding dies.

**The private key is written once and never printed.** It lives in its own file
with owner-only permissions, separate from the config, so that showing someone
your settings can never leak your identity.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from doorslip.client import Agent, ProtocolError, load_or_create_keypair

# Identities live under the human's own home, never inside an agent's install
# tree. Uninstalling an agent must not take the mailbox with it: there is no
# identity recovery (spec §10), so a deleted key is a handle lost for good.
DOORSLIP_ROOT = Path.home() / ".doorslip"
CONFIG_NAME = "config.json"
KEY_NAME = "key.json"
OUTBOX_NAME = "outbox.jsonl"

# Kept for callers that still import it; the real default is per agent.
DEFAULT_HOME = DOORSLIP_ROOT


def agent_home(label: str, root: Path | None = None) -> Path:
    """Where one agent's key and settings live.

    One directory per agent, not one per machine. The identity is shared —
    same human, same handle, same address book — but each agent holds its own
    key, which is what makes revoking one of them possible without locking the
    others out (spec §7.3).
    """
    return (root or DOORSLIP_ROOT) / label


def discover_home(root: Path | None = None) -> Path | None:
    """Find the only configured agent, if there is exactly one.

    Someone running a single agent should never have to think about this. With
    several, guessing would be worse than asking: acting as the wrong agent
    sends messages signed by a key the human did not choose.
    """
    base = root or DOORSLIP_ROOT
    if not base.is_dir():
        return None
    candidates = sorted(d for d in base.iterdir() if (d / CONFIG_NAME).is_file())
    if len(candidates) == 1:
        return candidates[0]
    return None


def _resolve_home(args: argparse.Namespace) -> Path:
    if getattr(args, "home", None):
        return Path(args.home)
    found = discover_home()
    if found is not None:
        return found
    base = DOORSLIP_ROOT
    configured = (
        sorted(d.name for d in base.iterdir() if (d / CONFIG_NAME).is_file())
        if base.is_dir()
        else []
    )
    if configured:
        raise SystemExit(
            json.dumps(
                {
                    "error": "several agents are set up here; say which one with --home",
                    "agents": configured,
                    "example": f"doorslip --home {base / configured[0]} inbox",
                }
            )
        )
    raise SystemExit(json.dumps({"error": "no agent set up yet; run `doorslip setup` first"}))


def _paths(home: Path) -> tuple[Path, Path]:
    return home / CONFIG_NAME, home / KEY_NAME


def _load_agent(home: Path) -> Agent:
    import httpx

    config_path, key_path = _paths(home)
    if not config_path.exists():
        raise SystemExit(
            json.dumps({"error": f"not set up yet; run `doorslip setup` first ({home})"})
        )
    config = json.loads(config_path.read_text(encoding="utf-8"))
    keypair = load_or_create_keypair(key_path)
    return Agent(
        httpx.Client(base_url=config["server"], timeout=30.0),
        handle=config["handle"],
        label=config["label"],
        keypair=keypair,
        outbox_path=home / OUTBOX_NAME,
    )


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


# -- commands -------------------------------------------------------------


def cmd_setup(args: argparse.Namespace) -> int:
    """Generate a key, register, and optionally redeem an invitation.

    Safe to re-run: the key is only generated the first time, so a second run
    against the same home directory re-registers nothing and loses nothing.
    """
    import httpx

    # One directory per agent, named after its label. A second agent on the
    # same machine enrols beside this one instead of overwriting its key.
    home = Path(args.home) if args.home else agent_home(args.label)
    home.mkdir(parents=True, exist_ok=True)
    config_path, key_path = _paths(home)

    keypair = load_or_create_keypair(key_path)
    agent = Agent(
        httpx.Client(base_url=args.server, timeout=30.0),
        handle=args.handle,
        label=args.label,
        keypair=keypair,
        outbox_path=home / OUTBOX_NAME,
    )

    result: dict[str, Any] = {"handle": args.handle, "server": args.server}
    try:
        registered = agent.register(enroll_code=args.enroll)
        result["registered"] = True
        result["welcome_handle"] = registered.get("welcome_handle")
        if registered.get("enrolled"):
            result["handle"] = registered["handle"]
            result["enrolled"] = True
            result["active_agents"] = registered.get("active_agents")
    except ProtocolError as exc:
        if exc.status != 409:
            _emit({"error": exc.detail, "status": exc.status})
            return 1
        # Already registered with this key. Re-running setup is not an error.
        result["registered"] = False
        result["note"] = "this handle or key was already registered"

    config_path.write_text(
        json.dumps(
            {
                "server": args.server,
                "handle": result["handle"],
                "label": args.label,
                "check_every": args.check_every,
            }
        ),
        encoding="utf-8",
    )
    result["check_every"] = args.check_every

    if args.invite:
        try:
            result["contact_added"] = agent.accept(args.invite)
        except ProtocolError as exc:
            result["invite_error"] = exc.detail

    # Someone who arrived without an invitation has exactly one useful move:
    # invite somebody. Handing them a code here removes the "now what?" beat
    # between registering and their first real conversation.
    if result.get("registered"):
        try:
            result["invite_to_share"] = agent.invite()
        except ProtocolError:
            pass

    if args.greet and result.get("welcome_handle"):
        try:
            agent.send(
                to=result["welcome_handle"],
                state={"topic": "hello", "status": "proposed"},
                prose=f"{args.handle} just set up an agent and is checking the channel.",
            )
            result["greeted_welcome_desk"] = True
        except ProtocolError as exc:
            result["greet_error"] = exc.detail

    _emit(result)
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    """Show this identity's settings. Never prints anything secret.

    The key lives in a separate file and is not read here at all, so showing
    someone your settings can never leak your identity.
    """
    config_path, _ = _paths(_resolve_home(args))
    if not config_path.exists():
        _emit({"error": "not set up yet; run `doorslip setup` first"})
        return 1
    settings = json.loads(config_path.read_text(encoding="utf-8"))
    try:
        notice = _load_agent(_resolve_home(args)).update_notice()
    except SystemExit:
        notice = None
    if notice:
        settings["update_available"] = notice
    _emit(settings)
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    agent = _load_agent(_resolve_home(args))
    try:
        state = json.loads(args.state) if args.state else {}
    except json.JSONDecodeError as exc:
        _emit({"error": f"--state is not valid JSON: {exc}"})
        return 1
    try:
        _emit(
            agent.send(
                to=args.to,
                state=state,
                prose=args.prose,
                thread_id=args.thread,
                parent_message_id=args.parent,
            )
        )
    except ProtocolError as exc:
        _emit({"error": exc.detail, "status": exc.status})
        return 1
    return 0


def cmd_inbox(args: argparse.Namespace) -> int:
    agent = _load_agent(_resolve_home(args))
    payload: dict[str, Any] = {"messages": agent.inbox(unacked_only=args.unacked)}
    # Surfaced where an agent already looks regularly. Telling a human their
    # client is behind is useful; interrupting them to say it is not.
    notice = agent.update_notice()
    if notice:
        payload["update_available"] = notice
    _emit(payload)
    return 0


def cmd_ack(args: argparse.Namespace) -> int:
    agent = _load_agent(_resolve_home(args))
    try:
        agent.ack(args.message_id)
    except ProtocolError as exc:
        _emit({"error": exc.detail, "status": exc.status})
        return 1
    _emit({"acked": args.message_id})
    return 0


def cmd_invite(args: argparse.Namespace) -> int:
    """Mint invitation codes — one per person, never one for a list.

    An invitation is "I, specifically, want to talk to you, specifically".
    A single code handed to a group is not an invitation, it is an open door,
    and it would dissolve the allowlist that is the whole trust model.
    """
    agent = _load_agent(_resolve_home(args))
    _emit({"codes": [agent.invite() for _ in range(args.count)]})
    return 0


def cmd_enroll_code(args: argparse.Namespace) -> int:
    """Mint a code so another of YOUR agents can join this same mailbox."""
    agent = _load_agent(_resolve_home(args))
    try:
        _emit({"code": agent.enroll_code()})
    except ProtocolError as exc:
        _emit({"error": exc.detail, "status": exc.status})
        return 1
    return 0


def cmd_accept(args: argparse.Namespace) -> int:
    agent = _load_agent(_resolve_home(args))
    try:
        _emit({"contact": agent.accept(args.code)})
    except ProtocolError as exc:
        _emit({"error": exc.detail, "status": exc.status})
        return 1
    return 0


def cmd_contacts(args: argparse.Namespace) -> int:
    agent = _load_agent(_resolve_home(args))
    _emit({"contacts": agent.contacts()})
    return 0


def cmd_thread(args: argparse.Namespace) -> int:
    """Fold one thread into its current state (spec §6.1)."""
    agent = _load_agent(_resolve_home(args))
    result = agent.thread_state(args.thread_id)
    _emit(
        {
            "state": result.state,
            "patches_applied": len(result.applied),
            "diverged": result.diverged,
        }
    )
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Poll this mailbox locally and announce new slips.

    Nothing about this touches the server beyond the reads any agent makes
    anyway. There is no push endpoint on purpose: notifying somebody requires
    knowing how to reach them, and that contact detail is exactly the personal
    data this protocol is worth using for not holding.
    """
    from doorslip.watch import interval_seconds, watch

    home = _resolve_home(args)
    config_path, _ = _paths(home)
    setting = args.every
    if setting is None:
        config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        setting = config.get("check_every", "manual")

    seconds = interval_seconds(setting)
    if seconds is None:
        _emit({"error": f"check_every is {setting!r}; nothing to watch", "setting": setting})
        return 1

    try:
        watch(_load_agent(home), every=seconds, use_notifications=not args.quiet)
    except KeyboardInterrupt:
        _emit({"event": "stopped"})
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from doorslip.api import create_app
    from doorslip.store import Store, connect

    # The desk's key lives beside the database so both survive a restart
    # together, and an in-memory database gets an ephemeral key to match.
    key_path = None if args.db == ":memory:" else Path(args.db).with_suffix(".welcome.json")
    app = create_app(
        Store(connect(args.db)),
        welcome_handle=args.welcome_handle,
        welcome_key_path=key_path,
    )
    print(f"Doorslip on http://{args.host}:{args.port}  ·  db={args.db}")
    print(f"welcome desk: {args.welcome_handle}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


# -- wiring ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="doorslip", description="Doorslip agent client")
    parser.add_argument(
        "--home",
        default=None,
        help="this agent's directory; defaults to ~/.doorslip/<label>, "
        "and is discovered automatically when only one agent is set up",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    setup = subcommands.add_parser("setup", help="generate a key and register")
    setup.add_argument("--server", required=True)
    setup.add_argument("--handle", default="", help="required unless --enroll is used")
    setup.add_argument("--label", default="agent")
    setup.add_argument("--invite", help="invitation code to redeem right away")
    setup.add_argument("--enroll", help="enrolment code, to join an existing mailbox")
    setup.add_argument("--greet", action="store_true", help="write to the welcome desk")
    setup.add_argument(
        "--check-every",
        default="manual",
        choices=["15m", "30m", "60m", "manual"],
        help="how often the agent should look for new slips during a session",
    )
    setup.set_defaults(func=cmd_setup)

    config = subcommands.add_parser("config", help="show this identity's settings")
    config.set_defaults(func=cmd_config)

    send = subcommands.add_parser("send", help="deposit a message")
    send.add_argument("--to", required=True)
    send.add_argument("--prose", required=True)
    send.add_argument("--state", help="JSON object; a merge patch when replying")
    send.add_argument("--thread")
    send.add_argument("--parent")
    send.set_defaults(func=cmd_send)

    inbox = subcommands.add_parser("inbox", help="read your messages")
    inbox.add_argument("--unacked", action="store_true")
    inbox.set_defaults(func=cmd_inbox)

    ack = subcommands.add_parser("ack", help="confirm a message was incorporated")
    ack.add_argument("message_id")
    ack.set_defaults(func=cmd_ack)

    invite = subcommands.add_parser("invite", help="mint invitation codes")
    invite.add_argument("--count", type=int, default=1)
    invite.set_defaults(func=cmd_invite)

    enroll = subcommands.add_parser(
        "enroll-code", help="mint a code for another of your own agents"
    )
    enroll.set_defaults(func=cmd_enroll_code)

    accept = subcommands.add_parser("accept", help="redeem an invitation code")
    accept.add_argument("code")
    accept.set_defaults(func=cmd_accept)

    contacts = subcommands.add_parser("contacts", help="list your address book")
    contacts.set_defaults(func=cmd_contacts)

    thread = subcommands.add_parser("thread", help="reconstruct a thread's state")
    thread.add_argument("thread_id")
    thread.set_defaults(func=cmd_thread)

    watcher = subcommands.add_parser("watch", help="poll locally and announce new slips")
    watcher.add_argument("--every", choices=["15m", "30m", "60m"], help="overrides the stored setting")
    watcher.add_argument("--quiet", action="store_true", help="no desktop notifications")
    watcher.set_defaults(func=cmd_watch)

    serve = subcommands.add_parser("serve", help="run the server")
    serve.add_argument("--db", default=":memory:")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--welcome-handle", default="welcome@doorslip.test")
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
