#!/usr/bin/env python3
"""Fetch all article metadata from a ScienceDirect journal volume page."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
IS_WINDOWS = platform.system() == "Windows"
CHROME_DEBUG_PORT = 9222


def detect_chrome_path() -> str:
    """Find a Chrome/Chromium executable on Linux or Windows."""
    if IS_WINDOWS:
        bases = [
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        candidates = [
            os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
            for base in bases
            if base
        ]
    else:
        candidates = [
            "/opt/google/chrome/chrome",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
            "/snap/bin/chromium",
        ]
    return next((path for path in candidates if Path(path).is_file()), "chrome.exe" if IS_WINDOWS else "google-chrome")


def default_user_data_dir() -> Path:
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "Google" / "Chrome" / "User Data"
        return Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
    return Path.home() / ".config" / "google-chrome"


def deep_update(target: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    for key, value in updates.items():
        if isinstance(value, dict):
            target[key] = deep_update(target.get(key, {}), value)
        else:
            target[key] = value
    return target


def ensure_chrome_preferences(user_data_dir: Path) -> None:
    """Apply the essential Chrome preferences used by the original launcher."""
    preferences_path = user_data_dir / "Default" / "Preferences"
    try:
        preferences = json.loads(preferences_path.read_text(encoding="utf-8")) if preferences_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        preferences = {}
    settings = {
        "plugins": {"always_open_pdf_externally": True},
        "download": {
            "default_directory": str(Path.home() / "Downloads"),
            "prompt_for_download": False,
            "directory_upgrade": True,
        },
        "browser": {"check_default_browser": False},
        "profile": {"exit_type": "Normal"},
    }
    preferences_path.parent.mkdir(parents=True, exist_ok=True)
    preferences_path.write_text(
        json.dumps(deep_update(preferences, settings), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def launch_chrome(use_user_config: bool = False, headless: bool = False) -> subprocess.Popen[Any]:
    """Launch Chrome with a CDP endpoint, using an isolated profile by default."""
    user_data_dir = default_user_data_dir() if use_user_config else Path(tempfile.mkdtemp(prefix="science_direct_chrome_"))
    ensure_chrome_preferences(user_data_dir)
    command = [
        detect_chrome_path(),
        f"--remote-debugging-port={CHROME_DEBUG_PORT}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--disable-default-apps",
        "--disable-sync",
        "--disable-extensions",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--disable-background-networking",
    ]
    if headless:
        command.append("--headless=new")
    process_options: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if IS_WINDOWS:
        process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        process_options["start_new_session"] = True
    process = subprocess.Popen(command, **process_options)
    print(f"Chrome 已启动 (PID: {process.pid}, CDP: {CHROME_DEBUG_PORT})", file=sys.stderr)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if chrome_is_running(port=CHROME_DEBUG_PORT):
            return process
        if process.poll() is not None:
            raise RuntimeError(f"Chrome 启动失败，退出码: {process.returncode}")
        time.sleep(0.25)
    raise TimeoutError(f"Chrome 已启动，但调试端口 {CHROME_DEBUG_PORT} 未就绪")


def chrome_is_running(host: str = "127.0.0.1", port: int = 9222) -> bool:
    """Return whether a Chrome DevTools endpoint is already listening."""
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_doi(value: str | None) -> str:
    value = clean_text(value)
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", value, flags=re.I)
    return value.strip().rstrip(".,;")


async def wait_for_articles(page: Any, timeout_ms: int) -> None:
    """Wait for and reveal lazy-loaded article cards."""
    await page.wait_for_selector('a[href*="/science/article/pii/"]', timeout=timeout_ms)
    previous = -1
    stable_rounds = 0
    for _ in range(30):
        count = await page.locator('a[href*="/science/article/pii/"]').count()
        stable_rounds = stable_rounds + 1 if count == previous else 0
        if stable_rounds >= 3:
            break
        previous = count
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(700)
    await page.evaluate("window.scrollTo(0, 0)")


async def extract_issue_metadata(page: Any) -> dict[str, Any]:
    """Extract issue fields and de-duplicated article cards from rendered DOM."""
    return await page.evaluate(
        r"""() => {
          const text = (el) => el ? el.textContent.replace(/\s+/g, ' ').trim() : '';
          const first = (selectors, root = document) => {
            for (const selector of selectors) {
              const el = root.querySelector(selector);
              if (el && text(el)) return el;
            }
            return null;
          };
          const journal = text(first([
            'h1.js-title', 'h1.publication-title', '.publication-title-link',
            'header h1', 'h1'
          ]));
          const issueHeading = text(first(['h2.js-vol-issue', 'h2.issue-info-heading']));
          const issueStatus = text(first(['div.js-issue-status', '.js-issue-status']));
          const labels = [...document.querySelectorAll('span.checkbox-label-value')];
          const doiPattern = /10\.\d{4,9}\/[A-Z0-9._;()/:+-]+/i;
          const findArticleCard = (label) => {
            let node = label.parentElement;
            while (node && node !== document.body) {
              const hasArticleLink = node.querySelector('a[href*="/science/article/pii/"]');
              const hasHiddenDoi = [...node.querySelectorAll('[hidden]')]
                .some(el => /^https?:\/\/(?:dx\.)?doi\.org\/10\./i.test(text(el)));
              if (hasArticleLink && hasHiddenDoi) return node;
              node = node.parentElement;
            }
            return label.closest('.js-article-list-item, .article-item, article, li');
          };
          const findArticleDoi = (card) => {
            // ScienceDirect 把论文自身 DOI 放在 article 板块的隐藏 div 中。
            // 只读取这一结构，避免误取 Appendix、数据集或代码仓库的 DOI。
            for (const el of card?.querySelectorAll('[hidden]') || []) {
              const value = text(el);
              if (/^https?:\/\/(?:dx\.)?doi\.org\/10\./i.test(value)) {
                return value.match(doiPattern)?.[0] || '';
              }
            }
            return '';
          };
          const seenArticles = new Set();
          const articles = [];
          for (const label of labels) {
            const card = findArticleCard(label);
            if (!card) continue;
            const title = text(label).replace(/^select article\s+/i, '').trim();
            if (!title) continue;
            const authorEl = first([
              '.author-group', '.Authors', '.authors', '.js-article-author-list',
              '[class*="author"]'
            ], card);
            const accessEl = first([
              '.js-open-access', '[class*="open-access"]',
              '.js-access-indicator', '.access-indicator-yes'
            ], card);
            const articleLinkEl = card.querySelector(
              'h3 a[href*="/science/article/pii/"], '
              + 'a.article-content-title[href*="/science/article/pii/"], '
              + 'a[href*="/science/article/pii/"]'
            );
            let link = '';
            if (articleLinkEl) {
              const parsedLink = new URL(articleLinkEl.href, location.href);
              link = parsedLink.origin + parsedLink.pathname;
            }
            const doi = findArticleDoi(card);
            // 页面源码会重复包含同一篇文章；按 DOI（没有 DOI 时按标题）
            // 保留 DOM 中第一次出现的记录。
            const articleKey = doi
              ? `doi:${doi.toLowerCase()}`
              : `title:${title.replace(/\s+/g, ' ').trim().toLowerCase()}`;
            if (seenArticles.has(articleKey)) continue;
            seenArticles.add(articleKey);
            articles.push({
              title,
              authors: text(authorEl).replace(/^Authors?\s*:?\s*/i, ''),
              doi,
              access: text(accessEl),
              link
            });
          }
          return {journal, issueHeading, issueStatus, articles};
        }"""
    )


async def fetch(args: argparse.Namespace) -> dict[str, Any]:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise SystemExit(
            "缺少 Playwright。请使用 Download_paper/.venv/bin/python 运行，"
            "或执行: pip install playwright"
        ) from exc

    url = f"https://www.sciencedirect.com/journal/{args.journal_name}/vol/{args.vol}"
    if args.issue:
        url += f"/issue/{args.issue}"
    if args.part:
        url += f"/part/{args.part}"
    if args.suppl:
        url += f"/suppl/{args.suppl}"
    referer = f"https://www.sciencedirect.com/journal/{args.journal_name}"
    chrome_process = None
    if not chrome_is_running():
        chrome_process = launch_chrome(use_user_config=args.user_config, headless=args.headless)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp("http://127.0.0.1:9222")
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()
        final_url = url
        try:
            print(f"打开: {url}", file=sys.stderr)
            await page.goto(
                url,
                referer=referer,
                wait_until="domcontentloaded",
                timeout=args.timeout * 1000,
            )
            await wait_for_articles(page, args.timeout * 1000)
            final_url = page.url
            if final_url.rstrip("/") != url.rstrip("/"):
                print(f"页面已跳转到: {final_url}", file=sys.stderr)
            raw = await extract_issue_metadata(page)
            if not raw["articles"]:
                raise RuntimeError("页面已打开，但没有找到文章；请检查验证码或页面结构是否变化")
        finally:
            await page.close()
            # Do not call browser.close(): with a CDP connection that would also
            # close a Chrome instance which may have existed before this run.

    volume_match = re.search(r"\b(?:volume|vol\.?)\s*(\d+)\b", raw["issueHeading"], re.I)
    issue_match = re.search(r"/issue/([^/?#]+)", final_url, re.I)
    part_match = re.search(r"/part/([^/?#]+)", final_url, re.I)
    suppl_match = re.search(r"/suppl/([^/?#]+)", final_url, re.I)
    resolved_issue = issue_match.group(1) if issue_match else ""
    resolved_part = part_match.group(1) if part_match else ""
    resolved_suppl = suppl_match.group(1) if suppl_match else ""
    result = {
        "journal-title": raw["journal"] or args.journal_name.replace("-", " ").title(),
        "vol": volume_match.group(1) if volume_match else str(args.vol),
        **({"issue": resolved_issue or None} if args.issue else {}),
        **({"part": resolved_part or None} if resolved_part or args.part else {}),
        **({"suppl": resolved_suppl or None} if resolved_suppl or args.suppl else {}),
        "year": raw["issueStatus"],
        "url": final_url,
        "article": [
            {
                "title": clean_text(item["title"]),
                "authors": clean_text(item.get("authors")),
                "doi": normalize_doi(item.get("doi")),
                "access": clean_text(item.get("access")),
                "link": clean_text(item.get("link")),
            }
            for item in raw["articles"]
        ],
    }
    # Keep a reference until collection is complete; Chrome intentionally remains open.
    _ = chrome_process
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抓取 ScienceDirect 某一期卷的所有文章信息")
    parser.add_argument(
        "journal_name",
        nargs="?",
        default="computer-physics-communications",
        help="期刊 URL 名（默认：computer-physics-communications）",
    )
    parser.add_argument("--vol", default="300", help="卷号（默认：300）")
    parser.add_argument("--issue", default="", help="期号（默认：空，不在 URL 中添加 issue）")
    parser.add_argument("--part", default="", help="分册字符串，如 P1、PB（默认：空，不添加 part）")
    parser.add_argument("--suppl", default="", help="增刊字符串，如 S1（默认：空，不添加 suppl）")
    parser.add_argument("-o", "--output", type=Path, help="输出 JSON 路径")
    parser.add_argument("--headless", action="store_true", help="以无头模式启动 Chrome")
    parser.add_argument("--user-config", action="store_true", help="使用现有 Chrome 用户配置")
    parser.add_argument("--timeout", type=int, default=60, help="每个页面的超时秒数（默认 60）")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = asyncio.run(fetch(args))
    actual_issue = result.get("issue", "")
    actual_part = result.get("part", "")
    actual_suppl = result.get("suppl", "")
    issue_suffix = f"_issue_{actual_issue}" if actual_issue else ""
    part_suffix = f"_part_{actual_part}" if actual_part else ""
    suppl_suffix = f"_suppl_{actual_suppl}" if actual_suppl else ""
    output = args.output or HERE / (
        f"{args.journal_name}_vol_{result['vol']}"
        f"{issue_suffix}{part_suffix}{suppl_suffix}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
    print(f"完成：抓取 {len(result['article'])} 篇文章，已写入 {output}")


if __name__ == "__main__":
    main()
