"""Orchestrator — scrape, dedup, extract, roll up, email.

    python -m src.main --seed        # record what exists now; send nothing
    python -m src.main --dry-run     # full pipeline, no send; writes preview.html
    python -m src.main               # the real weekly run
    python -m src.main --source a16z -v   # one source, debug logging

The pipeline, in the order the steps have to run:

    1. parallel index scrape                → posts + a SourceSummary each
    2. layer 1: URL dedup                    → drop already-seen, before any
                                               detail fetch (the main cost saver)
    3. detail fetch                          → fill each new post's body
    4. layer 2: content-hash dedup           → drop re-slugged reposts
    5. date filter                           → drop posts older than 60 days
    6. classify gate + extract               → investments  (skipped: --no-extract)
    7. layer 3a: collapse syndicates         → one round, several firms
    8. layer 3b: drop rounds seen before     → across-run investment dedup
    9. build the digest
   10. deliver — and only then persist state

Step 10 is deliberate: state is written **only after a successful send**. If
email fails, nothing is committed, so next run re-finds the posts and re-sends
— cheap, because the extraction cache survives independently and the expensive
Claude calls are not repeated. Saving before sending would drop a week of deals
on a transient SMTP blip, which is the one failure "nothing repeats" rules out.
"""

from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from . import emailer, state
from .extractor import Extractor
from .models import BlogPost, Investment, SourceSummary
from .sources.base import BaseSource
from .sources.firms import ALL_SOURCES, SOURCES_BY_NAME
from .trends import WeeklyDigest, build_digest
from .weekrange import latest_closed_week, parse_week_arg

logger = logging.getLogger(__name__)


class DeliveryError(RuntimeError):
    """The pipeline ran but the digest could not be delivered.

    Raised on a real run whose send failed, so main() exits non-zero and the
    scheduled workflow goes red — a silently-failed weekly email is exactly
    what "silence means breakage" exists to catch, and the CI failure
    notification is the only out-of-band signal available (you can't email
    someone that their email is broken). State is left unwritten either way,
    so the next run retries cheaply off the extraction cache.
    """


# Index scrapes run concurrently; detail fetches do not (see fetch_details).
MAX_WORKERS = 8

# A parsed date, when present, only rejects posts this old — an old post
# surfacing for the first time is probably an archive reshuffle, not news.
# Dates are a secondary filter; a post with no date always passes (PLAN §4).
MAX_POST_AGE_DAYS = 60


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="Weekly digest of new VC investments across 14 firms.",
    )
    parser.add_argument(
        "--seed", action="store_true",
        help="Record the posts that exist now and exit without emailing. "
             "Run once at setup so the first real run has a clean baseline.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run the full pipeline but send nothing. Writes preview.html and "
             "does not touch saved state, so it can be re-run.",
    )
    parser.add_argument(
        "--source", metavar="NAME",
        help="Run a single source by name (e.g. a16z, greylock, lightspeed). "
             "Useful while writing or debugging a parser.",
    )
    parser.add_argument(
        "--no-extract", action="store_true",
        help="Skip the Claude classifier and extraction. A fast structural "
             "test of the scrapers; sends nothing and saves no state.",
    )
    parser.add_argument(
        "--week", metavar="YYYY-MM-DD",
        help="Override the week-ending date (must be a Saturday). Sets the "
             "subject line and history key only — it does NOT reconstruct a "
             "past week; the URL diff still decides what is new.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Debug logging.",
    )
    return parser.parse_args(argv)


def _resolve_week(arg: Optional[str]) -> date:
    if arg:
        return parse_week_arg(arg)  # raises ValueError; main() reports it
    return latest_closed_week()


def _select_sources(name: Optional[str]) -> list[type[BaseSource]]:
    if not name:
        return list(ALL_SOURCES)
    cls = SOURCES_BY_NAME.get(name.lower())
    if cls is None:
        known = ", ".join(sorted(c.name for c in ALL_SOURCES))
        raise ValueError(f"Unknown source {name!r}. Known sources: {known}")
    return [cls]


