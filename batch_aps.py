#!/usr/bin/env python3
"""
Batch scan APS journal issues using aps_issue_cdp_scraper.py.

Usage:
    python3 batch_aps_prab.py --journal prab --volume-start 20 --volume-end 28 \
        --issue-start 1 --issue-end 12 --sleep 120

Each issue is fetched with a configurable cooldown between requests.
Already-existing output files are skipped only when they contain one or more
articles. Empty or invalid JSON files are fetched again.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def log(msg: str, log_file: Path):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_article_count(path: Path) -> int | None:
    """Return the article count for a valid issue JSON, otherwise None."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    articles = data.get("article")
    if not isinstance(articles, list):
        return None
    return len(articles)


def run_one(
    journal: str,
    volume: int,
    issue: int,
    scraper: Path,
    output_dir: Path,
    page_settle_time: float,
    skip_existing: bool = True,
    profile_dir: str | None = None,
    chrome_executable: str | None = None,
    headless: bool = False,
    scraper_timeout: float = 180,
) -> tuple[str, int | str]:
    out_file = output_dir / f"{journal}_v{volume}_i{issue:02d}.json"
    if skip_existing and out_file.exists():
        existing_count = read_article_count(out_file)
        if existing_count is not None and existing_count > 0:
            return "skipped", existing_count

    cmd = [
        sys.executable,
        str(scraper),
        journal,
        str(volume),
        str(issue),
        "-o",
        str(out_file),
        "--page-settle-time",
        str(page_settle_time),
    ]
    if profile_dir:
        cmd += ["--profile-dir", profile_dir]
    if chrome_executable:
        cmd += ["--chrome-executable", chrome_executable]
    if headless:
        cmd.append("--headless")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=scraper_timeout,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip().splitlines()
            return "error", err[-1] if err else "non-zero exit"

        if not out_file.exists():
            # The scraper intentionally does not create a JSON file when the
            # page contains zero articles.
            return "empty", 0

        count = read_article_count(out_file)
        if count is None:
            return "error", "output JSON is invalid or has no article list"
        if count == 0:
            return "empty", 0
        return "ok", count
    except subprocess.TimeoutExpired:
        return "timeout", 0
    except Exception as e:
        return "error", str(e)


def main():
    script_dir = Path(__file__).resolve().parent
    default_scraper = script_dir / "aps_issue_cdp_scraper.py"
    default_output_dir = script_dir / "aps_batch"
    default_log_file = script_dir / "batch.log"

    parser = argparse.ArgumentParser(
        description="Batch scan APS journal issues using aps_issue_cdp_scraper.py."
    )
    parser.add_argument("--journal", default="prl", help="Journal code (default: prl)")
    parser.add_argument(
        "--volume-start", type=int, default=20, help="Start volume (default: 20)"
    )
    parser.add_argument(
        "--volume-end", type=int, default=28, help="End volume (default: 28)"
    )
    parser.add_argument(
        "--issue-start", type=int, default=1, help="Start issue (default: 1)"
    )
    parser.add_argument(
        "--issue-end", type=int, default=12, help="End issue (default: 12)"
    )
    parser.add_argument(
        "--sleep", type=float, default=120, help="Seconds between requests (default: 120)"
    )
    parser.add_argument(
        "--page-settle-time",
        type=float,
        default=60,
        help="Seconds to wait after page load (passed to scraper, default: 60)",
    )
    parser.add_argument(
        "--scraper",
        type=Path,
        default=default_scraper,
        help="Path to aps_issue_cdp_scraper.py",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_output_dir,
        help="Directory for JSON output files",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=default_log_file,
        help="Path to log file",
    )
    parser.add_argument(
        "--profile-dir",
        default=None,
        help="Chrome profile dir (default: ~/.config/google-chrome)",
    )
    parser.add_argument(
        "--chrome-executable",
        default=None,
        help="Chrome/Chromium executable passed to the scraper",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chrome/Chromium in headless mode",
    )
    parser.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Re-fetch all issues, including valid JSON files with articles",
    )
    parser.add_argument(
        "--scraper-timeout",
        type=float,
        default=180,
        help="Timeout for a single scraper invocation in seconds (default: 180)",
    )
    args = parser.parse_args()

    if not args.scraper.exists():
        print(f"Scraper not found: {args.scraper}", file=sys.stderr)
        sys.exit(1)

    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Clear or truncate log file at start.
    with open(args.log_file, "w", encoding="utf-8") as f:
        f.write("")

    log(
        f"Starting batch scan: {args.journal} volumes {args.volume_start}-{args.volume_end}, "
        f"issues {args.issue_start}-{args.issue_end}",
        args.log_file,
    )
    log(f"Output directory: {args.output_dir}", args.log_file)
    log(f"Cooldown between requests: {args.sleep}s", args.log_file)
    log(f"Page settle time: {args.page_settle_time}s", args.log_file)

    total = (args.volume_end - args.volume_start + 1) * (args.issue_end - args.issue_start + 1)
    completed = 0
    ok_count = 0

    for volume in range(args.volume_start, args.volume_end + 1):
        for issue in range(args.issue_start, args.issue_end + 1):
            completed += 1
            status, info = run_one(
                journal=args.journal,
                volume=volume,
                issue=issue,
                scraper=args.scraper,
                output_dir=args.output_dir,
                page_settle_time=args.page_settle_time,
                skip_existing=not args.no_skip_existing,
                profile_dir=args.profile_dir,
                chrome_executable=args.chrome_executable,
                headless=args.headless,
                scraper_timeout=args.scraper_timeout,
            )
            if status == "ok":
                ok_count += 1
                log(f"[{completed}/{total}] v{volume} i{issue:02d}: ok ({info} articles)", args.log_file)
            elif status == "empty":
                log(f"[{completed}/{total}] v{volume} i{issue:02d}: empty (0 articles)", args.log_file)
            elif status == "skipped":
                log(
                    f"[{completed}/{total}] v{volume} i{issue:02d}: skipped (already exists, {info} articles)",
                    args.log_file,
                )
            else:
                log(f"[{completed}/{total}] v{volume} i{issue:02d}: {status} - {info}", args.log_file)

            # Sleep between actual requests, but not after skips or the very last one.
            if volume == args.volume_end and issue == args.issue_end:
                break
            if status != "skipped":
                time.sleep(args.sleep)

    log(f"Batch scan finished. {ok_count}/{total} issues fetched successfully.", args.log_file)


if __name__ == "__main__":
    main()
