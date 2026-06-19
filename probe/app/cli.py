from __future__ import annotations

import argparse
from collections.abc import Sequence

from probe import __version__
from probe.app.agent import ProbeAgent
from probe.app.client import ProbeCentralClient
from probe.app.config import load_runtime_config
from probe.app.storage import ProbeStorage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ping-probe",
        description="PING probe agent MVP.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="show probe agent version and exit",
    )
    parser.add_argument(
        "--config",
        default="probe-config.json",
        help="path to local probe config JSON",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one sync/check/submit cycle and exit",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"PING probe agent {__version__}")
        return 0

    if not args.once:
        parser.error("Only --once mode is implemented in the MVP probe agent.")

    runtime_config = load_runtime_config(args.config)
    storage = ProbeStorage(runtime_config.storage_dir)
    client = ProbeCentralClient(runtime_config)
    summary = ProbeAgent(client=client, storage=storage).run_once()
    print(
        "PING probe agent cycle complete: "
        f"sites_checked={summary.sites_checked}, "
        f"submitted_results={summary.submitted_results}, "
        f"queued_results={summary.queued_results}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
