"""Offline tests for the orchestrator.

No network, no API, no SMTP. Sources are stubbed to return canned posts, the
extractor is stubbed to return canned investments, and `state`'s file
constants are pointed at a tmp dir so a test run can't touch real memory.

The two that matter most are the PLAN §11 checks: run twice and the second
run finds nothing new (dedup), and a round two firms both blogged collapses to
one card (syndicate merge).
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import main, state  # noqa: E402
from src.models import BlogPost, Investment  # noqa: E402
from src.sources.base import BaseSource  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Point every state file at a fresh tmp dir."""
    monkeypatch.setattr(state, "SEEN_POSTS_FILE", str(tmp_path / "seen_posts.json"))
    monkeypatch.setattr(state, "SEEN_INVESTMENTS_FILE", str(tmp_path / "seen_investments.json"))
    monkeypatch.setattr(state, "WEEKLY_HISTORY_FILE", str(tmp_path / "weekly_history.json"))
    return tmp_path


def make_source(name: str, posts: list[BlogPost], *, fail: bool = False):
    """A BaseSource subclass that returns canned posts without touching the net."""

    class _Stub(BaseSource):
        pass

    _Stub.name = name
    _Stub.base_url = f"https://{name.lower().replace(' ', '')}.example.com"

    def scrape(self):
        if fail:
            raise RuntimeError("boom")
        return list(posts)

    def fetch_post_detail(self, post):
        if not post.body:
            post.body = f"Body for {post.title}."
        return post

    _Stub.scrape = scrape
    _Stub.fetch_post_detail = fetch_post_detail
    return _Stub


def post(firm: str, slug: str, **kw) -> BlogPost:
    return BlogPost(
        url=f"https://{firm.lower()}.example.com/blog/{slug}",
        title=kw.pop("title", slug.replace("-", " ").title()),
        vc_firm=firm,
        **kw,
    )


def investment(company="Acme", stage="Series B", firms=None, usd=30_000_000) -> Investment:
    return Investment(
        company_name=company,
        funding_amount_usd=usd,
        funding_amount_raw=f"${usd // 1_000_000}M",
        sector="AI Infrastructure",
        round_stage=stage,
        vc_firms=list(firms or ["Greylock"]),
    )


class StubExtractor:
    """Returns a fixed set of investments, ignoring the posts it's given."""

    def __init__(self, investments):
        self._investments = investments
        self.run_called_with = None

    def run(self, posts):
        self.run_called_with = list(posts)
        # Faithful to the real extractor: no posts in, no investments out.
        if not posts:
            return [], []
        return list(self._investments), []


def args(**kw):
    defaults = dict(seed=False, dry_run=False, source=None,
                    no_extract=False, week=None, verbose=False)
    defaults.update(kw)
    import argparse
    return argparse.Namespace(**defaults)


def capturing_deliver():
    calls = []

    def deliver(digest, dry_run):
        calls.append((digest, dry_run))
        return True

    deliver.calls = calls
    return deliver


# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------

class TestParseArgs:
    def test_defaults(self):
        a = main.parse_args([])
        assert not a.seed and not a.dry_run and not a.no_extract
        assert a.source is None and a.week is None

    def test_flags(self):
        a = main.parse_args(["--seed", "--source", "a16z", "-v"])
        assert a.seed is True
        assert a.source == "a16z"
        assert a.verbose is True


class TestSelectSources:
    def test_all_by_default(self):
        assert len(main._select_sources(None)) == 14

    def test_one_by_name_case_insensitive(self):
        selected = main._select_sources("A16Z")
        assert len(selected) == 1
        assert selected[0].name == "a16z"

    def test_lightspeed_alias_matches_registered_name(self):
        # The README says LSVP; the class registers name="Lightspeed".
        assert main._select_sources("lightspeed")[0].name == "Lightspeed"

    def test_unknown_source_is_an_error(self):
        with pytest.raises(ValueError, match="Unknown source"):
            main._select_sources("nonesuch")


# ---------------------------------------------------------------------------
# Parallel scrape and failure isolation
# ---------------------------------------------------------------------------

class TestScrapeAll:
    def test_collects_posts_from_every_source(self):
        a = make_source("A", [post("A", "one"), post("A", "two")])
        b = make_source("B", [post("B", "three")])
        _, posts, summaries = main.scrape_all([a, b])
        assert len(posts) == 3
        assert {s.source: s.posts_found for s in summaries} == {"A": 2, "B": 1}

    def test_one_failing_source_does_not_sink_the_others(self):
        good = make_source("Good", [post("Good", "x")])
        bad = make_source("Bad", [], fail=True)
        _, posts, summaries = main.scrape_all([good, bad])
        assert len(posts) == 1
        by_name = {s.source: s for s in summaries}
        assert by_name["Good"].error is None
        assert by_name["Bad"].error == "boom"
        assert by_name["Bad"].healthy is False


