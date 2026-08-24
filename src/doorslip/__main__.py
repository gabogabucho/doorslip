"""Run the Doorslip server.

    uv run python -m doorslip                     # in-memory, for a quick look
    uv run python -m doorslip --db doorslip.db    # persistent

The welcome handle defaults to the `.test` TLD (RFC 2606), which is reserved
and guaranteed never to resolve. That keeps development traffic from ever
reaching a real host by accident: if something tries to federate against the
handle's domain, it fails loudly instead of knocking on a stranger's door.
"""

from __future__ import annotations

import argparse

import uvicorn

from doorslip.api import create_app
from doorslip.store import Store, connect


def main() -> None:
    parser = argparse.ArgumentParser(prog="doorslip")
    parser.add_argument("--db", default=":memory:", help="SQLite path (default: in-memory)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--welcome-handle", default="welcome@doorslip.test")
    args = parser.parse_args()

    store = Store(connect(args.db))
    app = create_app(store, welcome_handle=args.welcome_handle)

    print(f"Doorslip on http://{args.host}:{args.port}  ·  db={args.db}")
    print(f"welcome desk: {args.welcome_handle}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
