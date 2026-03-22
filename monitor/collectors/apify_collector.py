"""
Apify-powered collectors for premium web scraping.

Uses Apify actors to scrape Google, LinkedIn, Facebook, Alibaba, and
generic B2B platforms — bypassing Cloudflare, CAPTCHAs, and login walls.

Requires APIFY_API_TOKEN in config.  If the token is empty, all Apify
collectors gracefully return [].
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from .base import BaseCollector, RawSignal
from monitor.config import (
    APIFY_API_TOKEN,
    KEYWORDS_DIRECT,
    KEYWORDS_INDIRECT,
    REQUEST_DELAY_SECONDS,
    SOURCES,
)

logger = logging.getLogger(__name__)

_APIFY_BASE = "https://api.apify.com/v2"
_TIMEOUT = 300  # max seconds to wait for actor run


# ── helpers ──────────────────────────────────────────────────────────

async def _run_actor(
    client: httpx.AsyncClient,
    actor_id: str,
    run_input: dict,
    token: str,
    timeout: int = _TIMEOUT,
) -> list[dict]:
    """Start an Apify actor, wait for it to finish, return dataset items."""
    url = f"{_APIFY_BASE}/acts/{actor_id}/runs"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Start run
    resp = await client.post(url, json=run_input, headers=headers, timeout=60)
    resp.raise_for_status()
    run_data = resp.json().get("data", {})
    run_id = run_data.get("id")
    if not run_id:
        logger.error("Apify actor %s: no run ID returned", actor_id)
        return []

    logger.info("Apify actor %s started: run %s", actor_id, run_id)

    # Poll until finished
    status_url = f"{_APIFY_BASE}/actor-runs/{run_id}"
    elapsed = 0
    poll_interval = 5
    while elapsed < timeout:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
        status_resp = await client.get(status_url, headers=headers, timeout=30)
        status_resp.raise_for_status()
        status = status_resp.json().get("data", {}).get("status")
        if status in ("SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"):
            break

    if status != "SUCCEEDED":
        logger.warning("Apify actor %s run %s ended with status: %s", actor_id, run_id, status)
        return []

    # Fetch dataset items
    dataset_id = run_data.get("defaultDatasetId")
    if not dataset_id:
        logger.warning("Apify actor %s: no dataset ID", actor_id)
        return []

    items_url = f"{_APIFY_BASE}/datasets/{dataset_id}/items?format=json"
    items_resp = await client.get(items_url, headers=headers, timeout=60)
    items_resp.raise_for_status()
    items = items_resp.json()
    logger.info("Apify actor %s returned %d items", actor_id, len(items))
    return items if isinstance(items, list) else []


def _check_token() -> bool:
    """Return True if APIFY_API_TOKEN is configured."""
    if not APIFY_API_TOKEN:
        logger.info("APIFY_API_TOKEN not set — Apify collector disabled.")
        return False
    return True


# =====================================================================
#  1. Google Search via Apify
# =====================================================================

class ApifyGoogleCollector(BaseCollector):
    """Google Search via apify/google-search-scraper actor."""

    name: str = "apify_google"

    async def collect(self) -> list[RawSignal]:
        cfg = SOURCES.get("apify_google", {})
        if not cfg.get("enabled", False) or not _check_token():
            return []

        actor_id = cfg.get("actor_id", "apify/google-search-scraper")
        max_results = cfg.get("max_results_per_keyword", 10)
        keywords = KEYWORDS_DIRECT[:8] + KEYWORDS_INDIRECT[:4]  # limit to control cost

        signals: list[RawSignal] = []
        now = datetime.now(timezone.utc).isoformat()

        async with httpx.AsyncClient() as client:
            run_input = {
                "queries": "\n".join(keywords),
                "resultsPerPage": max_results,
                "maxPagesPerQuery": 1,
                "languageCode": "en",
                "mobileResults": False,
            }
            try:
                items = await _run_actor(client, actor_id, run_input, APIFY_API_TOKEN)
                for item in items:
                    url = item.get("url") or item.get("link", "")
                    title = item.get("title", "")
                    desc = item.get("description") or item.get("snippet", "")
                    if not url or not title:
                        continue
                    signals.append(RawSignal(
                        source=self.name,
                        url=url,
                        title=title,
                        text=desc,
                        collected_at=now,
                        extra={"actor": actor_id, "query": item.get("searchQuery", "")},
                    ))
            except Exception:
                logger.exception("ApifyGoogleCollector failed")

        logger.info("ApifyGoogleCollector: %d signals", len(signals))
        return signals


# =====================================================================
#  2. LinkedIn Post Search via Apify
# =====================================================================

class ApifyLinkedInCollector(BaseCollector):
    """LinkedIn post/content search via Apify actor."""

    name: str = "apify_linkedin"

    async def collect(self) -> list[RawSignal]:
        cfg = SOURCES.get("apify_linkedin", {})
        if not cfg.get("enabled", False) or not _check_token():
            return []

        actor_id = cfg.get("actor_id", "curious_coder/linkedin-post-search-scraper")
        max_results = cfg.get("max_results", 50)

        # LinkedIn search queries
        queries = [
            "injection molding machine buy",
            "looking for injection moulding machine",
            "need plastic injection machine",
            "injection molding machine RFQ",
            "PET preform machine purchase",
        ]

        signals: list[RawSignal] = []
        now = datetime.now(timezone.utc).isoformat()

        async with httpx.AsyncClient() as client:
            run_input = {
                "searchTerms": queries,
                "maxResults": max_results,
                "sortBy": "date",
            }
            try:
                items = await _run_actor(client, actor_id, run_input, APIFY_API_TOKEN)
                for item in items:
                    text = item.get("text") or item.get("postText") or item.get("content", "")
                    url = item.get("url") or item.get("postUrl", "")
                    author = item.get("authorName") or item.get("author", "")
                    title = text[:100] + "..." if len(text) > 100 else text

                    if not text:
                        continue
                    signals.append(RawSignal(
                        source=self.name,
                        url=url,
                        title=title,
                        text=text,
                        buyer_name=author,
                        collected_at=now,
                        extra={
                            "actor": actor_id,
                            "author": author,
                            "likes": item.get("likes", 0),
                            "comments": item.get("comments", 0),
                            "company": item.get("authorCompany", ""),
                        },
                    ))
            except Exception:
                logger.exception("ApifyLinkedInCollector failed")

        logger.info("ApifyLinkedInCollector: %d signals", len(signals))
        return signals


# =====================================================================
#  3. Facebook Posts/Groups via Apify
# =====================================================================

class ApifyFacebookCollector(BaseCollector):
    """Facebook page/group posts via Apify actor."""

    name: str = "apify_facebook"

    async def collect(self) -> list[RawSignal]:
        cfg = SOURCES.get("apify_facebook", {})
        if not cfg.get("enabled", False) or not _check_token():
            return []

        actor_id = cfg.get("actor_id", "apify/facebook-posts-scraper")
        pages = cfg.get("pages", [])
        max_results = cfg.get("max_results", 50)

        signals: list[RawSignal] = []
        now = datetime.now(timezone.utc).isoformat()

        async with httpx.AsyncClient() as client:
            run_input = {
                "startUrls": [{"url": f"https://www.facebook.com/{p}"} for p in pages],
                "resultsLimit": max_results,
            }
            try:
                items = await _run_actor(client, actor_id, run_input, APIFY_API_TOKEN)
                for item in items:
                    text = item.get("text") or item.get("message", "")
                    url = item.get("url") or item.get("postUrl", "")
                    author = item.get("userName") or item.get("pageName", "")
                    title = text[:100] + "..." if len(text) > 100 else text

                    if not text:
                        continue

                    # Only keep posts with injection-molding related keywords
                    text_lower = text.lower()
                    if not any(kw in text_lower for kw in (
                        "injection mold", "injection mould", "plastic machine",
                        "molding machine", "moulding machine", "preform machine",
                        "注塑", "plastic factory",
                    )):
                        continue

                    signals.append(RawSignal(
                        source=self.name,
                        url=url,
                        title=title,
                        text=text,
                        buyer_name=author,
                        collected_at=now,
                        extra={
                            "actor": actor_id,
                            "page": item.get("pageName", ""),
                            "likes": item.get("likes", 0),
                            "shares": item.get("shares", 0),
                        },
                    ))
            except Exception:
                logger.exception("ApifyFacebookCollector failed")

        logger.info("ApifyFacebookCollector: %d signals", len(signals))
        return signals


# =====================================================================
#  4. Alibaba RFQ/Products via Apify
# =====================================================================

class ApifyAlibabaCollector(BaseCollector):
    """Alibaba buyer requests via Apify actor."""

    name: str = "apify_alibaba"

    async def collect(self) -> list[RawSignal]:
        cfg = SOURCES.get("apify_alibaba", {})
        if not cfg.get("enabled", False) or not _check_token():
            return []

        actor_id = cfg.get("actor_id", "epctex/alibaba-scraper")
        max_results = cfg.get("max_results", 30)

        search_terms = [
            "injection molding machine",
            "plastic injection machine",
            "PET preform machine",
        ]

        signals: list[RawSignal] = []
        now = datetime.now(timezone.utc).isoformat()

        async with httpx.AsyncClient() as client:
            for term in search_terms:
                run_input = {
                    "searchTerms": [term],
                    "maxItems": max_results,
                    "type": "rfq",  # focus on buyer requests
                }
                try:
                    items = await _run_actor(client, actor_id, run_input, APIFY_API_TOKEN)
                    for item in items:
                        title = item.get("title") or item.get("subject", "")
                        url = item.get("url") or item.get("link", "")
                        buyer = item.get("buyerName") or item.get("buyer", "")
                        country = item.get("buyerCountry") or item.get("country", "")
                        desc = item.get("description") or item.get("details", "")

                        if not title:
                            continue
                        signals.append(RawSignal(
                            source=self.name,
                            url=url,
                            title=title,
                            text=f"{title}\n{desc}",
                            buyer_name=buyer,
                            buyer_country=country,
                            collected_at=now,
                            extra={
                                "actor": actor_id,
                                "search_term": term,
                                "quantity": item.get("quantity", ""),
                            },
                        ))
                except Exception:
                    logger.exception("ApifyAlibabaCollector failed for '%s'", term)
                await asyncio.sleep(REQUEST_DELAY_SECONDS)

        logger.info("ApifyAlibabaCollector: %d signals", len(signals))
        return signals


# =====================================================================
#  5. Generic B2B Web Scraper via Apify
# =====================================================================

class ApifyB2BCollector(BaseCollector):
    """Scrape B2B platforms (go4world, tradekey, exportersindia) using
    Apify's generic web-scraper actor to bypass Cloudflare."""

    name: str = "apify_b2b"

    async def collect(self) -> list[RawSignal]:
        cfg = SOURCES.get("apify_b2b", {})
        if not cfg.get("enabled", False) or not _check_token():
            return []

        actor_id = cfg.get("actor_id", "apify/web-scraper")
        targets = cfg.get("targets", [])
        max_pages = cfg.get("max_pages", 3)

        signals: list[RawSignal] = []
        now = datetime.now(timezone.utc).isoformat()

        async with httpx.AsyncClient() as client:
            for target in targets:
                target_url = target.get("url", "")
                target_name = target.get("name", "b2b")

                run_input = {
                    "startUrls": [{"url": target_url}],
                    "maxRequestsPerCrawl": max_pages * 20,
                    "pageFunction": _B2B_PAGE_FUNCTION,
                    "proxyConfiguration": {"useApifyProxy": True},
                }
                try:
                    items = await _run_actor(client, actor_id, run_input, APIFY_API_TOKEN)
                    for item in items:
                        title = item.get("title", "")
                        url = item.get("url") or target_url
                        text = item.get("text") or item.get("body", "")
                        buyer = item.get("buyer", "")
                        country = item.get("country", "")

                        if not title and not text:
                            continue
                        signals.append(RawSignal(
                            source=f"apify_{target_name}",
                            url=url,
                            title=title or text[:80],
                            text=text,
                            buyer_name=buyer,
                            buyer_country=country,
                            collected_at=now,
                            extra={"actor": actor_id, "platform": target_name},
                        ))
                except Exception:
                    logger.exception("ApifyB2BCollector failed for %s", target_name)
                await asyncio.sleep(REQUEST_DELAY_SECONDS)

        logger.info("ApifyB2BCollector: %d signals", len(signals))
        return signals


