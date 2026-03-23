"""
微信推送模块 — 支持富文本卡片、分类分组、操作按钮。

推送渠道:
  1. 企业微信应用消息 (template_card) — 带操作按钮的交互卡片
  2. 企业微信群机器人 (webhook)        — 分类 markdown + news 卡片
  3. Server酱 (个人微信)               — 分类 markdown

企业微信应用消息: 设置 WECOM_CORP_ID + WECOM_AGENT_ID + WECOM_SECRET
企业微信群机器人: 设置 WECOM_WEBHOOK_URL
Server酱:        设置 SERVERCHAN_KEY
"""
from __future__ import annotations

import base64
import json
import logging
import time
import urllib.parse
from datetime import datetime
from typing import Any

import httpx

from monitor.config import (
    WECOM_CORP_ID, WECOM_AGENT_ID, WECOM_SECRET,
    WECOM_WEBHOOK_URL, SERVERCHAN_KEY,
)

logger = logging.getLogger(__name__)

# ── WeCom access token cache ─────────────────────────────────────────
_wecom_token_cache: dict[str, Any] = {"token": None, "expires_at": 0.0}

# ── H5 pages URL (for card links) ───────────────────────────────────
H5_BASE_URL = "https://magicy565-web.github.io/H5/"
H5_LEAD_URL = "https://magicy565-web.github.io/H5/lead.html"


def _build_lead_snapshot_url(lead: dict) -> str:
    """Build a snapshot URL for a lead detail page.

    Encodes lead data as base64 JSON in the URL hash fragment.
    The lead.html page reads this and renders a rich detail card.
    WeCom template_card URL limit is 1024 bytes — keep it tight.
    """
    # Use minimal keys and aggressive truncation to stay under 1024 bytes
    snapshot = {
        "t": (lead.get("title") or "")[:50],                          # title
        "s": lead.get("intentScore", 0),                              # score
        "c": lead.get("buyerCountry", ""),                            # country
        "n": (lead.get("buyerName") or "")[:20],                      # name
        "bt": lead.get("buyerType", ""),                              # buyerType
        "z": (lead.get("summaryZh") or "")[:80],                      # summaryZh
        "sp": (lead.get("machineSpecs") or "")[:40],                  # specs
        "u": lead.get("urgency", ""),                                 # urgency
        "a": lead.get("recommendedAction", ""),                       # action
        "sr": lead.get("source", ""),                                 # source
        "su": lead.get("sourceUrl", "") or lead.get("url", ""),       # sourceUrl
        "d": lead.get("discoveredAt", ""),                            # discoveredAt
    }
    # Remove empty values to save space
    snapshot = {k: v for k, v in snapshot.items() if v}
    json_str = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    b64 = base64.b64encode(json_str.encode("utf-8")).decode("ascii")
    url = f"{H5_LEAD_URL}#{b64}"

    # Hard limit: if over 1020 chars, progressively drop fields
    if len(url) > 1020:
        for drop_key in ["d", "su", "sp"]:
            snapshot.pop(drop_key, None)
            json_str = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
            b64 = base64.b64encode(json_str.encode("utf-8")).decode("ascii")
            url = f"{H5_LEAD_URL}#{b64}"
            if len(url) <= 1020:
                break

    return url

# ── Urgency / tier classification ────────────────────────────────────
_URGENCY_TIERS = {
    "紧急跟进": {"min_score": 5, "urgency": {"immediate"}, "color": "#FF4D4F", "emoji": "🔴"},
    "重点关注": {"min_score": 4, "urgency": {"immediate", "short_term"}, "color": "#FF8C00", "emoji": "🟠"},
    "持续跟踪": {"min_score": 3, "urgency": None, "color": "#1890FF", "emoji": "🔵"},
}


# =====================================================================
#  数据分类与报告构建
# =====================================================================

