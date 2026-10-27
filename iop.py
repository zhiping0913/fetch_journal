#!/home/zhiping/research-env/bin/python
"""Fetch an IOPScience issue through an existing Chrome CDP session."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen


DEFAULT_URL = "https://iopscience.iop.org/issue/0741-3335/60/2"
DEFAULT_CDP_URL = "http://127.0.0.1:9022"
DEFAULT_CHROME = Path("/usr/bin/google-chrome")
DEFAULT_CHROME_USER_DATA_DIR = Path("/tmp/chrome-9022")


EXTRACT_ISSUE_SCRIPT = r"""
({ requestedUrl }) => {
  function cleanText(value) {
    return String(value == null ? "" : value)
      .replace(/\s+/g, " ")
      .replace(/\s+([,.;:])/g, "$1")
      .trim();
  }

  function cleanLines(value) {
    return String(value == null ? "" : value)
      .split(/\n+/)
      .map(cleanText)
      .filter(Boolean);
  }

  function articleDoiFromHref(href) {
    try {
      const url = new URL(href, location.href);
      const match = url.pathname.match(/\/article\/(10\.\d{4,9}\/.+?)(?:\/(?:meta|pdf|references|abstract))?$/i);
      if (!match) return "";
      return decodeURIComponent(match[1]).replace(/[?#].*$/, "").replace(/\/$/, "");
    } catch {
      return "";
    }
  }

  function articleContainer(link, title) {
    let node = link;
    let best = link.parentElement || link;
    while (node && node !== document.body) {
      const text = node.innerText || node.textContent || "";
      const cleaned = cleanText(text);
      if (cleaned.includes(title) && cleaned.length < 3500) {
        best = node;
      }
      node = node.parentElement;
    }
    return best;
  }

  function articleNumberFromContainer(container, title) {
    const lines = cleanLines(container && (container.innerText || container.textContent));
    const titleIndex = lines.findIndex((line) => line === title || line.includes(title));
    const start = titleIndex >= 0 ? Math.max(0, titleIndex - 8) : 0;
    const end = titleIndex >= 0 ? titleIndex : Math.min(lines.length, 8);

    for (let i = end - 1; i >= start; i -= 1) {
      const match = lines[i].match(/^\d{5,6}$/);
      if (match) return match[0];
    }

    const compactText = cleanText(container && (container.innerText || container.textContent));
    const escapedTitle = title.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const beforeTitle = compactText.match(new RegExp("(\\d{5,6})\\s+" + escapedTitle));
    return beforeTitle ? beforeTitle[1] : "";
  }

  function accessFromCollectionLabels(container) {
    if (!container) return "";

    const walker = document.createTreeWalker(
      container,
      NodeFilter.SHOW_COMMENT | NodeFilter.SHOW_TEXT
    );
    const parts = [];
    let collecting = false;

    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (node.nodeType === Node.COMMENT_NODE) {
        const value = cleanText(node.nodeValue);
        if (/Start Collection Labels/i.test(value)) {
          collecting = true;
          continue;
        }
        if (/End Collection Labels/i.test(value)) {
          break;
        }
      }

      if (collecting && node.nodeType === Node.TEXT_NODE) {
        const value = cleanText(node.nodeValue);
        if (value) parts.push(value);
      }
    }

    return cleanText(parts.join(" "));
  }

  const seen = new Set();
  const articles = [];
  const links = Array.from(document.querySelectorAll("a[href*='/article/10.']"));

  for (const linkNode of links) {
    const href = linkNode.getAttribute("href") || "";
    if (/\/(?:meta|pdf|references|abstract)(?:[?#].*)?$/i.test(href)) continue;

    const title = cleanText(linkNode.innerText || linkNode.textContent);
    if (!title || /^View article/i.test(title) || /^PDF/i.test(title) || /^Open abstract/i.test(title)) {
      continue;
    }

    const link = new URL(href, location.href).href;
    const doi = articleDoiFromHref(link);
    if (!doi || seen.has(doi.toLowerCase())) continue;

    const container = articleContainer(linkNode, title);
    const articleNumber = articleNumberFromContainer(container, title);
    const access = accessFromCollectionLabels(container);

    seen.add(doi.toLowerCase());
    articles.push({
      "title": title,
      "doi": doi,
      "link": link,
      "article_number": articleNumber,
      "access": access
    });
  }

  return {
    "documentTitle": cleanText(document.title),
    "url": requestedUrl,
    "article": articles
  };
}
"""


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def output_name_from_title(title: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return f"{cleaned or 'iop_issue'}.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch an IOPScience issue through Chrome CDP and emit JSON."
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"IOPScience issue URL. Default: {DEFAULT_URL}.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output JSON path. Default: sanitized documentTitle + .json.",
    )
    parser.add_argument(
        "--cdp-url",
        default=DEFAULT_CDP_URL,
        help=f"Chrome DevTools endpoint. Default: {DEFAULT_CDP_URL}.",
    )
    parser.add_argument(
        "--wait",
        type=positive_int,
        default=120,
        help="Seconds to wait for manual captcha/network loading before extraction. Default: 120.",
    )
    parser.add_argument(
        "--goto-timeout",
        type=positive_int,
        default=15,
        help="Initial navigation timeout in seconds. Default: 15.",
    )
    parser.add_argument(
        "--chrome",
        type=Path,
        default=DEFAULT_CHROME,
        help=f"Chrome executable used when CDP is not already running. Default: {DEFAULT_CHROME}.",
    )
    parser.add_argument(
        "--user-data-dir",
        type=Path,
        default=DEFAULT_CHROME_USER_DATA_DIR,
        help=f"Chrome profile directory used when auto-starting Chrome. Default: {DEFAULT_CHROME_USER_DATA_DIR}.",
    )
    return parser.parse_args()


def cdp_port(cdp_url: str) -> int:
    parsed = urlparse(cdp_url)
    if not parsed.port:
        raise ValueError(f"CDP URL must include a port: {cdp_url}")
    return parsed.port


def is_cdp_available(cdp_url: str) -> bool:
    try:
        with urlopen(f"{cdp_url.rstrip('/')}/json/version", timeout=1) as response:
            return response.status == 200
    except Exception:
        return False


async def wait_for_cdp(cdp_url: str, timeout_seconds: int = 10) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if is_cdp_available(cdp_url):
            return True
        await asyncio.sleep(0.5)
    return False


async def ensure_chrome_cdp(cdp_url: str, chrome: Path, user_data_dir: Path) -> subprocess.Popen[bytes] | None:
    if is_cdp_available(cdp_url):
        return None

    if not chrome.exists():
        raise RuntimeError(f"Chrome executable does not exist: {chrome}")

    port = cdp_port(cdp_url)
    user_data_dir.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [
            str(chrome),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not await wait_for_cdp(cdp_url):
        raise RuntimeError(f"Started Chrome but CDP did not become available: {cdp_url}")
    return process


async def fetch_issue(
    url: str,
    *,
    cdp_url: str,
    wait_seconds: int,
    goto_timeout: int,
    chrome: Path,
    user_data_dir: Path,
) -> dict[str, Any]:
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency: playwright in /home/zhiping/research-env") from exc

    async with async_playwright() as playwright:
        await ensure_chrome_cdp(cdp_url, chrome, user_data_dir)
        try:
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:
            raise RuntimeError(f"Cannot connect to Chrome CDP at {cdp_url}") from exc

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="commit", timeout=goto_timeout * 1000)
        except PlaywrightTimeoutError:
            print("Initial navigation timed out; waiting on the current Chrome tab.", file=sys.stderr)

        print(
            f"Opened {url} through {cdp_url}. Waiting {wait_seconds}s before extraction.",
            file=sys.stderr,
        )
        await page.wait_for_timeout(wait_seconds * 1000)
        data = await page.evaluate(EXTRACT_ISSUE_SCRIPT, {"requestedUrl": url})

    return data


def write_json(data: dict[str, Any], output: Path | None) -> Path:
    target = output or Path(output_name_from_title(data.get("documentTitle", "")))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
    return target


async def main() -> int:
    args = parse_args()
    try:
        data = await fetch_issue(
            args.url,
            cdp_url=args.cdp_url,
            wait_seconds=args.wait,
            goto_timeout=args.goto_timeout,
            chrome=args.chrome,
            user_data_dir=args.user_data_dir,
        )
    except Exception as exc:
        print(f"iop.py: {exc}", file=sys.stderr)
        return 1

    output = write_json(data, args.output)
    print(f"Wrote {output}", file=sys.stderr)
    print(f"Article count: {len(data.get('article', []))}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
