"""Offline tests for the blog-parsing logic.

These run against saved fixtures, so they test the parsing rules rather than
the network. That's deliberate: a test that hits 14 live marketing sites tells
you about today's weather, not about your code.

What these DON'T cover: whether the fixtures still resemble the live sites.
That's what `python -m src.probe` is for, and it needs to be re-run whenever a
source starts returning zero.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models import BlogPost, Investment, normalize_company_name  # noqa: E402
from src.sources.base import BaseSource, canonicalize_url  # noqa: E402
from src.sources.firms import (  # noqa: E402
    ALL_SOURCES,
    A16ZSource,
    AntlerSource,
    BatterySource,
    ContrarySource,
    GeneralCatalystSource,
    GreylockSource,
    SequoiaSource,
)
from src.state import collapse_syndicates  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load(name: str, source, page_url: str):
    with open(os.path.join(FIXTURES, name)) as f:
        soup = BeautifulSoup(f.read(), "lxml")
    return source.parse_index(soup, page_url)


class _StubResponse:
    def __init__(self, content: bytes):
        self.content = content
        self.text = content.decode("utf-8")
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        import json as _json
        return _json.loads(self.text)


def load_feed(name: str, source):
    """Run a feed-backed source's real scrape() against a fixture.

    Feed sources don't go through parse_index, so the HTML `load` helper can't
    reach them. Stubbing the session keeps the test offline while still
    exercising scrape() -- the code the pipeline actually calls.
    """
    with open(os.path.join(FIXTURES, name), "rb") as f:
        payload = f.read()
    source.request_delay = 0
    source.session.get = lambda *a, **k: _StubResponse(payload)  # type: ignore[method-assign]
    return source.scrape()


# ---------------------------------------------------------------------------
# URL canonicalization — the foundation of dedup layer 1
# ---------------------------------------------------------------------------

class TestCanonicalizeURL:
    def test_strips_tracking_params_but_keeps_real_ones(self):
        assert canonicalize_url(
            "https://a16z.com/announcement/investing-in-volta/?utm_source=newsletter"
        ) == "https://a16z.com/announcement/investing-in-volta"
        # NEA's topic filter is meaningful and must survive
        assert "topic=investment" in canonicalize_url(
            "https://www.nea.com/blog?type=Read&topic=investment"
        )

    def test_trailing_slash_and_case_normalized(self):
        a = canonicalize_url("https://Greylock.com/blog/introducing-oak/")
        b = canonicalize_url("https://greylock.com/blog/introducing-oak")
        assert a == b

    def test_fragment_dropped(self):
        assert canonicalize_url("https://x.com/blog/post#section-2") == \
            "https://x.com/blog/post"

    def test_relative_resolved_against_base(self):
        assert canonicalize_url("/blog/post-a", base="https://lsvp.com/stories/") == \
            "https://lsvp.com/blog/post-a"

    def test_rejects_non_http_schemes(self):
        assert canonicalize_url("mailto:hi@example.com") == ""
        assert canonicalize_url("javascript:void(0)") == ""

    def test_same_post_via_two_routes_dedups(self):
        """The whole point of layer 1: one post, one key."""
        variants = [
            "https://contrary.com/blog/investing-in-voltra",
            "https://contrary.com/blog/investing-in-voltra/",
            "https://contrary.com/blog/investing-in-voltra?utm_campaign=x",
            "https://contrary.com/blog/investing-in-voltra#top",
        ]
        assert len({canonicalize_url(v) for v in variants}) == 1


# ---------------------------------------------------------------------------
# Per-source index parsing
# ---------------------------------------------------------------------------

class TestGreylock:
    @pytest.fixture
    def posts(self):
        return load("greylock_index.html", GreylockSource(),
                    "https://greylock.com/blog/portfolio-news/")

    def test_finds_all_five_posts(self, posts):
        assert len(posts) == 5

    def test_excludes_category_landing_pages(self, posts):
        """The filter nav shares the /blog/ prefix — those must not become posts."""
        urls = {p.url for p in posts}
        for category in ("portfolio-news", "greymatter", "firm-news"):
            assert f"https://greylock.com/blog/{category}" not in urls

    def test_excludes_pagination_and_og_image(self, posts):
        urls = {p.url for p in posts}
        assert not any("/page/" in u for u in urls)
        assert not any("opengraph-image" in u for u in urls)

    def test_excludes_offsite_job_board(self, posts):
        assert not any("jobs.greylock.com" in p.url for p in posts)

    def test_title_taken_from_heading_not_card_chrome(self, posts):
        titles = {p.title for p in posts}
        assert "Introducing Oak: The AI-Native Identity Operating System" in titles
        # The card also contains "Oak logo" and "Portfolio News" — neither should win
        assert not any(t.startswith("Oak logo") for t in titles)

    def test_flags_introducing_posts_as_investments(self, posts):
        oak = next(p for p in posts if "oak" in p.url)
        assert oak.likely_investment is True

    def test_pagination_urls_wellformed(self):
        pages = list(GreylockSource().paginate("https://greylock.com/blog/portfolio-news/"))
        assert pages[0] == "https://greylock.com/blog/portfolio-news/"
        assert pages[1] == "https://greylock.com/blog/portfolio-news/page/2/"


class TestA16Z:
    @pytest.fixture
    def posts(self):
        return load("a16z_index.html", A16ZSource(), "https://a16z.com/news-content/")

    def test_only_announcement_posts_captured(self, posts):
        """The /announcement/ scope is what keeps podcasts out of the digest."""
        assert len(posts) == 5
        assert all("/announcement/" in p.url for p in posts)

    def test_podcasts_and_essays_excluded(self, posts):
        urls = " ".join(p.url for p in posts)
        assert "/podcast/" not in urls
        assert "lighthouse-or-landgrab" not in urls

    def test_category_pages_excluded(self, posts):
        assert not any("/category/" in p.url for p in posts)

    def test_all_flagged_as_investments_skipping_classifier(self, posts):
        """URL path alone is sufficient here — no LLM call needed."""
        assert all(p.likely_investment is True for p in posts)

    def test_focus_area_label_extracted(self, posts):
        volta = next(p for p in posts if "volta" in p.url)
        assert "Infra" in volta.labels


class TestContrary:
    @pytest.fixture
    def posts(self):
        return load("contrary_index.html", ContrarySource(), "https://contrary.com/blog")

    def test_finds_posts_including_featured(self, posts):
        assert len(posts) == 6

    def test_parses_plaintext_date_without_time_element(self, posts):
        """Contrary has no <time> tags, so the generic date lookup would miss."""
        base_power = next(p for p in posts if "base-power" in p.url)
        assert base_power.published_date == datetime(2025, 10, 8)

    def test_date_stripped_out_of_title(self, posts):
        base_power = next(p for p in posts if "base-power" in p.url)
        assert "October 8, 2025" not in base_power.title
        assert base_power.title.startswith("Investing in Base Power")

    def test_read_more_stripped_from_title(self, posts):
        assert not any(p.title.endswith("Read more") for p in posts)

    def test_featured_prefix_stripped(self, posts):
        cls = next(p for p in posts if "class-of-2026" in p.url)
        assert not cls.title.lower().startswith("featured")

    def test_dollar_amount_in_title_flags_investment(self, posts):
        leland = next(p for p in posts if "leland" in p.url)
        assert leland.likely_investment is True

    def test_non_investment_post_not_falsely_flagged(self, posts):
        """Venture Partner Applications is not a funding round."""
        vp = next(p for p in posts if "venture-partner-apps" in p.url)
        assert vp.likely_investment is None  # unknown, defer to the classifier


class TestGeneralCatalyst:
    @pytest.fixture
    def posts(self):
        return load("general_catalyst_index.html", GeneralCatalystSource(),
                    "https://www.generalcatalyst.com/stories")

    def test_finds_story_posts(self, posts):
        assert len(posts) == 5

    def test_external_press_links_excluded(self, posts):
        """The NEWS section links to techcrunch/forbes/wsj — not our posts."""
        urls = " ".join(p.url for p in posts)
        for host in ("techcrunch.com", "forbes.com", "wsj.com"):
            assert host not in urls

    def test_perspectives_path_excluded(self, posts):
        assert not any("/perspectives/" in p.url for p in posts)

    def test_pagination_link_excluded(self, posts):
        assert not any("_page=" in p.url for p in posts)

    def test_title_recovered_from_empty_anchor(self, posts):
        """GC's anchors are empty; the title has to come from the slug."""
        arca = next(p for p in posts if "arca" in p.url)
        assert arca.title
        assert "investment" in arca.title.lower()

    def test_topic_tags_extracted_as_sector_hints(self, posts):
        arca = next(p for p in posts if "arca" in p.url)
        assert "Fintech" in arca.labels