def _classify_lead(lead: dict) -> str:
    """Classify a lead into an urgency tier."""
    score = lead.get("intentScore", 0) or 0
    urgency = lead.get("urgency", "none") or "none"
    action = lead.get("recommendedAction", "") or ""

    if score >= 5 or (score >= 4 and urgency == "immediate") or "立即联系" in action:
        return "紧急跟进"
    elif score >= 4 or urgency in ("immediate", "short_term"):
        return "重点关注"
    else:
        return "持续跟踪"


def _build_report(industry: str, leads: list[dict], source_counts: dict[str, int]) -> dict:
    """Build structured report data with classification."""
    total_signals = sum(source_counts.values())
    qualified = len(leads)

    # Classify leads into tiers
    tiers: dict[str, list[dict]] = {"紧急跟进": [], "重点关注": [], "持续跟踪": []}
    for lead in leads:
        tier = _classify_lead(lead)
        tiers[tier].append(lead)

    # Sort each tier by score desc
    for tier_leads in tiers.values():
        tier_leads.sort(key=lambda x: -(x.get("intentScore", 0) or 0))

    # Score distribution
    score_dist = {}
    for lead in leads:
        s = lead.get("intentScore", 0)
        score_dist[s] = score_dist.get(s, 0) + 1

    # Country distribution
    country_dist: dict[str, int] = {}
    for lead in leads:
        c = lead.get("buyerCountry", "Unknown") or "Unknown"
        country_dist[c] = country_dist.get(c, 0) + 1
    top_countries = sorted(country_dist.items(), key=lambda x: -x[1])[:5]

    return {
        "industry": industry,
        "total_signals": total_signals,
        "qualified": qualified,
        "source_counts": source_counts,
        "score_dist": score_dist,
        "top_countries": top_countries,
        "tiers": tiers,
        # Flat top list for backward compat
        "top_leads": sorted(leads, key=lambda x: -(x.get("intentScore", 0) or 0))[:10],
    }


# =====================================================================
#  格式化: 分类 Markdown (群机器人 + Server酱)
# =====================================================================

def _format_lead_line(lead: dict, idx: int) -> str:
    """Format a single lead as a markdown line."""
    score = lead.get("intentScore", "?")
    country = lead.get("buyerCountry", "?")
    buyer = lead.get("buyerName", "") or ""
    title = (lead.get("title") or "")[:45]
    summary = lead.get("summaryZh", "")
    action = lead.get("recommendedAction", "")
    urgency = lead.get("urgency", "")
    source = lead.get("source", "")

    line = f"**{idx}. [{score}分]** {country}"
    if buyer:
        line += f" | {buyer}"
    line += f"\n> {title}"
    if summary:
        line += f"\n> 💡 {summary[:80]}"
    tags = []
    if action:
        tags.append(action)
    if urgency and urgency != "none":
        urgency_zh = {"immediate": "紧急", "short_term": "近期", "long_term": "远期"}.get(urgency, urgency)
        tags.append(urgency_zh)
    if source:
        tags.append(source)
    if tags:
        line += f"\n> 🏷️ `{'` `'.join(tags)}`"
    return line


