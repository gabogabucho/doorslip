"""`python -m doorslip` — same entry point as the `doorslip` command."""

from __future__ import annotations

import sys

from doorslip.cli import main

if __name__ == "__main__":
    sys.exit(main())