# ---------------------------------------------------------------------------
# Scrape — the parallel fan-out
# ---------------------------------------------------------------------------

def scrape_all(
    source_classes: list[type[BaseSource]], max_workers: int = MAX_WORKERS
) -> tuple[dict[str, BaseSource], list[BlogPost], list[SourceSummary]]:
    """Scrape every source's index page in parallel.

    A source that throws must not kill the run — its future's exception is
    caught and logged into a SourceSummary so a silently broken scraper is
    visible in the email footer rather than just vanishing.

    Returns (instances_by_name, all_posts, summaries). The instances are kept
    so their `fetch_post_detail` and session can be reused for the detail pass.
    """
    instances: dict[str, BaseSource] = {}
    summaries: dict[str, SourceSummary] = {}
    posts: list[BlogPost] = []

    def scrape_one(cls: type[BaseSource]) -> tuple[BaseSource, list[BlogPost]]:
        source = cls()
        return source, source.scrape()

    workers = min(max_workers, len(source_classes)) or 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(scrape_one, cls): cls for cls in source_classes}
        for future in as_completed(futures):
            cls = futures[future]
            summary = SourceSummary(source=cls.name)
            try:
                source, found = future.result()
                instances[cls.name] = source
                posts.extend(found)
                summary.posts_found = len(found)
            except Exception as e:  # noqa: BLE001 — one source can't sink the run
                logger.error(f"[{cls.name}] scrape failed: {e}")
                summary.error = str(e)
            summaries[cls.name] = summary

    ordered = [summaries[cls.name] for cls in source_classes]
    return instances, posts, ordered


def fetch_details(
    instances: dict[str, BaseSource], posts: list[BlogPost]
) -> list[BlogPost]:
    """Fill in each post's body, in place.

    Sequential on purpose. The index scrape is what runs in parallel; detail
    fetches go through each post's own source instance so its request_delay
    politeness holds, and firing concurrent requests at one host is exactly
    what that delay exists to prevent. Post-dedup volume is small at a weekly
    cadence, so nothing is lost by keeping this serial.
    """
    for post in posts:
        source = instances.get(post.vc_firm)
        if source is None:
            continue
        source.fetch_post_detail(post)
    return posts


# ---------------------------------------------------------------------------
# Date filter
# ---------------------------------------------------------------------------

def _too_old(post: BlogPost, cutoff: datetime) -> bool:
    """A post is dropped only if it has a date AND that date is stale."""
    return post.published_date is not None and post.published_date < cutoff


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------