# ---------------------------------------------------------------------------
# Dedup layer 3 — syndicate collapse
# ---------------------------------------------------------------------------

class TestNormalizeCompanyName:
    @pytest.mark.parametrize("raw,expected", [
        ("Acme Inc.", "acme"),
        ("ACME", "acme"),
        ("  Acme  ", "acme"),
        ("Acme, Inc.", "acme"),
        ("Acme Corporation", "acme"),
        ("Acme Labs LLC", "acme labs"),
        ("Base Power", "base power"),
    ])
    def test_variants_collapse(self, raw, expected):
        assert normalize_company_name(raw) == expected

    def test_distinct_companies_stay_distinct(self):
        assert normalize_company_name("Neo") != normalize_company_name("Netris")


class TestCollapseSyndicates:
    def _inv(self, company, firm, amount=None, raw="Undisclosed", stage="Series B"):
        return Investment(
            company_name=company,
            round_stage=stage,
            funding_amount_usd=amount,
            funding_amount_raw=raw,
            vc_firms=[firm],
            source_posts=[BlogPost(
                url=f"https://{firm.lower()}.com/blog/{company.lower()}",
                title=f"Investing in {company}",
                vc_firm=firm,
            )],
        )

    def test_same_round_from_three_firms_becomes_one_entry(self):
        """The failure this prevents: one round, three cards, tripled dollars."""
        result = collapse_syndicates([
            self._inv("Acme", "Sequoia", 30_000_000, "$30M"),
            self._inv("Acme Inc.", "Index Ventures", 30_000_000, "$30M"),
            self._inv("ACME", "Greylock", 30_000_000, "$30M"),
        ])
        assert len(result) == 1
        assert sorted(result[0].vc_firms) == ["Greylock", "Index Ventures", "Sequoia"]
        assert result[0].funding_amount_usd == 30_000_000
        assert len(result[0].source_posts) == 3

    def test_different_stages_stay_separate(self):
        """A Seed and a Series A for the same company are two distinct events."""
        result = collapse_syndicates([
            self._inv("Acme", "Sequoia", stage="Seed"),
            self._inv("Acme", "Sequoia", stage="Series A"),
        ])
        assert len(result) == 2

    def test_disclosed_amount_beats_undisclosed(self):
        result = collapse_syndicates([
            self._inv("Acme", "Sequoia"),
            self._inv("Acme", "Greylock", 30_000_000, "$30M"),
        ])
        assert result[0].funding_amount_usd == 30_000_000

    def test_conflicting_amounts_lower_confidence(self):
        """Silently picking one number is the dangerous behavior — flag instead."""
        result = collapse_syndicates([
            self._inv("Acme", "Sequoia", 30_000_000, "$30M"),
            self._inv("Acme", "Greylock", 300_000_000, "$300M"),
        ])
        assert result[0].confidence == "low"
        assert "disagree" in result[0].notes.lower()

    def test_richer_description_wins(self):
        a = self._inv("Acme", "Sequoia")
        a.company_description = "Short."
        b = self._inv("Acme", "Greylock")
        b.company_description = "A much longer and more useful description of Acme."
        result = collapse_syndicates([a, b])
        assert result[0].company_description == b.company_description


