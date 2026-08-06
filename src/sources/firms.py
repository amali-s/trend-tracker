"""The 14 VC blog sources.

VERIFICATION STATUS — read this before trusting any class below.

I probed four of these sites directly and the patterns here reflect what the
pages actually served on 2026-08-05:

    Greylock           VERIFIED  static HTML (Next.js + Sanity), /page/N/ pagination
    a16z               VERIFIED  static HTML (WordPress), investments at /announcement/
    Contrary           VERIFIED  static HTML, dates on index, "Load more" button
    General Catalyst   VERIFIED  static HTML (Webflow), ?<hash>_page=N pagination
    Sequoia            VERIFIED  RSS feed; HTML index is client-rendered (2026-08-06)
    Antler             VERIFIED  static HTML; posts at /press-releases/ (2026-08-06)

The other eight are UNVERIFIED. Their `post_url_pattern` values are inferred from
the index URL you supplied, which is a reasonable guess for how a CMS lays out
post paths but is still a guess. Run `python -m src.probe` to check them against
the live sites, then correct the patterns and move each entry out of the
unverified block.

Do not assume an unverified source works because it imports cleanly.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from bs4 import BeautifulSoup

from ..models import BlogPost
from .base import BaseSource
from .rss_base import RSSSource, WordPressSource

# Titles that reliably mark a funding announcement across these blogs.
# Used only to set likely_investment=True and skip an LLM call — never to
# exclude a post.
COMMON_INVESTMENT_TITLES = [
    r"^investing in\b",
    r"^our investment in\b",
    r"^introducing\b",
    r"^announcing our investment\b",
    r"^welcoming\b",
    r"^backing\b",
    r"^partnering with\b",
    r"^why we invested\b",
    r"^doubling down on\b",
    r"^seeding the future with\b",
    r"\$\d+(\.\d+)?\s?[MB]\b",              # "Leland's $12M Series A"
    r"\bseries [A-J]\b",
    r"\b(seed|pre-seed) (round|funding)\b",
    r"\braises?\b.*\$",
]


# ===========================================================================
# VERIFIED SOURCES
# ===========================================================================

class GreylockSource(BaseSource):
    """Greylock — portfolio news.

    Verified 2026-08-05. Next.js front end over Sanity CMS. The index page
    ships post links in the initial HTML. Posts live at /blog/<slug>/ and the
    category landing pages share that prefix, so they're excluded explicitly.
    Pagination is /blog/portfolio-news/page/N/ and the index reported 8 pages.
    No dates on the index — they come from the detail page.
    """

    name = "Greylock"
    base_url = "https://greylock.com"
    index_urls = ["https://greylock.com/blog/portfolio-news/"]
    post_url_pattern = r"greylock\.com/blog/[a-z0-9][a-z0-9-]+$"
    exclude_url_patterns = [
        r"/blog/(portfolio-news|greymatter|firm-news)$",
        r"/blog$",
        r"/opengraph-image",
    ]
    investment_title_patterns = COMMON_INVESTMENT_TITLES
    max_pages = 2  # ~12 posts/page; 2 pages covers a week with room to spare

    def paginate(self, index_url: str) -> Iterable[str]:
        yield index_url
        for n in range(2, self.max_pages + 1):
            yield f"{index_url.rstrip('/')}/page/{n}/"


class A16ZSource(BaseSource):
    """a16z — news & content.

    Verified 2026-08-05. WordPress. The useful discovery here: investment
    announcements are published under a dedicated /announcement/ path
    ("Investing in Volta", "Investing in Neo", "Investing in Netris"), while
    podcasts sit under /podcast/ and essays at the site root. That path is a
    near-perfect classifier gate, so we scope discovery to it and skip the LLM
    classifier for these posts entirely.

    Trade-off: this deliberately ignores investment news that a16z publishes
    outside /announcement/. Given the volume of podcast and essay content on
    this blog, a precise filter is worth more than an exhaustive one. Widen
    post_url_pattern if you'd rather have recall and pay for the classifier.

    The index uses a JS "load more" button, so only the first batch is visible
    in static HTML. That's fine at a weekly cadence — the first batch is ~45
    items. Worth re-checking whether /wp-json/wp/v2/posts is reachable from
    your network; it returned nothing through my fetcher, which may have been
    the fetcher rather than the site.
    """

    name = "a16z"
    base_url = "https://a16z.com"
    index_urls = ["https://a16z.com/news-content/"]
    post_url_pattern = r"a16z\.com/announcement/[a-z0-9][a-z0-9-]+$"
    investment_url_pattern = r"/announcement/"
    max_pages = 1

    def extract_labels(self, anchor) -> list[str]:
        """a16z prints a focus-area label above each card ("Infra", "Consumer")."""
        container = anchor.find_parent(["article", "li", "div"])
        if not container:
            return []
        known = {
            "AI", "American Dynamism", "Bio + Health", "Consumer", "Crypto",
            "Enterprise", "Fintech", "General", "Growth", "Infra", "Perennial",
            "Speedrun", "Cultural Leadership Fund",
        }
        found = []
        for el in container.find_all(["span", "p", "div"], limit=12):
            text = el.get_text(strip=True)
            if text in known and text not in found:
                found.append(text)
        return found


class ContrarySource(BaseSource):
    """Contrary — blog.

    Verified 2026-08-05. Static HTML with visible dates ("October 8, 2025")
    and titles that follow a clean "Investing in X" convention. Posts at
    /blog/<slug>, no trailing slash.

    Note: this is a low-volume blog — at the time of probing the newest post
    was several months old. Expect many empty weeks, and don't read an empty
    result as a broken scraper here.

    Pagination is a JS "Load more" button with no URL form, so static parsing
    sees only the first ~9 posts. Adequate weekly.
    """

    name = "Contrary"
    base_url = "https://contrary.com"
    index_urls = ["https://contrary.com/blog"]
    post_url_pattern = r"contrary\.com/blog/[a-z0-9][a-z0-9-]+$"
    exclude_url_patterns = [r"/blog$"]
    investment_title_patterns = COMMON_INVESTMENT_TITLES
    max_pages = 1

    # Contrary renders the date as plain text inside the card rather than in a
    # <time> element, so the generic <time> lookup finds nothing.
    DATE_TEXT = re.compile(
        r"\b(January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+\d{1,2},\s+\d{4}\b"
    )

    def extract_date_near(self, anchor):
        text = anchor.get_text(" ", strip=True)
        match = self.DATE_TEXT.search(text)
        if match:
            return self.parse_date(match.group(0))
        return super().extract_date_near(anchor)

    def extract_title(self, anchor) -> str:
        """Strip the leading date out of the harvested anchor text.

        The whole card is one anchor, so its text reads
        "October 8, 2025 Investing in Base Power Announcing our... Read more".
        """
        text = super().extract_title(anchor)
        text = self.DATE_TEXT.sub("", text, count=1)
        text = re.sub(r"\s*Read more\s*$", "", text, flags=re.IGNORECASE)
        text = re.sub(r"^\s*Featured\s*", "", text, flags=re.IGNORECASE)
        return self.clean_text(text)


class GeneralCatalystSource(BaseSource):
    """General Catalyst — stories.

    Verified 2026-08-05. Webflow. Post links are in the initial HTML at
    /stories/<slug>. Announcement posts use the "Community" category and title
    conventions "Our Investment in X" / "Seeding the Future with X" /
    "Doubling Down on X".

    Two gotchas the probe surfaced:
      - The page also carries a long "NEWS" section of *external* press links
        (techcrunch, forbes, bloomberg). The same-host check in
        BaseSource.is_post_url filters those out.
      - Pagination uses a Webflow-generated parameter with a random hash
        (?c3e7011e_page=2). That hash is tied to the current build and will
        change on redeploy, so it isn't hardcoded here — page 1 only.

    GC's topic tags map unusually well onto our sector enum; extract_labels
    passes them to the extractor as a hint.
    """

    name = "General Catalyst"
    base_url = "https://www.generalcatalyst.com"
    index_urls = ["https://www.generalcatalyst.com/stories"]
    post_url_pattern = r"generalcatalyst\.com/stories/[a-z0-9][a-z0-9-]+$"
    exclude_url_patterns = [r"/stories$"]
    investment_title_patterns = COMMON_INVESTMENT_TITLES
    max_pages = 1

    GC_TOPICS = {
        "Applied AI", "Artificial Intelligence", "Industrials & Manufacturing",
        "Energy & Infrastructure", "Defense & Government", "Healthcare",
        "Global Resilience", "Fintech", "Enterprise", "Consumer", "Space",
        "Seed", "Create", "Grow", "CVF", "GCI", "HatCo",
    }

    def extract_labels(self, anchor) -> list[str]:
        container = anchor.find_parent(["div", "li", "article"])
        if not container:
            return []
        found = []
        for el in container.find_all(["div", "span", "p"], limit=20):
            text = el.get_text(strip=True)
            if text in self.GC_TOPICS and text not in found:
                found.append(text)
        return found


# ===========================================================================
# UNVERIFIED SOURCES
#
# Patterns below are inferred from the index URL, not observed. Run
# `python -m src.probe` and correct them before relying on the output.
# ===========================================================================

class SequoiaSource(RSSSource):
    """Sequoia — news stories. Verified 2026-08-06, via feed.

    The HTML index is a dead end: /stories/?_story-category=news served only
    five same-host links, all of them nav (/our-companies, /our-team, ...).
    The story cards are client-rendered, so there is nothing to harvest and
    the `?_story-category` filter question is moot.

    The feed answers everything the HTML could not. /feed/ returns 10 recent
    entries with real publication dates, and posts live at /article/<slug>/,
    not /stories/<slug>/ as inferred.

    Two things worth knowing:

    - Feed entries point at sequoiacap.com while the site is served from
      www.sequoiacap.com. BaseSource.is_post_url compares hosts with a `www.`
      prefix ignored, which is the only reason these match at all.
    - Sequoia tags its own posts, and "Funding announcement" appears on
      exactly the posts we want. That is a firm-authored gate, better than any
      title heuristic, and it lets the LLM classifier be skipped the way
      a16z's /announcement/ path does. Titles follow "Partnering with X: ..."
      but the tag is the reliable signal.

    A WordPress REST endpoint also exists at /wp-json/wp/v2/posts and would
    give deeper pagination, but it returns tags as numeric IDs requiring a
    second request. At Sequoia's cadence -- roughly two to four posts a month
    -- ten feed entries is over a month of coverage, so the tags are worth
    more than the depth.
    """

    name = "Sequoia"
    base_url = "https://www.sequoiacap.com"
    index_urls = ["https://www.sequoiacap.com/stories/?_story-category=news"]
    feed_urls = ["https://www.sequoiacap.com/feed/"]
    post_url_pattern = r"sequoiacap\.com/(article|stories)/[a-z0-9][a-z0-9-]+$"
    exclude_url_patterns = [r"/stories$"]
    investment_title_patterns = COMMON_INVESTMENT_TITLES
    investment_label_patterns = [r"^funding announcement$"]


class IndexVenturesSource(BaseSource):
    """Index Ventures — perspectives. UNVERIFIED.

    vc-job-agent's SCRAPER_DIAGNOSIS.md found indexventures.com to be a
    client-rendered Vue app on the jobs side of the site. If /perspectives/ is
    built the same way, this needs a Playwright fallback — check the probe's
    static-HTML result before writing off the simple path.

    Also note this blog is mostly long-form essays, so expect the classifier to
    reject most of what it returns.
    """

    name = "Index Ventures"
    base_url = "https://www.indexventures.com"
    index_urls = ["https://www.indexventures.com/perspectives/"]
    post_url_pattern = r"indexventures\.com/perspectives/[a-z0-9][a-z0-9-]+$"
    exclude_url_patterns = [r"/perspectives$"]
    investment_title_patterns = COMMON_INVESTMENT_TITLES


class KleinerPerkinsSource(BaseSource):
    """Kleiner Perkins — announcements. UNVERIFIED.

    The index URL is already scoped to the announcements category, which is
    the ideal case: a category that is *by definition* investment news. If the
    probe confirms it, set investment_url_pattern to the post path so the
    classifier can be skipped, the way a16z's /announcement/ path is handled.
    """

    name = "Kleiner Perkins"
    base_url = "https://www.kleinerperkins.com"
    index_urls = ["https://www.kleinerperkins.com/perspectives/category/announcements"]
    post_url_pattern = r"kleinerperkins\.com/perspectives/[a-z0-9][a-z0-9-]+$"
    exclude_url_patterns = [r"/perspectives(/category.*)?$"]
    investment_title_patterns = COMMON_INVESTMENT_TITLES


class AccelSource(BaseSource):
    """Accel — portfolio news. UNVERIFIED.

    vc-job-agent found jobs.accel.com on the older Getro/Next.js platform that
    still served __NEXT_DATA__. If www.accel.com is also Next.js, check for a
    __NEXT_DATA__ blob — parsing that JSON is far more stable than the DOM.
    """

    name = "Accel"
    base_url = "https://www.accel.com"
    index_urls = ["https://www.accel.com/news/portfolio"]
    post_url_pattern = r"accel\.com/noteworthies/[a-z0-9][a-z0-9-]+$|accel\.com/news/[a-z0-9][a-z0-9-]{3,}$"
    exclude_url_patterns = [r"/news(/portfolio)?$"]
    investment_title_patterns = COMMON_INVESTMENT_TITLES


class BatterySource(BaseSource):
    """Battery Ventures — news. UNVERIFIED.

    /news/ on these sites is often a mix of first-party posts and links out to
    press coverage. The same-host check drops external links automatically, but
    confirm that leaves anything behind.
    """

    name = "Battery Ventures"
    base_url = "https://www.battery.com"
    index_urls = ["https://www.battery.com/news/"]
    post_url_pattern = r"battery\.com/(news|blog)/[a-z0-9][a-z0-9-]+$"
    exclude_url_patterns = [r"/news$", r"/blog$"]
    investment_title_patterns = COMMON_INVESTMENT_TITLES


class NEASource(BaseSource):
    """NEA — investment blog. UNVERIFIED.

    The supplied URL already carries the filters we want:
    ?type=Read&topic=investment. Keep them, and walk `page`.

    Whether that filtering is server-side is the open question — if the page
    parameter doesn't change the HTML, it's a client-side SPA and needs
    Playwright or the underlying JSON endpoint.
    """

    name = "NEA"
    base_url = "https://www.nea.com"
    index_urls = ["https://www.nea.com/blog?type=Read&topic=investment"]
    post_url_pattern = r"nea\.com/blog/[a-z0-9][a-z0-9-]+$"
    exclude_url_patterns = [r"/blog$"]
    investment_title_patterns = COMMON_INVESTMENT_TITLES
    max_pages = 3

    def paginate(self, index_url: str) -> Iterable[str]:
        for n in range(1, self.max_pages + 1):
            yield f"{index_url}&page={n}"


class AntlerSource(BaseSource):
    """Antler — newsroom. Verified 2026-08-06.

    The inferred /newsroom/<slug> and /blog/<slug> paths do not exist. The
    newsroom index is a listing page and the posts it links to live at
    /press-releases/<slug>, e.g.
    /press-releases/agentio-raises-40m-series-b-to-scale-ai-native-platform...

    The index also carries 18 /location/<country> links (Antler operates in
    many geographies) and a handful of /legal/ pages, all two-segment paths
    that a looser pattern would happily swallow. Scoping to /press-releases/
    keeps them out without needing excludes for each.

    Pagination is Webflow's ?<hash>_page=N, and the page carries two different
    hashes (7eb107ba, f4c053cc) for two separate listings. Both are tied to
    the current build and will change on redeploy, so neither is hardcoded --
    page 1 only, as with General Catalyst.

    Not every press release is an investment: the index mixes rounds
    ("Agentio raises $40M Series B") with firm news ("Antler appoints Hiro
    Kiga as Partner") and Antler's own fundraising ("Antler raises additional
    $510 million"). That last category is a genuine trap -- it matches the
    investment title patterns perfectly but is the *firm* raising, not a
    portfolio company. The classifier has to catch it.

    Antler runs a very high volume of small pre-seed deals, so expect this
    source to dominate deal *count* while contributing little to deal
    *dollars* -- a reason the rollup reports both.
    """

    name = "Antler"
    base_url = "https://www.antler.co"
    index_urls = ["https://www.antler.co/newsroom"]
    post_url_pattern = r"antler\.co/press-releases/[a-z0-9][a-z0-9-]+$"
    exclude_url_patterns = [r"/press-releases$", r"/newsroom$", r"/blog$"]
    investment_title_patterns = COMMON_INVESTMENT_TITLES

    # Antler renders the date as plain text in the card, not in a <time>.
    DATE_TEXT = re.compile(
        r"\b(January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+\d{1,2},\s+\d{4}\b"
    )

    def _card(self, anchor):
        """Walk up to the card container that holds the title and date.

        The anchor itself is a bare "Read more" link; the title is a sibling
        several levels up. Stops at the first ancestor carrying real content
        rather than assuming a fixed depth, since the wrapper divs are
        Webflow-generated and their nesting is not a stable contract.
        """
        node = anchor
        for _ in range(5):
            node = node.parent
            if node is None:
                return None
            if len(node.get_text(" ", strip=True)) > 40:
                return node
        return None

    def extract_title(self, anchor) -> str:
        """Take the longest text node in the card.

        The card's strings are [region, date, title, "Read more" x3]. The
        title is always the longest by a wide margin, which is sturdier than
        positional indexing into Webflow's wrapper divs. Without this the
        anchor text wins and every post is titled "Read more".
        """
        card = self._card(anchor)
        if card is not None:
            pieces = [self.clean_text(s) for s in card.stripped_strings]
            pieces = [
                p for p in pieces
                if len(p) > 12 and not self.DATE_TEXT.fullmatch(p)
            ]
            if pieces:
                return max(pieces, key=len)
        return super().extract_title(anchor)

    def extract_date_near(self, anchor):
        card = self._card(anchor)
        if card is not None:
            match = self.DATE_TEXT.search(card.get_text(" ", strip=True))
            if match:
                return self.parse_date(match.group(0))
        return super().extract_date_near(anchor)


class LSVPSource(BaseSource):
    """Lightspeed — stories. UNVERIFIED."""

    name = "Lightspeed"
    base_url = "https://lsvp.com"
    index_urls = ["https://lsvp.com/stories/"]
    post_url_pattern = r"lsvp\.com/(stories|blog)/[a-z0-9][a-z0-9-]+$"
    exclude_url_patterns = [r"/stories$", r"/blog$"]
    investment_title_patterns = COMMON_INVESTMENT_TITLES


class BessemerSource(BaseSource):
    """Bessemer — news. UNVERIFIED.

    bvp.com publishes "atlases" and memos alongside announcements, so the
    classifier matters here.
    """

    name = "Bessemer"
    base_url = "https://www.bvp.com"
    index_urls = ["https://www.bvp.com/news"]
    post_url_pattern = r"bvp\.com/(news|atlas)/[a-z0-9][a-z0-9-]+$"
    exclude_url_patterns = [r"/news$", r"/atlas$"]
    investment_title_patterns = COMMON_INVESTMENT_TITLES


class DesignerFundSource(BaseSource):
    """Designer Fund — blog. UNVERIFIED.

    The only firm here not present in vc-job-agent. Designer Fund posts far
    less frequently than the others and writes more about design practice than
    about rounds, so expect few investments and many classifier rejections.
    Probe for a WordPress or Ghost feed first — a small blog is the most likely
    of the fourteen to have one.
    """

    name = "Designer Fund"
    base_url = "https://designerfund.com"
    index_urls = ["https://designerfund.com/blog"]
    post_url_pattern = r"designerfund\.com/(blog|stories)/[a-z0-9][a-z0-9-]+$"
    exclude_url_patterns = [r"/blog$"]
    investment_title_patterns = COMMON_INVESTMENT_TITLES


# ===========================================================================

VERIFIED_SOURCES = [
    GreylockSource,
    A16ZSource,
    ContrarySource,
    GeneralCatalystSource,
    SequoiaSource,
    AntlerSource,
]

UNVERIFIED_SOURCES = [
    IndexVenturesSource,
    KleinerPerkinsSource,
    AccelSource,
    BatterySource,
    NEASource,
    LSVPSource,
    BessemerSource,
    DesignerFundSource,
]

ALL_SOURCES = VERIFIED_SOURCES + UNVERIFIED_SOURCES

assert len(ALL_SOURCES) == 14, f"Expected 14 sources, got {len(ALL_SOURCES)}"

SOURCES_BY_NAME = {cls.name.lower(): cls for cls in ALL_SOURCES}
