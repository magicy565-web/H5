# CNSubscribe 多 Skill 并行分析 SSE API 接口规范

## 架构概览

前端并行调用 4 个独立 Skill API，每个 Skill 独立调用 LLM 做深度分析。

```
┌─────────┐     ┌──────────────────────┐
│         │────▶│ POST /api/skill/buyer-match      │ → LLM 采购商匹配
│         │────▶│ POST /api/skill/market-estimate   │ → LLM 市场预估
│  前端   │────▶│ POST /api/skill/order-forecast    │ → LLM 订单预测
│         │────▶│ POST /api/skill/competition       │ → LLM 竞争分析
└─────────┘     └──────────────────────┘
      并行发起，各自独立 SSE 流式返回
```

---

## 通用请求格式

所有 Skill 使用相同的请求格式：

```
POST /api/skill/{skill-name}
Content-Type: application/json
Accept: text/event-stream
```

```json
{
  "category": "精密加工",
  "capacity": "50-100万/月",
  "city": "东莞"
}
```

---

## 通用 SSE 事件格式

所有 Skill 返回相同的 SSE 事件类型：

### `step` - 新推理行
```
event: step
data: {"label": "search"}
```
label 取值: `search`(检索) / `match`(匹配) / `eval`(计算) / `result`(结论) / `predict`(预测) / `analyze`(分析)

### `delta` - 文本片段
```
event: delta
data: {"text": "连接采购商数据库..."}
```

### `result` - 结构化数据
```
event: result
data: { ... skill-specific JSON ... }
```

### `done` - 完成
```
event: done
data: {}
```

### `error` - 错误
```
event: error
data: {"message": "分析失败"}
```

---

## Skill 1: 采购商匹配

`POST /api/skill/buyer-match`

### 推理过程示例
```
event: step
data: {"label": "search"}
event: delta
data: {"text": "连接采购商数据库...在库 527 个认证采购商"}

event: step
data: {"label": "search"}
event: delta
data: {"text": "加载行业索引：制造业 → 精密加工 → 子类目展开..."}

event: step
data: {"label": "search"}
event: delta
data: {"text": "筛选「精密加工」相关类目 → 命中 156 个采购商"}

event: step
data: {"label": "match"}
event: delta
data: {"text": "交叉验证采购商资质：已认证 137 个 · 活跃度≥80% 114 个"}

event: step
data: {"label": "match"}
event: delta
data: {"text": "按产能「50-100万/月」过滤 → 111 个符合产能要求"}

event: step
data: {"label": "match"}
event: delta
data: {"text": "分析地域匹配度：东莞 → 美国航线覆盖 ✓"}

event: step
data: {"label": "match"}
event: delta
data: {"text": "分析地域匹配度：东莞 → 德国航线覆盖 ✓ 物流时效 12-18天"}

event: step
data: {"label": "result"}
event: delta
data: {"text": "匹配采购商主要来自：美国、德国、日本"}

event: step
data: {"label": "result"}
event: delta
data: {"text": "高意向采购商 4 个，观望中 2 个"}
```

### result 数据
```json
event: result
data: {
  "leads": [
    {"icon": "🇺🇸", "name": "GlobalTech Sourcing", "action": "浏览了你的档案", "country": "美国", "industry": "工业零部件", "time": "刚刚", "type": "view"},
    {"icon": "🇩🇪", "name": "Müller & Co. GmbH", "action": "发起询盘", "country": "德国", "industry": "汽车配件", "time": "2分钟前", "type": "inquiry"},
    {"icon": "🇯🇵", "name": "Tanaka Trading Co.", "action": "收藏了你", "country": "日本", "industry": "精密零件", "time": "5分钟前", "type": "fav"},
    {"icon": "🇺🇸", "name": "SecondSource LLC", "action": "浏览了你的档案", "country": "美国", "industry": "工业零部件", "time": "8分钟前", "type": "view"}
  ],
  "clients": [
    {"flag": "🇺🇸", "name": "GlobalTech Sourcing", "detail": "已询盘2次 · 月采购$15K-50K", "intent": "high"},
    {"flag": "🇩🇪", "name": "Oranje Supply BV", "detail": "浏览3次 · 未询盘", "intent": "mid"},
    {"flag": "🇩🇪", "name": "Müller & Co. GmbH", "detail": "已询盘1次 · 月采购$20K-80K", "intent": "high"},
    {"flag": "🇯🇵", "name": "Pacific Trade Group", "detail": "浏览1次 · 未询盘", "intent": "mid"},
    {"flag": "🇯🇵", "name": "Tanaka Trading Co.", "detail": "已询盘3次 · 要求样品", "intent": "high"},
    {"flag": "🇺🇸", "name": "SecondSource LLC", "detail": "已询盘2次 · 要求样品", "intent": "high"}
  ]
}
```