def _format_classified_markdown(report: dict) -> str:
    """Format report as classified Markdown with tier grouping."""
    r = report
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    tiers = r["tiers"]

    lines = [
        f"# 📊 采购意向日报 [{r['industry']}]",
        f"> 📅 {now}",
        "",
    ]

    # ── Stats summary bar ──
    urgent_n = len(tiers["紧急跟进"])
    important_n = len(tiers["重点关注"])
    tracking_n = len(tiers["持续跟踪"])
    lines.append(f"**总采集:** {r['total_signals']}条信号 → **{r['qualified']}条合格线索**")
    lines.append(
        f"🔴 紧急 **{urgent_n}** | 🟠 重点 **{important_n}** | 🔵 跟踪 **{tracking_n}**"
    )
    lines.append("")

    # ── Score distribution ──
    if r["score_dist"]:
        dist_parts = [f"{s}分({n}条)" for s, n in sorted(r["score_dist"].items(), reverse=True)]
        lines.append(f"**评分:** {' / '.join(dist_parts)}")

    # ── Country distribution ──
    if r["top_countries"]:
        country_parts = [f"{c}({n})" for c, n in r["top_countries"]]
        lines.append(f"**国家:** {', '.join(country_parts)}")
    lines.append("")

    # ── Tier: 紧急跟进 ──
    if tiers["紧急跟进"]:
        lines.append("---")
        lines.append(f"## 🔴 紧急跟进 ({urgent_n}条)")
        lines.append("")
        for i, lead in enumerate(tiers["紧急跟进"][:5], 1):
            lines.append(_format_lead_line(lead, i))
            lines.append("")

    # ── Tier: 重点关注 ──
    if tiers["重点关注"]:
        lines.append("---")
        lines.append(f"## 🟠 重点关注 ({important_n}条)")
        lines.append("")
        for i, lead in enumerate(tiers["重点关注"][:5], 1):
            lines.append(_format_lead_line(lead, i))
            lines.append("")

    # ── Tier: 持续跟踪 ──
    if tiers["持续跟踪"]:
        lines.append("---")
        lines.append(f"## 🔵 持续跟踪 ({tracking_n}条)")
        lines.append("")
        for i, lead in enumerate(tiers["持续跟踪"][:3], 1):
            lines.append(_format_lead_line(lead, i))
            lines.append("")

    # ── Source stats ──
    if r["source_counts"]:
        lines.append("---")
        lines.append("**数据源:** " + " | ".join(
            f"{src}({cnt})" for src, cnt in sorted(r["source_counts"].items())
        ))

    if r["qualified"] == 0:
        lines.append("")
        lines.append("*今日未发现新的合格线索。*")

    return "\n".join(lines)


def _format_plain(report: dict) -> str:
    """Format report as plain text (legacy fallback)."""
    r = report
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    tiers = r["tiers"]

    lines = [
        f"=== 采购意向日报 [{r['industry']}] ===",
        f"时间: {now}",
        f"合格线索: {r['qualified']}条",
        "",
        f"买家国家: {', '.join(f'{c}({n})' for c, n in r['top_countries'])}",
        "",
    ]

    # Score distribution
    if r["score_dist"]:
        dist_parts = [f"{s}分({n}条)" for s, n in sorted(r["score_dist"].items(), reverse=True)]
        lines.append(f"评分分布: {' / '.join(dist_parts)}")
        lines.append("")

    # Classified leads
    for tier_name, emoji in [("紧急跟进", "🔴"), ("重点关注", "🟠"), ("持续跟踪", "🔵")]:
        tier_leads = tiers.get(tier_name, [])
        if not tier_leads:
            continue
        lines.append(f"{emoji} {tier_name} ({len(tier_leads)}条):")
        for i, lead in enumerate(tier_leads[:5], 1):
            score = lead.get("intentScore", "?")
            country = lead.get("buyerCountry", "?")
            buyer = lead.get("buyerName", "") or ""
            title = (lead.get("title") or "")[:45]
            summary = (lead.get("summaryZh") or "")[:60]
            action = lead.get("recommendedAction", "")

            line = f"  {i}. [{score}分] {country}"
            if buyer:
                line += f" | {buyer}"
            lines.append(line)
            lines.append(f"     {title}")
            if summary:
                lines.append(f"     {summary}")
            if action:
                lines.append(f"     → {action}")
        lines.append("")

    return "\n".join(lines)


# =====================================================================
#  企业微信 access_token (带缓存)
# =====================================================================

async def _get_wecom_access_token() -> str | None:
    """Fetch access_token from WeCom API. Caches until 5min before expiry."""
    if not (WECOM_CORP_ID and WECOM_SECRET):
        return None

    if _wecom_token_cache["token"] and time.time() < _wecom_token_cache["expires_at"] - 300:
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


