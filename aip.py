#!/home/zhiping/research-env/bin/python
"""Fetch an AIP issue table of contents and emit JSON metadata."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any


AIP_ISSUE_URL = "https://pubs.aip.org/aip/{journal}/issue/{volume}/{issue}"
DEFAULT_OUTPUT = "{journal}_{volume}_{issue}.json"


EXTRACT_ISSUE_SCRIPT = r"""
({ journal, volume, issue, requestedUrl }) => {
  const monthPattern = String.raw`(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}`;
  const doiPattern = /10\.\d{4,9}\/[-._;()/:A-Z0-9]+/i;

  function cleanText(value) {
    return String(value == null ? "" : value)
      .replace(/\s+/g, " ")
      .replace(/\s+([,.;:])/g, "$1")
      .trim();
  }

  function metaValue(names) {
    for (const name of names) {
      const selector = [
        `meta[name="${name}"]`,
        `meta[property="${name}"]`,
        `meta[itemprop="${name}"]`
      ].join(",");
      const node = document.querySelector(selector);
      const value = node && cleanText(node.getAttribute("content"));
      if (value) return value;
    }
    return "";
  }

  function journalTitleFromDocumentTitle() {
    const parts = document.title.split("|").map(cleanText).filter(Boolean);
    if (parts.length >= 2) return parts[1];
    return "";
  }

  function monthYearFromDate(value) {
    const match = cleanText(value).match(/^(\d{4})-(\d{2})-\d{2}/);
    if (!match) return "";

    const months = [
      "January", "February", "March", "April", "May", "June",
      "July", "August", "September", "October", "November", "December"
    ];
    const month = months[Number(match[2]) - 1];
    return month ? `${month} ${match[1]}` : "";
  }

  function firstText(selectors) {
    for (const selector of selectors) {
      const node = document.querySelector(selector);
      const value = node && cleanText(node.textContent);
      if (value) return value;
    }
    return "";
  }

  function absoluteUrl(value) {
    const href = cleanText(value);
    if (!href) return "";
    try {
      return new URL(href, requestedUrl).href;
    } catch {
      return "";
    }
  }

  function accessMapFromPage() {
    const map = new Map();
    const icons = Array.isArray(window.accessIcons) ? window.accessIcons : [];
    for (const item of icons) {
      const id = cleanText(item && item.id);
      const access = cleanText(item && item.title);
      if (id && access) map.set(id, access);
    }
    return map;
  }

  function nearestContainer(node) {
    if (!node) return null;
    const start = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
    if (!start) return null;

    const selectors = [
      "article",
      ".issue-item",
      ".issue-item__content",
      ".tocArticle",
      ".article-item",
      ".al-article-item-wrap",
      ".al-article-items",
      ".al-article-item",
      ".articleEntry",
      ".widget-ArticleListItem",
      "li",
      ".card",
      ".item"
    ].join(",");

    return start.closest(selectors) || start.parentElement;
  }

  function articleTitleNode(container) {
    if (!container) return null;

    const selectors = [
      ".issue-item__title a",
      ".item-title a",
      ".article-title a",
      ".wi-article-title a",
      ".al-title a",
      "h2 a",
      "h3 a",
      "h4 a",
      "h5 a",
      "a[href*='/article/']:not(.viewArticleLink):not(.article-pdfLink)"
    ];

    for (const selector of selectors) {
      const node = container.querySelector(selector);
      const value = node && cleanText(node.textContent);
      if (value && !doiPattern.test(value) && !/^View article/i.test(value) && !/^Open the PDF/i.test(value)) {
        return node;
      }
    }
    return null;
  }

  function extractTitle(container) {
    if (!container) return "";

    const titleNode = articleTitleNode(container);
    if (titleNode) return cleanText(titleNode.textContent);

    const titleSelectors = [
      ".issue-item__title a",
      ".issue-item__title",
      ".item-title a",
      ".item-title",
      ".article-title a",
      ".article-title",
      ".wi-article-title a",
      ".wi-article-title",
      ".al-title a",
      ".al-title",
      "h2 a",
      "h2",
      "h3 a",
      "h3",
      "h4 a",
      "h4",
      "h5 a",
      "h5",
      "[class*='title'] a",
      "[class*='title']"
    ];

    for (const selector of titleSelectors) {
      const node = container.querySelector(selector);
      const value = node && cleanText(node.textContent);
      if (value && !doiPattern.test(value)) return value;
    }

    const articleLink = Array.from(container.querySelectorAll(
      "a[href*='/article/']:not(.viewArticleLink):not(.article-pdfLink), a[href*='/doi/']"
    ))
      .map((node) => cleanText(node.textContent))
      .find((value) =>
        value &&
        value.length > 8 &&
        !doiPattern.test(value) &&
        !/^View article/i.test(value) &&
        !/^Open the PDF/i.test(value)
      );
    return articleLink || "";
  }

  function extractLink(container) {
    const titleNode = articleTitleNode(container);
    if (titleNode) return absoluteUrl(titleNode.getAttribute("href"));

    const node = container && container.querySelector(
      "a[href*='/article/']:not(.viewArticleLink):not(.article-pdfLink)"
    );
    return node ? absoluteUrl(node.getAttribute("href")) : "";
  }

  function extractId(container) {
    if (!container) return "";

    const idNode = container.querySelector("[data-resource-id-access]");
    const id = idNode && cleanText(idNode.getAttribute("data-resource-id-access"));
    if (id) return id;

    const candidates = [
      container.querySelector("[data-resourceid]"),
      container.querySelector("[data-resource-id]"),
      container.querySelector("a[id^='aria']")
    ];
    for (const node of candidates) {
      if (!node) continue;
      const value = cleanText(
        node.getAttribute("data-resourceid") ||
        node.getAttribute("data-resource-id") ||
        node.getAttribute("id")
      );
      const match = value.match(/\d+/);
      if (match) return match[0];
    }

    const articleNode = container.querySelector("a[href*='/article/']");
    const linkMatch = articleNode && cleanText(articleNode.getAttribute("href")).match(/\/(\d+)(?:\/[^/]+)?$/);
    return linkMatch ? linkMatch[1] : "";
  }

  function extractAccess(container, id, accessById) {
    if (id && accessById.has(id)) return accessById.get(id);

    const node = container && container.querySelector(
      "[title*='Access'],[title='Available'],[class*='availability']"
    );
    return node ? cleanText(node.getAttribute("title") || node.textContent) : "";
  }

  function extractDoiFromNode(node) {
    const dataDoi = node.getAttribute && cleanText(node.getAttribute("data-doi"));
    if (dataDoi && doiPattern.test(dataDoi)) return dataDoi.match(doiPattern)[0];

    const textDoi = cleanText(node.textContent || "");
    const textMatch = textDoi.match(/(?:https?:\/\/(?:dx\.)?doi\.org\/)?(10\.\d{4,9}\/[-._;()/:A-Z0-9]+)$/i);
    if (textMatch) return textMatch[1].replace(/[).,;]+$/, "");

    const candidates = [
      node.getAttribute && node.getAttribute("content"),
      node.getAttribute && node.getAttribute("href")
    ];

    for (const candidate of candidates) {
      const value = String(candidate || "");
      const doiUrlMatch = value.match(/(?:https?:\/\/(?:dx\.)?doi\.org\/)(10\.\d{4,9}\/[^?#\s]+)/i);
      if (doiUrlMatch) return doiUrlMatch[1].replace(/[).,;]+$/, "");

      const aipPathMatch = value.match(/\/doi\/(10\.\d{4,9}\/[^/?#\s]+)/i);
      if (aipPathMatch) return aipPathMatch[1].replace(/[).,;]+$/, "");

      const match = value.match(doiPattern);
      if (match) return match[0].replace(/[).,;]+$/, "");
    }
    return "";
  }

  const doiNodes = [
    ...document.querySelectorAll(".citation-label"),
    ...document.querySelectorAll("[data-doi]"),
    ...document.querySelectorAll("a[href*='doi.org/10.'], a[href*='/doi/10.']")
  ];

  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    if (doiPattern.test(walker.currentNode.nodeValue || "")) {
      doiNodes.push(walker.currentNode.parentElement);
    }
  }

  const seen = new Set();
  const articles = [];
  const accessById = accessMapFromPage();
  for (const node of doiNodes) {
    const doi = extractDoiFromNode(node);
    if (!doi || seen.has(doi.toLowerCase())) continue;

    const container = nearestContainer(node);
    const title = extractTitle(container);
    if (!title) continue;
    const link = extractLink(container);
    const id = extractId(container);
    const access = extractAccess(container, id, accessById);

    seen.add(doi.toLowerCase());
    articles.push({ title, doi, link, id, access });
  }

  let journalTitle = metaValue([
    "citation_journal_title",
    "dc.Source",
    "prism.publicationName",
    "og:site_name"
  ]);
  if (!journalTitle || /^AIP Publishing$/i.test(journalTitle)) {
    journalTitle = firstText([
      ".journal-title",
      ".journalHeader .title",
      ".journal-header__title",
      "a[href*='/aip/" + journal + "']"
    ]) || journalTitleFromDocumentTitle() || journalTitle;
  }
  journalTitle = cleanText(journalTitle);

  let pubDate = metaValue([
    "citation_publication_date",
    "dc.Date",
    "prism.publicationDate",
    "article:published_time"
  ]);
  pubDate = monthYearFromDate(pubDate) || pubDate;
  const monthMatch = cleanText(pubDate).match(new RegExp(monthPattern, "i"));
  if (monthMatch) {
    pubDate = monthMatch[0];
  } else {
    const headingText = Array.from(document.querySelectorAll("h1,h2,h3,.issue-date,.cover-date,.toc-issue__date"))
      .map((node) => cleanText(node.textContent))
      .join(" ");
    const headingMatch = headingText.match(new RegExp(monthPattern, "i"));
    const bodyMatch = cleanText(document.body.textContent).match(new RegExp(monthPattern, "i"));
    pubDate = headingMatch ? headingMatch[0] : (bodyMatch ? bodyMatch[0] : cleanText(pubDate));
  }

  return {
    "journal-title": journalTitle,
    "volume": String(volume),
    "issue": String(issue),
    "url": requestedUrl,
    "pub-date": pubDate,
    "article": articles
  };
}
"""


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError("must be true or false")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch an AIP issue page with a headless browser and emit JSON."
    )
    parser.add_argument(
        "journal",
        nargs="?",
        default="pop",
        help="AIP journal abbreviation. Default: pop.",
    )
    parser.add_argument(
        "volume",
        nargs="?",
        default="32",
        help="Volume number. Default: 32.",
    )
    parser.add_argument(
        "issue",
        nargs="?",
        default="12",
        help="Issue number. Default: 12.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path(DEFAULT_OUTPUT),
        help="Write JSON to this file. Default: {journal}_{volume}_{issue}.json.",
    )
    parser.add_argument(
        "--timeout",
        type=positive_int,
        default=45,
        help="Navigation timeout in seconds. Default: 45.",
    )
    parser.add_argument(
        "--show-browser",
        action="store_true",
        help="Run Chromium visibly instead of headless.",
    )
    parser.add_argument(
        "--headless",
        type=parse_bool,
        default=True,
        help="Run Chromium in headless mode. Use '--headless False' for headed mode. Default: True.",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Write compact JSON instead of pretty-printed JSON.",
    )
    return parser.parse_args()


async def fetch_issue(
    journal: str,
    volume: str,
    issue: str,
    *,
    timeout_seconds: int = 45,
    headless: bool = True,
) -> dict[str, Any]:
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing dependency: playwright. Install it with:\n"
            "  python3 -m pip install playwright\n"
            "  python3 -m playwright install chromium"
        ) from exc

    url = AIP_ISSUE_URL.format(journal=journal, volume=volume, issue=issue)
    timeout_ms = timeout_seconds * 1000

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        try:
            page = await browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
            )
            page.set_default_timeout(timeout_ms)
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 15000))
            except PlaywrightTimeoutError:
                pass

            # Trigger lazy-loaded issue entries before reading the DOM.
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1000)

            data = await page.evaluate(
                EXTRACT_ISSUE_SCRIPT,
                {
                    "journal": journal,
                    "volume": volume,
                    "issue": issue,
                    "requestedUrl": url,
                },
            )
        finally:
            await browser.close()

    return data


def write_json(data: dict[str, Any], output: Path | None, compact: bool) -> None:
    if compact:
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(data, ensure_ascii=False, indent=4)

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


async def main() -> int:
    args = parse_args()
    journal = args.journal.strip().lower()
    volume = args.volume.strip()
    issue = args.issue.strip()
    output = Path(
        str(args.output).format(journal=journal, volume=volume, issue=issue)
    )

    try:
        data = await fetch_issue(
            journal,
            volume,
            issue,
            timeout_seconds=args.timeout,
            headless=args.headless and not args.show_browser,
        )
    except Exception as exc:
        print(f"aip.py: {exc}", file=sys.stderr)
        return 1

    write_json(data, output, args.compact)
    print(f"Wrote {output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
