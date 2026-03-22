"""
CNSubscribe Multi-Skill SSE Backend
FastAPI server with 4 parallel LLM-powered Skill endpoints.
"""

import os
import json
import time
import random
import asyncio
import logging
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
LLM_API_KEY = os.getenv("DASHSCOPE_API_KEY", "sk-9cd6b877d45c4bb6a29925c2e1dab4b3")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen-plus")

# Fallback model chain: tried in order when the current model fails (quota/rate limit)
# Format: list of (base_url, api_key, model_name) tuples
# Models on DashScope free tier: qwen-turbo, qwen-long, qwen-plus, qwen-max
LLM_FALLBACK_MODELS = [
    # Primary
    (LLM_BASE_URL, LLM_API_KEY, "qwen-plus"),
    # Fallback 1: qwen-turbo (large free quota, fast)
    (LLM_BASE_URL, LLM_API_KEY, "qwen-turbo"),
    # Fallback 2: qwen-long (optimized for long context, separate quota)
    (LLM_BASE_URL, LLM_API_KEY, "qwen-long"),
    # Fallback 3: qwen-max (highest quality, separate quota)
    (LLM_BASE_URL, LLM_API_KEY, "qwen-max"),
]

# Allow overriding via env: comma-separated model names
_env_models = os.getenv("LLM_FALLBACK_MODELS", "")
if _env_models:
    LLM_FALLBACK_MODELS = [
        (LLM_BASE_URL, LLM_API_KEY, m.strip())
        for m in _env_models.split(",") if m.strip()
    ]

# Track which models are currently failing (circuit breaker)
_model_failures: dict[str, float] = {}  # model_name -> timestamp of last failure
MODEL_COOLDOWN_SECONDS = 300  # skip failed models for 5 minutes

BASE_DIR = Path(__file__).resolve().parent.parent  # /workspace/output
DB_DIR = BASE_DIR / "db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cnsubscribe")

# ---------------------------------------------------------------------------
# LLM client pool (one client per unique base_url+api_key)
# ---------------------------------------------------------------------------
_llm_clients: dict[str, AsyncOpenAI] = {}

def _get_llm_client(base_url: str, api_key: str) -> AsyncOpenAI:
    cache_key = f"{base_url}|{api_key}"
    if cache_key not in _llm_clients:
        _llm_clients[cache_key] = AsyncOpenAI(api_key=api_key, base_url=base_url)
    return _llm_clients[cache_key]


