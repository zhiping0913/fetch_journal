#!/home/zhiping/research-env/bin/python
"""Batch fetch AIP issue metadata by calling aip.py."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


PYTHON = Path("/home/zhiping/research-env/bin/python")
AIP_SCRIPT = Path("/home/zhiping/Projects/fetch_journal/aip.py")


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def non_negative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return number


def parse_range(value: str) -> list[int]:
    text = value.strip()
    if not text:
        raise argparse.ArgumentTypeError("range cannot be empty")

    if "-" not in text:
        return [positive_int(text)]

    start_text, end_text = text.split("-", 1)
    start = positive_int(start_text.strip())
    end = positive_int(end_text.strip())
    if start > end:
        raise argparse.ArgumentTypeError("range start must be <= range end")
    return list(range(start, end + 1))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch call aip.py for a journal over volume and issue ranges."
    )
    parser.add_argument(
        "volume_range",
        nargs="?",
        default="10-20",
        help="Volume range, for example: 10-20. Default: 10-20.",
    )
    parser.add_argument(
        "issue_range",
        nargs="?",
        default="1-12",
        help="Issue range, for example: 1-12. Default: 1-12.",
    )
    parser.add_argument(
        "--journal",
        default="pop",
        help="AIP journal abbreviation. Default: pop.",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("."),
        help="Directory for JSON files. Default: current directory.",
    )
    parser.add_argument(
        "--timeout",
        type=positive_int,
        default=45,
        help="Per-issue timeout passed to aip.py, in seconds. Default: 45.",
    )
    parser.add_argument(
        "--delay",
        type=non_negative_float,
        default=60.0,
        help="Delay between issue fetches, in seconds. Default: 60.",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip an issue when its target JSON file already exists.",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop immediately when one aip.py call fails.",
    )
    return parser.parse_args()


def output_path(output_dir: Path, journal: str, volume: int, issue: int) -> Path:
    return output_dir / f"{journal}_{volume}_{issue}.json"


def run_one(
    *,
    journal: str,
    volume: int,
    issue: int,
    output: Path,
    timeout: int,
) -> int:
    command = [
        str(PYTHON),
        str(AIP_SCRIPT),
        journal,
        str(volume),
        str(issue),
        "-o",
        str(output),
        "--timeout",
        str(timeout),
    ]
    return subprocess.run(command, check=False).returncode


def article_count(path: Path) -> int | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    articles = data.get("article")
    if not isinstance(articles, list):
        return None
    return len(articles)


def main() -> int:
    args = parse_args()
    journal = args.journal.strip().lower()
    volumes = parse_range(args.volume_range)
    issues = parse_range(args.issue_range)
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    total = len(volumes) * len(issues)
    failures: list[tuple[int, int, int]] = []
    counts: list[tuple[int, int, int | None]] = []
    completed = 0

    for volume in volumes:
        for issue in issues:
            target = output_path(output_dir, journal, volume, issue)
            completed += 1

            if args.skip_existing and target.exists():
                print(f"[{completed}/{total}] skip existing {target}", file=sys.stderr)
                counts.append((volume, issue, article_count(target)))
                continue

            print(f"[{completed}/{total}] fetch {journal} volume={volume} issue={issue}", file=sys.stderr)
            returncode = run_one(
                journal=journal,
                volume=volume,
                issue=issue,
                output=target,
                timeout=args.timeout,
            )
            if returncode != 0:
                failures.append((volume, issue, returncode))
                print(
                    f"[{completed}/{total}] failed volume={volume} issue={issue} returncode={returncode}",
                    file=sys.stderr,
                )
                if args.stop_on_error:
                    break
            else:
                counts.append((volume, issue, article_count(target)))

            if args.delay and completed < total:
                time.sleep(args.delay)

        if failures and args.stop_on_error:
            break

    if failures:
        print("Failed issues:", file=sys.stderr)
        for volume, issue, returncode in failures:
            print(f"  volume={volume} issue={issue} returncode={returncode}", file=sys.stderr)

    if counts:
        print("Article counts:", file=sys.stderr)
        for volume, issue, count in counts:
            count_text = str(count) if count is not None else "unknown"
            print(f"  volume={volume} issue={issue}: {count_text}", file=sys.stderr)

    print(f"Done: {total} issue(s)", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
