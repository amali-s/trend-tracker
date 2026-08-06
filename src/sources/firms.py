"""The 14 VC blog sources.

VERIFICATION STATUS — read this before trusting any class below.

Patterns here reflect what the pages actually served when probed:

    Greylock           VERIFIED  static HTML (Next.js + Sanity), /page/N/ pagination
    a16z               VERIFIED  static HTML (WordPress), investments at /announcement/
    Contrary           VERIFIED  static HTML, dates on index, "Load more" button
    General Catalyst   VERIFIED  static HTML (Webflow), ?<hash>_page=N pagination
    Sequoia            VERIFIED  RSS feed; HTML index is client-rendered (2026-08-06)
    Antler             VERIFIED  static HTML; posts at /press-releases/ (2026-08-06)
    Battery Ventures   VERIFIED  WordPress REST; /news/ is outbound-only (2026-08-06)
    Kleiner Perkins    VERIFIED  RSS feed, chosen over a working index for dates (2026-08-06)
    Lightspeed         VERIFIED  WordPress REST; /feed/ is a decoy (2026-08-06)
    Designer Fund      VERIFIED  static HTML (Framer); no feed exists (2026-08-06)
    Index Ventures     VERIFIED  static HTML; title from slug, not the card (2026-08-06)
    Accel              VERIFIED  static HTML; filter tabs look like posts (2026-08-06)
    NEA                VERIFIED  static HTML; first heading is often a label (2026-08-06)
    Bessemer           VERIFIED  static HTML; nav steals a post URL (2026-08-06)

All fourteen are now verified against the live sites and covered by fixture
tests. UNVERIFIED_SOURCES is empty and kept only so a newly added source has
somewhere to sit until it is probed.

`python -m src.probe` remains the check that matters: these are marketing
sites that redesign without notice, and a fixture test proves the parser still
handles the page as it was on 2026-08-06, not as it is today. Re-run the probe
whenever a source starts returning zero.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ..models import BlogPost
from .base import BaseSource
from .rss_base import RSSSource, WordPressSource

# "October 8, 2025" as it appears in card markup. Several of these sites render
# the date as plain text with no <time> element, so the generic date lookup in
# BaseSource finds nothing and each has to reach for this.
PLAINTEXT_DATE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2},\s+\d{4}\b"
)

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
    DATE_TEXT = PLAINTEXT_DATE

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
    """Index Ventures — perspectives. Verified 2026-08-06.

    Not a client-rendered Vue app after all — vc-job-agent found that on the
    *jobs* side of the domain, but /perspectives/ ships 62 post links in the
    initial HTML and needs no Playwright.

    The title has to come from the slug, which is unusual enough to explain.
    Every post anchor is a bare "Read more Opens in a new window." with no
    heading inside it, and the surrounding card describes the *founder* rather
    than the post: the card for "Simulating Society at Scale: Our Investment
    in Similes' $200M Series B" reads "Joon Sung Park. Multidisciplinary
    artist. Agent architect. Simulator of worlds."

    So the usual card-walk that fixed Antler is actively wrong here -- it
    would title the post with a person's name and tagline. The slug is the
    only thing on the page that describes the post, and it is unusually rich:
    simulating-society-at-scale-our-investment-in-similes-200m-series-b.

    Consequence to keep in mind: slug-derived titles lose punctuation and
    case, so "$200M" arrives as "200M". That is good enough for the
    likely_investment heuristics but not for display, and the detail page's
    <h1> should replace it once fetch_post_detail runs. Do not use these
    titles in the email without that step.

    No dates anywhere on the index and no feed, so dates come from the detail
    page or not at all. Mostly long-form essays and founder profiles, so
    expect heavy classifier rejection.
    """

    name = "Index Ventures"
    base_url = "https://www.indexventures.com"
    index_urls = ["https://www.indexventures.com/perspectives/"]
    post_url_pattern = r"indexventures\.com/perspectives/[a-z0-9][a-z0-9-]+$"
    exclude_url_patterns = [r"/perspectives$"]
    investment_title_patterns = COMMON_INVESTMENT_TITLES

    # "Read more", "Opens in a new window." and similar chrome — never a title.
    CTA_TEXT = re.compile(
        r"^(read more|read|learn more|opens in a new window\.?|\s|·|→)+$",
        re.IGNORECASE,
    )

    def extract_title(self, anchor) -> str:
        text = self.clean_text(anchor.get_text(" ", strip=True))
        if text and not self.CTA_TEXT.match(text):
            return text
        slug = urlparse(anchor.get("href", "")).path.rstrip("/").split("/")[-1]
        return slug.replace("-", " ").title() if slug else ""


class KleinerPerkinsSource(RSSSource):
    """Kleiner Perkins — perspectives, via RSS. Verified 2026-08-06.

    The inferred /perspectives/<slug> path was right and the HTML index does
    work, returning 6 posts. It is used anyway only as a fallback, because the
    HTML index carries no dates at all while the feed carries a real pubDate
    on every entry. Dates are worth more here than the extra reach.

    The trade-off taken knowingly: the feed is site-wide, so it loses the
    /category/announcements scoping the index URL provided. More essays reach
    the classifier as a result. That is the cheaper mistake -- the scoped index
    gave no dates, and an announcement-only feed does not exist.

    Note for dedup: this feed republishes the same post under two slugs.
    "K2 Space: Building Bigger" appears at both /k2-space-building-bigger/ and
    /k2-space-building-bigger-2/, tagged 'Perspectives' on one and
    'Media','Portfolio Perspectives' on the other, and "CuspAI: A Search
    Engine..." does the same with only a capitalisation difference in the
    title. Layer 1 sees two distinct URLs and lets both through; this is
    exactly the case dedup layer 2 (content hash of title + body) exists for,
    and it is worth confirming against this source once bodies are fetched.
    """

    name = "Kleiner Perkins"
    base_url = "https://www.kleinerperkins.com"
    index_urls = ["https://www.kleinerperkins.com/perspectives/category/announcements"]
    feed_urls = ["https://www.kleinerperkins.com/feed/"]
    post_url_pattern = r"kleinerperkins\.com/perspectives/[a-z0-9][a-z0-9-]+$"
    exclude_url_patterns = [r"/perspectives(/category.*)?$"]
    investment_title_patterns = COMMON_INVESTMENT_TITLES


class AccelSource(BaseSource):
    """Accel — portfolio news. Verified 2026-08-06.

    Posts are at /news/<slug>; the /noteworthies/ half of the inferred pattern
    matched nothing and is dropped. No __NEXT_DATA__ blob on www.accel.com --
    that was the jobs subdomain.

    Two things the raw pattern got wrong. The category filter tabs are
    themselves /news/<word> URLs -- /news/insights, /news/podcasts,
    /news/portfolio -- so they matched and arrived as posts titled "Insights"
    and "Podcasts". They are excluded by name rather than by length, because a
    real slug can be short too.

    Each card's anchor text is "<category> <title> <date>" with no heading
    element, e.g. "Portfolio News Cyera + Oasis: Securing the AI-Native
    Enterprise July 28, 2026". Both ends are stripped, which also gives a
    date -- Accel is one of the few of the fourteen to publish one on the
    index.

    Note for the classifier: this index carries acquisitions alongside rounds
    ("Cognition Acquires TierZero"), and the category is "Portfolio News" for
    both.
    """

    name = "Accel"
    base_url = "https://www.accel.com"
    index_urls = ["https://www.accel.com/news/portfolio"]
    post_url_pattern = r"accel\.com/news/[a-z0-9][a-z0-9-]{3,}$"
    exclude_url_patterns = [
        r"/news(/portfolio)?$",
        r"/news/(insights|podcasts|portfolio|all|news)$",
    ]
    investment_title_patterns = COMMON_INVESTMENT_TITLES

    # Card category labels, printed before the title in the same anchor.
    CATEGORY_PREFIX = re.compile(
        r"^(Portfolio News|Insights|Podcasts|News|All)\s+", re.IGNORECASE
    )

    def extract_title(self, anchor) -> str:
        text = super().extract_title(anchor)
        text = self.CATEGORY_PREFIX.sub("", text)
        text = PLAINTEXT_DATE.sub("", text)
        return self.clean_text(text)

    def extract_date_near(self, anchor):
        match = PLAINTEXT_DATE.search(anchor.get_text(" ", strip=True))
        if match:
            return self.parse_date(match.group(0))
        return super().extract_date_near(anchor)


class BatterySource(WordPressSource):
    """Battery Ventures — blog, via the WordPress REST API. Verified 2026-08-06.

    The /news/ index was the wrong page. It is not "a mix of first-party posts
    and press links" as guessed -- it is *entirely* outbound: every article
    link on it points at businesswire.com, cfo.com, forterro.com and the like.
    The same-host check correctly drops all of them, which is why the source
    returned zero and why no pattern over /news/ could have fixed it. The
    /news/page/N links are real, but every page is more of the same.

    Battery's own writing is at /blog/<slug>/, exposed through WordPress at
    /wp-json/wp/v2/posts. The REST API is preferred over /feed/ because the
    feed caps at 4 items, which is too shallow to guarantee a full week.

    Honest limitation: this blog is largely research and market commentary
    ("Measuring AI ROI", "How Agentic Coding Is Reshaping the SDLC") rather
    than round announcements, and Battery appears to route portfolio funding
    news to the outbound press coverage on /news/ instead. Expect this source
    to contribute few investments and many classifier rejections. That is the
    site's editorial shape, not a broken parser -- do not read a zero here as
    breakage without re-probing.
    """

    name = "Battery Ventures"
    base_url = "https://www.battery.com"
    index_urls = ["https://www.battery.com/blog"]
    wp_api_base = "https://www.battery.com/wp-json/wp/v2"
    post_url_pattern = r"battery\.com/blog/[a-z0-9][a-z0-9-]+$"
    exclude_url_patterns = [r"/blog$", r"/blog/category/"]
    investment_title_patterns = COMMON_INVESTMENT_TITLES
    max_pages = 1
    per_page = 50


class NEASource(BaseSource):
    """NEA — blog. Verified 2026-08-06.

    Static HTML at /blog/<slug>, as inferred. Not an SPA.

    Titles needed work. Each card anchor stacks several headings and the
    first one is frequently not the title -- it is either a content-type
    label ("Blog", "Read", "Listen") or a newsletter series marker ("The
    Current #16"), with the real title in the *next* heading. Taking the
    first heading, which is what BaseSource does, produced posts titled
    "Blog" and "The Current #16". extract_title skips the known chrome and
    takes the first heading that is not a label.

    Cards also append an author line ("Thomas Joshi, Madison Faulkner, Lila
    Tretikov and Andrew Schoen") and a "Read More" CTA, which is why the
    heading is used rather than the anchor's flat text.

    **The `?topic=investment` filter is not doing what the URL implies.** The
    results are dominated by essays and by "The Current", a recurring
    consumer-data newsletter, rather than funding announcements. The filter is
    kept because it does no harm, but the classifier carries the load here --
    do not treat this source's volume as deal flow.

    On the feed, which the probe reports as 1430 items: it is served from
    `statamic.nea.com`, their headless CMS backend, not from www.nea.com. Its
    entries are a different host and mix /team/ and /portfolio/ records in
    with /blog/. Rewriting the host would work today and would supply real
    dates, but it means depending on a backend origin that is not the
    published site and pulling the entire archive weekly. Rejected on both
    counts; dates come from the detail page instead. Revisit if the index
    ever starts exposing dates.
    """

    name = "NEA"
    base_url = "https://www.nea.com"
    index_urls = ["https://www.nea.com/blog?type=Read&topic=investment"]
    post_url_pattern = r"nea\.com/blog/[a-z0-9][a-z0-9-]+$"
    exclude_url_patterns = [r"/blog$"]
    investment_title_patterns = COMMON_INVESTMENT_TITLES
    max_pages = 3

    # Headings that are card chrome rather than a post title.
    HEADING_CHROME = re.compile(
        r"^(blog|read|listen|watch|featured|news|the current\s*#?\d*)$",
        re.IGNORECASE,
    )

    def paginate(self, index_url: str) -> Iterable[str]:
        for n in range(1, self.max_pages + 1):
            yield f"{index_url}&page={n}"

    def extract_title(self, anchor) -> str:
        for heading in anchor.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
            text = self.clean_text(heading.get_text(" ", strip=True))
            if len(text) > 3 and not self.HEADING_CHROME.match(text):
                return text
        return super().extract_title(anchor)


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
    DATE_TEXT = PLAINTEXT_DATE

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


class LSVPSource(WordPressSource):
    """Lightspeed — stories, via the WordPress REST API. Verified 2026-08-06.

    Two wrong paths were ruled out before this one.

    The HTML index "works" -- 32 matches -- but the post anchors are empty, so
    every title fell back to the slug and lost its punctuation:
    "Lightspeed Announces Lead Investment In Anthropics 3 5B Series E
    Financing". A round size rendered as "3 5B" is worse than useless to the
    extractor, which has to read a dollar figure out of it.

    /feed/ is a decoy. It parses cleanly and returns 10 dated entries, which
    is exactly why it is dangerous -- but they are /founder/<name> and
    /company/<slug> CMS records, not stories. A source pointed at it would
    look healthy in the probe and never surface a single announcement.

    /wp-json/wp/v2/posts is the real one: /stories/<slug> with titles intact
    ("Audit's Moment Has Arrived. Why We Invested in Andera.") and a real
    publication date on every item.
    """

    name = "Lightspeed"
    base_url = "https://lsvp.com"
    index_urls = ["https://lsvp.com/stories/"]
    wp_api_base = "https://lsvp.com/wp-json/wp/v2"
    post_url_pattern = r"lsvp\.com/(stories|blog)/[a-z0-9][a-z0-9-]+$"
    exclude_url_patterns = [r"/stories$", r"/blog$"]
    investment_title_patterns = COMMON_INVESTMENT_TITLES
    max_pages = 1
    per_page = 50


class BessemerSource(BaseSource):
    """Bessemer — news. Verified 2026-08-06.

    Static HTML at /news/<slug>, and the richest of the fourteen: the index
    lists the whole archive in one page, ~207 posts. That is heavy for a
    weekly run but harmless once seeded, since dedup layer 1 drops everything
    already seen. Expect a large first `--seed`.

    Titles come from a real <h2> inside each card, so they were already clean.
    One anchor was not: the site nav links "Funds > Flagship" straight at
    /news/expanding-opportunities-with-3-85-billion-for-early-stage-investments,
    and because parse_index takes the first anchor it finds for a URL, that
    post arrived titled "Flagship". extract_title returns "" for a headingless
    nav-shaped anchor, which makes parse_index skip it *without* marking the
    URL as seen -- so the real card later in the page still claims it.

    Dates are published, but in an unusual "7.29.26" form rather than the
    "July 29, 2026" every other site here uses, which is why the shared
    PLAINTEXT_DATE finds nothing.

    bvp.com publishes "atlases" and long memos alongside announcements, and
    its own fund news ("$3.85 billion for early-stage investments") reads
    exactly like a portfolio round, so the classifier matters here.
    """

    name = "Bessemer"
    base_url = "https://www.bvp.com"
    index_urls = ["https://www.bvp.com/news"]
    post_url_pattern = r"bvp\.com/(news|atlas)/[a-z0-9][a-z0-9-]+$"
    exclude_url_patterns = [r"/news$", r"/atlas$"]
    investment_title_patterns = COMMON_INVESTMENT_TITLES

    # Bessemer stamps cards "7.29.26" — month.day.two-digit-year.
    NUMERIC_DATE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2})\b")

    def extract_title(self, anchor) -> str:
        heading = anchor.find(["h1", "h2", "h3", "h4", "h5", "h6"])
        if heading:
            text = self.clean_text(heading.get_text(" ", strip=True))
            if len(text) > 3:
                return text

        # No heading: this is nav chrome pointed at a post URL, not a card.
        # Returning "" makes parse_index skip it without recording the URL,
        # leaving the real card free to claim it further down the page.
        text = self.clean_text(anchor.get_text(" ", strip=True))
        if len(text) < 25 and not self.NUMERIC_DATE.search(text):
            return ""
        return text

    def extract_date_near(self, anchor):
        match = self.NUMERIC_DATE.search(anchor.get_text(" ", strip=True))
        if match:
            month, day, year = (int(g) for g in match.groups())
            try:
                return datetime(2000 + year, month, day)
            except ValueError:
                return None
        return super().extract_date_near(anchor)


class DesignerFundSource(BaseSource):
    """Designer Fund — blog. Verified 2026-08-06.

    The healthiest of the inferred sources: the pattern was already right and
    titles came out clean, because this is a Framer site that puts a real <h1>
    inside each card anchor. 31 posts.

    No feed, despite being the small blog most likely of the fourteen to have
    one — /feed/, /rss/, /feed.xml and the WordPress REST path were all
    probed and none exists.

    The only fix needed was the date. Framer renders it as plain text next to
    a "New Feature" badge rather than in a <time> element, so the generic
    lookup found nothing and every post came back undated. Note the <h1> is
    emitted twice per card (Framer's ssr-variant duplication); extract_title
    takes the first, so the duplication is harmless.

    The only firm here not present in vc-job-agent. It posts infrequently and
    writes about design practice far more than about rounds, so expect few
    investments and many classifier rejections.
    """

    name = "Designer Fund"
    base_url = "https://designerfund.com"
    index_urls = ["https://designerfund.com/blog"]
    post_url_pattern = r"designerfund\.com/(blog|stories)/[a-z0-9][a-z0-9-]+$"
    exclude_url_patterns = [r"/blog$"]
    investment_title_patterns = COMMON_INVESTMENT_TITLES

    def extract_date_near(self, anchor):
        match = PLAINTEXT_DATE.search(anchor.get_text(" ", strip=True))
        if match:
            return self.parse_date(match.group(0))
        return super().extract_date_near(anchor)


# ===========================================================================

VERIFIED_SOURCES = [
    GreylockSource,
    A16ZSource,
    ContrarySource,
    GeneralCatalystSource,
    SequoiaSource,
    AntlerSource,
    BatterySource,
    KleinerPerkinsSource,
    LSVPSource,
    DesignerFundSource,
    IndexVenturesSource,
    AccelSource,
    NEASource,
    BessemerSource,
]

UNVERIFIED_SOURCES: list = []

ALL_SOURCES = VERIFIED_SOURCES + UNVERIFIED_SOURCES

assert len(ALL_SOURCES) == 14, f"Expected 14 sources, got {len(ALL_SOURCES)}"

SOURCES_BY_NAME = {cls.name.lower(): cls for cls in ALL_SOURCES}