# ---------------------------------------------------------------------------
# Content hashing — dedup layer 2
# ---------------------------------------------------------------------------

class TestContentHash:
    def test_same_content_different_url_same_hash(self):
        a = BlogPost(url="https://x.com/blog/a", title="Investing in Acme",
                     vc_firm="X", body="We are thrilled to back Acme.")
        b = BlogPost(url="https://x.com/blog/a-v2", title="Investing in Acme",
                     vc_firm="X", body="We are thrilled to back Acme.")
        assert a.content_hash == b.content_hash

    def test_different_content_different_hash(self):
        a = BlogPost(url="https://x.com/blog/a", title="Investing in Acme",
                     vc_firm="X", body="We are thrilled to back Acme.")
        b = BlogPost(url="https://x.com/blog/b", title="Investing in Bcme",
                     vc_firm="X", body="We are thrilled to back Bcme.")
        assert a.content_hash != b.content_hash


# ---------------------------------------------------------------------------
# Registry sanity
# ---------------------------------------------------------------------------

class TestHostScoping:
    """The same-host check in is_post_url, tested directly.

    Most sources embed their domain in post_url_pattern, which makes the host
    check redundant for them — a mutation test showed the General Catalyst
    external-press-link assertion passing even with the host check removed.
    But the check is the only thing protecting a source whose pattern is
    path-only, which is the natural way to write one. So it needs its own test.
    """

    def _path_only_source(self):
        class PathOnlySource(BaseSource):
            name = "PathOnly"
            base_url = "https://example.com"
            index_urls = ["https://example.com/blog"]
            post_url_pattern = r"/blog/[a-z0-9-]+$"  # no domain in the pattern
            exclude_url_patterns = [r"/blog$"]
        return PathOnlySource()

    def test_offsite_url_rejected_even_when_path_matches(self):
        source = self._path_only_source()
        assert source.is_post_url("https://example.com/blog/our-post") is True
        # Same path shape, different host — must be rejected
        assert source.is_post_url("https://techcrunch.com/blog/some-article") is False

    def test_subdomain_is_a_different_host(self):
        """jobs.greylock.com is not greylock.com."""
        source = self._path_only_source()
        assert source.is_post_url("https://jobs.example.com/blog/a-post") is False

    def test_www_is_the_same_host(self):
        """www.example.com and example.com are one site.

        Real case: Sequoia's index is served from www.sequoiacap.com but every
        entry in its RSS feed points at sequoiacap.com. A literal netloc
        comparison drops the entire feed.
        """
        source = self._path_only_source()  # base_url is https://example.com
        assert source.is_post_url("https://www.example.com/blog/a-post") is True

    def test_www_equivalence_is_symmetric(self):
        """The www-form base_url must accept bare-host post URLs too."""
        class WwwSource(BaseSource):
            name = "Www"
            base_url = "https://www.example.com"
            index_urls = ["https://www.example.com/blog"]
            post_url_pattern = r"/blog/[a-z0-9-]+$"
            exclude_url_patterns = [r"/blog$"]

        source = WwwSource()
        assert source.is_post_url("https://example.com/blog/a-post") is True
        assert source.is_post_url("https://jobs.example.com/blog/a-post") is False

    def test_www_stripping_does_not_merge_unrelated_hosts(self):
        """Only the `www.` label is stripped, not any leading label."""
        from src.sources.base import registrable_host
        assert registrable_host("https://www.example.com/x") == "example.com"
        assert registrable_host("https://example.com/x") == "example.com"
        # Not a `www.` prefix — must survive intact
        assert registrable_host("https://wwwfoo.example.com/x") == "wwwfoo.example.com"
        assert registrable_host("https://jobs.example.com/x") == "jobs.example.com"

    def test_external_links_filtered_from_real_index(self):
        """End-to-end: GC's index carries wsj/forbes/techcrunch links."""
        posts = load("general_catalyst_index.html", GeneralCatalystSource(),
                     "https://www.generalcatalyst.com/stories")
        assert posts, "fixture should yield posts"
        assert all(
            "generalcatalyst.com" in p.url for p in posts
        ), "an off-site link leaked into the results"


