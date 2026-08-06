"""Persistence and deduplication — the "only show me what's new" engine.

Ported from vc-job-agent's history.py, with a third dedup layer added for
investment identity (see PLAN.md §4).

Three layers, applied in order:

  1. Post URL          — seen_posts.json     — the normal case
  2. Content hash      — seen_posts.json     — re-slugged / re-published posts
  3. Company + stage   — seen_investments.json — one round, several firms

Layer 3 is the one vc-job-agent has no analogue for and the one that matters
most here. When a company closes a round, every firm in the syndicate tends to
blog about it. Layers 1 and 2 see three distinct URLs with distinct content and
let all three through — you'd get the company three times in one email and
triple-count its dollars in the sector totals.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from .models import BlogPost, Investment

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
SEEN_POSTS_FILE = os.path.join(DATA_DIR, "seen_posts.json")
SEEN_INVESTMENTS_FILE = os.path.join(DATA_DIR, "seen_investments.json")
WEEKLY_HISTORY_FILE = os.path.join(DATA_DIR, "weekly_history.json")

# How long to remember a post URL. Long enough that an archive reshuffle or a
# temporarily-broken source can't resurface old posts as new.
POST_RETENTION_DAYS = 400
INVESTMENT_RETENTION_DAYS = 400
HISTORY_WEEKS = 12


def _load(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError, TypeError) as e:
        # A corrupt state file must not be silently treated as "nothing seen" —
        # that would resend everything. Loud failure is the safer default.
        logger.error(
            f"Could not read state file {path}: {e}. "
            f"Refusing to treat this as an empty state — fix or delete the file, "
            f"then re-run with --seed."
        )
        raise


def _save(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)  # atomic — a crash mid-write can't corrupt the state


# ---------------------------------------------------------------------------
# Layers 1 + 2 — post-level dedup
# ---------------------------------------------------------------------------

def load_seen_posts() -> dict[str, dict]:
    """Load the memory of every post URL seen so far."""
    return _load(SEEN_POSTS_FILE)


def save_seen_posts(memory: dict[str, dict]) -> None:
    """Persist post memory, pruning entries older than the retention window."""
    cutoff = (datetime.utcnow() - timedelta(days=POST_RETENTION_DAYS)).isoformat()
    pruned = {
        url: meta for url, meta in memory.items()
        if meta.get("last_seen", "") >= cutoff
    }
    dropped = len(memory) - len(pruned)
    if dropped:
        logger.info(f"Pruned {dropped} post entries past the retention window")
    _save(SEEN_POSTS_FILE, pruned)


def filter_new_posts(
    posts: list[BlogPost], memory: dict[str, dict]
) -> tuple[list[BlogPost], dict[str, dict]]:
    """Split posts into net-new vs. already-seen and update the memory.

    Layer 1 (URL) runs here. Layer 2 (content hash) can only run after the
    body is fetched, so it lives in `is_duplicate_content` below.

    Returns (new_posts, updated_memory).
    """
    now = datetime.utcnow().isoformat()
    new_posts: list[BlogPost] = []

    for post in posts:
        entry = memory.get(post.url)
        if entry:
            entry["last_seen"] = now
        else:
            memory[post.url] = {
                "first_seen": now,
                "last_seen": now,
                "vc_firm": post.vc_firm,
                "title": post.title,
                "content_hash": None,  # filled once the body is fetched
            }
            new_posts.append(post)

    return new_posts, memory


def known_content_hashes(memory: dict[str, dict]) -> set[str]:
    """All content hashes recorded so far, for layer 2."""
    return {
        meta["content_hash"]
        for meta in memory.values()
        if meta.get("content_hash")
    }


def is_duplicate_content(post: BlogPost, hashes: set[str]) -> bool:
    """Layer 2 — has this exact content been seen at a different URL?"""
    return post.content_hash in hashes


def record_content_hash(post: BlogPost, memory: dict[str, dict]) -> None:
    """Store a post's content hash once its body has been fetched."""
    if post.url in memory and post.body:
        memory[post.url]["content_hash"] = post.content_hash


# ---------------------------------------------------------------------------
# Layer 3 — investment-level dedup
# ---------------------------------------------------------------------------

def load_seen_investments() -> dict[str, dict]:
    return _load(SEEN_INVESTMENTS_FILE)


def save_seen_investments(memory: dict[str, dict]) -> None:
    cutoff = (datetime.utcnow() - timedelta(days=INVESTMENT_RETENTION_DAYS)).isoformat()
    pruned = {
        key: meta for key, meta in memory.items()
        if meta.get("last_seen", "") >= cutoff
    }
    _save(SEEN_INVESTMENTS_FILE, pruned)


def collapse_syndicates(investments: list[Investment]) -> list[Investment]:
    """Merge investments that describe the same round within a single run.

    Two firms blogging the same Series B become one entry listing both firms.
    """
    merged: dict[str, Investment] = {}

    for inv in investments:
        key = inv.dedup_key
        if key in merged:
            merged[key].merge(inv)
            logger.info(
                f"Merged duplicate announcement of {inv.company_name} "
                f"{inv.round_stage} → firms now {merged[key].vc_firms}"
            )
        else:
            merged[key] = inv

    return list(merged.values())


def filter_new_investments(
    investments: list[Investment], memory: dict[str, dict]
) -> tuple[list[Investment], dict[str, dict]]:
    """Drop rounds already reported in a previous week; record the rest.

    Call this *after* `collapse_syndicates`.
    """
    now = datetime.utcnow().isoformat()
    fresh: list[Investment] = []

    for inv in investments:
        key = inv.dedup_key
        entry = memory.get(key)
        if entry:
            entry["last_seen"] = now
            logger.info(
                f"Skipping {inv.company_name} {inv.round_stage} — "
                f"already reported {entry.get('first_seen', '?')[:10]}"
            )
        else:
            memory[key] = {
                "first_seen": now,
                "last_seen": now,
                "company_name": inv.company_name,
                "round_stage": inv.round_stage,
                "funding_amount_usd": inv.funding_amount_usd,
                "vc_firms": inv.vc_firms,
            }
            fresh.append(inv)

    return fresh, memory


# ---------------------------------------------------------------------------
# Weekly history — for the four-week trend comparison
# ---------------------------------------------------------------------------

def load_weekly_history() -> dict[str, dict]:
    return _load(WEEKLY_HISTORY_FILE)


def save_week(week_ending: str, investments: list[Investment]) -> dict[str, dict]:
    """Append this week's rollup to history, keeping the last HISTORY_WEEKS."""
    history = load_weekly_history()

    by_sector: dict[str, dict] = {}
    for inv in investments:
        bucket = by_sector.setdefault(inv.sector, {"deals": 0, "total_usd": 0, "undisclosed": 0})
        bucket["deals"] += 1
        if inv.funding_amount_usd:
            bucket["total_usd"] += inv.funding_amount_usd
        else:
            bucket["undisclosed"] += 1

    history[week_ending] = {
        "deals": len(investments),
        "total_usd": sum(i.funding_amount_usd or 0 for i in investments),
        "by_sector": by_sector,
        "investments": [i.to_dict() for i in investments],
    }

    # Keep only the most recent N weeks
    for stale in sorted(history.keys(), reverse=True)[HISTORY_WEEKS:]:
        del history[stale]

    _save(WEEKLY_HISTORY_FILE, history)
    return history
