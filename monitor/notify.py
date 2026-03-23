"""
微信推送模块 — 支持企业微信应用消息、群机器人和Server酱三种推送方式。

企业微信应用消息: 设置 WECOM_CORP_ID + WECOM_AGENT_ID + WECOM_SECRET 环境变量
企业微信群机器人: 设置 WECOM_WEBHOOK_URL 环境变量
Server酱:        设置 SERVERCHAN_KEY 环境变量
三者可同时启用。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import httpx

from monitor.config import (
    WECOM_CORP_ID, WECOM_AGENT_ID, WECOM_SECRET,
    WECOM_WEBHOOK_URL, SERVERCHAN_KEY,
)

logger = logging.getLogger(__name__)

# ── WeCom access token cache ─────────────────────────────────────────
_wecom_token_cache: dict[str, Any] = {
    "token": None,
    "expires_at": 0.0,
}


def _build_report(industry: str, leads: list[dict], source_counts: dict[str, int]) -> dict:
    """Build structured report data from leads."""
    total_signals = sum(source_counts.values())
    qualified = len(leads)

    # Top 10 leads sorted by score
    top = sorted(leads, key=lambda x: -(x.get("intentScore", 0) or 0))[:10]

    # Score distribution
    score_dist = {}
    for l in leads:
        s = l.get("intentScore", 0)
        score_dist[s] = score_dist.get(s, 0) + 1

    # Country distribution
    country_dist = {}
    for l in leads:
        c = l.get("buyerCountry", "Unknown") or "Unknown"
        country_dist[c] = country_dist.get(c, 0) + 1
    top_countries = sorted(country_dist.items(), key=lambda x: -x[1])[:5]

    return {
        "industry": industry,
        "total_signals": total_signals,
        "qualified": qualified,
        "source_counts": source_counts,
        "score_dist": score_dist,
        "top_countries": top_countries,
        "top_leads": top,
    }


def _format_markdown(report: dict) -> str:
    """Format report as Markdown (used by both WeChat channels)."""
    r = report
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [
        f"## 采购意向日报 [{r['industry']}]",
        f"> {now}",
        "",
        f"**采集信号:** {r['total_signals']}条 | **合格线索:** {r['qualified']}条",
        "",
    ]

    # Source breakdown
    if r["source_counts"]:
        lines.append("**数据来源:**")
        for src, cnt in sorted(r["source_counts"].items()):
            lines.append(f"- {src}: {cnt}条")
        lines.append("")

    # Score distribution
    if r["score_dist"]:
        dist_parts = []
        for score in sorted(r["score_dist"].keys(), reverse=True):
            dist_parts.append(f"{score}分({r['score_dist'][score]}条)")
        lines.append(f"**评分分布:** {' / '.join(dist_parts)}")
        lines.append("")

    # Top countries
    if r["top_countries"]:
        country_parts = [f"{c}({n})" for c, n in r["top_countries"]]
        lines.append(f"**买家国家:** {', '.join(country_parts)}")
        lines.append("")

    # Top leads
    if r["top_leads"]:
        lines.append("**重点线索:**")
        for i, lead in enumerate(r["top_leads"][:8], 1):
            score = lead.get("intentScore", "?")
            country = lead.get("buyerCountry", "?")
            title = (lead.get("title") or "")[:50]
            summary = lead.get("summaryZh", "")
            action = lead.get("recommendedAction", "")

            line = f"{i}. **[{score}分]** {country} | {title}"
            if summary:
                line += f"\n   > {summary}"
            if action:
                line += f" → {action}"
            lines.append(line)
        lines.append("")

    if r["qualified"] == 0:
        lines.append("*今日未发现新的合格线索。*")

    return "\n".join(lines)


def _format_plain(report: dict) -> str:
    """Format report as plain text (fallback)."""
    r = report
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [
        f"=== 采购意向日报 [{r['industry']}] ===",
        f"时间: {now}",
        f"采集信号: {r['total_signals']}条",
        f"合格线索: {r['qualified']}条",
        "",
    ]

    if r["top_leads"]:
        lines.append("重点线索:")
        for i, lead in enumerate(r["top_leads"][:5], 1):
            score = lead.get("intentScore", "?")
            country = lead.get("buyerCountry", "?")
            title = (lead.get("title") or "")[:45]
            lines.append(f"  {i}. [{score}分] {country} | {title}")

    return "\n".join(lines)


# =====================================================================
#  企业微信应用消息推送 (需要 CorpID + AgentID + Secret)
# =====================================================================

async def _get_wecom_access_token() -> str | None:
    """Fetch access_token from WeCom API using corp credentials.

    Caches the token and reuses it until 5 minutes before expiry
    (tokens are valid for 7200 seconds / 2 hours).
    """
    import time

    if not (WECOM_CORP_ID and WECOM_SECRET):
        return None

    # Return cached token if still valid (with 5-min safety margin)
    if (_wecom_token_cache["token"]
            and time.time() < _wecom_token_cache["expires_at"] - 300):
        return _wecom_token_cache["token"]

    url = (
        f"https://qyapi.weixin.qq.com/cgi-bin/gettoken"
        f"?corpid={WECOM_CORP_ID}&corpsecret={WECOM_SECRET}"
    )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            if data.get("errcode") == 0:
                _wecom_token_cache["token"] = data["access_token"]
                _wecom_token_cache["expires_at"] = time.time() + data.get("expires_in", 7200)
                return data["access_token"]
            logger.warning("WeCom token error: %s", data)
            return None
    except Exception:
        logger.exception("Failed to get WeCom access_token")
        return None


async def push_wecom_app(report: dict) -> bool:
    """Push report via WeCom application message API (应用消息).

    Sends to all users (@all). Requires WECOM_CORP_ID, WECOM_AGENT_ID,
    WECOM_SECRET, and the server IP must be in the app's trusted IP list.
    """
    if not (WECOM_CORP_ID and WECOM_AGENT_ID and WECOM_SECRET):
        logger.debug("WeCom app credentials not set, skipping app push.")
        return False

    token = await _get_wecom_access_token()
    if not token:
        return False

    # App messages use plain text (markdown only works for group bots)
    content = _format_plain(report)

    # WeCom has 2048 char limit for text messages; truncate if needed
    if len(content) > 2000:
        content = content[:1997] + "..."

    payload = {
        "touser": "@all",
        "msgtype": "text",
        "agentid": WECOM_AGENT_ID,
        "text": {"content": content},
    }

    send_url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(send_url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if data.get("errcode") == 0:
                logger.info("WeCom app push succeeded for [%s]", report["industry"])
                return True
            else:
                logger.warning("WeCom app push error: %s", data)
                return False
    except Exception:
        logger.exception("WeCom app push failed")
        return False


# =====================================================================
#  企业微信群机器人推送
# =====================================================================

async def push_wecom(report: dict) -> bool:
    """Push report to WeCom (企业微信) group bot via webhook.

    Webhook URL format:
    https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
    """
    if not WECOM_WEBHOOK_URL:
        logger.debug("WECOM_WEBHOOK_URL not set, skipping WeCom push.")
        return False

    markdown = _format_markdown(report)

    # WeCom markdown message format
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": markdown,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                WECOM_WEBHOOK_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("errcode") == 0:
                logger.info("WeCom push succeeded for [%s]", report["industry"])
                return True
            else:
                logger.warning("WeCom push error: %s", data)
                return False
    except Exception:
        logger.exception("WeCom push failed")
        return False


# =====================================================================
#  Server酱推送 (个人微信)
# =====================================================================

async def push_serverchan(report: dict) -> bool:
    """Push report to personal WeChat via Server酱 (https://sct.ftqq.com/).

    Set SERVERCHAN_KEY to your SendKey.
    """
    if not SERVERCHAN_KEY:
        logger.debug("SERVERCHAN_KEY not set, skipping Server酱 push.")
        return False

    title = f"采购意向日报[{report['industry']}] {report['qualified']}条线索"
    markdown = _format_markdown(report)

    url = f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                data={"title": title, "desp": markdown},
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") == 0:
                logger.info("Server酱 push succeeded for [%s]", report["industry"])
                return True
            else:
                logger.warning("Server酱 push error: %s", data)
                return False
    except Exception:
        logger.exception("Server酱 push failed")
        return False


# =====================================================================
#  统一推送入口
# =====================================================================

async def notify(
    industry: str,
    leads: list[dict],
    source_counts: dict[str, int],
) -> None:
    """Send daily report to all configured push channels."""
    report = _build_report(industry, leads, source_counts)

    results = []
    if WECOM_CORP_ID and WECOM_AGENT_ID and WECOM_SECRET:
        results.append(("WeCom应用消息", await push_wecom_app(report)))
    if WECOM_WEBHOOK_URL:
        results.append(("WeCom群机器人", await push_wecom(report)))
    if SERVERCHAN_KEY:
        results.append(("Server酱", await push_serverchan(report)))

    if not results:
        logger.info("No push channels configured. Set WECOM_WEBHOOK_URL or SERVERCHAN_KEY.")
        return

    for name, ok in results:
        status = "success" if ok else "FAILED"
        logger.info("Push [%s] %s: %s", industry, name, status)