# JavaScript pageFunction for the generic web scraper — extracts
# structured buy-lead data from typical B2B listing pages.
_B2B_PAGE_FUNCTION = """
async function pageFunction(context) {
    const { request, page, log } = context;
    log.info(`Scraping ${request.url}`);

    const results = [];
    // Generic extraction: find listing blocks
    const cards = await page.$$('.product-item, .lead-item, .rfq-item, .buying-lead, article, .list-item, tr[class*="lead"]');

    if (cards.length === 0) {
        // Fallback: grab page title + body text
        const title = await page.title();
        const body = await page.$eval('body', el => el.innerText.substring(0, 3000));
        results.push({ title, text: body, url: request.url, buyer: '', country: '' });
    } else {
        for (const card of cards.slice(0, 30)) {
            const title = await card.$eval('h2, h3, h4, a[class*="title"], .title', el => el.innerText).catch(() => '');
            const text = await card.$eval('p, .desc, .description, .details', el => el.innerText).catch(() => '');
            const link = await card.$eval('a', el => el.href).catch(() => request.url);
            const buyer = await card.$eval('.buyer, .company, .user-name, .poster', el => el.innerText).catch(() => '');
            const country = await card.$eval('.country, .flag + span, .location', el => el.innerText).catch(() => '');

            if (title || text) {
                results.push({ title, text, url: link, buyer, country });
            }
        }
    }
    return results;
}
"""
