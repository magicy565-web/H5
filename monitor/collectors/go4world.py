"""
Go4WorldBusiness buy-lead collector for injection molding machines.
"""
import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx

from .base import BaseCollector, RawSignal
from monitor.config import REQUEST_DELAY_SECONDS

logger = logging.getLogger(__name__)


def _get_search_terms() -> list[str]:
    """Build go4world search terms from active industry keywords."""
    from monitor.config import KEYWORDS_DIRECT
    terms = set()
    for kw in KEYWORDS_DIRECT[:6]:
        # Convert keyword to URL slug: "buy bedding sets wholesale" -> "bedding-sets"
        words = kw.lower().replace('"', '').split()
        # Filter out common verbs/prepositions
        skip = {"buy", "from", "china", "import", "wholesale", "supplier",
                "needed", "wanted", "looking", "for", "bulk", "purchase", "求购", "采购"}
        meaningful = [w for w in words if w not in skip and len(w) > 2]
        if meaningful:
            terms.add("-".join(meaningful[:3]))
    return list(terms) if terms else ["injection-molding-machine"]

_BASE_URL = "https://www.go4worldbusiness.com/buy-leads/{term}.html"

# Precompiled patterns for lightweight HTML parsing.
_RE_LEAD_BLOCK = re.compile(
    r'<div[^>]*class="[^"]*lead[_-]?item[^"]*"[^>]*>(.*?)</div\s*>',
    re.S | re.I,
)
_RE_TITLE = re.compile(r"<a[^>]*>(.*?)</a>", re.S | re.I)
_RE_HREF = re.compile(r'href="([^"]+)"', re.I)
_RE_COUNTRY = re.compile(
    r'(?:country|location|flag)[^>]*>([^<]{2,60})<', re.I
)
_RE_BUYER = re.compile(
    r'(?:buyer|posted\s*by|company)[^>]*>([^<]{2,120})<', re.I
)
_RE_DESC = re.compile(
    r'(?:desc|detail|requirement|message)[^>]*>(.*?)<', re.S | re.I
)
_RE_NEXT_PAGE = re.compile(
    r'<a[^>]*href="([^"]*)"[^>]*>\s*(?:next|&raquo;|>)\s*</a>', re.I
)
_RE_TAG = re.compile(r"<[^>]+>")


def _strip_tags(html: str) -> str:
    return _RE_TAG.sub("", html).strip()


def _clean(text: str) -> str:
    return " ".join(text.split())


class Go4WorldBusinessCollector(BaseCollector):
    """Scrapes go4worldbusiness.com buy-lead listings."""

    name: str = "go4worldbusiness"

    async def collect(self) -> list[RawSignal]:
        from monitor.config import SOURCES
        cfg = SOURCES.get("go4worldbusiness", {})
        if not cfg.get("enabled", False):
            logger.info("go4worldbusiness collector is disabled, skipping")
            return []

        max_pages = cfg.get("max_pages", 3)
        search_terms = _get_search_terms()
        signals: list[RawSignal] = []
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
        ) as client:
            for term in search_terms:
                url = _BASE_URL.format(term=term)
                try:
                    new = await self._scrape_term(client, url, term, max_pages)
                    signals.extend(new)
                except Exception:
                    logger.exception("Failed to scrape term %s", term)

        logger.info(
            "go4worldbusiness collected %d raw signals", len(signals)
        )
        return signals

    async def _scrape_term(
        self,
        client: httpx.AsyncClient,
        start_url: str,
        term: str,
        max_pages: int = 3,
    ) -> list[RawSignal]:
        signals: list[RawSignal] = []
        url: Optional[str] = start_url

        for page in range(1, max_pages + 1):
            if url is None:
                break

            logger.debug("Fetching page %d for '%s': %s", page, term, url)
            try:
                resp = await client.get(url)
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "HTTP %s for %s – stopping pagination",
                    exc.response.status_code,
                    url,
                )
                break
            except httpx.RequestError as exc:
                logger.warning("Request error for %s: %s", url, exc)
                break

            html = resp.text
            page_signals = self._parse_leads(html, url)
            signals.extend(page_signals)

            # Resolve next page URL.
            url = self._find_next_page(html, url)

            if page < _MAX_PAGES and url is not None:
                await asyncio.sleep(REQUEST_DELAY_SECONDS)

        return signals

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_leads(self, html: str, page_url: str) -> list[RawSignal]:
        """Extract lead items from a listing page."""
        now = datetime.now(timezone.utc).isoformat()
        signals: list[RawSignal] = []

        blocks = _RE_LEAD_BLOCK.findall(html)
        if not blocks:
            # Fallback: split by common separators and scan each chunk.
            blocks = re.split(r'<(?:div|tr|li)[^>]*class="[^"]*lead', html, flags=re.I)
            blocks = blocks[1:]  # first element is before the first match

        for block in blocks:
            title = self._extract_title(block)
            if not title:
                continue

            lead_url = self._extract_url(block, page_url)
            country = self._extract_field(_RE_COUNTRY, block)
            buyer = self._extract_field(_RE_BUYER, block)
            desc = self._extract_field(_RE_DESC, block)

            signals.append(
                RawSignal(
                    source=self.name,
                    url=lead_url or page_url,
                    title=_clean(title),
                    text=_clean(desc) if desc else _clean(title),
                    buyer_name=_clean(buyer) if buyer else "",
                    buyer_country=_clean(country) if country else "",
                    collected_at=now,
                )
            )

        return signals

    @staticmethod
    def _extract_title(block: str) -> str:
        m = _RE_TITLE.search(block)
        return _strip_tags(m.group(1)) if m else ""

    @staticmethod
    def _extract_url(block: str, base_url: str) -> Optional[str]:
        m = _RE_HREF.search(block)
        if not m:
            return None
        href = m.group(1)
        if href.startswith("http"):
            return href
        if href.startswith("/"):
            # Resolve against origin.
            from urllib.parse import urlparse
            parsed = urlparse(base_url)
            return f"{parsed.scheme}://{parsed.netloc}{href}"
        return href

    @staticmethod
    def _extract_field(pattern: re.Pattern, block: str) -> str:  # type: ignore[type-arg]
        m = pattern.search(block)
        return _strip_tags(m.group(1)) if m else ""

    @staticmethod
    def _find_next_page(html: str, current_url: str) -> Optional[str]:
        m = _RE_NEXT_PAGE.search(html)
        if not m:
            return None
        href = m.group(1)
        if href.startswith("http"):
            return href
        if href.startswith("/"):
            from urllib.parse import urlparse
            parsed = urlparse(current_url)
            return f"{parsed.scheme}://{parsed.netloc}{href}"
        return None