async def llm_chat_stream(messages: list[dict], temperature: float = 0.7, max_tokens: int = 4096):
    """Try each model in the fallback chain until one succeeds. Returns an async stream."""
    import time as _time

    errors = []
    for base_url, api_key, model_name in LLM_FALLBACK_MODELS:
        # Circuit breaker: skip models that failed recently
        last_fail = _model_failures.get(model_name, 0)
        if _time.time() - last_fail < MODEL_COOLDOWN_SECONDS:
            logger.info(f"Skipping {model_name} (cooldown, failed {int(_time.time() - last_fail)}s ago)")
            continue

        client = _get_llm_client(base_url, api_key)
        try:
            stream = await client.chat.completions.create(
                model=model_name,
                messages=messages,
                stream=True,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            logger.info(f"Using model: {model_name}")
            # Clear failure record on success
            _model_failures.pop(model_name, None)
            return stream, model_name
        except Exception as e:
            err_str = str(e)
            errors.append((model_name, err_str))
            logger.warning(f"Model {model_name} failed: {err_str[:200]}")
            # Mark as failed for circuit breaker
            _model_failures[model_name] = _time.time()
            continue

    # All models failed
    error_summary = "; ".join(f"{m}: {e[:100]}" for m, e in errors)
    raise Exception(f"All LLM models failed: {error_summary}")

# ---------------------------------------------------------------------------
# Buyer database
# ---------------------------------------------------------------------------
BUYER_DB: dict[str, list] = {}
HIGH_POTENTIAL_DB: dict[str, list] = {}  # industry -> high-potential buyers

def load_buyer_db():
    """Load all industry JSON files into memory."""
    for f in DB_DIR.glob("*.json"):
        if f.name.startswith("_"):
            continue
        stem = f.stem
        # High-potential files: 注塑模具_高潜力.json -> stored separately
        if "_高潜力" in stem:
            industry = stem.replace("_高潜力", "")
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and "buyers" in data:
                HIGH_POTENTIAL_DB[industry] = data["buyers"]
            elif isinstance(data, list):
                HIGH_POTENTIAL_DB[industry] = data
            logger.info(f"Loaded high-potential: {industry} ({len(HIGH_POTENTIAL_DB.get(industry, []))} buyers)")
            continue
        industry = stem
        with open(f, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        # Support both formats: raw list or {"buyers": [...]}
        if isinstance(data, dict) and "buyers" in data:
            BUYER_DB[industry] = data["buyers"]
        elif isinstance(data, list):
            BUYER_DB[industry] = data
        else:
            logger.warning(f"Unexpected format in {f.name}")
            continue
    total = sum(len(v) for v in BUYER_DB.values())
    hp_total = sum(len(v) for v in HIGH_POTENTIAL_DB.values())
    logger.info(f"Loaded buyer DB: {len(BUYER_DB)} industries, {total} buyers, {hp_total} high-potential")

# ---------------------------------------------------------------------------
# Helper: sample buyers for context
# ---------------------------------------------------------------------------
def sample_buyers(category: str, capacity: str, city: str, n: int = 30) -> list[dict]:
    """Sample relevant buyers to inject into LLM context.
    Prioritizes high-potential buyers when available."""
    # Try high-potential pool first
    hp_pool = HIGH_POTENTIAL_DB.get(category, [])
    pool = BUYER_DB.get(category, [])
    if not pool:
        # fuzzy match
        for key, buyers in BUYER_DB.items():
            if category in key or key in category:
                pool = buyers
                break
    if not pool:
        pool = list(BUYER_DB.values())[0] if BUYER_DB else []

    # Mix: take half from high-potential, half from general pool
    result = []
    if hp_pool:
        hp_sorted = sorted(hp_pool, key=lambda b: (
            -b.get("potentialScore", 0),
            -b.get("activityScore", 0),
        ))
        hp_sample = random.sample(hp_sorted[:min(n * 2, len(hp_sorted))], min(n // 2, len(hp_sorted)))
        result.extend(hp_sample)

    # Fill remaining from general pool
    remaining = n - len(result)
    if remaining > 0:
        scored = sorted(pool, key=lambda b: (
            -b.get("activityScore", 0),
            -int(b.get("verified", False)),
            b.get("lastActiveDaysAgo", 999),
        ))
        top = scored[:min(remaining * 3, len(scored))]
        general_sample = random.sample(top, min(remaining, len(top)))
        result.extend(general_sample)

    return result


def buyers_to_context(buyers: list[dict]) -> str:
    """Format sampled buyers as compact text for LLM."""
    lines = []
    for b in buyers:
        lines.append(
            f"- {b['flag']} {b['name']} ({b['country']}/{b.get('city','')}) "
            f"规模:{b['scale']} 年采购:{b['annualProcurement']} "
            f"产品:{','.join(b.get('products',[])[:3])} "
            f"认证:{','.join(b.get('certsRequired',[])[:3])} "
            f"MOQ:{b.get('moq',0)} 活跃度:{b.get('activityScore',0)} "
            f"已认证:{'✓' if b.get('verified') else '✗'} "
            f"上次活跃:{b.get('lastActiveDaysAgo',0)}天前 "
            f"采购频率:{b.get('procurementFreq','')} "
            f"付款:{b.get('paymentTerms','')}"
        )
    return "\n".join(lines)


def get_industry_stats(category: str) -> dict:
    """Get basic stats for the industry."""
    pool = BUYER_DB.get(category, [])
    if not pool:
        for key, buyers in BUYER_DB.items():
            if category in key or key in category:
                pool = buyers
                break
    total = len(pool)
    verified = sum(1 for b in pool if b.get("verified"))
    countries = {}
    for b in pool:
        c = b.get("country", "未知")
        countries[c] = countries.get(c, 0) + 1
    top_countries = sorted(countries.items(), key=lambda x: -x[1])[:5]
    avg_activity = sum(b.get("activityScore", 0) for b in pool) / max(total, 1)
    return {
        "total": total,
        "verified": verified,
        "top_countries": top_countries,
        "avg_activity": round(avg_activity, 1),
    }

# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------
def sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------
SYSTEM_PROMPTS = {}

# PLACEHOLDER: filled in below
SYSTEM_PROMPTS["buyer-match"] = ""
SYSTEM_PROMPTS["market-estimate"] = ""
SYSTEM_PROMPTS["order-forecast"] = ""
SYSTEM_PROMPTS["competition"] = ""

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="CNSubscribe API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    load_buyer_db()


# ---------------------------------------------------------------------------
# Skill SSE handler (generic)
# ---------------------------------------------------------------------------
async def skill_stream(
    skill_name: str, category: str, capacity: str, city: str
) -> AsyncGenerator[str, None]:
    """Generic SSE generator for any skill.

    Strategy: accumulate full LLM output, then parse markers.
    We stream delta events in real-time for text between markers,
    but for [RESULT] we accumulate until JSON is complete.
    """
    try:
        buyers = sample_buyers(category, capacity, city)
        stats = get_industry_stats(category)
        buyer_context = buyers_to_context(buyers)

        system_prompt = build_system_prompt(skill_name, category, capacity, city, buyer_context, stats)

        user_msg = f"行业: {category}\n月产能: {capacity}\n城市: {city}"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ]

        stream, model_used = await llm_chat_stream(messages, temperature=0.7, max_tokens=4096)
        logger.info(f"Skill {skill_name} using model: {model_used}")

        # Two-phase approach:
        # Phase 1: stream tokens, emit step/delta events, detect [RESULT]
        # Phase 2: once [RESULT] detected, accumulate remaining for JSON

        buffer = ""
        result_mode = False
        result_buf = ""

        async for chunk in stream:
            delta = chunk.choices[0].delta
            if not delta.content:
                continue
            token = delta.content

            if result_mode:
                # Phase 2: accumulating JSON after [RESULT]
                result_buf += token
                continue

            buffer += token

            # Check if [RESULT] marker is now complete in buffer
            ri = buffer.find("[RESULT]")
            if ri != -1:
                # Emit any text before [RESULT]
                before = buffer[:ri]
                async for evt in _flush_text(before):
                    yield evt
                # Switch to result accumulation mode
                result_buf = buffer[ri + 8:]  # after "[RESULT]"
                buffer = ""
                result_mode = True
                continue

            # Fallback: check if buffer contains inline JSON (no [RESULT] marker)
            # Only check when buffer is long enough to contain a JSON object
            if len(buffer) > 50:
                import re
                json_patterns = r'\{\s*"(?:leads|exposure|orders|factoryCount|clients|inquiries)"'
                m = re.search(json_patterns, buffer)
                if m:
                    # Found JSON start - emit text before it, switch to result mode
                    json_start = m.start()
                    before = buffer[:json_start]
                    async for evt in _flush_text(before):
                        yield evt
                    result_buf = buffer[json_start:]
                    buffer = ""
                    result_mode = True
                    logger.info(f"Detected inline JSON for {skill_name} (no [RESULT] marker)")
                    continue

            # Check if buffer might contain a partial marker at the end
            # e.g. "[", "[S", "[STEP", "[STEP:", "[R", "[RE", "[RES", "[RESU", "[RESUL", "[RESULT"
            safe_end = _find_safe_end(buffer)

            if safe_end > 0:
                to_emit = buffer[:safe_end]
                buffer = buffer[safe_end:]
                async for evt in _flush_text(to_emit):
                    yield evt

        # Stream ended - flush any remaining buffer
        if result_mode:
            # Parse the accumulated result JSON
            json_str = result_buf.strip()
            if json_str:
                result_data = _try_parse_json(json_str)
                if result_data is not None:
                    yield sse_event("result", result_data)
                else:
                    logger.warning(f"Failed to parse result JSON for {skill_name}: {json_str[:300]}")
        else:
            # No [RESULT] found - try to extract JSON from buffer as fallback
            if buffer.strip():
                text_part, json_data = _extract_json_fallback(buffer)
                if text_part.strip():
                    async for evt in _flush_text(text_part):
                        yield evt
                if json_data is not None:
                    yield sse_event("result", json_data)
                    logger.info(f"Fallback JSON extraction succeeded for {skill_name}")
                else:
                    logger.warning(f"No [RESULT] marker and no JSON found for {skill_name}")

        yield sse_event("done", {})

    except Exception as e:
        logger.error(f"Skill {skill_name} error: {e}", exc_info=True)
        yield sse_event("error", {"message": f"分析失败: {str(e)}"})


def _find_safe_end(buf: str) -> int:
    """Find the safe emit boundary - everything before a potential partial marker."""
    import re
    # Look for any '[' that could be the start of a [STEP:xxx] or [RESULT] marker
    # that hasn't been closed yet with ']'
    last_bracket = buf.rfind("[")
    if last_bracket != -1:
        remainder = buf[last_bracket:]
        # If there's no closing ']', it might be a partial marker
        if "]" not in remainder:
            # Check if it looks like it could become [STEP:xxx] or [RESULT]
            if re.match(r'^\[(?:S(?:T(?:E(?:P(?::(?:\w*)?)?)?)?)?|R(?:E(?:S(?:U(?:L(?:T)?)?)?)?)?)?$', remainder):
                return last_bracket
    return len(buf)


def _try_parse_json(s: str):
    """Try to parse a JSON string, with fallback to find valid JSON boundaries."""
    s = s.strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # Try to find the outermost { ... } pair
    first_brace = s.find('{')
    if first_brace == -1:
        return None
    # Try from the end backwards
    for i in range(len(s) - 1, first_brace, -1):
        if s[i] == '}':
            try:
                return json.loads(s[first_brace:i + 1])
            except json.JSONDecodeError:
                continue
    return None


def _extract_json_fallback(text: str):
    """Try to extract JSON object from text that may have JSON concatenated after prose.

    Returns (text_before_json, parsed_json_or_None).
    If no JSON found, returns (original_text, None).
    """
    import re
    # Look for JSON start patterns that match our expected result schemas
    # Expected keys: "leads", "exposure", "orders", "factoryCount", "clients"
    json_start_patterns = [
        r'\{\s*"leads"',
        r'\{\s*"exposure"',
        r'\{\s*"orders"',
        r'\{\s*"factoryCount"',
        r'\{\s*"clients"',
        r'\{\s*"inquiries"',
    ]
    combined = '|'.join(json_start_patterns)
    match = re.search(combined, text)
    if not match:
        return (text, None)

    json_start = match.start()
    json_candidate = text[json_start:]
    parsed = _try_parse_json(json_candidate)
    if parsed is not None:
        return (text[:json_start], parsed)
    return (text, None)


async def _flush_text(text: str):
    """Parse text for [STEP:xxx] markers, yield step and delta events.

    Text is split into small chunks so the frontend can render progressively.
    Also detects inline JSON and emits it as a result event.
    """
    import re

    # Check if text contains inline JSON (LLM sometimes concatenates JSON without [RESULT])
    text_part, json_data = _extract_json_fallback(text)
    if json_data is not None:
        text = text_part

    _CN_PUNCT = re.compile(r'(?<=[，。、：；！？])')

    def _small_chunks(s: str):
        segments = _CN_PUNCT.split(s)
        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue
            while len(seg) > 20:
                yield seg[:20]
                seg = seg[20:]
            if seg:
                yield seg

    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue

        parts = re.split(r'\[STEP:(\w+)\]', line)
        for i, part in enumerate(parts):
            if i % 2 == 1:
                yield sse_event("step", {"label": part})
            else:
                for chunk in _small_chunks(part):
                    yield sse_event("delta", {"text": chunk})

    if json_data is not None:
        yield sse_event("result", json_data)



def build_system_prompt(
    skill_name: str, category: str, capacity: str, city: str,
    buyer_context: str, stats: dict
) -> str:
    """Build system prompt per skill, injecting real buyer data."""

    if skill_name == "buyer-match":
        return f"""你是 CNSubscribe 采购商智能匹配系统。你的任务是从真实采购商数据库中，为一家{city}的{category}工厂精准匹配海外采购商。

## 工厂画像
- 行业：{category}
- 月产能：{capacity}
- 所在地：{city}

## 采购商数据库概况
「{category}」行业在库采购商 {stats['total']} 个，已平台认证 {stats['verified']} 个，平均活跃度 {stats['avg_activity']}/100。
采购商来源国分布：{', '.join(f'{c}({n}个)' for c,n in stats['top_countries'])}

## 采购商样本（从数据库中抽取的高活跃采购商）
{buyer_context}

## 你的角色
你是匹配算法本身。你正在执行一个多阶段匹配流程：
1. 扫描数据库 → 按行业关键词初筛
2. 用工厂产能与采购商MOQ/年采购额做规模匹配
3. 用认证要求做资质交叉验证
4. 按采购商活跃度、采购频率排序
5. 输出匹配结果

## 输出格式（严格遵守）

每行格式：[STEP:label]一句话（label 只能是 search / match / result）
每个STEP后只跟一行简短文字（不超过40字），不要段落。共6-10个步骤。

模拟真实匹配过程，引用具体数字和采购商属性。示例风格：
[STEP:search]连接 CNSubscribe 采购商数据库...{stats['total']} 条记录
[STEP:search]按「{category}」产品标签过滤 → 命中 {stats['verified']} 个认证采购商
[STEP:search]提取近30天活跃采购商 → 活跃度≥75 共 XXX 个
[STEP:match]匹配产能{capacity}与采购商MOQ/年采购额...
[STEP:match]资质交叉：ISO9001 覆盖率 87% · IATF16949 需求 34%
[STEP:match]按采购频率排序：月采购 > 季度采购 > 年采购
[STEP:match]{city}→北美海运15天 · 欧洲22天 · 日韩5天
[STEP:result]匹配到高意向采购商 X 个，观望中 X 个
[STEP:result]主要来源：美国、德国、日本

所有STEP输出完毕后，必须换行输出标记 [RESULT]，然后紧跟一个合法 JSON。
[RESULT] 必须独占一行，前后各一个换行符。

JSON格式：
{{
  "leads": [
    {{"icon": "🇺🇸", "name": "采购商公司名", "action": "浏览了你的档案/发起询盘/收藏了你", "country": "国家", "industry": "该采购商采购的具体产品类目", "time": "刚刚/X分钟前/X天前", "type": "view/inquiry/fav"}},
    // 4-6 条
  ],
  "clients": [
    {{"flag": "🇺🇸", "name": "采购商公司名", "detail": "已询盘X次 · 月采购$XXK-XXK", "intent": "high/mid"}},
    // 6-8 条
  ]
}}

## 真实感规则（必须遵守）
1. 所有公司名必须从上方采购商样本数据中选取，不要编造公司名。
2. leads[].industry 必须来自采购商样本的 products 字段（如"薄壁注塑件""热流道模具""数控车削件"），是具体产品名，不是泛泛的行业名。
3. clients[].detail 的月采购金额要从采购商的 annualProcurement 换算（年÷12），如年$1M-5M → 月采购$83K-417K。
4. leads[].time 要合理分布：有"刚刚""3分钟前""1小时前""3天前"等不同时间。
5. intent 判断依据：活跃度≥80且已认证→high，其余→mid。
6. 国旗emoji必须与国家匹配。"""

    elif skill_name == "market-estimate":
        return f"""你是 CNSubscribe 市场曝光预估模型。基于采购商数据库的真实数据，为工厂预测入驻后的曝光量和询盘量。

## 工厂画像
- 行业：{category}
- 月产能：{capacity}
- 所在地：{city}

## 采购商数据库实况
「{category}」行业在库采购商 {stats['total']} 个，已认证 {stats['verified']} 个，平均活跃度 {stats['avg_activity']}/100。
来源国：{', '.join(f'{c}({n}个)' for c,n in stats['top_countries'])}

## 高活跃采购商样本
{buyer_context}

## 你的角色
你是曝光预估算法。你正在执行：
1. 从行业采购商基数计算月曝光基准
2. 按工厂产能等级调整系数（产能越大，匹配到的采购商越多）
3. 按城市物流优势加成
4. 用行业平均转化率算出询盘数
5. 从采购商采购周期推算首单周期

## 输出格式（严格遵守）

每行格式：[STEP:label]一句话（label 只能是 eval / result）
每个STEP后只跟一行简短文字（不超过40字），不要段落。共6-10个步骤。

产能档位对应的曝光基数参考：
- 10万以下/月 → 基准800-1500
- 10-50万/月 → 基准1500-3000
- 50-100万/月 → 基准3000-5000
- 100-500万/月 → 基准5000-10000
- 500万以上/月 → 基准10000-20000

询盘转化率范围：1.5%-3.5%（认证齐全、产能匹配的工厂转化率更高）

示例风格：
[STEP:eval]加载「{category}」采购商活跃数据，在库 {stats['total']} 个
[STEP:eval]近30天活跃采购商 XXX 个，月均浏览供应商 4.2 次
[STEP:eval]产能{capacity}匹配采购商MOQ范围 → 覆盖率 XX%
[STEP:eval]{city}出口物流评分：北美 A · 欧洲 B+ · 日韩 A+
[STEP:eval]综合曝光预估：XXXX 次/月
[STEP:eval]转化率模型：{category}行业均值 X.X% → 询盘 XX 条/月
[STEP:eval]首单周期：基于采购商采购频率加权 → XX 天
[STEP:result]{city}「{category}」月曝光 XXXX · 月询盘 XX
[STEP:result]主力采购国：美国、德国、日本

所有STEP输出完毕后，必须换行输出标记 [RESULT]，然后紧跟一个合法 JSON。
[RESULT] 必须独占一行，前后各一个换行符。

JSON格式：
{{
  "exposure": 数字,
  "inquiries": 数字,
  "cycle": "XX天",
  "topBuyers": ["国家1", "国家2", "国家3"],
  "avgOrder": "$XXK-XXK"
}}

## 真实感规则
1. 数字必须与产能档位匹配，不要所有产能都给相近数字。
2. topBuyers 从采购商数据库来源国 top3 中选取。
3. avgOrder 参考采购商样本的 annualProcurement 字段÷采购频率。
4. cycle 参考采购商 lastActiveDaysAgo 和 procurementFreq 加权计算。"""

    elif skill_name == "order-forecast":
        return f"""你是 CNSubscribe 订单预测模型。基于采购商行为数据和行业转化漏斗，预测工厂入驻后的订单时间线。

## 工厂画像
- 行业：{category}
- 月产能：{capacity}
- 所在地：{city}

## 采购商数据库
「{category}」行业采购商 {stats['total']} 个，已认证 {stats['verified']} 个。
来源国：{', '.join(f'{c}({n}个)' for c,n in stats['top_countries'])}

## 高匹配采购商样本
{buyer_context}

## 你的角色
你是订单预测算法。你正在基于历史转化数据构建预测漏斗：
1. 匹配到的采购商 → 发起询盘 → 索要报价 → 样品单 → 试单 → 返单
2. 每个阶段的转化率基于采购商活跃度和采购频率
3. 时间线基于行业平均成单周期

## 输出格式（严格遵守）

每行格式：[STEP:label]一句话（label 只能是 predict / result）
每个STEP后只跟一行简短文字（不超过40字），不要段落。共6-10个步骤。

示例风格：
[STEP:predict]构建{category}行业询盘→成单转化漏斗
[STEP:predict]匹配采购商 XX → 询盘 XX → 报价 XX → 样品 X → 成单 X
[STEP:predict]首单预测：XXX公司（美国） · 采购频率月度 · $X.XK 样品单
[STEP:predict]第4周 XXX公司 试单 → {category}具体产品 × 数量
[STEP:predict]{city}出口报关+海运周期 → 北美15天/欧洲22天
[STEP:predict]第8周起进入稳定返单阶段
[STEP:result]预计 M3 起月均订单 $XXK
[STEP:result]长期合作伙伴：XXX公司 框架协议

所有STEP输出完毕后，必须换行输出标记 [RESULT]，然后紧跟一个合法 JSON。
[RESULT] 必须独占一行，前后各一个换行符。

JSON格式：
{{
  "orders": [
    {{"icon": "📦", "buyer": "采购商公司名", "type": "样品单/试单/小批量/返单", "detail": "具体产品名 × 数量", "amount": "$X.XK", "week": "第X周"}},
    // 4-6 条，时间从第2周到第12周，金额递增
  ],
  "revenue": [
    {{"month": "M1", "value": 0}},
    {{"month": "M2", "value": X.X}},
    {{"month": "M3", "value": X.X}},
    {{"month": "M4", "value": XX}},
    {{"month": "M5", "value": XX}},
    {{"month": "M6", "value": XX}},
    {{"month": "M7", "value": XX}},
    {{"month": "M8", "value": XX}}
  ],
  "partnership": {{
    "buyer": "采购商公司名",
    "terms": "框架协议 $XXK/月"
  }}
}}

## 真实感规则（必须遵守）
1. orders[].buyer 和 partnership.buyer 必须从上方采购商样本中选取真实公司名。
2. orders[].detail 必须是采购商 products 字段中的具体产品名（如"薄壁注塑件""热流道模具""数控车削件"），不要用泛泛的行业名。
3. orders[].amount 要符合采购商规模：小型采购商 $0.5K-5K 样品单，大型 $5K-50K。
4. revenue 的 value 单位是千美元(K)，从0逐月增长，增速与产能{capacity}匹配。
5. 产能越大，revenue 增长越快、金额越高。10万以下/月的工厂 M8 约 $30-60K，500万以上/月的工厂 M8 可达 $500K+。"""

    elif skill_name == "competition":
        return f"""你是 CNSubscribe 竞争格局分析模型。基于采购商需求侧数据，分析工厂在目标市场的竞争定位。

注意：你分析的是「采购商对供应商的需求格局」，不是分析工厂之间的竞争。核心视角是：这些采购商在找什么样的供应商？你的工厂在采购商眼中处于什么位置？

## 工厂画像
- 行业：{category}
- 月产能：{capacity}
- 所在地：{city}

## 采购商数据库
「{category}」行业采购商 {stats['total']} 个，已认证 {stats['verified']} 个。
来源国：{', '.join(f'{c}({n}个)' for c,n in stats['top_countries'])}

## 你的角色
你是竞争分析算法。你从采购商需求侧分析：
1. 采购商在{city}地区寻找了多少家{category}供应商（即你的竞争对手数量）
2. 采购商的报价接受区间（FOB价格）
3. 采购商对供应商的认证要求分布
4. 该工厂在采购商筛选漏斗中的排名位置
5. 差异化建议

## 输出格式（严格遵守）

每行格式：[STEP:label]一句话（label 只能是 analyze / result）
每个STEP后只跟一行简短文字（不超过40字），不要段落。共6-10个步骤。

示例风格：
[STEP:analyze]扫描采购商询盘记录，提取{city}地区供应商数据
[STEP:analyze]近90天{city}「{category}」被询价供应商 XX 家
[STEP:analyze]采购商报价接受区间：FOB $X.X-$XX.X/件
[STEP:analyze]采购商认证要求：ISO9001 XX% · IATF16949 XX% · CE XX%
[STEP:analyze]产能{capacity}在被询供应商中排名前 XX%
[STEP:analyze]月采购频率采购商偏好响应速度 ≤48h 的供应商
[STEP:result]{city}「{category}」供应商竞争度XX，你排名前XX%
[STEP:result]建议：突出XX优势，优先对接XX地区采购商

所有STEP输出完毕后，必须换行输出标记 [RESULT]，然后紧跟一个合法 JSON。
[RESULT] 必须独占一行，前后各一个换行符。

JSON格式：
{{
  "factoryCount": 数字（被采购商询价的{city}同行业供应商数量）,
  "competitionLevel": "低/中等/激烈",
  "priceRange": "$X.X-$XX.X/件",
  "advantage": "基于采购商需求分析出的你的优势",
  "suggestion": "具体可执行的建议"
}}

## 真实感规则
1. factoryCount 是采购商询过价的{city}{category}供应商数，不是{city}所有工厂数。通常 30-150 之间。
2. priceRange 是采购商接受的FOB价格区间，要符合{category}行业特征。
3. advantage 要具体，如"产能覆盖大型采购商MOQ需求""认证齐全覆盖欧美市场"。
4. suggestion 要可执行，如"优先对接MOQ 5万+的美国采购商，响应时间控制在24h内"。"""

    return ""


# ---------------------------------------------------------------------------
# Skill endpoints
# ---------------------------------------------------------------------------
@app.post("/api/skill/buyer-match")
async def skill_buyer_match(request: Request):
    body = await request.json()
    category = body.get("category", "精密加工")
    capacity = body.get("capacity", "50-100万/月")
    city = body.get("city", "东莞")
    return StreamingResponse(
        skill_stream("buyer-match", category, capacity, city),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/skill/market-estimate")
async def skill_market_estimate(request: Request):
    body = await request.json()
    category = body.get("category", "精密加工")
    capacity = body.get("capacity", "50-100万/月")
    city = body.get("city", "东莞")
    return StreamingResponse(
        skill_stream("market-estimate", category, capacity, city),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/skill/order-forecast")
async def skill_order_forecast(request: Request):
    body = await request.json()
    category = body.get("category", "精密加工")
    capacity = body.get("capacity", "50-100万/月")
    city = body.get("city", "东莞")
    return StreamingResponse(
        skill_stream("order-forecast", category, capacity, city),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/skill/competition")
async def skill_competition(request: Request):
    body = await request.json()
    category = body.get("category", "精密加工")
    capacity = body.get("capacity", "50-100万/月")
    city = body.get("city", "东莞")
    return StreamingResponse(
        skill_stream("competition", category, capacity, city),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Registration API (preserved from original)
# ---------------------------------------------------------------------------
REGISTRATIONS: list[dict] = []
EVENTS: list[dict] = []

@app.post("/api/invite/register")
async def register(request: Request):
    body = await request.json()
    REGISTRATIONS.append(body)
    return JSONResponse({"success": True, "message": "注册成功"})


@app.post("/api/track")
async def track_event(request: Request):
    try:
        body = await request.json()
        EVENTS.append(body)
        logger.info(f"Track: {body.get('event', '?')} {body.get('data', {})}")
    except Exception:
        pass
    return JSONResponse({"ok": True})


@app.get("/api/admin/stats")
async def admin_stats():
    # Aggregate event counts
    event_counts = {}
    for evt in EVENTS:
        e = evt.get("event", "unknown")
        event_counts[e] = event_counts.get(e, 0) + 1
    return JSONResponse({
        "total_registrations": len(REGISTRATIONS),
        "registrations": REGISTRATIONS[-20:],
        "total_events": len(EVENTS),
        "event_counts": event_counts,
        "funnel": {
            "page_view": event_counts.get("page_view", 0),
            "form_submit": event_counts.get("form_submit", 0),
            "skills_complete": event_counts.get("skills_complete", 0),
            "cta_submit": event_counts.get("cta_submit", 0),
        }
    })


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    return JSONResponse({
        "status": "ok",
        "industries": list(BUYER_DB.keys()),
        "total_buyers": sum(len(v) for v in BUYER_DB.values()),
        "high_potential_industries": list(HIGH_POTENTIAL_DB.keys()),
        "total_high_potential": sum(len(v) for v in HIGH_POTENTIAL_DB.values()),
        "models": [m[2] for m in LLM_FALLBACK_MODELS],
        "model_failures": {k: int(time.time() - v) for k, v in _model_failures.items()},
    })


@app.post("/api/high-potential")
async def get_high_potential(request: Request):
    """Return high-potential buyers for a given industry, with pagination."""
    body = await request.json()
    category = body.get("category", "注塑模具")
    page = body.get("page", 1)
    page_size = body.get("pageSize", 20)
    min_score = body.get("minScore", 2)

    pool = HIGH_POTENTIAL_DB.get(category, [])
    if not pool:
        # fuzzy match
        for key, buyers in HIGH_POTENTIAL_DB.items():
            if category in key or key in category:
                pool = buyers
                break

    # Filter by minimum score
    filtered = [b for b in pool if b.get("potentialScore", 0) >= min_score]
    # Sort by score desc, then activity desc
    filtered.sort(key=lambda b: (-b.get("potentialScore", 0), -b.get("activityScore", 0)))

    total = len(filtered)
    start = (page - 1) * page_size
    end = start + page_size
    page_data = filtered[start:end]

    # Score distribution
    score_dist = {}
    for b in pool:
        s = b.get("potentialScore", 0)
        score_dist[s] = score_dist.get(s, 0) + 1

    # Country distribution for filtered
    country_dist = {}
    for b in filtered:
        c = b.get("country", "未知")
        country_dist[c] = country_dist.get(c, 0) + 1
    top_countries = sorted(country_dist.items(), key=lambda x: -x[1])[:8]

    return JSONResponse({
        "total": total,
        "totalAll": len(pool),
        "page": page,
        "pageSize": page_size,
        "scoreDist": score_dist,
        "topCountries": top_countries,
        "buyers": [{
            "id": b.get("id", ""),
            "name": b.get("name", ""),
            "country": b.get("country", ""),
            "flag": b.get("flag", ""),
            "city": b.get("city", ""),
            "scale": b.get("scale", ""),
            "industry": b.get("industry", ""),
            "annualProcurement": b.get("annualProcurement", ""),
            "procurementFreq": b.get("procurementFreq", ""),
            "products": b.get("products", [])[:3],
            "activityScore": b.get("activityScore", 0),
            "contactCount": b.get("contactCount", 0),
            "verified": b.get("verified", False),
            "potentialScore": b.get("potentialScore", 0),
            "potentialReasons": b.get("potentialReasons", []),
            "paymentTerms": b.get("paymentTerms", ""),
        } for b in page_data]
    })


# ---------------------------------------------------------------------------
# Serve frontend (index.html and static files)
# ---------------------------------------------------------------------------
@app.api_route("/", methods=["GET", "HEAD"])
async def serve_index():
    index_path = BASE_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(index_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>CNSubscribe</h1><p>index.html not found</p>", status_code=404)


# Serve any other static files from output dir
@app.api_route("/{path:path}", methods=["GET", "HEAD"])
async def serve_static(path: str):
    file_path = BASE_DIR / path
    if file_path.is_file():
        content_type = "text/html"
        if path.endswith(".js"):
            content_type = "application/javascript"
        elif path.endswith(".css"):
            content_type = "text/css"
        elif path.endswith(".json"):
            content_type = "application/json"
        elif path.endswith(".png"):
            content_type = "image/png"
        elif path.endswith(".jpg") or path.endswith(".jpeg"):
            content_type = "image/jpeg"
        elif path.endswith(".svg"):
            content_type = "image/svg+xml"
        from starlette.responses import Response
        return Response(
            content=file_path.read_bytes(),
            media_type=content_type,
        )
    return JSONResponse({"error": "not found"}, status_code=404)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
