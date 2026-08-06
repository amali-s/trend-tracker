"""Source recon — run this before trusting any parser.

Ten of the fourteen sources in firms.py have URL patterns that were inferred
rather than observed. This script checks each one against the live site and
tells you which tier it belongs in and whether the pattern actually matches.

    python -m src.probe                 # all 14
    python -m src.probe greylock a16z   # named sources only
    python -m src.probe --save-fixtures # also write HTML to tests/fixtures/

For each source it reports:
  - whether a feed exists (RSS, Atom, or WordPress REST)
  - whether post links appear in the static HTML
  - how many of those links match the configured post_url_pattern
  - a sample of matched and unmatched-but-plausible links
  - whether dates are available on the index page
  - whether an embedded JSON blob (__NEXT_DATA__ etc.) is present

Read the output as a to-do list: any source with 0 matches needs its pattern
corrected, and any source with no static links at all needs Playwright or a
JSON endpoint.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections import Counter
from urllib.parse import urljoin, urlparse

from .sources.base import BaseSource, canonicalize_url
from .sources.firms import ALL_SOURCES, SOURCES_BY_NAME, VERIFIED_SOURCES

logging.basicConfig(level=logging.WARNING, format="%(message)s")

FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures"
)

FEED_CANDIDATES = [
    "/feed/", "/feed", "/rss/", "/rss", "/feed.xml", "/rss.xml",
    "/atom.xml", "/index.xml", "/blog/feed/", "/blog/rss/",
]
WP_CANDIDATES = ["/wp-json/wp/v2/posts?per_page=1"]


def probe_feeds(source: BaseSource) -> list[str]:
    """Find any working feed endpoints on the source's host."""
    found = []
    root = f"{urlparse(source.base_url).scheme}://{urlparse(source.base_url).netloc}"

    for path in FEED_CANDIDATES:
        url = urljoin(root, path)
        try:
            resp = source.session.get(url, timeout=15, allow_redirects=True)
        except Exception:  # noqa: BLE001
            continue
        if resp.status_code != 200:
            continue
        head = resp.text[:2000].lower()
        if "<rss" in head or "<feed" in head or "<?xml" in head:
            # Count items so an empty feed doesn't read as a win
            n = len(re.findall(r"<(item|entry)[\s>]", resp.text, re.IGNORECASE))
            found.append(f"{url}  ({n} items)")

    for path in WP_CANDIDATES:
        url = urljoin(root, path)
        try:
            resp = source.session.get(url, timeout=15)
            if resp.status_code == 200 and isinstance(resp.json(), list):
                found.append(f"{url}  (WordPress REST)")
        except Exception:  # noqa: BLE001
            continue

    return found


def probe_source(source: BaseSource, save_fixtures: bool = False) -> dict:
    """Probe one source and return a findings dict."""
    result = {
        "name": source.name,
        "feeds": [],
        "static_links": 0,
        "matched": [],
        "near_misses": [],
        "has_dates": False,
        "embedded_json": None,
        "error": None,
        "http_status": None,
        "scraped": 0,
        "dated": 0,
        "sample_titles": [],
        "scrape_error": None,
    }

    result["feeds"] = probe_feeds(source)

    index_url = (source.index_urls or [source.base_url])[0]
    try:
        resp = source.session.get(index_url, timeout=30)
        result["http_status"] = resp.status_code
        resp.raise_for_status()
        html = resp.text
    except Exception as e:  # noqa: BLE001
        result["error"] = str(e)[:200]
        return result

    if save_fixtures:
        os.makedirs(FIXTURE_DIR, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "_", source.name.lower()).strip("_")
        with open(os.path.join(FIXTURE_DIR, f"{slug}_index.html"), "w") as f:
            f.write(html)

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")

    # Embedded JSON is worth more than any selector — flag it loudly
    for marker in ("__NEXT_DATA__", "__NUXT__", "__remixContext", "window.__DATA__"):
        if marker in html:
            result["embedded_json"] = marker
            break

    host = urlparse(source.base_url).netloc.lower()
    all_urls = []
    for a in soup.find_all("a", href=True):
        u = canonicalize_url(a["href"], base=index_url)
        if u and urlparse(u).netloc.lower() == host:
            all_urls.append(u)

    result["static_links"] = len(set(all_urls))
    matched = {u for u in all_urls if source.is_post_url(u)}
    result["matched"] = sorted(matched)[:6]
    result["match_count"] = len(matched)

    # Links with a two-segment path are the most plausible post-shaped URLs;
    # showing them makes a wrong pattern obvious at a glance.
    if not matched:
        depth_two = [
            u for u in set(all_urls)
            if len([p for p in urlparse(u).path.split("/") if p]) >= 2
        ]
        prefixes = Counter(
            "/" + urlparse(u).path.strip("/").split("/")[0] for u in depth_two
        )
        result["near_misses"] = [f"{p} ({n} links)" for p, n in prefixes.most_common(8)]

    # Ground truth: run the source's real discovery path. Everything above is
    # static analysis of one index page; this is what the pipeline will call.
    try:
        discovered = source.scrape()
        result["scraped"] = len(discovered)
        result["sample_titles"] = [p.title for p in discovered[:3]]
        result["dated"] = sum(1 for p in discovered if p.published_date)
    except Exception as e:  # noqa: BLE001
        result["scrape_error"] = str(e)[:200]

    result["has_dates"] = bool(soup.find("time")) or bool(
        re.search(
            r"\b(January|February|March|April|May|June|July|August|September|"
            r"October|November|December)\s+\d{1,2},\s+\d{4}\b",
            soup.get_text(" ", strip=True)[:20000],
        )
    )

    return result


