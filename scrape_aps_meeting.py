#!/usr/bin/env python3
"""
APS Meeting scraper (generic) v3
============================================================
Extracts sessions, presentations, authors (structured name+affiliation),
abstract, type using robust html.split() approach between h2 tags.

Usage:
    python3 scrape_dpp2025_v3.py --url https://meetings-archive.aps.org/dpp/2025
    python3 scrape_dpp2025_v3.py --url https://meetings-archive.aps.org/dpp/2024
    python3 scrape_dpp2025_v3.py --url https://meetings-archive.aps.org/dfd/2025
    python3 scrape_dpp2025_v3.py --url https://meetings-archive.aps.org/dpp/2025 --limit 5
"""
import json, re, time, os, argparse
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup, NavigableString


def convert_katex_to_latex(html: str) -> str:
    """Replace every ``<span class="katex">…</span>`` block with the raw LaTeX
    source pulled from its ``<annotation encoding="application/x-tex">``.

    KaTeX renders each formula as three nested spans:

        <span><span class="katex">
          <span class="katex-mathml">
            <math><semantics><mrow>…</mrow>
              <annotation encoding="application/x-tex">f_{s,1}</annotation>
            </semantics></math>
          </span>
          <span class="katex-html" aria-hidden="true">…visual layout…</span>
        </span></span>

    The annotation carries the original TeX, so we drop the whole katex span
    (including the huge katex-html visual layout siblings) and substitute
    ``$LATEX$``. The outer plain ``<span>`` wrapper from the scraper page
    then holds only that text and gets unwrapped for a clean abstract.
    """
    if not html or 'katex-mathml' not in html:
        return html

    soup = BeautifulSoup(html, 'html.parser')
    for katex in soup.find_all('span', class_='katex'):
        ann = katex.find('annotation', attrs={'encoding': 'application/x-tex'})
        latex = ann.get_text().strip() if ann else ''
        katex.replace_with(NavigableString(f'${latex}$' if latex else ''))

    # Unwrap the bare parent <span> (no attrs) that wrapped the katex block.
    for span in list(soup.find_all('span')):
        if span.attrs:
            continue
        if all(isinstance(c, NavigableString) for c in span.children):
            span.unwrap()

    return str(soup)


def main():
    parser = argparse.ArgumentParser(description="Scrape an APS meeting archive.")
    parser.add_argument('--url', required=True,
                        help='Base URL of the APS meeting archive (e.g. https://meetings-archive.aps.org/dpp/2025)')
    parser.add_argument('--limit', type=int, default=0,
                        help='Limit number of sessions to process (0 = no limit)')
    args = parser.parse_args()

    # Parse URL — extract domain, path, series, and year
    parsed = urlparse(args.url)
    domain = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip('/')        # e.g. /dpp/2025
    path_prefix = path.lstrip('/')        # e.g. dpp/2025

    path_parts = path_prefix.split('/')
    series = path_parts[0] if len(path_parts) > 0 else "aps"
    year_str = path_parts[1] if len(path_parts) > 1 else "unknown"

    output = f"/home/zhiping/aps_{series}{year_str}.json"

    print(f"URL:    {args.url}")
    print(f"Domain: {domain}")
    print(f"Path:   {path}")
    print(f"Series: {series}")
    print(f"Year:   {year_str}")
    print(f"Output: {output}")
    if args.limit:
        print(f"Limit:  {args.limit} sessions")

    scrape(domain, path, path_prefix, series, year_str, output, args.limit)


def full_url(href, domain):
    """Build absolute URL from a relative href."""
    if href.startswith(domain):
        return href.rstrip('/')
    if href.startswith('/'):
        return domain + href.rstrip('/')
    return domain + '/' + href.rstrip('/')


def extract_between_h2(html, heading_text):
    """
    Extract raw HTML content between <h2>heading_text</h2> and the next <h2> tag.
    Returns (raw_html, BeautifulSoup_parsed_text)
    """
    pattern = f'<h2>{heading_text}</h2>'
    parts = html.split(pattern, 1)
    if len(parts) < 2:
        return "", ""
    after = parts[1]
    # Find next h2 (also handle h2 with attributes)
    next_h2 = re.search(r'<h2[\s>]', after)
    end = next_h2.start() if next_h2 else len(after)
    raw = after[:end].strip()
    # Clean up by parsing
    soup = BeautifulSoup(raw, 'html.parser')
    text = soup.get_text(separator=' ', strip=True)
    return raw, text


