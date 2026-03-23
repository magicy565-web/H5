"""
独立推送脚本 — 将已采集的 Leads 推送到企业微信。

用法:
    python3 -m monitor.push_leads                    # 推送所有行业
    python3 -m monitor.push_leads --industry 家纺     # 推送单个行业
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime

import httpx

from monitor.config import (
    WECOM_CORP_ID,
    WECOM_AGENT_ID,
    WECOM_SECRET,
    DB_DIR,
    INDUSTRY_PROFILES,
)

INDUSTRY_FILES = {
    name: profile["leads_file"]
    for name, profile in INDUSTRY_PROFILES.items()
}

logger = logging.getLogger(__name__)


# ── WeCom API ─────────────────────────────────────────────────────────

async def get_access_token() -> str:
    """Fetch WeCom access_token."""
    url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={WECOM_CORP_ID}&corpsecret={WECOM_SECRET}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        data = resp.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"Failed to get token: {data}")
        return data["access_token"]


def format_report(industry: str, leads: list[dict]) -> str:
    """Format leads into a readable plain-text report."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    leads_sorted = sorted(leads, key=lambda x: -(x.get("intentScore", 0) or 0))

    lines = [
        f"=== 采购意向日报 [{industry}] ===",
        f"时间: {now}",
        f"合格线索: {len(leads)}条",
        "",
    ]

    # Country distribution
    country_dist: dict[str, int] = {}
    for l in leads:
        c = l.get("buyerCountry", "Unknown") or "Unknown"
        country_dist[c] = country_dist.get(c, 0) + 1
    top_countries = sorted(country_dist.items(), key=lambda x: -x[1])[:5]
    if top_countries:
        parts = [f"{c}({n})" for c, n in top_countries]
        lines.append(f"买家国家: {', '.join(parts)}")
        lines.append("")

    # Score distribution
    score_dist: dict[int, int] = {}
    for l in leads:
        s = l.get("intentScore", 0) or 0
        score_dist[s] = score_dist.get(s, 0) + 1
    if score_dist:
        parts = [f"{s}分({score_dist[s]}条)" for s in sorted(score_dist, reverse=True)]
        lines.append(f"评分分布: {' / '.join(parts)}")
        lines.append("")

    # Top leads
    lines.append("重点线索:")
    for i, lead in enumerate(leads_sorted[:10], 1):
        score = lead.get("intentScore", "?")
        country = lead.get("buyerCountry", "?")
        buyer = lead.get("buyerName", "")
        title = (lead.get("title") or "")[:45]
        summary = lead.get("summaryZh", "")

        line = f"  {i}. [{score}分] {country}"
        if buyer:
            line += f" | {buyer}"
        line += f"\n     {title}"
        if summary:
            line += f"\n     {summary}"
        lines.append(line)

    return "\n".join(lines)


async def send_message(token: str, content: str) -> dict:
    """Send a text message via WeCom app API."""
    if len(content) > 2000:
        content = content[:1997] + "..."

    payload = {
        "touser": "@all",
        "msgtype": "text",
        "agentid": WECOM_AGENT_ID,
        "text": {"content": content},
    }
    url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload)
        return resp.json()


async def push_industry(token: str, industry: str) -> None:
    """Push leads for a single industry."""
    filename = INDUSTRY_FILES.get(industry)
    if not filename:
        print(f"[ERROR] Unknown industry: {industry}")
        return

    filepath = DB_DIR / filename
    if not filepath.exists():
        print(f"[SKIP] No leads file: {filepath}")
        return

    leads = json.loads(filepath.read_text(encoding="utf-8"))
    if not leads:
        print(f"[SKIP] {industry}: 0 leads")
        return

    report = format_report(industry, leads)
    result = await send_message(token, report)

    if result.get("errcode") == 0:
        print(f"[OK] {industry}: {len(leads)} leads pushed successfully")
    else:
        print(f"[FAIL] {industry}: {result}")


async def main_async(industry: str | None) -> None:
    print("Getting WeCom access token...")
    token = await get_access_token()
    print("Token obtained.\n")

    if industry:
        await push_industry(token, industry)
    else:
        for ind in INDUSTRY_FILES:
            await push_industry(token, ind)
            print()


def main():
    parser = argparse.ArgumentParser(description="Push collected leads to WeCom")
    parser.add_argument("--industry", "-i", type=str, default=None,
                        help="Industry to push (注塑机/家纺/家具). Default: all")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    asyncio.run(main_async(args.industry))


if __name__ == "__main__":
    main()
