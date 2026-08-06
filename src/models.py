"""Data models for Trend Tracker."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


def normalize_company_name(name: str) -> str:
    """Normalize a company name for dedup layer 3.

    "Acme Inc." / "ACME"  / "  Acme  " all collapse to "acme".
    """
    if not name:
        return ""
    n = name.lower().strip()
    # Strip common legal suffixes
    n = re.sub(
        r"[,.]?\s*\b(inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|"
        r"co|gmbh|plc|sa|s\.a|bv|b\.v|ag|oy|ab|pte|pty)\b\.?$",
        "",
        n,
    )
    # Strip trailing punctuation, collapse whitespace and internal punctuation
    n = re.sub(r"[^\w\s-]", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


@dataclass
class BlogPost:
    """A single post discovered on a VC firm's blog index page."""

    url: str  # canonical, query-stripped — the dedup key
    title: str
    vc_firm: str
    published_date: Optional[datetime] = None
    body: str = ""  # filled in by BaseSource.fetch_post_detail()
    # Category/topic labels the index page exposed, if any. Useful as a
    # cheap pre-filter and as a sector hint for the extractor.
    labels: list[str] = field(default_factory=list)
    # True when the source can tell from the URL or label alone that this is an
    # investment announcement (e.g. a16z's /announcement/ path). Lets the
    # classifier skip an LLM call.
    likely_investment: Optional[bool] = None
    discovered_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def content_hash(self) -> str:
        """Dedup layer 2 — catches re-slugged or re-published posts."""
        basis = f"{self.title.strip().lower()}|{self.body.strip()[:2000].lower()}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()

    def __hash__(self):
        return hash(self.url)

    def __eq__(self, other):
        return isinstance(other, BlogPost) and self.url == other.url

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "vc_firm": self.vc_firm,
            "published_date": self.published_date.isoformat() if self.published_date else None,
            "labels": self.labels,
            "likely_investment": self.likely_investment,
            "discovered_at": self.discovered_at.isoformat(),
        }


@dataclass
class Investment:
    """A funding round extracted from a blog post."""

    company_name: str
    company_description: str = ""
    company_url: Optional[str] = None
    sector: str = "Other"
    sub_sector: str = ""
    funding_amount_usd: Optional[int] = None  # normalized; None = undisclosed
    funding_amount_raw: str = "Undisclosed"
    round_stage: str = "Unknown"
    co_investors: list[str] = field(default_factory=list)
    vc_firms: list[str] = field(default_factory=list)
    source_posts: list[BlogPost] = field(default_factory=list)
    confidence: str = "medium"  # high | medium | low
    notes: str = ""

    @property
    def dedup_key(self) -> str:
        """Dedup layer 3 — same round announced by multiple firms."""
        return f"{normalize_company_name(self.company_name)}|{self.round_stage.lower()}"

    @property
    def primary_url(self) -> Optional[str]:
        return self.source_posts[0].url if self.source_posts else None

    def merge(self, other: "Investment") -> None:
        """Fold another announcement of the same round into this one.

        Used when several firms in a syndicate each blog the same raise.
        Keeps the richer description and the higher-confidence amount.
        """
        for firm in other.vc_firms:
            if firm not in self.vc_firms:
                self.vc_firms.append(firm)
        for post in other.source_posts:
            if post.url not in {p.url for p in self.source_posts}:
                self.source_posts.append(post)
        for inv in other.co_investors:
            if inv not in self.co_investors:
                self.co_investors.append(inv)

        if len(other.company_description) > len(self.company_description):
            self.company_description = other.company_description
        if not self.company_url and other.company_url:
            self.company_url = other.company_url
        # Prefer a disclosed amount over an undisclosed one
        if self.funding_amount_usd is None and other.funding_amount_usd is not None:
            self.funding_amount_usd = other.funding_amount_usd
            self.funding_amount_raw = other.funding_amount_raw
        # Disagreement on a disclosed amount is a real signal — flag it
        elif (
            self.funding_amount_usd is not None
            and other.funding_amount_usd is not None
            and self.funding_amount_usd != other.funding_amount_usd
        ):
            self.confidence = "low"
            self.notes = (
                f"Sources disagree on amount: {self.funding_amount_raw} vs "
                f"{other.funding_amount_raw}"
            ).strip()

    def to_dict(self) -> dict:
        return {
            "company_name": self.company_name,
            "company_description": self.company_description,
            "company_url": self.company_url,
            "sector": self.sector,
            "sub_sector": self.sub_sector,
            "funding_amount_usd": self.funding_amount_usd,
            "funding_amount_raw": self.funding_amount_raw,
            "round_stage": self.round_stage,
            "co_investors": self.co_investors,
            "vc_firms": self.vc_firms,
            "confidence": self.confidence,
            "notes": self.notes,
            "source_urls": [p.url for p in self.source_posts],
        }


@dataclass
class SourceSummary:
    """Per-source scrape result, for the email footer and health monitoring."""

    source: str
    posts_found: int = 0
    new_posts: int = 0
    previously_seen: int = 0
    investments: int = 0
    error: Optional[str] = None

    @property
    def healthy(self) -> bool:
        return self.error is None and self.posts_found > 0
