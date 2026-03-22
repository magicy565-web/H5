"""
Intent Monitor – main entry point.

Orchestrates collection, deduplication, analysis, and reporting.

Usage:
    python3 -m monitor.intent_monitor --industry 注塑机
    python3 -m monitor.intent_monitor --industry 家纺
    python3 -m monitor.intent_monitor --industry 家具
    python3 -m monitor.intent_monitor --industry all
    python3 -m monitor.intent_monitor --list              # show available industries
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from typing import Any

from monitor.collectors import (
    GoogleSearchCollector,
    Go4WorldBusinessCollector,
    RedditCollector,
    RSSCollector,
    TradeKeyCollector,
    ApifyGoogleCollector,
    ApifyLinkedInCollector,
    ApifyFacebookCollector,
    ApifyAlibabaCollector,
    ApifyB2BCollector,
)
from monitor.collectors.base import RawSignal
from monitor.config import (
    load_industry,
    get_active_industry,
    list_industries,
)
from monitor.dedup import Deduplicator
from monitor import storage

logger = logging.getLogger(__name__)

# Map SOURCES config keys to collector classes
_COLLECTOR_MAP: dict[str, type] = {
    "google_search": GoogleSearchCollector,
    "go4worldbusiness": Go4WorldBusinessCollector,
    "reddit": RedditCollector,
    "rss": RSSCollector,
    "tradekey": TradeKeyCollector,
    # Apify premium collectors
    "apify_google": ApifyGoogleCollector,
    "apify_linkedin": ApifyLinkedInCollector,
    "apify_facebook": ApifyFacebookCollector,
    "apify_alibaba": ApifyAlibabaCollector,
    "apify_b2b": ApifyB2BCollector,
}


async def _run_collector(collector: Any) -> list[RawSignal]:
    """Run a single collector, catching and logging any exception."""
    try:
        signals = await collector.collect()
        logger.info("  [%s] collected %d signals.", collector.name, len(signals))
        return signals
    except Exception:
        logger.exception("  [%s] failed with error.", collector.name)
        return []


async def run_monitor(industry: str) -> None:
    """Main monitoring pipeline for a single industry."""
    # Load industry profile (updates config module globals)
    profile = load_industry(industry)

    # Re-import SOURCES after load_industry updated it
    from monitor.config import SOURCES

    start = datetime.now()
    logger.info("=" * 60)
    logger.info("Intent Monitor [%s] started at %s",
                industry, start.strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("=" * 60)

    # ── 1. Initialize enabled collectors ──────────────────────────
    collectors = []
    for key, cls in _COLLECTOR_MAP.items():
        cfg = SOURCES.get(key, {})
        if cfg.get("enabled", False):
            collectors.append(cls())
            logger.info("Enabled collector: %s", key)
        else:
            logger.info("Skipped collector (disabled): %s", key)

    if not collectors:
        logger.warning("No collectors are enabled – nothing to do.")
        return

    # ── 2. Run all collectors concurrently ────────────────────────
    logger.info("Running %d collectors concurrently ...", len(collectors))
    results: list[list[RawSignal]] = await asyncio.gather(
        *(_run_collector(c) for c in collectors)
    )

    all_signals: list[RawSignal] = []
    source_counts: dict[str, int] = {}
    for signals in results:
        for sig in signals:
            all_signals.append(sig)
            source_counts[sig.source] = source_counts.get(sig.source, 0) + 1

    logger.info("Total raw signals collected: %d", len(all_signals))
    for src, cnt in sorted(source_counts.items()):
        logger.info("  - %s: %d", src, cnt)

    # ── 3. Deduplicate ────────────────────────────────────────────
    dedup = Deduplicator()
    new_signals = dedup.filter_new(all_signals)
    logger.info("New signals after dedup: %d / %d", len(new_signals), len(all_signals))

    if not new_signals:
        logger.info("No new signals found – nothing to analyse.")
        _print_summary(industry, start, source_counts, 0, 0, [])
        return

    # ── 4. Analyse intent ─────────────────────────────────────────
    try:
        from monitor.analyzer import IntentAnalyzer
    except ImportError:
        logger.error("monitor.analyzer module not found – skipping analysis.")
        raw_dicts = [
            {
                "contentHash": s.content_hash,
                "source": s.source,
                "url": s.url,
                "title": s.title,
                "text": s.text[:500],
                "buyerName": s.buyer_name,
                "buyerCountry": s.buyer_country,
                "contactInfo": s.contact_info,
                "collectedAt": s.collected_at or start.isoformat(),
                "intentScore": 0,
                "industry": industry,
            }
            for s in new_signals
        ]
        all_leads = storage.append_leads(raw_dicts)
        date_str = start.strftime("%Y%m%d")
        storage.generate_excel(all_leads, date_str, industry)
        _print_summary(industry, start, source_counts, len(new_signals), len(raw_dicts), raw_dicts)
        return

    analyzer = IntentAnalyzer()
    leads = await analyzer.analyze_all(new_signals)
    qualified = [l for l in leads]
    logger.info("Qualified leads from analysis: %d", len(qualified))

    # ── 5. Persist ────────────────────────────────────────────────
    all_leads = storage.append_leads(qualified)

    # ── 6. Excel report ───────────────────────────────────────────
    date_str = start.strftime("%Y%m%d")
    filepath = storage.generate_excel(all_leads, date_str, industry)
    logger.info("Report written to %s", filepath)

    # ── 7. Summary ────────────────────────────────────────────────
    _print_summary(industry, start, source_counts, len(new_signals), len(qualified), qualified)


async def run_all_industries() -> None:
    """Run monitor for all configured industries sequentially."""
    industries = list_industries()
    logger.info("Running monitor for %d industries: %s", len(industries), ", ".join(industries))
    for industry in industries:
        try:
            await run_monitor(industry)
        except Exception:
            logger.exception("Failed to run monitor for industry: %s", industry)
        logger.info("")


def _print_summary(
    industry: str,
    start: datetime,
    source_counts: dict[str, int],
    new_count: int,
    qualified_count: int,
    leads: list,
) -> None:
    """Print a human-readable summary to stdout (captured by cron)."""
    end = datetime.now()
    duration = (end - start).total_seconds()

    lines = [
        "",
        "=" * 60,
        f"  Intent Monitor Summary [{industry}]  ({start.strftime('%Y-%m-%d %H:%M')})",
        "=" * 60,
        "",
        "Sources:",
    ]
    for src, cnt in sorted(source_counts.items()):
        lines.append(f"  {src:25s} {cnt:>4d} signals")
    lines.append(f"  {'TOTAL':25s} {sum(source_counts.values()):>4d} signals")
    lines.append("")
    lines.append(f"New signals (after dedup): {new_count}")
    lines.append(f"Qualified leads:           {qualified_count}")
    lines.append("")

    if leads:
        lines.append("Top leads:")
        preview_items = leads[:5] if isinstance(leads, list) else []
        for i, lead in enumerate(preview_items, start=1):
            if isinstance(lead, dict):
                title = lead.get("title", "N/A")[:50]
                score = lead.get("intentScore", lead.get("intent_score", "?"))
                source = lead.get("source", "?")
                country = lead.get("buyerCountry", lead.get("buyer_country", "?"))
            else:
                title = getattr(lead, "title", "N/A")[:50]
                score = getattr(lead, "intentScore", getattr(lead, "intent_score", "?"))
                source = getattr(lead, "source", "?")
                country = getattr(lead, "buyerCountry", getattr(lead, "buyer_country", "?"))
            lines.append(f"  {i}. [{score}] ({source} | {country}) {title}")
        lines.append("")

    lines.append(f"Duration: {duration:.1f}s")
    lines.append("=" * 60)

    summary = "\n".join(lines)
    print(summary)
    logger.info(summary)


def main():
    parser = argparse.ArgumentParser(description="Intent Monitor — multi-industry purchase signal scanner")
    parser.add_argument("--industry", "-i", type=str, default="注塑机",
                        help="Industry to monitor (e.g. 注塑机, 家纺, 家具, all)")
    parser.add_argument("--list", "-l", action="store_true",
                        help="List all available industries and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if args.list:
        print("Available industries:")
        for name in list_industries():
            print(f"  - {name}")
        return

    if args.industry == "all":
        asyncio.run(run_all_industries())
    else:
        asyncio.run(run_monitor(args.industry))


if __name__ == "__main__":
    main()
