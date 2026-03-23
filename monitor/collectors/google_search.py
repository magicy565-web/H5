"""
Google Search collector for injection molding purchase intent signals.

Uses Google Custom Search JSON API when GOOGLE_CSE_ID and GOOGLE_API_KEY
environment variables are set; otherwise falls back to scraping google.com
search results via httpx with browser-like headers.
"""
import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote_plus, urlencode

import httpx

from .base import BaseCollector, RawSignal
from monitor.config import REQUEST_DELAY_SECONDS

logger = logging.getLogger(__name__)

GOOGLE_API_KEY: Optional[str] = os.getenv("GOOGLE_API_KEY")
GOOGLE_CSE_ID: Optional[str] = os.getenv("GOOGLE_CSE_ID")

_CSE_ENDPOINT = "https://www.googleapis.com/customsearch/v1"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class GoogleSearchCollector(BaseCollector):
    """Collect purchase-intent signals from Google search results."""

    name: str = "google_search"

    def __init__(self) -> None:
        from monitor.config import SOURCES
        cfg = SOURCES.get("google_search", {})
        self._max_results: int = cfg.get("max_results_per_keyword", 5)
        self._use_api: bool = bool(GOOGLE_API_KEY and GOOGLE_CSE_ID)
        if self._use_api:
            logger.info("GoogleSearchCollector: using Custom Search JSON API")
        else:
            logger.info("GoogleSearchCollector: using httpx scraping fallback")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def collect(self) -> list[RawSignal]:
        from monitor.config import SOURCES, KEYWORDS_DIRECT, KEYWORDS_INDIRECT
        cfg = SOURCES.get("google_search", {})
        if not cfg.get("enabled", False):
            logger.info("Google search source is disabled, skipping.")
            return []

        keywords = KEYWORDS_DIRECT + KEYWORDS_INDIRECT
        signals: list[RawSignal] = []

        async with httpx.AsyncClient(
            headers=_HEADERS, timeout=30.0, follow_redirects=True
        ) as client:
            for idx, keyword in enumerate(keywords):
                if idx > 0:
                    await asyncio.sleep(REQUEST_DELAY_SECONDS)
                try:
                    results = await self._search(client, keyword)
                    signals.extend(results)
                    logger.info(
                        "Keyword %r yielded %d results", keyword, len(results)
                    )
                except Exception:
                    logger.exception("Error searching for keyword %r", keyword)

        logger.info("GoogleSearchCollector finished: %d total signals", len(signals))
        return signals

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _search(
        self, client: httpx.AsyncClient, keyword: str
    ) -> list[RawSignal]:
        if self._use_api:
            return await self._search_with_retry(self._search_api, client, keyword)
        return await self._search_with_retry(self._search_scrape, client, keyword)

    async def _search_with_retry(
        self, search_fn, client: httpx.AsyncClient, keyword: str, max_retries: int = 2
    ) -> list[RawSignal]:
        """Call search_fn with exponential backoff retry (max 2 retries)."""
        for attempt in range(max_retries + 1):
            try:
                return await search_fn(client, keyword)
            except Exception as exc:
                if attempt < max_retries:
                    delay = 2 ** (attempt + 1)  # 2s, 4s
                    logger.warning(
                        "Search attempt %d/%d for %r failed (%s), retrying in %ds …",
                        attempt + 1, max_retries + 1, keyword, exc, delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "All %d search attempts for %r failed: %s",
                        max_retries + 1, keyword, exc,
                    )
                    return []
        return []  # unreachable but satisfies type checker

    # --- Google Custom Search JSON API path ---

    async def _search_api(
        self, client: httpx.AsyncClient, keyword: str
    ) -> list[RawSignal]:
        params = {
            "key": GOOGLE_API_KEY,
            "cx": GOOGLE_CSE_ID,
            "q": keyword,
            "num": min(self._max_results, 10),
        }
        resp = await client.get(_CSE_ENDPOINT, params=params)
        resp.raise_for_status()
        data = resp.json()

        now = datetime.now(timezone.utc).isoformat()
        signals: list[RawSignal] = []
        for item in data.get("items", [])[: self._max_results]:
            signals.append(
                RawSignal(
                    source=self.name,
                    url=item.get("link", ""),
                    title=item.get("title", ""),
                    text=item.get("snippet", ""),
                    collected_at=now,
                    extra={"keyword": keyword, "method": "cse_api"},
                )
            )
        return signals

    # --- Scraping fallback path ---

    async def _search_scrape(
        self, client: httpx.AsyncClient, keyword: str
    ) -> list[RawSignal]:
        url = f"https://www.google.com/search?{urlencode({'q': keyword, 'num': self._max_results + 5})}"
        resp = await client.get(url)
        if resp.status_code in (429, 403, 503):
            logger.warning("Google returned %d for %r, skipping", resp.status_code, keyword)
            return []
        resp.raise_for_status()
        return self._parse_html(resp.text, keyword)

    def _parse_html(self, html: str, keyword: str) -> list[RawSignal]:
        """Extract result entries from raw Google results HTML.

        This is intentionally simple regex-based parsing suitable for
        an MVP.  It looks for the standard ``<a href="/url?q=...">``
        redirect links and nearby text that forms titles and snippets.
        """
        now = datetime.now(timezone.utc).isoformat()
        signals: list[RawSignal] = []

        # Google wraps organic links in <a href="/url?q=ACTUAL_URL&...">
        link_pattern = re.compile(r'<a[^>]+href="/url\?q=([^&"]+)&[^"]*"[^>]*>(.*?)</a>', re.DOTALL)
        # Snippet blocks often sit in <span> tags near the link
        snippet_pattern = re.compile(r'<span[^>]*class="[^"]*(?:st|aCOpRe)[^"]*"[^>]*>(.*?)</span>', re.DOTALL)

        snippets = [self._strip_tags(m.group(1)) for m in snippet_pattern.finditer(html)]
        snippet_iter = iter(snippets)

        for match in link_pattern.finditer(html):
            link = match.group(1)
            # Skip Google-internal links
            if "google.com" in link or "webcache" in link or "translate.google" in link:
                continue

            title = self._strip_tags(match.group(2)).strip()
            if not title:
                continue

            snippet = next(snippet_iter, "")

            signals.append(
                RawSignal(
                    source=self.name,
                    url=link,
                    title=title,
                    text=snippet,
                    collected_at=now,
                    extra={"keyword": keyword, "method": "scrape"},
                )
            )
            if len(signals) >= self._max_results:
                break

        return signals

    @staticmethod
    def _strip_tags(html: str) -> str:
        """Remove HTML tags and collapse whitespace."""
        text = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", text).strip()
