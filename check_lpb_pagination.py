#!/usr/bin/env python3
"""Check Cambridge pagination for LPB issues whose first page has 20 items."""

import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import async_playwright


URL_FILE = Path("/tmp/lpb_pagination_candidates.txt")
OUTPUT = Path("/tmp/lpb_pagination_audit.json")
CHROME = "/usr/bin/google-chrome"


async def main() -> None:
    urls = [line.strip() for line in URL_FILE.read_text().splitlines() if line.strip()]
    results = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=CHROME,
            headless=True,
            args=["--no-sandbox", "--disable-gpu"],
        )
        page = await browser.new_page()
        for index, url in enumerate(urls, 1):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                await page.wait_for_selector(
                    "div.representation.overview.search", timeout=90_000
                )
                hrefs = await page.locator('a[href*="pageNum="]').evaluate_all(
                    "elements => elements.map(element => element.href)"
                )
                page_numbers = []
                for href in hrefs:
                    match = re.search(r"[?&]pageNum=(\d+)", href)
                    if match:
                        page_numbers.append(int(match.group(1)))
                pages = max(page_numbers, default=1)
                results.append(
                    {"url": url, "pages": pages, "title": await page.title(), "error": None}
                )
                print(f"{index}/{len(urls)} pages={pages} {url}", flush=True)
            except Exception as exc:
                results.append(
                    {"url": url, "pages": None, "title": None, "error": str(exc)}
                )
                print(f"{index}/{len(urls)} ERROR {url}: {exc}", flush=True)
            await page.wait_for_timeout(750)
        await browser.close()

    report = {
        "checked": len(results),
        "multi_page_count": sum((item["pages"] or 0) > 1 for item in results),
        "single_page_exactly_20": sum(item["pages"] == 1 for item in results),
        "errors": sum(item["error"] is not None for item in results),
        "issues": results,
    }
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print("SUMMARY " + json.dumps({k: v for k, v in report.items() if k != "issues"}))


if __name__ == "__main__":
    asyncio.run(main())