**leads[].type**: `view` / `inquiry` / `fav`
**clients[].intent**: `high` / `mid`

---

## Skill 2: 市场预估

`POST /api/skill/market-estimate`

### 推理过程示例
```
event: step
data: {"label": "eval"}
event: delta
data: {"text": "计算基础曝光分析：320 × 1.3"}

event: step
data: {"label": "eval"}
event: delta
data: {"text": "行业基准对比：精密加工 平均曝光 320/月"}

event: step
data: {"label": "eval"}
event: delta
data: {"text": "产能加权：50-100万/月 → 系数 1.3 = 416 次/月"}

event: step
data: {"label": "eval"}
event: delta
data: {"text": "询盘转化模型：曝光 416 × 转化率 5.6% = 23 条/月"}

event: step
data: {"label": "result"}
event: delta
data: {"text": "综合预估：月曝光 416，月询盘 23，首单周期 14天"}
```

### result 数据
```json
event: result
data: {
  "exposure": 416,
  "inquiries": 23,
  "cycle": "14天",
  "topBuyers": ["美国", "德国", "日本"],
  "avgOrder": "$12K-45K"
}
```

---

## Skill 3: 订单预测

`POST /api/skill/order-forecast`

### 推理过程示例
```
event: step
data: {"label": "predict"}
event: delta
data: {"text": "预测样品单周期：基于14天转化模型"}

event: step
data: {"label": "predict"}
event: delta
data: {"text": "模拟订单漏斗：询盘 23 → 报价 16 → 样品 7"}

event: step
data: {"label": "predict"}
event: delta
data: {"text": "样品单转化 → $800 (GlobalTech Sourcing)"}

event: step
data: {"label": "predict"}
event: delta
data: {"text": "试单预测 → Müller & Co. GmbH $5.2K (第11周)"}

event: step
data: {"label": "result"}
event: delta
data: {"text": "18周后预计稳定 OEM 订单 $18K/月"}
```

### result 数据
```json
event: result
data: {
  "orders": [
    {"icon": "📦", "buyer": "GlobalTech Sourcing", "type": "样品单", "detail": "CNC铝合金零件 × 50pcs", "amount": "$800", "week": "第9周"},
    {"icon": "📋", "buyer": "Müller & Co. GmbH", "type": "试单", "detail": "注塑外壳 × 2,000pcs", "amount": "$5.2K", "week": "第11周"},
    {"icon": "📦", "buyer": "Tanaka Trading Co.", "type": "小批量", "detail": "PCB组装 × 500套", "amount": "$3.8K", "week": "第13周"},
    {"icon": "🔄", "buyer": "GlobalTech Sourcing", "type": "返单", "detail": "CNC铝合金零件 × 500pcs（加量）", "amount": "$7.5K", "week": "第16周"}
  ],
  "revenue": [
    {"month": "M1", "value": 0},
    {"month": "M2", "value": 0.8},
    {"month": "M3", "value": 3.2},
    {"month": "M4", "value": 5.8},
    {"month": "M5", "value": 12},
    {"month": "M6", "value": 18},
    {"month": "M7", "value": 25},
    {"month": "M8", "value": 32}
  ],
  "partnership": {
    "buyer": "GlobalTech",
    "terms": "框架协议 $18K/月"
  }
}
```

---

