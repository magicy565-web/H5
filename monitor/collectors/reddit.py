"""
Reddit collector – scrapes old.reddit.com/r/{sub}/new/.json for purchase-intent posts.
No API key required; Reddit serves JSON at .json endpoints.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from .base import BaseCollector, RawSignal
from monitor.config import REQUEST_DELAY_SECONDS

logger = logging.getLogger(__name__)


def _get_intent_keywords() -> list[str]:
    """Build intent keyword list dynamically from active industry config."""
    from monitor.config import KEYWORDS_DIRECT, KEYWORDS_INDIRECT
    # Extract core terms from the configured keywords
    terms = set()
    for kw in KEYWORDS_DIRECT + KEYWORDS_INDIRECT:
        # Clean quotes and country names
        cleaned = kw.strip('"').lower()
        # Add as-is for matching
        terms.add(cleaned)
        # Also add individual significant words/phrases
        for word in cleaned.split():
            if len(word) > 4 and word not in ("from", "with", "that", "this", "looking"):
                terms.add(word)
    return list(terms) if terms else ["injection molding"]

_USER_AGENT = (
    "Mozilla/5.0 (compatible; IntentMonitorBot/1.0; "
    "+https://github.com/example/intent-monitor)"
)


def _matches_keywords(title: str, body: str) -> bool:
    """Return True if title or body contains any intent keyword."""
    combined = f"{title} {body}".lower()
    keywords = _get_intent_keywords()
    return any(kw in combined for kw in keywords)


def _post_to_signal(post: dict[str, Any], subreddit: str) -> RawSignal:
    """Convert a Reddit post JSON object into a RawSignal."""
    data = post.get("data", post)
    created_utc = data.get("created_utc", 0)
    ts = datetime.fromtimestamp(created_utc, tz=timezone.utc).isoformat()

    permalink = data.get("permalink", "")
    url = f"https://www.reddit.com{permalink}" if permalink else data.get("url", "")

    return RawSignal(
        source="reddit",
        url=url,
        title=data.get("title", ""),
        text=data.get("selftext", "")[:2000],
        buyer_name=data.get("author", ""),
        collected_at=ts,
        extra={
            "subreddit": subreddit,
            "score": data.get("score", 0),
            "num_comments": data.get("num_comments", 0),
        },
    )


class RedditCollector(BaseCollector):
    """Collects purchase-intent signals from Reddit subreddits."""

    name: str = "reddit"

    def __init__(self) -> None:
        from monitor.config import SOURCES
        cfg = SOURCES.get("reddit", {})
        self.enabled: bool = cfg.get("enabled", False)
        self.subreddits: list[str] = cfg.get("subreddits", [])

    async def collect(self) -> list[RawSignal]:
        if not self.enabled:
            logger.info("Reddit collector is disabled – skipping.")
            return []

        signals: list[RawSignal] = []
        headers = {"User-Agent": _USER_AGENT}

        async with httpx.AsyncClient(headers=headers, timeout=20.0) as client:
            for sub in self.subreddits:
                try:
                    signals.extend(await self._fetch_subreddit(client, sub))
                except Exception:
                    logger.exception("Failed to fetch r/%s", sub)
                await asyncio.sleep(REQUEST_DELAY_SECONDS)

        logger.info("Reddit collector finished – %d signals from %d subreddits.",
                     len(signals), len(self.subreddits))
        return signals

    async def _fetch_subreddit(
        self, client: httpx.AsyncClient, subreddit: str
    ) -> list[RawSignal]:
        """Fetch and filter new posts from a single subreddit."""
        url = f"https://old.reddit.com/r/{subreddit}/new/.json"
        resp = await client.get(url, params={"limit": 100})
        resp.raise_for_status()

        payload = resp.json()
        children: list[dict[str, Any]] = (
            payload.get("data", {}).get("children", [])
        )

        results: list[RawSignal] = []
        for post in children:
            data = post.get("data", {})
            title = data.get("title", "")
            body = data.get("selftext", "")

            if _matches_keywords(title, body):
                results.append(_post_to_signal(post, subreddit))

        logger.debug("r/%s: %d/%d posts matched intent keywords.",
                      subreddit, len(results), len(children))
        return results