async def _wecom_send(token: str, payload: dict) -> bool:
    """Send a message via WeCom message API."""
    send_url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(send_url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if data.get("errcode") == 0:
                return True
            logger.warning("WeCom send error: %s", data)
            return False
    except Exception:
        logger.exception("WeCom send failed")
        return False


# =====================================================================
#  企业微信应用消息 — 模板卡片 + 操作按钮
# =====================================================================

def _build_summary_card(report: dict) -> dict:
    """Build a template_card summary message for the daily report.

    Uses WeCom 'text_notice' template card with action buttons.
    """
    r = report
    now = datetime.now().strftime("%m/%d %H:%M")
    tiers = r["tiers"]

    urgent_n = len(tiers["紧急跟进"])
    important_n = len(tiers["重点关注"])
    tracking_n = len(tiers["持续跟踪"])

    # Main text
    main_title = f"📊 采购意向日报 [{r['industry']}]"
    sub_title = f"{now} | 合格线索 {r['qualified']}条"

    # Horizontal content (key-value pairs)
    horizontal = [
        {"keyname": "🔴 紧急跟进", "value": f"{urgent_n}条"},
        {"keyname": "🟠 重点关注", "value": f"{important_n}条"},
        {"keyname": "🔵 持续跟踪", "value": f"{tracking_n}条"},
    ]

    # Top countries as a line
    if r["top_countries"]:
        country_str = ", ".join(f"{c}({n})" for c, n in r["top_countries"][:4])
        horizontal.append({"keyname": "🌍 买家国家", "value": country_str})

    # Score distribution
    if r["score_dist"]:
        dist_str = " / ".join(f"{s}分({n})" for s, n in sorted(r["score_dist"].items(), reverse=True))
        horizontal.append({"keyname": "📈 评分分布", "value": dist_str})

    # Card buttons
    card_action = {
        "type": 1,  # open URL
        "url": H5_BASE_URL,
        "appid": "",
        "pagepath": "",
    }

    return {
        "touser": "@all",
        "msgtype": "template_card",
        "agentid": WECOM_AGENT_ID,
        "template_card": {
            "card_type": "text_notice",
            "source": {
                "icon_url": "",
                "desc": "采购意向监听",
                "desc_color": 1,  # green
            },
            "main_title": {
                "title": main_title,
                "desc": sub_title,
            },
            "horizontal_content_list": horizontal,
            "card_action": card_action,
        },
    }


def _build_lead_card(lead: dict, idx: int, total: int, industry: str) -> dict:
    """Build a template_card for a single high-priority lead.

    Uses 'text_notice' card type with structured data and action button.
    """
    score = lead.get("intentScore", 0)
    country = lead.get("buyerCountry", "Unknown")
    buyer = lead.get("buyerName", "") or "未知买家"
    title = (lead.get("title") or "")[:60]
    summary = lead.get("summaryZh", "")
    action = lead.get("recommendedAction", "持续跟踪")
    urgency = lead.get("urgency", "none")
    source = lead.get("source", "")
    source_url = lead.get("sourceUrl", "") or lead.get("url", "") or ""
    specs = lead.get("machineSpecs", "") or ""
    contact = lead.get("contactInfo", "") or ""
    buyer_type = lead.get("buyerType", "") or ""

    # Tier determination
    tier = _classify_lead(lead)
    tier_info = _URGENCY_TIERS.get(tier, {})
    tier_emoji = tier_info.get("emoji", "🔵")

    # Urgency translation
    urgency_zh = {
        "immediate": "紧急", "short_term": "近期",
        "long_term": "远期", "none": "未知"
    }.get(urgency, urgency)

    main_title = f"{tier_emoji} [{score}分] {country} | {buyer}"
    sub_title = f"#{idx}/{total} · {industry} · {tier}"

    horizontal = []
    horizontal.append({"keyname": "📋 标题", "value": title[:30]})
    if summary:
        horizontal.append({"keyname": "💡 摘要", "value": summary[:40]})
    if specs:
        horizontal.append({"keyname": "🔧 规格", "value": specs[:30]})
    horizontal.append({"keyname": "⏰ 紧迫度", "value": urgency_zh})
    if buyer_type:
        horizontal.append({"keyname": "🏢 买家类型", "value": buyer_type})
    if contact:
        horizontal.append({"keyname": "📞 联系方式", "value": contact[:30]})
    horizontal.append({"keyname": "🎯 建议", "value": action})
    if source:
        horizontal.append({"keyname": "📡 来源", "value": source})

    # Limit to 6 items (WeCom API limit)
    horizontal = horizontal[:6]

    # Action URL — link to snapshot detail page (not the raw source)
    snapshot_url = _build_lead_snapshot_url(lead)

    return {
        "touser": "@all",
        "msgtype": "template_card",
        "agentid": WECOM_AGENT_ID,
        "template_card": {
            "card_type": "text_notice",
            "source": {
                "icon_url": "",
                "desc": f"采购线索 · {tier}",
                "desc_color": 0 if tier == "紧急跟进" else (1 if tier == "重点关注" else 2),
            },
            "main_title": {
                "title": main_title,
                "desc": sub_title,
            },
            "horizontal_content_list": horizontal,
            "card_action": {
                "type": 1,
                "url": snapshot_url,
            },
        },
    }


async def push_wecom_app(report: dict) -> bool:
    """Push report via WeCom app messages — summary card + per-lead cards.

    Sends:
    1. One summary card (daily overview with stats)
    2. Individual cards for each 紧急跟进 and 重点关注 lead (max 10)
    """
    if not (WECOM_CORP_ID and WECOM_AGENT_ID and WECOM_SECRET):
        logger.debug("WeCom app credentials not set, skipping app push.")
        return False

    token = await _get_wecom_access_token()
    if not token:
        return False

    industry = report["industry"]
    tiers = report["tiers"]

    # 1. Send summary card
    summary_payload = _build_summary_card(report)
    ok = await _wecom_send(token, summary_payload)
    if ok:
        logger.info("WeCom app: summary card sent for [%s]", industry)
    else:
        logger.warning("WeCom app: summary card failed for [%s]", industry)

    # 2. Send individual lead cards for urgent + important (max 10)
    priority_leads = tiers["紧急跟进"] + tiers["重点关注"]
    total_priority = len(priority_leads)
    cards_sent = 0

    for i, lead in enumerate(priority_leads[:10], 1):
        lead_payload = _build_lead_card(lead, i, total_priority, industry)
        lead_ok = await _wecom_send(token, lead_payload)
        if lead_ok:
            cards_sent += 1
        # Small delay to avoid rate limit (max 30 msg/sec for apps)
        if i < len(priority_leads):
            import asyncio
            await asyncio.sleep(0.1)

    if cards_sent > 0:
        logger.info("WeCom app: %d/%d lead cards sent for [%s]", cards_sent, min(total_priority, 10), industry)

    return ok


# =====================================================================
#  企业微信群机器人 — 分类 markdown + news 卡片
# =====================================================================

def _build_news_cards(leads: list[dict], tier_name: str) -> list[dict]:
    """Build news card articles from leads for group bot."""
    articles = []
    tier_info = _URGENCY_TIERS.get(tier_name, {})
    emoji = tier_info.get("emoji", "🔵")

    for lead in leads[:4]:  # WeCom news max 8 articles total
        score = lead.get("intentScore", "?")
        country = lead.get("buyerCountry", "?")
        buyer = lead.get("buyerName", "") or ""
        title = (lead.get("title") or "")[:50]
        summary = lead.get("summaryZh", "")
        action = lead.get("recommendedAction", "")
        source_url = lead.get("sourceUrl", "") or lead.get("url", "") or H5_BASE_URL

        article_title = f"{emoji}[{score}分] {country}"
        if buyer:
            article_title += f" | {buyer}"

        desc_parts = [title]
        if summary:
            desc_parts.append(f"💡 {summary[:60]}")
        if action:
            desc_parts.append(f"🎯 {action}")
        description = "\n".join(desc_parts)

        snapshot_url = _build_lead_snapshot_url(lead)
        articles.append({
            "title": article_title,
            "description": description,
            "url": snapshot_url,
            "picurl": "",
        })

    return articles


async def push_wecom(report: dict) -> bool:
    """Push report to WeCom group bot via webhook.

    Sends:
    1. Classified markdown summary
    2. News cards for urgent leads (if any)
    """
    if not WECOM_WEBHOOK_URL:
        logger.debug("WECOM_WEBHOOK_URL not set, skipping WeCom push.")
        return False

    tiers = report["tiers"]

    # 1. Send classified markdown summary
    markdown = _format_classified_markdown(report)

    # WeCom webhook markdown limit is ~4096 chars
    if len(markdown) > 4000:
        markdown = markdown[:3997] + "..."

    payload = {
        "msgtype": "markdown",
        "markdown": {"content": markdown},
    }

    ok = False
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                WECOM_WEBHOOK_URL, json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("errcode") == 0:
                ok = True
                logger.info("WeCom bot: markdown sent for [%s]", report["industry"])
            else:
                logger.warning("WeCom bot markdown error: %s", data)
    except Exception:
        logger.exception("WeCom bot markdown push failed")

    # 2. Send news cards for urgent leads
    urgent_leads = tiers["紧急跟进"]
    if urgent_leads:
        articles = _build_news_cards(urgent_leads, "紧急跟进")
        news_payload = {
            "msgtype": "news",
            "news": {"articles": articles},
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    WECOM_WEBHOOK_URL, json=news_payload,
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("errcode") == 0:
                    logger.info("WeCom bot: %d urgent news cards sent", len(articles))
                else:
                    logger.warning("WeCom bot news error: %s", data)
        except Exception:
            logger.exception("WeCom bot news push failed")

    return ok


# =====================================================================
#  Server酱推送 (个人微信) — 分类 markdown
# =====================================================================

async def push_serverchan(report: dict) -> bool:
    """Push classified report to personal WeChat via Server酱."""
    if not SERVERCHAN_KEY:
        logger.debug("SERVERCHAN_KEY not set, skipping Server酱 push.")
        return False

    tiers = report["tiers"]
    urgent_n = len(tiers["紧急跟进"])
    important_n = len(tiers["重点关注"])

    title_parts = [f"采购日报[{report['industry']}]"]
    if urgent_n > 0:
        title_parts.append(f"🔴紧急{urgent_n}")
    if important_n > 0:
        title_parts.append(f"🟠重点{important_n}")
    title_parts.append(f"共{report['qualified']}条")
    title = " ".join(title_parts)

    markdown = _format_classified_markdown(report)

    url = f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, data={"title": title, "desp": markdown})
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
    """Send classified daily report to all configured push channels.

    Sends:
    - Summary card + per-lead cards via WeCom app (template_card)
    - Classified markdown + news cards via WeCom group bot
    - Classified markdown via Server酱
    """
    report = _build_report(industry, leads, source_counts)

    tiers = report["tiers"]
    logger.info(
        "Notify [%s]: 🔴紧急 %d | 🟠重点 %d | 🔵跟踪 %d",
        industry, len(tiers["紧急跟进"]), len(tiers["重点关注"]), len(tiers["持续跟踪"]),
    )

    results = []
    if WECOM_CORP_ID and WECOM_AGENT_ID and WECOM_SECRET:
        results.append(("WeCom应用卡片", await push_wecom_app(report)))
    if WECOM_WEBHOOK_URL:
        results.append(("WeCom群机器人", await push_wecom(report)))
    if SERVERCHAN_KEY:
        results.append(("Server酱", await push_serverchan(report)))

    if not results:
        logger.info("No push channels configured. Set WECOM_WEBHOOK_URL or SERVERCHAN_KEY.")
        return

    for name, ok in results:
        status = "✅ success" if ok else "❌ FAILED"
        logger.info("Push [%s] %s: %s", industry, name, status)
