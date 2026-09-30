"""The ``byoai`` command: ``byoai shield check-file <path>``."""
from __future__ import annotations

import sys

USAGE = "usage: byoai shield check-file <path> [--row ID] [--name ORIGINAL] [--ledger PATH]"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:2] != ["shield", "check-file"]:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        from byoai.integrations.shield_check import main as check
    except ModuleNotFoundError as exc:
        if (exc.name or "").split(".")[0] != "cryptography":
            raise
        from byoai.integrations.shield_launcher import INSTALL_HINT
        print(INSTALL_HINT, file=sys.stderr)
        return 1
    return check(argv[2:])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