def extract_authors_split(html):
    """Extract authors with affiliations from raw HTML using string-split method."""
    authors = []
    pattern = '<h2>Authors</h2>'
    parts = html.split(pattern, 1)
    if len(parts) < 2:
        return authors

    after = parts[1]
    next_h2 = re.search(r'<h2[\s>]', after)
    end = next_h2.start() if next_h2 else len(after)
    raw = after[:end].strip()

    if not raw:
        return authors

    soup = BeautifulSoup(raw, 'html.parser')

    lis = soup.find_all('li', itemprop='contributor')
    if not lis:
        ul = soup.find('ul')
        if ul:
            lis = ul.find_all('li', recursive=False)
        else:
            lis = []

    for li in lis:
        addr = li.find('address')
        if not addr:
            continue
        name_el = addr.find(['p', 'span'], itemprop='name')
        name = name_el.get_text(strip=True) if name_el else ''
        if not name:
            continue

        affils = []
        affil_lis = addr.find_all('li', itemprop='affiliation')
        if not affil_lis:
            inner_ul = addr.find('ul')
            if inner_ul:
                affil_lis = inner_ul.find_all('li', recursive=False)

        for affil_li in affil_lis:
            span = affil_li.find('span', itemprop='name')
            if span:
                affils.append(span.get_text(strip=True))
            else:
                txt = affil_li.get_text(strip=True)
                if txt:
                    affils.append(txt)

        authors.append({"name": name, "affiliation": affils})

    return authors


def extract_presenters_split(html):
    """Extract presenters with affiliations using string-split method."""
    presenters = []
    pattern = '<h2>Presenters</h2>'
    parts = html.split(pattern, 1)
    if len(parts) < 2:
        # Try h4 as well (some session pages use h4)
        pattern = '<h4>Presenters</h4>'
        parts = html.split(pattern, 1)
    if len(parts) < 2:
        return presenters

    after = parts[1]
    next_h = re.search(r'<h[234][\s>]', after)
    end = next_h.start() if next_h else len(after)
    raw = after[:end].strip()

    if not raw:
        return presenters

    soup = BeautifulSoup(raw, 'html.parser')

    lis = soup.find_all('li', itemprop='performer')
    if not lis:
        ul = soup.find('ul')
        if ul:
            lis = ul.find_all('li', recursive=False)
        if not lis:
            lis = []

    for li in lis:
        addr = li.find('address')
        if not addr:
            continue
        name_el = addr.find(['p', 'span'], itemprop='name')
        name = name_el.get_text(strip=True) if name_el else ''
        if not name:
            continue

        affils = []
        affil_lis = addr.find_all('li', itemprop='affiliation')
        if not affil_lis:
            inner_ul = addr.find('ul')
            if inner_ul:
                affil_lis = inner_ul.find_all('li', recursive=False)

        for affil_li in affil_lis:
            span = affil_li.find('span', itemprop='name')
            if span:
                affils.append(span.get_text(strip=True))
            else:
                txt = affil_li.get_text(strip=True)
                if txt:
                    affils.append(txt)

        presenters.append({"name": name, "affiliation": affils})

    return presenters


def extract_abstract_split(html):
    """Extract abstract HTML between <h2>Abstract</h2> and next <h2>.

    KaTeX-rendered inline math is collapsed to raw ``$…$`` LaTeX before the
    15 000-char cap is applied — otherwise a single formula's visual span
    layout (typically 500-1500 bytes) would eat a big chunk of the budget.
    """
    raw, text = extract_between_h2(html, 'Abstract')
    if not raw:
        return ""
    raw = convert_katex_to_latex(raw)
    return raw[:15000]