def run_pipeline(
    args: argparse.Namespace,
    source_classes: Optional[list[type[BaseSource]]] = None,
    extractor: Optional[Extractor] = None,
    deliver_fn: Optional[Callable[[WeeklyDigest, bool], bool]] = None,
) -> Optional[WeeklyDigest]:
    """Run the whole thing once. Returns the digest, or None for --seed.

    The injectable parameters exist for testing — the defaults are the real
    sources, a real Extractor, and the real emailer.
    """
    week_ending = _resolve_week(args.week)
    source_classes = source_classes or _select_sources(args.source)
    deliver_fn = deliver_fn or emailer.deliver

    logger.info(f"Scanning {len(source_classes)} source(s) for week ending {week_ending}")

    # 1. Parallel scrape.
    instances, posts, summaries = scrape_all(source_classes)
    summary_by_name = {s.source: s for s in summaries}

    # 2. Layer 1 — drop already-seen URLs before any detail fetch.
    seen_posts = state.load_seen_posts()
    new_posts, seen_posts = state.filter_new_posts(posts, seen_posts)
    _attribute_new_posts(new_posts, posts, summary_by_name)
    logger.info(f"{len(new_posts)} new of {len(posts)} posts after URL dedup")

    # --seed stops here: record the baseline, send nothing.
    if args.seed:
        state.save_seen_posts(seen_posts)
        logger.info(f"Seeded {len(new_posts)} new post URLs; sending nothing.")
        print_summary(summaries)
        return None

    # 3. Detail fetch — fill bodies for the new posts only.
    fetch_details(instances, new_posts)

    # 4. Layer 2 — content-hash dedup, now that bodies exist.
    hashes = state.known_content_hashes(seen_posts)
    deduped: list[BlogPost] = []
    for post in new_posts:
        if post.body and state.is_duplicate_content(post, hashes):
            logger.info(f"Dropped re-slugged repost: {post.url}")
            continue
        state.record_content_hash(post, seen_posts)
        if post.content_hash:
            hashes.add(post.content_hash)
        deduped.append(post)

    # 5. Date filter — reject posts with a stale parsed date.
    cutoff = datetime.utcnow() - timedelta(days=MAX_POST_AGE_DAYS)
    fresh = [p for p in deduped if not _too_old(p, cutoff)]
    if len(fresh) != len(deduped):
        logger.info(f"Dropped {len(deduped) - len(fresh)} posts older than {MAX_POST_AGE_DAYS} days")

    # 6. Extract — unless we're only testing the scrapers.
    if args.no_extract:
        logger.info("--no-extract: skipping classifier and extraction.")
        print_summary(summaries)
        return None

    extractor = extractor or Extractor()
    investments, _rejected = extractor.run(fresh)

    # 7. Layer 3a — merge a syndicated round into one entry within this run.
    investments = state.collapse_syndicates(investments)

    # 8. Layer 3b — drop rounds already reported in a previous week.
    seen_investments = state.load_seen_investments()
    investments, seen_investments = state.filter_new_investments(investments, seen_investments)
    _attribute_investments(investments, summary_by_name)

    # 9. Build the digest.
    digest = build_digest(investments, state.load_weekly_history(), week_ending, summaries)
    print_summary(summaries)

    # 10. Deliver, then persist — only on success.
    sent = deliver_fn(digest, args.dry_run)

    if args.dry_run:
        logger.info("--dry-run: wrote preview, saved no state.")
        return digest

    if not sent:
        logger.error(
            "Send failed — leaving state unwritten so next run retries. "
            "Extraction is cached, so the retry is cheap."
        )
        raise DeliveryError(f"digest for week ending {week_ending} was not delivered")

    state.save_seen_posts(seen_posts)
    state.save_seen_investments(seen_investments)
    state.save_week(week_ending.isoformat(), investments)
    logger.info("Sent digest and committed state.")
    return digest


def _attribute_new_posts(
    new_posts: list[BlogPost], all_posts: list[BlogPost],
    summaries: dict[str, SourceSummary],
) -> None:
    for post in all_posts:
        s = summaries.get(post.vc_firm)
        if s:
            s.previously_seen += 1
    for post in new_posts:
        s = summaries.get(post.vc_firm)
        if s:
            s.new_posts += 1
            s.previously_seen -= 1  # counted above; it's new, not previously seen


def _attribute_investments(
    investments: list[Investment], summaries: dict[str, SourceSummary]
) -> None:
    for inv in investments:
        for firm in inv.vc_firms:
            s = summaries.get(firm)
            if s:
                s.investments += 1


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_summary(summaries: list[SourceSummary]) -> None:
    """Per-source result table to stdout."""
    print()
    print(f"  {'SOURCE':<20}{'FOUND':>7}{'NEW':>6}{'DEALS':>7}  STATUS")
    print(f"  {'-' * 20}{'-' * 7}{'-' * 6}{'-' * 7}  {'-' * 12}")
    for s in sorted(summaries, key=lambda x: x.source.lower()):
        status = f"error: {s.error}" if s.error else ("ok" if s.healthy else "0 posts")
        print(f"  {s.source:<20}{s.posts_found:>7}{s.new_posts:>6}{s.investments:>7}  {status}")
    print()


# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        run_pipeline(args)
    except ValueError as e:  # bad --week or --source
        print(f"error: {e}", file=sys.stderr)
        return 2
    except DeliveryError as e:  # ran fine, but the email didn't go out
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