# ---------------------------------------------------------------------------
# --seed
# ---------------------------------------------------------------------------

class TestSeed:
    def test_records_urls_and_sends_nothing(self, isolated_state):
        src = make_source("A", [post("A", "one"), post("A", "two")])
        deliver = capturing_deliver()
        ex = StubExtractor([investment()])

        digest = main.run_pipeline(
            args(seed=True), source_classes=[src], extractor=ex, deliver_fn=deliver,
        )

        assert digest is None
        assert deliver.calls == []          # nothing sent
        assert ex.run_called_with is None   # extractor never invoked
        saved = json.load(open(state.SEEN_POSTS_FILE))
        assert len(saved) == 2

    def test_a_real_run_after_seed_finds_nothing_new(self, isolated_state):
        posts = [post("A", "one")]
        src = make_source("A", posts)
        main.run_pipeline(args(seed=True), source_classes=[src], extractor=StubExtractor([]))

        deliver = capturing_deliver()
        ex = StubExtractor([investment()])
        digest = main.run_pipeline(
            args(), source_classes=[make_source("A", posts)], extractor=ex, deliver_fn=deliver,
        )
        # The seeded post is already known, so nothing reaches extraction.
        assert ex.run_called_with == []
        assert digest.is_empty


# ---------------------------------------------------------------------------
# The dedup invariant (PLAN §11)
# ---------------------------------------------------------------------------

class TestDedup:
    def test_second_run_finds_zero_new_posts(self, isolated_state):
        posts = [post("A", "one"), post("A", "two")]
        ex1 = StubExtractor([investment()])
        d1 = main.run_pipeline(
            args(), source_classes=[make_source("A", posts)],
            extractor=ex1, deliver_fn=capturing_deliver(),
        )
        assert len(ex1.run_called_with) == 2  # both new the first time

        ex2 = StubExtractor([investment()])
        d2 = main.run_pipeline(
            args(), source_classes=[make_source("A", posts)],
            extractor=ex2, deliver_fn=capturing_deliver(),
        )
        assert ex2.run_called_with == []       # nothing new the second time
        assert d2.is_empty

    def test_a_round_reported_last_week_is_dropped_this_week(self, isolated_state):
        # Week 1: Acme Series B is reported and committed.
        main.run_pipeline(
            args(), source_classes=[make_source("A", [post("A", "acme")])],
            extractor=StubExtractor([investment("Acme", "Series B")]),
            deliver_fn=capturing_deliver(),
        )
        # Week 2: a *different* post announces the same round again.
        d2 = main.run_pipeline(
            args(), source_classes=[make_source("B", [post("B", "acme-again")])],
            extractor=StubExtractor([investment("Acme", "Series B", firms=["Index Ventures"])]),
            deliver_fn=capturing_deliver(),
        )
        assert d2.is_empty  # layer 3b drops the already-reported round


# ---------------------------------------------------------------------------
# Syndicate collapse (PLAN §11)
# ---------------------------------------------------------------------------

class TestSyndicateCollapse:
    def test_two_firms_one_round_collapse_to_one_card(self, isolated_state):
        two = make_source("A", [post("A", "acme"), post("A", "acme-mirror")])
        ex = StubExtractor([
            investment("Acme", "Series B", firms=["Sequoia"]),
            investment("Acme", "Series B", firms=["Index Ventures"]),
        ])
        digest = main.run_pipeline(
            args(), source_classes=[two], extractor=ex, deliver_fn=capturing_deliver(),
        )
        assert len(digest.investments) == 1
        assert set(digest.investments[0].vc_firms) == {"Sequoia", "Index Ventures"}


# ---------------------------------------------------------------------------
# Date filter
# ---------------------------------------------------------------------------