class TestAntler:
    @pytest.fixture
    def posts(self):
        return load("antler_index.html", AntlerSource(),
                    "https://www.antler.co/newsroom")

    def test_finds_press_releases(self, posts):
        assert len(posts) == 3
        assert all("/press-releases/" in p.url for p in posts)

    def test_location_and_legal_pages_excluded(self, posts):
        """18 /location/<country> links share the two-segment shape of a post."""
        urls = " ".join(p.url for p in posts)
        assert "/location/" not in urls
        assert "/legal/" not in urls
        assert "/insights" not in urls

    def test_pagination_links_excluded(self, posts):
        assert not any("_page=" in p.url for p in posts)

    def test_title_comes_from_the_card_not_the_anchor(self, posts):
        """The anchor is a bare "Read more"; the title is a sibling.

        Without the card walk every post is titled "Read more", which is what
        the source did when its pattern was first corrected.
        """
        titles = {p.title for p in posts}
        assert "Read more" not in titles
        assert any(t.startswith("Agentio raises $40M Series B") for t in titles)

    def test_plaintext_date_parsed(self, posts):
        """Antler has no <time> element, so the generic lookup finds nothing."""
        agentio = next(p for p in posts if "agentio" in p.url)
        assert agentio.published_date == datetime(2026, 7, 22)
        assert all(p.published_date is not None for p in posts)

    def test_date_and_region_kept_out_of_the_title(self, posts):
        for post in posts:
            assert "July" not in post.title
            assert not post.title.startswith(("Global", "Asia"))

    def test_firm_fundraise_still_reaches_the_classifier(self, posts):
        """"Antler Raises additional $510 Million" is the firm, not a portfolio
        company -- but it matches the title patterns, so the flag says True and
        the classifier has to reject it on the body. Documenting the gap."""
        antler_raise = next(p for p in posts if "510-million" in p.url)
        assert antler_raise.likely_investment is True


