#!/usr/bin/env python3
"""
Scrape APS journal issue pages via a headed Chrome launched from the user's profile.

The script copies the local Chrome profile, starts Chrome with remote debugging,
uses CDP to grab the rendered HTML, then parses article metadata as JSON.

Usage:
    python aps_issue_cdp_scraper.py <journal> <volume> <issue> [--output file.json]

Example:
    python aps_issue_cdp_scraper.py prab 29 1
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urljoin, urlparse

import websockets
from bs4 import BeautifulSoup

DEFAULT_USER_PROFILE = os.path.expanduser("~/.config/google-chrome")


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def copy_profile(src: str, dst: str) -> None:
    """Copy a Chrome profile, skipping Singleton lock/socket files."""
    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.copytree(
        src,
        dst,
        ignore=lambda _src, names: [n for n in names if n.startswith("Singleton")],
    )


def wait_for_cdp(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/json/list"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def get_page_ws_url(port: int, page_url_hint: str) -> str:
    url = f"http://127.0.0.1:{port}/json/list"
    with urllib.request.urlopen(url, timeout=10) as resp:
        pages = json.loads(resp.read())
    for page in pages:
        if page_url_hint in page["url"]:
            return page["webSocketDebuggerUrl"]
    # Fallback: first non-background page
    for page in pages:
        if page["type"] == "page":
            return page["webSocketDebuggerUrl"]
    raise RuntimeError("No suitable CDP page found")


async def fetch_html_via_cdp(ws_url: str, target_url: str, load_timeout: float = 60.0, page_settle_time: float = 60.0) -> str:
    async def cdp_call(ws, method, params=None):
        req_id = cdp_call.counter
        cdp_call.counter += 1
        msg = {"id": req_id, "method": method}
        if params is not None:
            msg["params"] = params
        await ws.send(json.dumps(msg))
        deadline = asyncio.get_event_loop().time() + load_timeout
        while asyncio.get_event_loop().time() < deadline:
            resp = await asyncio.wait_for(ws.recv(), timeout=load_timeout)
            data = json.loads(resp)
            if data.get("id") == req_id:
                return data
        raise TimeoutError(f"CDP call {method} timed out")

    async def cdp_event(ws, event_name: str, timeout: float):
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            resp = await asyncio.wait_for(ws.recv(), timeout=timeout)
            data = json.loads(resp)
            if data.get("method") == event_name:
                return data
        raise TimeoutError(f"CDP event {event_name} timed out")

    async def get_title(ws) -> str:
        r = await cdp_call(ws, "Runtime.evaluate", {
            "expression": "document.title",
            "returnByValue": True,
        })
        try:
            return r["result"]["result"].get("value", "")
        except (KeyError, TypeError):
            return ""

    cdp_call.counter = 1

    async with websockets.connect(ws_url) as ws:
        await cdp_call(ws, "Runtime.enable")
        await cdp_call(ws, "Page.enable")
        await cdp_call(ws, "Page.navigate", {"url": target_url})
        await cdp_event(ws, "Page.loadEventFired", timeout=load_timeout)

        # If Cloudflare serves a challenge page, wait for it to resolve.
        cf_deadline = asyncio.get_event_loop().time() + load_timeout
        while asyncio.get_event_loop().time() < cf_deadline:
            title = await get_title(ws)
            if "Just a moment" not in title:
                break
            await asyncio.sleep(1)

        # Extra settle time so dynamic content / Cloudflare redirects finish.
        if page_settle_time > 0:
            await asyncio.sleep(page_settle_time)

        result = await cdp_call(ws, "Runtime.evaluate", {
            "expression": "document.documentElement.outerHTML",
            "returnByValue": True,
        })
        try:
            return result["result"]["result"]["value"]
        except (KeyError, TypeError):
            raise RuntimeError(f"Unexpected CDP response: {result}")


def doi_from_href(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        href = urlparse(href).path
    m = re.search(r"/abstract/(.+)", href)
    if m:
        return m.group(1).strip("/")
    return ""


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_issue(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    issue_date = ""
    label = soup.find("div", {"id": "issue-date-label"})
    if label:
        issue_date = normalize_text(label.get_text(" ", strip=True))

    seen = set()
    articles = []
    for card in soup.find_all("div", class_="browse-result-card issue"):
        title_tag = card.find(class_="title")
        if not title_tag:
            continue
        title_a = title_tag.find("a", href=True)
        if not title_a:
            continue

        title = normalize_text(title_a.get_text(" ", strip=True))
        rel_link = title_a["href"]
        doi = doi_from_href(rel_link)
        if not doi or doi in seen:
            continue
        seen.add(doi)

        authors = ""
        citation = card.find("section", class_="citation-container")
        if citation:
            ps = citation.find_all("p")
            if ps:
                authors = normalize_text(ps[0].get_text(" ", strip=True))

        abstract = ""
        summary = card.find("section", class_="summary-container")
        if summary:
            abs_div = summary.find("div", class_="self-stretch")
            if abs_div:
                p = abs_div.find("p")
                if p:
                    abstract = normalize_text(p.get_text(" ", strip=True))

        articles.append({
            "title": title,
            "authors": authors,
            "doi": doi,
            "link": urljoin(url, rel_link),
            "abstract": abstract,
        })

    # Extract volume/issue from URL if not provided
    m = re.search(r"/issues/(\d+)/(\d+)", url)
    volume = m.group(1) if m else ""
    issue = m.group(2) if m else ""

    return {
        "volume": volume,
        "issue": issue,
        "issue-date": issue_date,
        "url": url,
        "article": articles,
    }


def scrape_issue(journal: str, volume: str, issue: str, profile_dir: str, page_settle_time: float = 60.0) -> dict:
    url = f"https://journals.aps.org/{journal}/issues/{volume}/{issue}"

    tmp_profile = tempfile.mkdtemp(prefix="aps_chrome_profile_")
    copied_profile = os.path.join(tmp_profile, "profile")
    copy_profile(profile_dir, copied_profile)

    port = find_free_port()
    cmd = [
        "google-chrome",
        "--no-sandbox",
        f"--user-data-dir={copied_profile}",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    try:
        if not wait_for_cdp(port, timeout=30):
            raise RuntimeError("Chrome did not start CDP in time")

        ws_url = get_page_ws_url(port, "aps.org")
        html = asyncio.run(fetch_html_via_cdp(ws_url, target_url=url, load_timeout=60, page_settle_time=page_settle_time))
        return parse_issue(html, url)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        # Best-effort cleanup of the temporary profile.
        try:
            shutil.rmtree(tmp_profile)
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(
        description="Scrape APS journal issue pages using the local Chrome profile."
    )
    parser.add_argument("journal", help="Journal code, e.g. prab, prl, pra")
    parser.add_argument("volume", help="Volume number")
    parser.add_argument("issue", help="Issue number")
    parser.add_argument("--output", "-o", help="Output JSON file (default: stdout)")
    parser.add_argument(
        "--profile-dir",
        default=DEFAULT_USER_PROFILE,
        help="Path to the Chrome user profile to copy (default: ~/.config/google-chrome)",
    )
    parser.add_argument(
        "--page-settle-time",
        type=float,
        default=60.0,
        help="Seconds to wait after page load before reading HTML (default: 60)",
    )
    args = parser.parse_args()

    if not os.path.isdir(args.profile_dir):
        print(f"Chrome profile not found: {args.profile_dir}", file=sys.stderr)
        sys.exit(1)

    result = scrape_issue(
        args.journal,
        args.volume,
        args.issue,
        args.profile_dir,
        page_settle_time=args.page_settle_time,
    )

    article_count = len(result.get("article", []))
    json_str = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        if article_count == 0:
            print(f"No articles found; output not written: {args.output}")
            return
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(json_str + "\n")
        print(f"Wrote {article_count} articles to {args.output}")
    else:
        print(json_str)


if __name__ == "__main__":
    main()