def extract_type_from_html(html):
    """Extract presentation type from <data> tags in raw HTML."""
    soup = BeautifulSoup(html, 'html.parser')
    for dt in soup.find_all('data'):
        val = dt.get('value', '')
        if val in ['ORAL', 'POSTER', 'TUTORIAL', 'INVITED']:
            return val
    return ""


def extract_presiders(html):
    """Extract presiders using string-split method."""
    presiders = []
    pattern = '<h2>Presiders</h2>'
    parts = html.split(pattern, 1)
    if len(parts) < 2:
        return presiders

    after = parts[1]
    next_h = re.search(r'<h[234][\s>]', after)
    end = next_h.start() if next_h else len(after)
    raw = after[:end].strip()

    if not raw:
        return presiders

    soup = BeautifulSoup(raw, 'html.parser')

    for addr in soup.find_all('address'):
        name_el = addr.find(['p', 'span'], itemprop='name')
        name = name_el.get_text(strip=True) if name_el else ''
        if not name:
            continue

        affils = []
        for affil_li in addr.find_all('li', itemprop='affiliation'):
            span = affil_li.find('span', itemprop='name')
            if span:
                affils.append(span.get_text(strip=True))

        if name:
            presiders.append({"name": name, "affiliation": affils})

    return presiders


def scrape(domain, path, path_prefix, series, year_str, output, limit):
    url_prefix = path           # e.g. /dpp/2025
    sess_base = domain + url_prefix  # full base URL

    result = {
        "url": sess_base,
        "Conference Overview": {"Year": year_str, "Series": series, "Total Presentations": 0, "Total Sessions": 0},
        "Events": []
    }

    with sync_playwright() as p:
        # === MAIN PAGE: get all session links ===
        print("Fetching main page...")
        main_browser = p.chromium.launch(headless=True, args=['--disable-dev-shm-usage', '--no-sandbox'])
        page = main_browser.new_page()
        page.goto(sess_base, wait_until="networkidle")
        time.sleep(2)
        html = page.content()
        soup = BeautifulSoup(html, 'html.parser')

        sessions = []
        seen_codes = set()

        # Generic session-code pattern: e.g. /dpp/2025/bo09/ or /dfd/2025/aa15/
        href_pattern = re.compile(r'^' + re.escape(url_prefix) + r'/([a-z]+\d+)/$')

        for a in soup.select(f'a[href*="{url_prefix}/"]'):
            href = a.get('href', '')
            m = href_pattern.match(href)
            if m:
                code = m.group(1)
                if code not in seen_codes:
                    seen_codes.add(code)
                    name = a.get_text(strip=True)
                    sessions.append({'url': full_url(href, domain), 'name': name, 'code': code})

        page.close()
        main_browser.close()
        print(f"Sessions found: {len(sessions)}")

        if limit and limit < len(sessions):
            sessions = sessions[:limit]
            print(f"Limited to first {limit} sessions")

        # === PROCESS EACH SESSION - fresh browser per session ===
        for si, session in enumerate(sessions):
            print(f"\n[{si+1}/{len(sessions)}] {session['code']}: {session['name'][:70]}")

            event = {
                "name": session["name"],
                "url": session["url"],
                # "Schedule": "",
                # "Presiders": [],
                # "Locations": "",
                "Presentations": []
            }

            sess_browser = None
            try:
                sess_browser = p.chromium.launch(headless=True, args=['--disable-dev-shm-usage', '--no-sandbox'])
                sp = sess_browser.new_page()
                sp.goto(session["url"], wait_until="networkidle", timeout=20000)
                time.sleep(1)
                shtml = sp.content()

                # Schedule
                # raw, txt = extract_between_h2(shtml, 'Schedule')
                # event["Schedule"] = txt

                # Presiders
                # event["Presiders"] = extract_presiders(shtml)

                # Locations
                # raw, txt = extract_between_h2(shtml, 'Locations')
                # event["Locations"] = txt

                # Presentation links from h3 tags
                code = session['code']
                pres_links = []
                seen_urls = set()

                ssoup = BeautifulSoup(shtml, 'html.parser')

                # Generic presentation URL pattern: e.g. /dpp/2025/bo09/12345/
                pres_href_pattern = re.compile(
                    r'^' + re.escape(url_prefix) + r'/' + re.escape(code) + r'/(\d+)/$'
                )

                # Primary: h3 links
                for h3 in ssoup.find_all('h3'):
                    a = h3.find('a', href=True)
                    if a:
                        href = a['href']
                        m2 = pres_href_pattern.match(href)
                        if m2:
                            url = full_url(href, domain)
                            if url not in seen_urls:
                                seen_urls.add(url)
                                title = a.get_text(strip=True)
                                ptype_guess = ""
                                ptag = h3.find_next_sibling('p')
                                if ptag:
                                    ptxt = ptag.get_text(strip=True)
                                    for t in ["ORAL", "POSTER", "INVITED", "TUTORIAL"]:
                                        if t in ptxt:
                                            ptype_guess = t
                                            break
                                pres_links.append({"url": url, "title": title, "type_guess": ptype_guess})

                # Fallback: any matching link
                if not pres_links:
                    for a in ssoup.find_all('a', href=True):
                        href = a['href']
                        m2 = pres_href_pattern.match(href)
                        if m2:
                            url = full_url(href, domain)
                            if url not in seen_urls:
                                seen_urls.add(url)
                                title = a.get_text(strip=True)
                                if title.strip() in ('View abstract \u2192', 'View abstract'):
                                    continue
                                pres_links.append({"url": url, "title": title, "type_guess": ""})

                sp.close()
                sess_browser.close()

                # === FETCH EACH PRESENTATION ===
                for pj, pres in enumerate(pres_links):
                    print(f"  [{pj+1}/{len(pres_links)}]", end=" ", flush=True)
                    pres_browser = None
                    try:
                        pres_browser = p.chromium.launch(headless=True, args=['--disable-dev-shm-usage', '--no-sandbox'])
                        pp = pres_browser.new_page()
                        pp.goto(pres["url"], wait_until="networkidle", timeout=15000)
                        time.sleep(0.5)
                        ph = pp.content()
                        pp.close()
                        pres_browser.close()

                        # Title from h1
                        psoup = BeautifulSoup(ph, 'html.parser')
                        h1 = psoup.find('h1')
                        title = h1.get_text(strip=True) if h1 else pres['title']

                        # Type
                        ptype = extract_type_from_html(ph)
                        if not ptype:
                            ptype = pres.get('type_guess', '')

                        # Authors (use robust split method)
                        authors = extract_authors_split(ph)

                        # Presenters (use robust split method)
                        presenters = extract_presenters_split(ph)

                        # Abstract (use robust split method)
                        abstract = extract_abstract_split(ph)

                        presentation = {
                            "title": title,
                            "url": pres["url"],
                            "Abstract": abstract,
                            "Presenters": presenters,
                            "Authors": authors,
                            "type": ptype
                        }
                        event["Presentations"].append(presentation)
                        print(f"OK ({len(abstract)} ch, {len(authors)} auth, {len(presenters)} pres)")
                    except Exception as e:
                        print(f"FAIL: {str(e)[:80]}")
                        event["Presentations"].append({
                            "title": pres["title"], "url": pres["url"],
                            "Abstract": "", "Presenters": [], "Authors": [], "type": ""
                        })

            except Exception as e:
                print(f"  SESSION ERROR: {e}")

            result["Events"].append(event)

            # Checkpoint every 5 sessions
            if (si + 1) % 5 == 0:
                total_pres = sum(len(e["Presentations"]) for e in result["Events"])
                result["Conference Overview"]["Total Presentations"] = total_pres
                result["Conference Overview"]["Total Sessions"] = len(result["Events"])
                with open(output, 'w') as f:
                    json.dump(result, f, indent=2, ensure_ascii=False)
                print(f"  >>> CHECKPOINT: {si+1}/{len(sessions)} sessions, {total_pres} pres")

    total_pres = sum(len(e["Presentations"]) for e in result["Events"])
    result["Conference Overview"]["Total Presentations"] = total_pres
    result["Conference Overview"]["Total Sessions"] = len(result["Events"])
    with open(output, 'w') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"\nDONE! {len(result['Events'])} sessions, {total_pres} presentations")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise
