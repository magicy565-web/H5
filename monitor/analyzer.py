"""
LLM intent analysis module – uses Qwen via OpenAI-compatible API
to score buyer intent and extract structured lead data.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from openai import AsyncOpenAI

from monitor.collectors.base import RawSignal
from monitor.config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_BATCH_SIZE,
    LLM_FALLBACK_MODELS,
    LLM_MODEL,
    MIN_INTENT_SCORE,
    get_active_profile,
    get_active_industry,
)

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Lead dataclass
# ------------------------------------------------------------------

@dataclass
class Lead:
    """A qualified lead produced by LLM analysis."""

    id: str                  # "lead-YYYYMMDD-NNN"
    source: str
    sourceUrl: str
    discoveredAt: str
    title: str
    rawText: str
    intentScore: int         # 1-5
    buyerCountry: str
    buyerFlag: str
    buyerName: str
    buyerType: str           # e.g. "终端工厂", "贸易商", "个人"
    machineSpecs: str
    urgency: str             # immediate / short_term / long_term / none
    summaryZh: str           # Chinese summary
    recommendedAction: str   # 立即联系 / 持续跟踪 / 暂时忽略
    contactInfo: str
    contentHash: str


# ------------------------------------------------------------------
# Country -> flag emoji helper
# ------------------------------------------------------------------

_COUNTRY_FLAGS: dict[str, str] = {
    "China": "\U0001f1e8\U0001f1f3", "India": "\U0001f1ee\U0001f1f3",
    "Vietnam": "\U0001f1fb\U0001f1f3", "Indonesia": "\U0001f1ee\U0001f1e9",
    "Mexico": "\U0001f1f2\U0001f1fd", "USA": "\U0001f1fa\U0001f1f8",
    "Turkey": "\U0001f1f9\U0001f1f7", "Brazil": "\U0001f1e7\U0001f1f7",
    "Thailand": "\U0001f1f9\U0001f1ed", "Pakistan": "\U0001f1f5\U0001f1f0",
    "Bangladesh": "\U0001f1e7\U0001f1e9", "Nigeria": "\U0001f1f3\U0001f1ec",
    "Egypt": "\U0001f1ea\U0001f1ec", "Russia": "\U0001f1f7\U0001f1fa",
}


def _flag_for(country: str) -> str:
    return _COUNTRY_FLAGS.get(country, "")


# ------------------------------------------------------------------
# Prompt templates — dynamically filled from active industry profile
# ------------------------------------------------------------------

def _get_system_prompt() -> str:
    profile = get_active_profile()
    return profile.get("llm_system_prompt", "你是一位专业的外贸销售情报分析师。")


def _get_user_prompt(count: int, signals_json: str) -> str:
    profile = get_active_profile()
    spec_hint = profile.get("llm_spec_field_hint", "提及的产品规格或需求细节")
    return f"""\
以下是 {count} 条从网上采集到的潜在买家信号，请逐条分析并返回一个 JSON 数组。

每条分析结果必须包含以下字段：
- "intent_score": 购买意向评分 1-5（1=无意向, 2=可能相关, 3=有一定意向, 4=意向明确, 5=紧急采购）
- "buyer_country": 买家所在国家（英文）
- "buyer_name": 买家名称（如可提取）
- "buyer_type": 买家类型（终端工厂 / 贸易商 / 个人 / 未知）
- "machine_specs": {spec_hint}
- "urgency": 紧急程度（immediate / short_term / long_term / none）
- "summary_zh": 一句话中文摘要
- "recommended_action": 建议动作（立即联系 / 持续跟踪 / 暂时忽略）

只返回一个合法的 JSON 数组，不要包含任何其他文字、解释或 markdown。

信号列表：
{signals_json}
"""

# ------------------------------------------------------------------
# Lead ID generator
# ------------------------------------------------------------------

_lead_counter: int = 0
_lead_counter_lock = asyncio.Lock()


async def _next_lead_id() -> str:
    global _lead_counter
    async with _lead_counter_lock:
        _lead_counter += 1
        counter_val = _lead_counter
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    industry_tag = get_active_profile().get("name_en", "unknown")
    return f"lead-{industry_tag}-{date_str}-{counter_val:03d}"


# ------------------------------------------------------------------
# IntentAnalyzer
# ------------------------------------------------------------------

class IntentAnalyzer:
    """Analyse raw signals with an LLM and produce qualified Leads."""

    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            base_url=LLM_BASE_URL,
            api_key=LLM_API_KEY,
        )

    # ---- batch analysis ----

    async def analyze_batch(self, signals: list[RawSignal]) -> list[Lead]:
        """Send a batch of signals to the LLM and return qualified Leads."""
        if not signals:
            return []

        signals_payload = [
            {
                "index": idx,
                "source": s.source,
                "url": s.url,
                "title": s.title,
                "text": s.text[:1500],  # truncate to control token usage
                "buyer_name": s.buyer_name,
                "buyer_country": s.buyer_country,
                "contact_info": s.contact_info,
            }
            for idx, s in enumerate(signals)
        ]

        user_prompt = _get_user_prompt(
            count=len(signals),
            signals_json=json.dumps(signals_payload, ensure_ascii=False, indent=2),
        )

        models_to_try = [LLM_MODEL] + [
            m for m in LLM_FALLBACK_MODELS if m != LLM_MODEL
        ]

        for model in models_to_try:
            try:
                return await self._call_llm(model, user_prompt, signals)
            except Exception as exc:
                logger.warning("Model %s failed: %s", model, exc)

        logger.error("All LLM models failed for batch of %d signals.", len(signals))
        return []

    def _match_signal(self, analysis: dict, idx: int, signals: list[RawSignal]) -> RawSignal:
        """Match an LLM analysis entry back to its original signal.

        Tries to match by title/URL first; falls back to index only if
        the index is in range.
        """
        # Try matching by title or URL from the analysis payload
        a_title = (analysis.get("title") or "").strip().lower()
        a_url = (analysis.get("url") or "").strip().lower()
        if a_title or a_url:
            for sig in signals:
                if a_title and sig.title.strip().lower() == a_title:
                    return sig
                if a_url and sig.url.strip().lower() == a_url:
                    return sig

        # Fall back to index-based matching (only if in range)
        if 0 <= idx < len(signals):
            return signals[idx]

        # Last resort: return first signal rather than silently mis-matching
        logger.warning(
            "Could not match analysis idx=%d to any signal; defaulting to first signal.", idx
        )
        return signals[0]

    async def _call_llm(
        self,
        model: str,
        user_prompt: str,
        signals: list[RawSignal],
    ) -> list[Lead]:
        """Call a single model and parse the response into Leads."""
        logger.info("Calling model %s for %d signals …", model, len(signals))

        response = await self._client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _get_system_prompt()},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=4096,
        )

        raw_text = response.choices[0].message.content or ""
        raw_text = raw_text.strip()

        # Strip possible markdown fences
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[-1]
        if raw_text.endswith("```"):
            raw_text = raw_text.rsplit("```", 1)[0]
        raw_text = raw_text.strip()

        try:
            analyses: list[dict] = json.loads(raw_text)
            if not isinstance(analyses, list):
                raise ValueError("LLM response is not a JSON array")
        except (json.JSONDecodeError, ValueError) as parse_err:
            logger.warning("JSON parse failed (%s), retrying with stricter prompt …", parse_err)
            # Retry once with an explicit JSON-only prompt
            retry_prompt = (
                "Your previous response was not valid JSON. "
                "Please return ONLY a valid JSON array, no markdown, no explanation.\n\n"
                + user_prompt
            )
            retry_resp = await self._client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _get_system_prompt()},
                    {"role": "user", "content": retry_prompt},
                ],
                temperature=0.1,
                max_tokens=4096,
            )
            retry_text = (retry_resp.choices[0].message.content or "").strip()
            if retry_text.startswith("```"):
                retry_text = retry_text.split("\n", 1)[-1]
            if retry_text.endswith("```"):
                retry_text = retry_text.rsplit("```", 1)[0]
            retry_text = retry_text.strip()
            try:
                analyses = json.loads(retry_text)
                if not isinstance(analyses, list):
                    raise ValueError("Retry response is not a JSON array")
            except (json.JSONDecodeError, ValueError) as retry_err:
                logger.error(
                    "JSON parse failed on retry (%s). Creating minimal leads from raw signals.",
                    retry_err,
                )
                return await self._minimal_leads_from_signals(signals)

        leads: list[Lead] = []
        now_iso = datetime.now(timezone.utc).isoformat()

        for idx, analysis in enumerate(analyses):
            score = int(analysis.get("intent_score", 0))
            if score < MIN_INTENT_SCORE:
                continue

            signal = self._match_signal(analysis, idx, signals)
            country = analysis.get("buyer_country", signal.buyer_country) or "Unknown"

            lead = Lead(
                id=await _next_lead_id(),
                source=signal.source,
                sourceUrl=signal.url,
                discoveredAt=now_iso,
                title=signal.title,
                rawText=signal.text[:2000],
                intentScore=score,
                buyerCountry=country,
                buyerFlag=_flag_for(country),
                buyerName=analysis.get("buyer_name", signal.buyer_name) or "",
                buyerType=analysis.get("buyer_type", "未知"),
                machineSpecs=analysis.get("machine_specs", ""),
                urgency=analysis.get("urgency", "none"),
                summaryZh=analysis.get("summary_zh", ""),
                recommendedAction=analysis.get("recommended_action", "暂时忽略"),
                contactInfo=signal.contact_info,
                contentHash=signal.content_hash,
            )
            leads.append(lead)

        logger.info(
            "Model %s returned %d analyses, %d qualified leads (score >= %d).",
            model, len(analyses), len(leads), MIN_INTENT_SCORE,
        )
        return leads

    async def _minimal_leads_from_signals(self, signals: list[RawSignal]) -> list[Lead]:
        """Create minimal leads directly from raw signals when LLM parsing fails entirely."""
        now_iso = datetime.now(timezone.utc).isoformat()
        leads: list[Lead] = []
        for sig in signals:
            lead = Lead(
                id=await _next_lead_id(),
                source=sig.source,
                sourceUrl=sig.url,
                discoveredAt=now_iso,
                title=sig.title,
                rawText=sig.text[:2000],
                intentScore=MIN_INTENT_SCORE,
                buyerCountry=sig.buyer_country or "Unknown",
                buyerFlag=_flag_for(sig.buyer_country or "Unknown"),
                buyerName=sig.buyer_name,
                buyerType="未知",
                machineSpecs="",
                urgency="none",
                summaryZh="LLM解析失败，请人工审核",
                recommendedAction="持续跟踪",
                contactInfo=sig.contact_info,
                contentHash=sig.content_hash,
            )
            leads.append(lead)
        logger.info("Created %d minimal leads from raw signals (LLM fallback).", len(leads))
        return leads

    # ---- process all signals in batches ----

    async def analyze_all(self, signals: list[RawSignal]) -> list[Lead]:
        """Split signals into batches, analyse each, and return all leads."""
        if not signals:
            return []

        all_leads: list[Lead] = []
        for i, start in enumerate(range(0, len(signals), LLM_BATCH_SIZE)):
            batch = signals[start : start + LLM_BATCH_SIZE]
            logger.info(
                "Analysing batch %d–%d of %d signals …",
                start + 1, start + len(batch), len(signals),
            )
            # Add 1-second delay between batches to avoid rate limiting
            if i > 0:
                await asyncio.sleep(1.0)
            leads = await self.analyze_batch(batch)
            all_leads.extend(leads)

        logger.info("Total qualified leads: %d / %d signals.", len(all_leads), len(signals))
        return all_leads