class TestBatteryWordPress:
    """Battery is read through the WordPress REST API.

    Its /news/ index links only to outbound press coverage, so no pattern over
    /news/ could ever have matched — the first-party writing is at /blog/.
    """

    def _posts(self):
        return load_feed("battery_wp_posts.json", BatterySource())

    def test_discovers_posts_from_wp_api(self):
        posts = self._posts()
        assert len(posts) == 4  # 5 records, one is a category archive

    def test_category_archive_excluded(self):
        assert not any("/category/" in p.url for p in self._posts())

    def test_wp_api_supplies_dates(self):
        posts = self._posts()
        assert all(p.published_date is not None for p in posts)
        newest = max(posts, key=lambda p: p.published_date)
        assert newest.published_date.date() == datetime(2026, 7, 30).date()

    def test_html_entities_decoded_in_titles(self):
        """WordPress returns title.rendered with entities still encoded."""
        titles = {p.title for p in self._posts()}
        assert not any("&#8216" in t or "&#822" in t for t in titles)

    def test_funding_post_flagged(self):
        by_slug = {p.url.rsplit("/", 1)[-1]: p for p in self._posts()}
        assert by_slug["backing-nomad-data-series-a"].likely_investment is True

    def test_research_essays_left_to_the_classifier(self):
        """Most of this blog is commentary; the flag must not guess False."""
        by_slug = {p.url.rsplit("/", 1)[-1]: p for p in self._posts()}
        assert by_slug[
            "how-agentic-coding-is-reshaping-the-software-development-lifecycle"
        ].likely_investment is None


