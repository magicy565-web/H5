"""
CNSubscribe Multi-Skill SSE Backend
FastAPI server with 4 parallel LLM-powered Skill endpoints.
"""

import os
import json
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

BASE_DIR = Path(__file__).resolve().parent.parent  # /workspace/output
DB_DIR = BASE_DIR / "db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cnsubscribe")

# ---------------------------------------------------------------------------
# LLM client
# ---------------------------------------------------------------------------
llm_client = AsyncOpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

# ---------------------------------------------------------------------------
# Buyer database
# ---------------------------------------------------------------------------
BUYER_DB: dict[str, list] = {}

def load_buyer_db():
    """Load all industry JSON files into memory."""
    for f in DB_DIR.glob("*.json"):
        if f.name.startswith("_"):
            continue
        industry = f.stem
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
    logger.info(f"Loaded buyer DB: {len(BUYER_DB)} industries, {total} buyers")

# ---------------------------------------------------------------------------
# Helper: sample buyers for context
# ---------------------------------------------------------------------------
def sample_buyers(category: str, capacity: str, city: str, n: int = 30) -> list[dict]:
    """Sample relevant buyers to inject into LLM context."""
    pool = BUYER_DB.get(category, [])
    if not pool:
        # fuzzy match
        for key, buyers in BUYER_DB.items():
            if category in key or key in category:
                pool = buyers
                break
    if not pool:
        pool = list(BUYER_DB.values())[0] if BUYER_DB else []

    # Prefer high activity, verified buyers
    scored = sorted(pool, key=lambda b: (
        -b.get("activityScore", 0),
        -int(b.get("verified", False)),
        b.get("lastActiveDaysAgo", 999),
    ))
    top = scored[:min(n * 3, len(scored))]
    sample = random.sample(top, min(n, len(top)))
    return sample


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

        stream = await llm_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            stream=True,
            temperature=0.7,
            max_tokens=4096,
        )

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
                try:
                    result_data = json.loads(json_str)
                    yield sse_event("result", result_data)
                except json.JSONDecodeError:
                    # Try to extract valid JSON by finding last } or ]
                    for end_char in ['}', ']']:
                        last = json_str.rfind(end_char)
                        if last != -1:
                            try:
                                result_data = json.loads(json_str[:last + 1])
                                yield sse_event("result", result_data)
                                break
                            except json.JSONDecodeError:
                                continue
                    else:
                        logger.warning(f"Failed to parse result JSON for {skill_name}: {json_str[:300]}")
        else:
            # No [RESULT] found, flush remaining buffer
            if buffer.strip():
                async for evt in _flush_text(buffer):
                    yield evt

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


async def _flush_text(text: str):
    """Parse text for [STEP:xxx] markers, yield step and delta events.

    Text is split into small chunks so the frontend can render progressively:
    1. Split by newlines
    2. Emit step events for [STEP:xxx] markers
    3. Split remaining text by Chinese punctuation or ~20 char chunks
    """
    import re

    # Chinese punctuation that makes good split points
    _CN_PUNCT = re.compile(r'(?<=[，。、：；！？])')

    def _small_chunks(s: str):
        """Yield small segments from *s*."""
        # First split on Chinese punctuation
        segments = _CN_PUNCT.split(s)
        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue
            # If still long, chop into ~20-char pieces
            while len(seg) > 20:
                yield seg[:20]
                seg = seg[20:]
            if seg:
                yield seg

    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue

        # A single line may contain multiple [STEP:xxx] markers
        parts = re.split(r'\[STEP:(\w+)\]', line)
        # parts = [before, label1, after1, label2, after2, ...]
        for i, part in enumerate(parts):
            if i % 2 == 1:
                # This is a step label
                yield sse_event("step", {"label": part})
            else:
                # Text fragment – break into small delta events
                for chunk in _small_chunks(part):
                    yield sse_event("delta", {"text": chunk})


