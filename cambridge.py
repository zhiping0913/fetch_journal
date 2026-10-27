#!/usr/bin/env python3
"""Fetch a Cambridge Core journal issue/continuous volume with headless Chrome."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


DEFAULT_CHROME_CANDIDATES = (
    Path("/opt/google/chrome/chrome"),
    Path("/usr/bin/google-chrome"),
    Path("/usr/bin/chromium"),
)


EXTRACT_ISSUE_SCRIPT = r"""
() => {
  const cleanText = (value) => String(value == null ? '' : value)
    .replace(/\s+/g, ' ')
    .replace(/\s+([,.;:])/g, '$1')
    .trim();

  const documentTitle = cleanText(document.title);
  const journalTitle = cleanText(
    document.querySelector('meta[name="citation_journal_title"]')?.content
    || document.querySelector('h1')?.textContent
  );
  const headingTexts = [...document.querySelectorAll('h1, h2, h3, h4')]
    .map(node => cleanText(node.textContent))
    .filter(Boolean);
  const volumeTitle = headingTexts
    .filter(value => /^Volume\b/i.test(value))
    .sort((left, right) => right.length - left.length)[0] || '';
  const seen = new Set();
  const articles = [];

  for (const card of document.querySelectorAll('div.representation.overview.search')) {
    const titleLink = card.querySelector('li.title h3 a.part-link, li.title a.part-link');
    const title = cleanText(titleLink?.textContent);
    if (!title || !titleLink) continue;

    const linkUrl = new URL(titleLink.getAttribute('href') || '', location.href);
    linkUrl.search = '';
    linkUrl.hash = '';
    const link = linkUrl.href;

    const doiNode = card.querySelector('[data-doi^="10."]');
    const doi = cleanText(doiNode?.getAttribute('data-doi'));
    const key = (doi || link || title).toLowerCase();
    if (seen.has(key)) continue;

    const authorNode = card.querySelector('li.author');
    const abstractNode = card.querySelector(
      'div.abstract[data-abstract-type="normal"], div.abstract'
    );
    const openAccessNode = card.querySelector('li.status.open-access');

    seen.add(key);
    articles.push({
      title,
      authors: cleanText(authorNode?.textContent),
      doi,
      link,
      abstract: cleanText(abstractNode?.textContent),
      access: openAccessNode ? 'Open access' : null
    });
  }

  return {
    documentTitle,
    journalTitle,
    volumeTitle,
    finalUrl: location.href,
    articles
  };
}
"""


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch a Cambridge Core journal issue or continuous volume using headless Chrome."
    )
    parser.add_argument(
        "--url",
        required=True,
        help="Cambridge Core journal issue or continuous-volume URL.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output JSON path. Default: sanitized HTML document title + .json.",
    )
    parser.add_argument(
        "--timeout",
        type=positive_int,
        default=90,
        help="Navigation/selector timeout in seconds. Default: 90.",
    )
    parser.add_argument(
        "--wait",
        type=positive_int,
        default=3,
        help="Extra wait after article cards appear, in seconds. Default: 3.",
    )
    parser.add_argument(
        "--chrome",
        type=Path,
        help="Chrome/Chromium executable. Default: auto-detect.",
    )
    return parser.parse_args()


def journal_slug_from_url(url: str) -> str:
    path = unquote(urlparse(url).path)
    match = re.search(r"/core/journals/([^/]+)/(?:issue|volume)/", path, re.I)
    if not match:
        raise ValueError(
            "URL must match /core/journals/<journal-name>/(issue|volume)/<id>"
        )
    return match.group(1)


def find_chrome(explicit: Path | None) -> Path:
    if explicit:
        if not explicit.is_file():
            raise FileNotFoundError(f"Chrome executable does not exist: {explicit}")
        return explicit
    for candidate in DEFAULT_CHROME_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Could not find Google Chrome or Chromium")


def clean_filename_component(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", value)
    return re.sub(r"\s+", " ", value).strip()


async def fetch_issue(
    url: str,
    *,
    chrome: Path,
    timeout_seconds: int,
    wait_seconds: int,
) -> dict[str, Any]:
    try:
        from playwright.async_api import async_playwright
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing Playwright; run with /home/zhiping/miniconda3/envs/dev/bin/python"
        ) from exc

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=str(chrome),
            headless=True,
            args=["--disable-gpu", "--no-sandbox"],
        )
        page = await browser.new_page()
        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=timeout_seconds * 1000,
            )
            if response and response.status >= 400:
                raise RuntimeError(f"Cambridge returned HTTP {response.status}")
            await page.wait_for_selector(
                "div.representation.overview.search",
                timeout=timeout_seconds * 1000,
            )
            await page.wait_for_timeout(wait_seconds * 1000)
            extracted = await page.evaluate(EXTRACT_ISSUE_SCRIPT)
        finally:
            await browser.close()

    if not extracted["articles"]:
        raise RuntimeError("No article cards found; the page may be blocked")

    journal_slug = journal_slug_from_url(extracted["finalUrl"])
    return {
        "journal-title": extracted["journalTitle"] or journal_slug.replace("-", " ").title(),
        "document-title": extracted["documentTitle"],
        "volume-title": extracted["volumeTitle"],
        "url": extracted["finalUrl"],
        "article": extracted["articles"],
    }


def output_path(data: dict[str, Any], requested_url: str, explicit: Path | None) -> Path:
    if explicit:
        return explicit
    document_title = clean_filename_component(data["document-title"])
    if not document_title:
        document_title = journal_slug_from_url(data.get("url") or requested_url)
    return Path(f"{document_title}.json")


async def main() -> int:
    args = parse_args()
    try:
        journal_slug_from_url(args.url)
        data = await fetch_issue(
            args.url,
            chrome=find_chrome(args.chrome),
            timeout_seconds=args.timeout,
            wait_seconds=args.wait,
        )
        target = output_path(data, args.url, args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(data, ensure_ascii=False, indent=4) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"cambridge.py: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {target}", file=sys.stderr)
    print(f"Article count: {len(data['article'])}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