class TestSequoiaFeed:
    """Sequoia is feed-backed: its HTML index is client-rendered and yields
    nothing, so scrape() reads /feed/ instead."""

    def _posts(self):
        return load_feed("sequoia_feed.xml", SequoiaSource())

    def test_discovers_posts_from_feed(self):
        posts = self._posts()
        assert len(posts) == 5

    def test_bare_host_links_are_accepted(self):
        """Feed links use sequoiacap.com; base_url is www.sequoiacap.com.

        Without www-insensitive host matching every entry is discarded and
        this source silently returns zero.
        """
        posts = self._posts()
        assert posts, "feed entries were rejected by the host check"
        assert all("sequoiacap.com" in p.url for p in posts)

    def test_feed_supplies_real_publication_dates(self):
        """The reason to prefer a feed: the HTML index has no dates at all."""
        posts = self._posts()
        assert all(p.published_date is not None for p in posts)
        by_url = {p.url.rsplit("/", 1)[-1]: p for p in posts}
        assert by_url["americas-open-model-paradox"].published_date.date() == \
            datetime(2026, 7, 24).date()

    def test_funding_tag_gates_the_classifier(self):
        """The tag alone must be sufficient, with no help from the title.

        "All Systems Nominal" matches no investment_title_pattern, so if this
        passes, investment_label_patterns is genuinely doing the work. The
        obvious version of this test -- asserting on the "Partnering with X"
        posts -- passes via the title pattern even with the tag gate deleted.
        """
        by_slug = {p.url.rsplit("/", 1)[-1]: p for p in self._posts()}
        post = by_slug["all-systems-nominal"]
        assert post.likely_investment is True
        assert "Funding announcement" in post.labels
        # Guard the premise: if Sequoia's title rules ever start matching this
        # title, the test silently stops proving anything.
        source = SequoiaSource()
        assert source.looks_like_investment(post.url, post.title, labels=[]) is None

    def test_tagged_posts_matching_title_convention_also_flagged(self):
        by_slug = {p.url.rsplit("/", 1)[-1]: p for p in self._posts()}
        assert by_slug["partnering-with-sable-closing-the-diffusion-gap"].likely_investment is True

    def test_untagged_posts_stay_undecided_not_rejected(self):
        """likely_investment is three-valued: True or None, never False.

        An essay and an acquisition post both go to the classifier rather than
        being dropped here, because only the classifier reads the body.
        """
        by_slug = {p.url.rsplit("/", 1)[-1]: p for p in self._posts()}
        assert by_slug["americas-open-model-paradox"].likely_investment is None
        assert by_slug["cyera-and-oasis-stronger-together"].likely_investment is None

    def test_tags_are_captured_as_labels(self):
        by_slug = {p.url.rsplit("/", 1)[-1]: p for p in self._posts()}
        labels = by_slug["partnering-with-bunkerhill-health-ai-agents-that-improve-patient-outcomes"].labels
        assert "Healthcare" in labels and "AI" in labels


class TestRegistry:
    def test_exactly_fourteen_sources(self):
        assert len(ALL_SOURCES) == 14

    def test_names_unique(self):
        names = [c.name for c in ALL_SOURCES]
        assert len(names) == len(set(names))

    def test_every_source_declares_an_index_url(self):
        for cls in ALL_SOURCES:
            assert cls.index_urls, f"{cls.name} has no index_urls"

    def test_every_source_scoped_to_its_own_host(self):
        """Guards against a copy-paste error sending one firm's scraper at another."""
        from urllib.parse import urlparse
        for cls in ALL_SOURCES:
            host = urlparse(cls.base_url).netloc.lower()
            assert host, f"{cls.name} has no base_url"
            for index in cls.index_urls:
                assert urlparse(index).netloc.lower() == host, \
                    f"{cls.name}: index_url host does not match base_url"

    def test_post_patterns_reject_index_pages(self):
        """A pattern that matches its own index page produces a phantom post."""
        for cls in ALL_SOURCES:
            source = cls()
            for index in cls.index_urls:
                assert not source.is_post_url(canonicalize_url(index)), \
                    f"{cls.name}: post_url_pattern matches its own index page"
