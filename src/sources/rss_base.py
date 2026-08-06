"""Feed-based source. Preferred whenever a site offers a feed.

A feed is a contract; a DOM is not. Feeds also carry real publication dates,
which the HTML index pages on most of these sites do not expose at all.

Covers three shapes:
  - RSS / Atom            via feedparser
  - WordPress REST API    /wp-json/wp/v2/posts
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from ..models import BlogPost
from .base import BaseSource, canonicalize_url

logger = logging.getLogger(__name__)


class RSSSource(BaseSource):
    """Reads posts from an RSS/Atom feed."""

    feed_urls: list[str] = []

    def scrape(self) -> list[BlogPost]:
        try:
            import feedparser
        except ImportError:
            logger.error(f"[{self.name}] feedparser not installed; cannot read feed")
            return []

        posts: list[BlogPost] = []
        seen: set[str] = set()

        for feed_url in self.feed_urls:
            try:
                # Fetch through our own session so the UA header and delay apply —
                # some of these hosts 403 feedparser's default UA.
                import time
                time.sleep(self.request_delay)
                resp = self.session.get(feed_url, timeout=30)
                resp.raise_for_status()
                parsed = feedparser.parse(resp.content)
            except Exception as e:  # noqa: BLE001
                logger.error(f"[{self.name}] Feed fetch failed for {feed_url}: {e}")
                continue

            if parsed.bozo and not parsed.entries:
                logger.error(
                    f"[{self.name}] Feed at {feed_url} did not parse: "
                    f"{getattr(parsed, 'bozo_exception', 'unknown')}"
                )
                continue

            for entry in parsed.entries:
                url = canonicalize_url(entry.get("link", ""))
                if not url or url in seen:
                    continue
                if not self.is_post_url(url):
                    continue

                title = self.clean_text(entry.get("title", ""))
                if not title:
                    continue

                labels = [
                    t.get("term", "") for t in entry.get("tags", []) if t.get("term")
                ]
                seen.add(url)
                posts.append(BlogPost(
                    url=url,
                    title=title,
                    vc_firm=self.name,
                    published_date=self._entry_date(entry),
                    labels=labels,
                    likely_investment=self.looks_like_investment(url, title, labels),
                ))

        logger.info(f"[{self.name}] Discovered {len(posts)} posts from feed")
        return posts

    @staticmethod
    def _entry_date(entry) -> Optional[datetime]:
        for key in ("published_parsed", "updated_parsed"):
            parsed = entry.get(key)
            if parsed:
                try:
                    return datetime(*parsed[:6])
                except (TypeError, ValueError):
                    continue
        return None


class WordPressSource(BaseSource):
    """Reads posts from the WordPress REST API.

    Richer than RSS: no item cap, real pagination, and the excerpt often saves
    a detail fetch. Only works if the site hasn't disabled the endpoint.
    """

    wp_api_base: str = ""  # e.g. "https://a16z.com/wp-json/wp/v2"
    per_page: int = 50

    def scrape(self) -> list[BlogPost]:
        posts: list[BlogPost] = []
        seen: set[str] = set()

        for page in range(1, self.max_pages + 1):
            url = f"{self.wp_api_base}/posts?per_page={self.per_page}&page={page}"
            data = self.fetch_json(url)
            if not data or not isinstance(data, list):
                break

            for item in data:
                link = canonicalize_url(item.get("link", ""))
                if not link or link in seen:
                    continue

                title = self.clean_text(
                    (item.get("title") or {}).get("rendered", "")
                )
                if not title:
                    continue

                seen.add(link)
                posts.append(BlogPost(
                    url=link,
                    title=title,
                    vc_firm=self.name,
                    published_date=self.parse_date(item.get("date")),
                    likely_investment=self.looks_like_investment(link, title),
                ))

            if len(data) < self.per_page:
                break

        logger.info(f"[{self.name}] Discovered {len(posts)} posts from WP API")
        return posts
