"""Secret-safe command-line interface for passive scanning."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TextIO

from .models import ScanLimits, VerifiedKey
from .scanner import scan_wechat_key


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wechat-key-scanner")
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="scan a running WeChat process read-only")
    scan.add_argument("--data-root", type=Path)
    scan.add_argument("--timeout", type=float, default=30.0)
    scan.add_argument("--print-key", action="store_true")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    scan=scan_wechat_key,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    args = _parser().parse_args(argv)
    try:
        limits = ScanLimits(timeout_seconds=args.timeout)
    except ValueError as error:
        print(f"invalid_options: {error}", file=stderr)
        return 2
    result = scan(data_root=args.data_root, limits=limits)
    if not isinstance(result, VerifiedKey):
        print(f"{result.code.value}: {result.message}", file=stderr)
        return 3
    print(
        f"verified account={result.database.account_id} pid={result.process.pid} "
        f"mode={result.mode.value} database={result.database.path}",
        file=stdout,
    )
    if args.print_key:
        print(result.key.hex(), file=stdout)
    return 0
