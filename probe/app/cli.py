from __future__ import annotations

import argparse
from collections.abc import Sequence

from probe import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ping-probe",
        description="PING probe agent stub. Runtime logic is not implemented yet.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="show probe stub version and exit",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"PING probe stub {__version__}")
    else:
        print("PING probe stub: runtime logic is not implemented yet.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