def build_system_prompt(
    skill_name: str, category: str, capacity: str, city: str,
    buyer_context: str, stats: dict
) -> str:
    """Build system prompt per skill, injecting real buyer data."""

    if skill_name == "buyer-match":
        return f"""你是 CNSubscribe 采购商匹配引擎。基于真实采购商数据库为工厂匹配海外采购商。

数据库：「{category}」行业 {stats['total']} 个采购商，已认证 {stats['verified']} 个。
来源国：{', '.join(f'{c}({n}个)' for c,n in stats['top_countries'])}

采购商数据：
{buyer_context}

工厂：行业={category}, 月产能={capacity}, 城市={city}

## 输出格式（严格遵守）

每个步骤只输出一行简短文字（不超过40个中文字符），不要输出段落或多行解释。
总共输出6-10个步骤。
每行格式：[STEP:label]一句话描述（label 只能是 search / match / result）

示例（严格模仿此风格，每个STEP后只跟一行短文字）：
[STEP:search]连接采购商数据库...在库 {stats['total']} 个认证采购商
[STEP:search]加载行业索引：制造业 → {category} → 子类目展开
[STEP:search]筛选「{category}」相关类目 → 命中 156 个采购商
[STEP:match]交叉验证资质：已认证 137 个 · 活跃度≥80% 114 个
[STEP:match]按产能「{capacity}」过滤 → 89 个符合要求
[STEP:match]地域匹配：{city} → 美国航线覆盖 ✓
[STEP:result]匹配采购商主要来自：美国、德国、日本
[STEP:result]高意向 5 个，观望中 3 个

最后输出 [RESULT] 紧跟一个合法 JSON（公司名从采购商数据中选取）：
{{
  "leads": [
    {{"icon": "🇺🇸", "name": "公司名", "action": "浏览了你的档案/发起询盘/收藏了你", "country": "国家", "industry": "行业", "time": "刚刚/X分钟前", "type": "view/inquiry/fav"}},
    // 4-6 条
  ],
  "clients": [
    {{"flag": "🇺🇸", "name": "公司名", "detail": "已询盘X次 · 月采购$XXK-XXK", "intent": "high/mid"}},
    // 6-8 条
  ]
}}

重要：国旗emoji与国家匹配，数字基于实际数据。"""

    elif skill_name == "market-estimate":
        return f"""你是 CNSubscribe 市场分析引擎。基于行业数据预估工厂的曝光量和询盘量。

数据：「{category}」行业采购商 {stats['total']} 个，已认证 {stats['verified']} 个，平均活跃度 {stats['avg_activity']}。
来源国：{', '.join(f'{c}({n}个)' for c,n in stats['top_countries'])}

高活跃采购商样本：
{buyer_context}

工厂：行业={category}, 月产能={capacity}, 城市={city}

## 输出格式（严格遵守）

每个步骤只输出一行简短文字（不超过40个中文字符），不要输出段落或多行解释。
总共输出6-10个步骤。
每行格式：[STEP:label]一句话描述（label 只能是 eval / result）

示例（严格模仿此风格，每个STEP后只跟一行短文字）：
[STEP:eval]加载「{category}」行业基准数据
[STEP:eval]行业月均曝光基准：2,340 次/月
[STEP:eval]产能系数：{capacity} → ×1.4 = 3,276 次/月
[STEP:eval]询盘转化模型：3,276 × 2.1% = 69 条/月
[STEP:eval]首单周期：行业均值 42 天
[STEP:eval]平均订单金额：$8K-$25K
[STEP:result]月曝光 3,276，月询盘 69，首单 42 天
[STEP:result]主力采购国：美国、德国、日本

最后输出 [RESULT] 紧跟 JSON：
{{
  "exposure": 数字,
  "inquiries": 数字,
  "cycle": "XX天",
  "topBuyers": ["国家1", "国家2", "国家3"],
  "avgOrder": "$XXK-XXK"
}}

重要：数据基于行业规模合理计算。"""

    elif skill_name == "order-forecast":
        return f"""你是 CNSubscribe 订单预测引擎。基于行业转化数据预测订单时间线。

数据：「{category}」行业采购商 {stats['total']} 个，已认证 {stats['verified']} 个。
来源国：{', '.join(f'{c}({n}个)' for c,n in stats['top_countries'])}

高匹配采购商：
{buyer_context}

工厂：行业={category}, 月产能={capacity}, 城市={city}

## 输出格式（严格遵守）

每个步骤只输出一行简短文字（不超过40个中文字符），不要输出段落或多行解释。
总共输出6-10个步骤。
每行格式：[STEP:label]一句话描述（label 只能是 predict / result）

示例（严格模仿此风格，每个STEP后只跟一行短文字）：
[STEP:predict]构建询盘→成单转化漏斗模型
[STEP:predict]询盘 89 → 报价 52 → 样品 18 → 成单 7
[STEP:predict]样品单周期：平均 21 天转化
[STEP:predict]首张样品单 → GlobalTech $450（第3周）
[STEP:predict]试单预测 → MegaCorp $2.8K（第6周）
[STEP:predict]第8周进入稳定返单阶段
[STEP:result]预计 M4 起稳定 OEM 订单 $15K/月
[STEP:result]长期合作伙伴：MegaCorp 框架协议

最后输出 [RESULT] 紧跟 JSON（公司名从数据中选取）：
{{
  "orders": [
    {{"icon": "📦", "buyer": "公司名", "type": "样品单/试单/小批量/返单", "detail": "产品描述 × 数量", "amount": "$XXX", "week": "第X周"}},
    // 4-6 条
  ],
  "revenue": [
    {{"month": "M1", "value": 0}},
    {{"month": "M2", "value": X.X}},
    {{"month": "M3", "value": X.X}},
    {{"month": "M4", "value": X.X}},
    {{"month": "M5", "value": XX}},
    {{"month": "M6", "value": XX}},
    {{"month": "M7", "value": XX}},
    {{"month": "M8", "value": XX}}
  ],
  "partnership": {{
    "buyer": "公司名",
    "terms": "框架协议 $XXK/月"
  }}
}}

重要：revenue 的 value 单位是千美元(K)，从0逐月增长。"""

    elif skill_name == "competition":
        return f"""你是 CNSubscribe 竞争分析引擎。分析目标城市同品类工厂竞争格局。

数据：「{category}」行业采购商 {stats['total']} 个，已认证 {stats['verified']} 个。
来源国：{', '.join(f'{c}({n}个)' for c,n in stats['top_countries'])}

工厂：行业={category}, 月产能={capacity}, 城市={city}

## 输出格式（严格遵守）

每个步骤只输出一行简短文字（不超过40个中文字符），不要输出段落或多行解释。
总共输出6-10个步骤。
每行格式：[STEP:label]一句话描述（label 只能是 analyze / result）

示例（严格模仿此风格，每个STEP后只跟一行短文字）：
[STEP:analyze]采集{city}同品类工厂数据...
[STEP:analyze]发现 87 家「{category}」工厂
[STEP:analyze]FOB 均价区间：$2.5-$18.0/件
[STEP:analyze]产能规模对比：你的工厂排名前 35%
[STEP:analyze]认证覆盖率：ISO 72% · CE 45% · UL 18%
[STEP:analyze]出口目的地重合度：68%
[STEP:result]竞争度中等，建议优先对接欧美市场
[STEP:result]差异化优势：产能规模 + 认证齐全

最后输出 [RESULT] 紧跟 JSON：
{{
  "factoryCount": 数字,
  "competitionLevel": "低/中等/激烈",
  "priceRange": "$X.X-$XX.X/件",
  "advantage": "优势描述",
  "suggestion": "建议描述"
}}

重要：数据基于{city}真实产业特征合理分析。"""

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

@app.post("/api/invite/register")
async def register(request: Request):
    body = await request.json()
    REGISTRATIONS.append(body)
    return JSONResponse({"success": True, "message": "注册成功"})


@app.get("/api/admin/stats")
async def admin_stats():
    return JSONResponse({
        "total_registrations": len(REGISTRATIONS),
        "registrations": REGISTRATIONS[-20:],
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
        "model": LLM_MODEL,
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
