"""
TradeKey buy-offers collector for injection molding machine purchase intent signals.
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


def _get_search_slugs() -> list[str]:
    """Build tradekey search slugs from active industry keywords."""
    from monitor.config import KEYWORDS_DIRECT
    slugs = set()
    skip = {"buy", "from", "china", "import", "wholesale", "supplier",
            "needed", "wanted", "looking", "for", "bulk", "purchase", "求购", "采购"}
    for kw in KEYWORDS_DIRECT[:6]:
        words = kw.lower().replace('"', '').split()
        meaningful = [w for w in words if w not in skip and len(w) > 2]
        if meaningful:
            slugs.add("-".join(meaningful[:3]))
    return list(slugs) if slugs else ["injection-molding-machine"]


_SEARCH_SLUGS_CACHE: list[str] | None = None

_BASE_URL = "https://www.tradekey.com/buy-offers"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _extract_listings(html: str) -> list[dict[str, str]]:
    """Extract buy-offer listings from TradeKey HTML using regex."""
    listings: list[dict[str, str]] = []

    # Match product listing blocks -- TradeKey wraps each offer in an <h2> or
    # heading tag with a link, followed by descriptive text and metadata.
    title_pattern = re.compile(
        r'<h[23][^>]*>\s*<a\s+href="([^"]+)"[^>]*>\s*(.+?)\s*</a>\s*</h[23]>',
        re.IGNORECASE | re.DOTALL,
    )

    country_pattern = re.compile(
        r'(?:country|location|flag)[^>]*>([^<]{2,50})<',
        re.IGNORECASE,
    )

    date_pattern = re.compile(
        r'(?:date|posted|time)[^>]*>\s*([A-Za-z0-9,\s\-/]+)\s*<',
        re.IGNORECASE,
    )

    desc_pattern = re.compile(
        r'<p[^>]*class="[^"]*desc[^"]*"[^>]*>\s*(.+?)\s*</p>',
        re.IGNORECASE | re.DOTALL,
    )

    # Split into rough listing blocks to correlate fields
    block_pattern = re.compile(
        r'(<div[^>]*class="[^"]*(?:product|listing|offer|item)[^"]*"[^>]*>.*?</div>\s*(?:</div>)?)',
        re.IGNORECASE | re.DOTALL,
    )

    blocks = block_pattern.findall(html)

    # Fallback: if no blocks found, try to extract titles directly
    if not blocks:
        blocks = _split_by_titles(html)

    for block in blocks:
        title_match = title_pattern.search(block)
        if not title_match:
            continue

        url = title_match.group(1).strip()
        if not url.startswith("http"):
            url = f"https://www.tradekey.com{url}"
        title = _strip_tags(title_match.group(2)).strip()

        if not title:
            continue

        country_match = country_pattern.search(block)
        country = _strip_tags(country_match.group(1)).strip() if country_match else ""

        date_match = date_pattern.search(block)
        date_str = _strip_tags(date_match.group(1)).strip() if date_match else ""

        desc_match = desc_pattern.search(block)
        description = _strip_tags(desc_match.group(1)).strip() if desc_match else ""

        listings.append({
            "url": url,
            "title": title,
            "country": country,
            "date": date_str,
            "description": description,
        })

    return listings


def _split_by_titles(html: str) -> list[str]:
    """Fallback: split HTML around <h2>/<h3> headings containing links."""
    parts: list[str] = []
    indices = [m.start() for m in re.finditer(r'<h[23][^>]*>\s*<a\s', html, re.IGNORECASE)]
    for i, start in enumerate(indices):
        end = indices[i + 1] if i + 1 < len(indices) else min(start + 3000, len(html))
        parts.append(html[start:end])
    return parts


def _strip_tags(text: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    cleaned = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', cleaned).strip()


class TradeKeyCollector(BaseCollector):
    """Collects buy-offer signals from tradekey.com."""

    name: str = "tradekey"

    def __init__(self) -> None:
        from monitor.config import SOURCES
        cfg = SOURCES.get("tradekey", {})
        self.enabled: bool = cfg.get("enabled", True)
        self.max_pages: int = cfg.get("max_pages", 2)

    async def collect(self) -> list[RawSignal]:
        if not self.enabled:
            logger.info("TradeKey collector is disabled, skipping.")
            return []

        search_slugs = _get_search_slugs()
        signals: list[RawSignal] = []
        now = datetime.now(timezone.utc).isoformat()

        async with httpx.AsyncClient(
            headers=_HEADERS,
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        ) as client:
            for slug in search_slugs:
                slug_signals = await self._scrape_slug(client, slug, now)
                signals.extend(slug_signals)

        logger.info("TradeKey collector finished: %d signals total.", len(signals))
        return signals

    async def _scrape_slug(
        self,
        client: httpx.AsyncClient,
        slug: str,
        collected_at: str,
    ) -> list[RawSignal]:
        """Scrape paginated buy-offer listings for one search slug."""
        signals: list[RawSignal] = []

        for page in range(1, self.max_pages + 1):
            url = f"{_BASE_URL}/{slug}/{page}.html" if page > 1 else f"{_BASE_URL}/{slug}/"
            try:
                logger.debug("TradeKey: fetching %s", url)
                resp = await client.get(url)
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "TradeKey HTTP %s for %s — stopping pagination for '%s'.",
                    exc.response.status_code, url, slug,
                )
                break
            except httpx.RequestError as exc:
                logger.error("TradeKey request failed for %s: %s", url, exc)
                break

            listings = _extract_listings(resp.text)
            if not listings:
                logger.debug("TradeKey: no listings found on %s, stopping.", url)
                break

            for item in listings:
                signals.append(
                    RawSignal(
                        source="tradekey",
                        url=item["url"],
                        title=item["title"],
                        text=item["description"],
                        buyer_country=item["country"],
                        collected_at=collected_at,
                        extra={"date_posted": item["date"], "search_slug": slug},
                    )
                )

            logger.debug(
                "TradeKey: page %d of '%s' yielded %d listings.", page, slug, len(listings),
            )

            # Respect rate limiting between page fetches
            if page < self.max_pages:
                await asyncio.sleep(REQUEST_DELAY_SECONDS)

        return signals
