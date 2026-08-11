"""``coriqo-verify`` — offline integrity check of a recorder ledger.

Usage::

    coriqo-verify <ledger.db> [--pubkey B64] [--json]

Exit code 0 means the record verified clean. Exit code 1 means the record
cannot be relied upon and the reasons are printed.
"""

from __future__ import annotations

import argparse
import json
import sys

from byoai.recorder.verify import VerifyError, VerifyReport, verify_ledger

_EXIT_OK = 0
_EXIT_FAILED = 1
_EXIT_UNREADABLE = 2


def _subject(report: VerifyReport) -> str:
    if len(report.session_ids) == 1:
        subject = f"session `{report.session_ids[0]}`"
    elif report.session_ids:
        subject = f"{len(report.session_ids)} sessions"
    else:
        subject = "session (unknown)"
    if len(report.device_ids) == 1:
        return f"Device `{report.device_ids[0]}` — {subject}"
    if report.device_ids:
        return f"{len(report.device_ids)} devices — {subject}"
    return subject.capitalize()


def _fmt_seq(value: int | None) -> str:
    return "—" if value is None else f"{value:,}"


def _window(report: VerifyReport) -> str:
    if report.ts_first and report.ts_last:
        return f"{report.ts_first}–{report.ts_last} UTC"
    return "time window unknown"


def format_report(report: VerifyReport) -> str:
    lines: list[str] = []
    lines.append(_subject(report))
    lines.append(
        f"  seq {_fmt_seq(report.seq_start)}–{_fmt_seq(report.seq_end)} "
        f"({report.entries_checked:,} events, {_window(report)})"
    )
    lines.append(
        f"  {report.entries_checked:,} entry hashes re-derived from stored data; "
        f"{report.checkpoints_checked} checkpoint(s) examined"
        + ("" if report.signatures_verified else "; signatures not checked")
    )
    lines.append("")

    if report.ok and not report.unpaired_tool_uses:
        if report.signatures_verified:
            lines.append("VERDICT: record complete and unaltered.")
        else:
            # Without a public key, a chain that is internally consistent
            # end-to-end is not distinguishable from one that was fully
            # rewritten and re-hashed by whoever controls the file — only a
            # verified checkpoint signature can rule that out. Say so
            # instead of claiming an integrity guarantee this pass can't
            # back up.
            lines.append(
                "VERDICT: record internally consistent, but NOT cryptographically "
                "verified — rerun with --pubkey to rule out a wholesale rewrite."
            )
        return "\n".join(lines)

    lines.append("FINDINGS")

    for start, end in report.gaps:
        count = end - start + 1
        lines.append(
            f"  - record incomplete: seq {start:,}–{end:,} missing "
            f"({count:,} event{'s' if count != 1 else ''})."
        )

    for seq in report.broken_links:
        lines.append(
            f"  - entry seq {seq:,} does not match its own hash chain: the stored "
            "record was altered or replaced after it was written."
        )

    for seq_end in report.bad_signatures:
        lines.append(
            f"  - checkpoint covering up to seq {seq_end:,} failed verification: "
            "the device signature or the sealed chain head is not authentic."
        )

    for tool_use_id in report.orphan_tool_results:
        lines.append(
            f"  - tool result `{tool_use_id}` has no preceding tool_use: the client "
            "returned the outcome of an action the model never requested."
        )

    for tool_use_id in report.unpaired_tool_uses:
        lines.append(
            f"  - tool call `{tool_use_id}` has no recorded result: the agent "
            "requested an action whose outcome was never returned."
        )

    for note in report.notes:
        lines.append(f"  - note: {note}")

    lines.append("")
    if report.ok:
        lines.append("VERDICT: chain intact; incomplete tool pairings noted above for review.")
    else:
        lines.append("VERDICT: record CANNOT be relied upon — see findings above.")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coriqo-verify",
        description="Offline integrity verification of a Coriqo agent ledger.",
    )
    parser.add_argument("ledger", help="path to the ledger SQLite file")
    parser.add_argument(
        "--pubkey",
        dest="pubkey",
        default=None,
        help="base64 Ed25519 device public key used to check checkpoint signatures",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit the verification report as machine-readable JSON",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        report = verify_ledger(args.ledger, public_key_b64=args.pubkey)
    except VerifyError as exc:
        print(f"coriqo-verify: {exc}", file=sys.stderr)
        return _EXIT_UNREADABLE

    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(format_report(report))
    return _EXIT_OK if report.ok else _EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
