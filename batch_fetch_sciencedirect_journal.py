#!/usr/bin/env python3
"""Batch-call fetch_sciencedirect_journal.py over volume and issue ranges."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


HERE = Path(__file__).resolve().parent
PYTHON = Path("/home/zhiping/research-env/bin/python")
FETCH_SCRIPT = HERE / "fetch_sciencedirect_journal.py"


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def non_negative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("不能是负数")
    return number


def parse_range(value: str) -> list[int]:
    """Parse either one positive integer or an inclusive START-END range."""
    text = value.strip()
    if not text:
        raise argparse.ArgumentTypeError("范围不能为空")
    if "-" not in text:
        return [positive_int(text)]
    start_text, end_text = text.split("-", 1)
    start = positive_int(start_text.strip())
    end = positive_int(end_text.strip())
    if start > end:
        raise argparse.ArgumentTypeError("范围起点不能大于终点")
    return list(range(start, end + 1))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按 volume 和 issue 范围批量调用 fetch_sciencedirect_journal.py"
    )
    parser.add_argument(
        "volume_range",
        nargs="?",
        default="36-173",
        help="卷号或闭区间，例如 36-173（默认：36-173）",
    )
    parser.add_argument(
        "issue_range",
        nargs="?",
        default="1-5",
        help="期号或闭区间，例如 1-5（默认：1-5）",
    )
    parser.add_argument(
        "--journal",
        default="computer-physics-communications",
        help="ScienceDirect 期刊 URL 名",
    )
    parser.add_argument(
        "--part",
        default="",
        help="每次请求使用的 part，例如 P2（默认：空，不传 part）",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=HERE,
        help="JSON 输出目录（默认：本脚本目录）",
    )
    parser.add_argument(
        "--timeout",
        type=positive_int,
        default=60,
        help="传给单次抓取程序的页面超时秒数（默认：60）",
    )
    parser.add_argument(
        "--delay",
        type=non_negative_float,
        default=60.0,
        help="成功后等待秒数（默认：60）",
    )
    parser.add_argument(
        "--error-delay",
        type=non_negative_float,
        default=300.0,
        help="失败或被拦截后等待秒数（默认：300）",
    )
    parser.add_argument(
        "--long-break-every",
        type=positive_int,
        default=30,
        help="每实际访问多少个页面后长休眠（默认：30）",
    )
    parser.add_argument(
        "--long-break",
        type=non_negative_float,
        default=1800.0,
        help="长休眠秒数（默认：1800）",
    )
    parser.add_argument("--skip-existing", action="store_true", help="请求期号的目标 JSON 已存在时跳过")
    parser.add_argument("--stop-on-error", action="store_true", help="任一次抓取失败后立即停止")
    parser.add_argument("--headless", action="store_true", help="将 --headless 传给单次抓取程序")
    parser.add_argument("--user-config", action="store_true", help="将 --user-config 传给单次抓取程序")
    return parser.parse_args()


def output_path(
    output_dir: Path,
    journal: str,
    volume: int | str,
    issue: object,
    part: object = None,
) -> Path:
    issue_suffix = f"_issue_{issue}" if issue not in (None, "") else ""
    part_suffix = f"_part_{part}" if part not in (None, "") else ""
    return output_dir / f"{journal}_vol_{volume}{issue_suffix}{part_suffix}.json"


def article_count(path: Path) -> int | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    articles = data.get("article")
    return len(articles) if isinstance(articles, list) else None


def run_one(args: argparse.Namespace, volume: int, issue: int, pending: Path) -> int:
    command = [
        str(PYTHON),
        str(FETCH_SCRIPT),
        args.journal,
        "--vol",
        str(volume),
        "--issue",
        str(issue),
    ]
    if args.part:
        command += ["--part", args.part]
    command += [
        "-o",
        str(pending),
        "--timeout",
        str(args.timeout),
    ]
    if args.headless:
        command.append("--headless")
    if args.user_config:
        command.append("--user-config")
    return subprocess.run(command, check=False).returncode


def finalize_output(pending: Path, output_dir: Path, journal: str, requested_volume: int) -> tuple[Path, int | None]:
    data = json.loads(pending.read_text(encoding="utf-8"))
    actual_volume = data.get("vol") or requested_volume
    actual_issue = data.get("issue")
    actual_part = data.get("part")
    target = output_path(output_dir, journal, actual_volume, actual_issue, actual_part)
    pending.replace(target)
    return target, article_count(target)


def main() -> int:
    args = parse_args()
    volumes = parse_range(args.volume_range)
    issues = parse_range(args.issue_range)
    journal = args.journal.strip().strip("/")
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    total = len(volumes) * len(issues)
    completed = 0
    page_attempts = 0
    failures: list[tuple[int, int, int]] = []

    for volume in volumes:
        for issue in issues:
            completed += 1
            requested_target = output_path(output_dir, journal, volume, issue, args.part)
            if args.skip_existing and requested_target.exists():
                print(f"[{completed}/{total}] 跳过已有文件: {requested_target}", file=sys.stderr)
                continue

            pending = output_dir / f".{journal}_vol_{volume}_issue_{issue}.pending.json"
            print(f"[{completed}/{total}] 抓取 volume={volume}, issue={issue}", file=sys.stderr)
            page_attempts += 1
            returncode = run_one(args, volume, issue, pending)
            if returncode == 0 and pending.exists():
                try:
                    target, count = finalize_output(pending, output_dir, journal, volume)
                    count_text = str(count) if count is not None else "未知"
                    print(f"[{completed}/{total}] 成功: {target}，文章数={count_text}", file=sys.stderr)
                except (OSError, json.JSONDecodeError, TypeError) as exc:
                    returncode = 1
                    print(f"[{completed}/{total}] 输出处理失败: {exc}", file=sys.stderr)

            if returncode != 0:
                failures.append((volume, issue, returncode))
                print(
                    f"[{completed}/{total}] 失败: volume={volume}, issue={issue}, returncode={returncode}",
                    file=sys.stderr,
                )
                if args.stop_on_error:
                    break
                delay = args.error_delay
            else:
                delay = args.delay

            if completed < total:
                if page_attempts % args.long_break_every == 0:
                    delay = args.long_break
                    print(
                        f"已访问 {page_attempts} 个页面，长休眠 {delay:g} 秒……",
                        file=sys.stderr,
                    )
                elif delay:
                    print(f"等待 {delay:g} 秒……", file=sys.stderr)
                if delay:
                    time.sleep(delay)

        if failures and args.stop_on_error:
            break

    if failures:
        print("失败项目：", file=sys.stderr)
        for volume, issue, returncode in failures:
            print(f"  volume={volume}, issue={issue}, returncode={returncode}", file=sys.stderr)
    print(f"完成：计划 {total} 项，失败 {len(failures)} 项", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