class TestDateFilter:
    def test_a_post_older_than_sixty_days_is_dropped(self, isolated_state):
        from datetime import datetime, timedelta
        old = post("A", "ancient", published_date=datetime.utcnow() - timedelta(days=120))
        recent = post("A", "fresh", published_date=datetime.utcnow() - timedelta(days=3))
        ex = StubExtractor([investment()])
        main.run_pipeline(
            args(), source_classes=[make_source("A", [old, recent])],
            extractor=ex, deliver_fn=capturing_deliver(),
        )
        titles = [p.title for p in ex.run_called_with]
        assert "Fresh" in titles
        assert "Ancient" not in titles

    def test_a_post_without_a_date_is_kept(self, isolated_state):
        undated = post("A", "no-date", published_date=None)
        ex = StubExtractor([investment()])
        main.run_pipeline(
            args(), source_classes=[make_source("A", [undated])],
            extractor=ex, deliver_fn=capturing_deliver(),
        )
        assert len(ex.run_called_with) == 1


# ---------------------------------------------------------------------------
# --dry-run and --no-extract don't persist state
# ---------------------------------------------------------------------------

class TestNonPersisting:
    def test_dry_run_writes_no_state(self, isolated_state):
        posts = [post("A", "one")]
        deliver = capturing_deliver()
        d = main.run_pipeline(
            args(dry_run=True), source_classes=[make_source("A", posts)],
            extractor=StubExtractor([investment()]), deliver_fn=deliver,
        )
        assert deliver.calls[0][1] is True   # dry_run flag passed through
        assert not os.path.exists(state.SEEN_POSTS_FILE)

        # A following real run still sees the post as new.
        ex = StubExtractor([investment()])
        main.run_pipeline(
            args(), source_classes=[make_source("A", posts)],
            extractor=ex, deliver_fn=capturing_deliver(),
        )
        assert len(ex.run_called_with) == 1

    def test_no_extract_skips_extraction_and_state(self, isolated_state):
        deliver = capturing_deliver()
        ex = StubExtractor([investment()])
        d = main.run_pipeline(
            args(no_extract=True), source_classes=[make_source("A", [post("A", "one")])],
            extractor=ex, deliver_fn=deliver,
        )
        assert d is None
        assert ex.run_called_with is None
        assert deliver.calls == []
        assert not os.path.exists(state.SEEN_POSTS_FILE)


# ---------------------------------------------------------------------------
# Send failure leaves state unwritten
# ---------------------------------------------------------------------------

class TestSendFailure:
    def test_failed_send_raises_and_does_not_commit_state(self, isolated_state):
        posts = [post("A", "one")]

        def failing_deliver(digest, dry_run):
            return False

        ex1 = StubExtractor([investment()])
        with pytest.raises(main.DeliveryError):
            main.run_pipeline(
                args(), source_classes=[make_source("A", posts)],
                extractor=ex1, deliver_fn=failing_deliver,
            )
        assert not os.path.exists(state.SEEN_POSTS_FILE)

        # Next run re-finds the post because nothing was committed.
        ex2 = StubExtractor([investment()])
        main.run_pipeline(
            args(), source_classes=[make_source("A", posts)],
            extractor=ex2, deliver_fn=capturing_deliver(),
        )
        assert len(ex2.run_called_with) == 1

    def test_main_returns_nonzero_when_the_send_fails(self, isolated_state, monkeypatch):
        """A failed weekly email must make the workflow go red."""
        monkeypatch.setattr(
            main, "_select_sources",
            lambda name: [make_source("A", [post("A", "one")])],
        )
        monkeypatch.setattr(main, "Extractor", lambda: StubExtractor([investment()]))
        monkeypatch.setattr(main.emailer, "deliver", lambda digest, dry_run: False)
        assert main.main([]) == 1


# ---------------------------------------------------------------------------
# --source
# ---------------------------------------------------------------------------

class TestSourceFilter:
    def test_runs_a_single_source(self, isolated_state, monkeypatch):
        a = make_source("a16z", [post("a16z", "one")])
        # _select_sources resolves the real registry; override it for the test.
        monkeypatch.setattr(main, "_select_sources", lambda name: [a] if name == "a16z" else [])
        d = main.run_pipeline(
            args(source="a16z"), extractor=StubExtractor([investment()]),
            deliver_fn=capturing_deliver(),
        )
        assert len(d.source_summaries) == 1
        assert d.source_summaries[0].source == "a16z"


# ---------------------------------------------------------------------------
# main() exit codes
# ---------------------------------------------------------------------------

class TestMain:
    def test_bad_week_is_a_usage_error(self, isolated_state, capsys):
        code = main.main(["--week", "2026-08-05"])  # a Wednesday
        assert code == 2
        assert "Saturday" in capsys.readouterr().err

    def test_unknown_source_is_a_usage_error(self, isolated_state):
        assert main.main(["--source", "nonesuch", "--no-extract"]) == 2
