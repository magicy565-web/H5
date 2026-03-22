"""
RSS feed collector for injection molding purchase intent signals.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx

from .base import BaseCollector, RawSignal
from monitor.config import REQUEST_DELAY_SECONDS

logger = logging.getLogger(__name__)


def _get_keywords_re():
    """Build a regex dynamically from active industry keywords."""
    from monitor.config import KEYWORDS_DIRECT
    # Extract key phrases for matching
    terms = set()
    for kw in KEYWORDS_DIRECT:
        cleaned = kw.strip('"').lower()
        # Take multi-word phrases
        words = cleaned.split()
        if len(words) >= 2:
            terms.add(r"\s+".join(re.escape(w) for w in words[:3]))
        elif len(words) == 1 and len(words[0]) > 4:
            terms.add(re.escape(words[0]))
    if not terms:
        terms = {"injection.mol[du]ing"}
    return re.compile("|".join(terms), re.IGNORECASE)


class RSSCollector(BaseCollector):
    """Collect purchase-intent signals from industry RSS feeds."""

    name: str = "rss"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def collect(self) -> list[RawSignal]:
        from monitor.config import SOURCES
        cfg: dict[str, Any] = SOURCES.get("rss", {})
        if not cfg.get("enabled", False):
            logger.info("RSS collector is disabled – skipping.")
            return []

        feeds: list[str] = cfg.get("feeds", [])
        if not feeds:
            logger.warning("No RSS feeds configured.")
            return []

        signals: list[RawSignal] = []
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(30.0),
        ) as client:
            for url in feeds:
                try:
                    new_signals = await self._process_feed(client, url)
                    signals.extend(new_signals)
                except Exception:
                    logger.exception("Failed to process feed: %s", url)
                await asyncio.sleep(REQUEST_DELAY_SECONDS)

        logger.info("RSS collector found %d signals from %d feeds.", len(signals), len(feeds))
        return signals

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _process_feed(
        self,
        client: httpx.AsyncClient,
        feed_url: str,
    ) -> list[RawSignal]:
        """Fetch a single feed, parse it, and return matching signals."""
        logger.debug("Fetching RSS feed: %s", feed_url)
        response = await client.get(feed_url)
        response.raise_for_status()

        parsed = feedparser.parse(response.text)
        if parsed.bozo and not parsed.entries:
            logger.warning("Malformed feed (%s): %s", feed_url, parsed.bozo_exception)
            return []

        signals: list[RawSignal] = []
        for entry in parsed.entries:
            title: str = entry.get("title", "")
            summary: str = entry.get("summary", entry.get("description", ""))
            link: str = entry.get("link", "")
            searchable = f"{title} {summary}"

            if not _get_keywords_re().search(searchable):
                continue

            published = self._parse_date(entry)
            signals.append(
                RawSignal(
                    source=self.name,
                    url=link,
                    title=title,
                    text=summary,
                    collected_at=datetime.now(timezone.utc).isoformat(),
                    extra={"feed": feed_url, "published": published},
                ),
            )

        logger.debug("Feed %s yielded %d matching entries.", feed_url, len(signals))
        return signals

    @staticmethod
    def _parse_date(entry: Any) -> str:
        """Best-effort extraction of an ISO-formatted published date."""
        for key in ("published_parsed", "updated_parsed"):
            tp = entry.get(key)
            if tp is not None:
                try:
                    return datetime(*tp[:6], tzinfo=timezone.utc).isoformat()
                except (TypeError, ValueError):
                    pass
        # Fall back to the raw string if the struct is missing
        return entry.get("published", entry.get("updated", ""))