def tier_for(result: dict) -> str:
    """Classify the site.

    Deliberately does NOT consider `feeds` a pass on its own. A feed existing
    on the host says nothing about whether *this source* is configured to read
    it — every source here is HTML-based unless it subclasses RSSSource — and
    an earlier version of this function returned "B (feed)" before it ever
    looked at match_count, which reported Sequoia and Battery as OK while both
    were discovering exactly zero posts.
    """
    if result["error"]:
        return "ERROR"
    if result.get("scraped"):
        return "A (static HTML)" if not result["feeds"] else "B (feed-backed)"
    if result.get("match_count"):
        return "A (pattern matches, but scrape() returned nothing)"
    if result["feeds"]:
        return "B? (feed on host, but this source reads HTML and matched 0)"
    if result["embedded_json"]:
        return "D (embedded JSON)"
    if result["static_links"] < 15:
        return "C (client-rendered — needs a JSON endpoint or Playwright)"
    return "A? (links present, pattern wrong)"


def is_ok(result: dict) -> bool:
    """A source passes only if its real discovery path yields posts.

    `scrape()` is the ground truth because it is the code the pipeline
    actually runs — feed subclasses included. Static link-matching is kept
    around for diagnosis, not as the pass condition.
    """
    return bool(result.get("scraped"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe VC blog sources")
    parser.add_argument("names", nargs="*", help="Source names (default: all)")
    parser.add_argument("--save-fixtures", action="store_true",
                        help="Write index HTML to tests/fixtures/")
    args = parser.parse_args()

    if args.names:
        classes = []
        for name in args.names:
            cls = SOURCES_BY_NAME.get(name.lower())
            if cls is None:
                print(f"Unknown source: {name}")
                print(f"Available: {', '.join(sorted(SOURCES_BY_NAME))}")
                return 2
            classes.append(cls)
    else:
        classes = ALL_SOURCES

    verified_names = {c.name for c in VERIFIED_SOURCES}
    results = []

    for cls in classes:
        source = cls()
        mark = "verified" if cls.name in verified_names else "UNVERIFIED"
        print(f"\n{'=' * 68}\n{source.name}  [{mark}]\n{'=' * 68}")
        result = probe_source(source, save_fixtures=args.save_fixtures)
        results.append(result)

        tier = tier_for(result)
        print(f"  Tier:            {tier}")
        if result["http_status"]:
            print(f"  HTTP:            {result['http_status']}")
        if result["error"]:
            print(f"  Error:           {result['error']}")
        if result["feeds"]:
            print("  Feeds found:")
            for feed in result["feeds"]:
                print(f"    - {feed}")
        else:
            print("  Feeds found:     none")
        print(f"  Same-host links: {result['static_links']}")
        print(f"  Pattern matches: {result.get('match_count', 0)}")
        for url in result["matched"]:
            print(f"    + {url}")
        if result["near_misses"]:
            print("  No matches. Path prefixes present on the page:")
            for hint in result["near_misses"]:
                print(f"    ? {hint}")
        print(f"  Dates on index:  {'yes' if result['has_dates'] else 'no'}")
        if result["embedded_json"]:
            print(f"  Embedded JSON:   {result['embedded_json']}")

        # The authoritative line: what the real discovery path returned.
        if result["scrape_error"]:
            print(f"  scrape() ERROR:  {result['scrape_error']}")
        else:
            print(f"  scrape() posts:  {result['scraped']}"
                  f"  ({result['dated']} with a publication date)")
            for title in result["sample_titles"]:
                print(f"    > {title[:70]}")

    print(f"\n\n{'=' * 68}\nSUMMARY\n{'=' * 68}")
    for result in results:
        status = "OK " if is_ok(result) else "FIX"
        print(f"  [{status}] {result['name']:<20} {result['scraped']:>3} posts  "
              f"{tier_for(result)}")

    broken = [r["name"] for r in results if not is_ok(r)]
    if broken:
        print(f"\n{len(broken)} source(s) need attention: {', '.join(broken)}")
        print("Correct their post_url_pattern in src/sources/firms.py and re-run.")
    else:
        print("\nAll probed sources returned matches.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