## Skill 4: 竞争分析

`POST /api/skill/competition`

### 推理过程示例
```
event: step
data: {"label": "analyze"}
event: delta
data: {"text": "东莞同品类工厂数据采集..."}

event: step
data: {"label": "analyze"}
event: delta
data: {"text": "发现 60 家同品类工厂"}

event: step
data: {"label": "analyze"}
event: delta
data: {"text": "分析 FOB 均价区间：$2.6-$11.5/件"}

event: step
data: {"label": "analyze"}
event: delta
data: {"text": "产能规模对比：你的工厂排名前 25%"}

event: step
data: {"label": "result"}
event: delta
data: {"text": "竞争度中等，建议优先对接美国市场"}
```

### result 数据
```json
event: result
data: {
  "factoryCount": 60,
  "competitionLevel": "中等",
  "priceRange": "$2.6-$11.5/件",
  "advantage": "产能规模优势",
  "suggestion": "建议优先对接美国市场"
}
```

---

## 后端实现建议

### 每个 Skill 独立的 LLM System Prompt

**采购商匹配 Skill:**
```
你是 CNSubscribe 采购商匹配引擎。分析工厂信息，搜索匹配的海外采购商。

输出格式：
每个步骤用 [STEP:label] 开头 (label: search/match/result)
最后输出 [RESULT] + JSON

用户工厂：{category}, 月产能 {capacity}, 城市 {city}
```

**市场预估 Skill:**
```
你是 CNSubscribe 市场分析引擎。基于行业数据计算工厂入驻后的曝光量和询盘量。

输出格式：
每个步骤用 [STEP:label] 开头 (label: eval/result)
最后输出 [RESULT] + JSON

用户工厂：{category}, 月产能 {capacity}, 城市 {city}
```

**订单预测 Skill:**
```
你是 CNSubscribe 订单预测引擎。基于行业转化数据，预测从入驻到稳定接单的时间线。

输出格式：
每个步骤用 [STEP:label] 开头 (label: predict/result)
最后输出 [RESULT] + JSON

用户工厂：{category}, 月产能 {capacity}, 城市 {city}
```

**竞争分析 Skill:**
```
你是 CNSubscribe 竞争分析引擎。分析目标城市同品类工厂的竞争格局。

输出格式：
每个步骤用 [STEP:label] 开头 (label: analyze/result)
最后输出 [RESULT] + JSON

用户工厂：{category}, 月产能 {capacity}, 城市 {city}
```

### 后端解析逻辑（每个 Skill 相同）

```python
# 伪代码
async def skill_handler(request):
    params = await request.json()

    async def generate():
        async for chunk in call_llm(system_prompt, user_input):
            if chunk.startswith('[STEP:'):
                label = chunk.split(':')[1].rstrip(']')
                yield f"event: step\ndata: {json.dumps({'label': label})}\n\n"
            elif chunk.startswith('[RESULT]'):
                data = json.loads(chunk[8:])
                yield f"event: result\ndata: {json.dumps(data)}\n\n"
            else:
                yield f"event: delta\ndata: {json.dumps({'text': chunk})}\n\n"

        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
```

### CORS 配置

```
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: POST, OPTIONS
Access-Control-Allow-Headers: Content-Type
```

---

## 前端降级策略

每个 Skill 独立降级：
- 如果某个 Skill API 失败，该 Skill 自动切换到本地 MOCK
- 其他 Skill 不受影响，继续使用 API
- MOCK 模拟 25-40s 的打字机推理过程
- 用户体验完全一致

## 前端调用时序

```
t=0s    ┌─ POST /api/skill/buyer-match ──── SSE stream ────┐
        ├─ POST /api/skill/market-estimate ── SSE stream ──┤
        ├─ POST /api/skill/order-forecast ── SSE stream ───┤
        └─ POST /api/skill/competition ──── SSE stream ────┘
                                                            │
t=30-40s ◄──────── all skills done ─────────────────────────┘
        │
        ▼  折叠思考块 → 曝光卡 → 竞争洞察 → 时间线 → CTA
```
