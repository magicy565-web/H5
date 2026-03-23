"""
Intent Monitor Configuration — Multi-industry support

Usage:
    python3 -m monitor.intent_monitor --industry 注塑机
    python3 -m monitor.intent_monitor --industry 家纺
    python3 -m monitor.intent_monitor --industry 家具
    python3 -m monitor.intent_monitor --industry all    # run all industries
"""
import os

# --- LLM ---
LLM_API_KEY = os.getenv("DASHSCOPE_API_KEY", "sk-9cd6b877d45c4bb6a29925c2e1dab4b3")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen-plus")
LLM_FALLBACK_MODELS = ["qwen-plus", "qwen-turbo", "qwen-long", "qwen-max"]

# --- Paths ---
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent.parent  # /workspace/H5
DB_DIR = BASE_DIR / "db"
OUTPUT_DIR = BASE_DIR / "monitor" / "output"

# --- Apify ---
APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN", "")

# --- 微信推送 ---
# 企业微信群机器人 Webhook URL（在群设置 → 群机器人 → 添加 → 复制webhook地址）
WECOM_WEBHOOK_URL = os.getenv("WECOM_WEBHOOK_URL", "")

# Server酱推送 Key（https://sct.ftqq.com/ 注册获取）
SERVERCHAN_KEY = os.getenv("SERVERCHAN_KEY", "")

# --- Analysis ---
MIN_INTENT_SCORE = 3
LLM_BATCH_SIZE = 10

# --- Rate Limiting ---
REQUEST_DELAY_SECONDS = 2


# =====================================================================
#  行业配置 — 每个行业一个完整的 profile
# =====================================================================

INDUSTRY_PROFILES: dict[str, dict] = {

    # ─── 注塑机 ───────────────────────────────────────────────────
    "注塑机": {
        "name_en": "injection_molding_machine",
        "leads_file": "leads_注塑机.json",
        "llm_system_prompt": (
            "你是一位专业的注塑机外贸销售情报分析师。你的任务是分析从互联网上采集到的"
            "潜在买家信号，判断其购买注塑机（injection molding machine）的意向强度，"
            "并提取结构化信息。请严格按照要求返回 JSON。"
        ),
        "llm_spec_field_hint": "提及的机器规格或需求细节（吨位、型号、数量等）",
        "keywords_direct": [
            "buy injection molding machine",
            "injection molding machine RFQ",
            "need injection moulding machine",
            "wanted injection molding machine",
            "looking for injection molding machine",
            "used injection molding machine",
            "second hand injection moulding machine",
            "PET preform machine buyer",
            "injection molding machine supplier",
            "plastic injection machine purchase",
            "injection molding equipment buy",
            "求购注塑机",
            "采购注塑机",
        ],
        "keywords_indirect": [
            '"new plastic factory" Vietnam',
            '"new plastic factory" India',
            '"new plastic factory" Mexico',
            '"new plastic factory" Indonesia',
            '"injection molding factory setup"',
            '"factory manager" "injection molding"',
            '"plant manager" "plastics manufacturing"',
        ],
        "sources": {
            "google_search": {
                "enabled": True,
                "max_results_per_keyword": 5,
            },
            "go4worldbusiness": {
                "enabled": True,
                "max_pages": 3,
            },
            "reddit": {
                "enabled": True,
                "subreddits": ["InjectionMolding", "manufacturing", "Machinists", "plastics"],
            },
            "tradekey": {
                "enabled": True,
                "max_pages": 2,
            },
            "rss": {
                "enabled": True,
                "feeds": [
                    "https://www.plasticstoday.com/rss.xml",
                    "https://www.plasticsnews.com/rss/all",
                ],
            },
            "apify_google": {
                "enabled": True,
                "actor_id": "apify/google-search-scraper",
                "max_results_per_keyword": 10,
            },
            "apify_linkedin": {
                "enabled": True,
                "actor_id": "curious_coder/linkedin-post-search-scraper",
                "max_results": 50,
            },
            "apify_facebook": {
                "enabled": True,
                "actor_id": "apify/facebook-posts-scraper",
                "pages": [
                    "InjectionMoldingMachines",
                    "PlasticMachineryManufacturers",
                ],
                "max_results": 50,
            },
            "apify_alibaba": {
                "enabled": True,
                "actor_id": "epctex/alibaba-scraper",
                "max_results": 30,
            },
            "apify_b2b": {
                "enabled": True,
                "actor_id": "apify/web-scraper",
                "targets": [
                    {"url": "https://www.go4worldbusiness.com/buy-leads/injection-molding-machine.html", "name": "go4world"},
                    {"url": "https://www.tradekey.com/buyoffer/injection-molding-machine.htm", "name": "tradekey"},
                    {"url": "https://www.exportersindia.com/indian-buyers/injection-moulding-machine.htm", "name": "exportersindia"},
                ],
                "max_pages": 3,
            },
        },
    },

    # ─── 家纺 ─────────────────────────────────────────────────────
    "家纺": {
        "name_en": "home_textile",
        "leads_file": "leads_家纺.json",
        "llm_system_prompt": (
            "你是一位专业的家纺产品外贸销售情报分析师。你的任务是分析从互联网上采集到的"
            "潜在买家信号，判断其采购家纺产品（床上用品、窗帘、毛巾、地毯、靠垫等）的意向强度，"
            "并提取结构化信息。请严格按照要求返回 JSON。"
        ),
        "llm_spec_field_hint": "提及的产品规格或需求细节（品类、材质、数量、尺寸等）",
        "keywords_direct": [
            "buy bedding sets wholesale",
            "import bed sheets from China",
            "wholesale towels supplier",
            "bulk curtain fabric buy",
            "home textile buyer",
            "bedding supplier needed",
            "looking for cushion cover supplier",
            "wholesale blankets import",
            "cotton bed linen RFQ",
            "hotel linen supplier wanted",
            "terry towel wholesale buy",
            "carpet rug import from China",
            "duvet cover wholesale purchase",
            "pillow case bulk order",
            "采购家纺",
            "求购床上用品",
            "毛巾批发采购",
        ],
        "keywords_indirect": [
            '"new hotel opening" bedding',
            '"new hotel" "linen supplier"',
            '"home textile" import Vietnam',
            '"home textile" import India',
            '"home textile fair" buyer',
            '"interior design" "fabric supplier"',
            '"hospital linen" tender',
            '"army blanket" tender procurement',
        ],
        "sources": {
            "google_search": {
                "enabled": True,
                "max_results_per_keyword": 5,
            },
            "go4worldbusiness": {
                "enabled": True,
                "max_pages": 3,
            },
            "reddit": {
                "enabled": True,
                "subreddits": ["HomeDecorating", "InteriorDesign", "Hospitality", "Bedding"],
            },
            "tradekey": {
                "enabled": True,
                "max_pages": 2,
            },
            "rss": {
                "enabled": True,
                "feeds": [
                    "https://www.hometextilestoday.com/feed/",
                    "https://www.textileworld.com/feed/",
                ],
            },
            "apify_google": {
                "enabled": True,
                "actor_id": "apify/google-search-scraper",
                "max_results_per_keyword": 10,
            },
            "apify_linkedin": {
                "enabled": True,
                "actor_id": "curious_coder/linkedin-post-search-scraper",
                "max_results": 50,
            },
            "apify_facebook": {
                "enabled": True,
                "actor_id": "apify/facebook-posts-scraper",
                "pages": [
                    "HomeTextilesBuyers",
                    "BeddingWholesale",
                ],
                "max_results": 50,
            },
            "apify_alibaba": {
                "enabled": True,
                "actor_id": "epctex/alibaba-scraper",
                "max_results": 30,
            },
            "apify_b2b": {
                "enabled": True,
                "actor_id": "apify/web-scraper",
                "targets": [
                    {"url": "https://www.go4worldbusiness.com/buy-leads/bed-linen.html", "name": "go4world"},
                    {"url": "https://www.go4worldbusiness.com/buy-leads/home-textile.html", "name": "go4world"},
                    {"url": "https://www.tradekey.com/buyoffer/home-textile.htm", "name": "tradekey"},
                ],
                "max_pages": 3,
            },
        },
    },

    # ─── 家具 ─────────────────────────────────────────────────────
    "家具": {
        "name_en": "furniture",
        "leads_file": "leads_家具.json",
        "llm_system_prompt": (
            "你是一位专业的家具外贸销售情报分析师。你的任务是分析从互联网上采集到的"
            "潜在买家信号，判断其采购家具（沙发、餐桌椅、办公家具、户外家具、定制家具等）的意向强度，"
            "并提取结构化信息。请严格按照要求返回 JSON。"
        ),
        "llm_spec_field_hint": "提及的产品规格或需求细节（品类、材质、数量、风格等）",
        "keywords_direct": [
            "buy furniture wholesale China",
            "import sofa from China",
            "wholesale office furniture supplier",
            "furniture buyer looking for supplier",
            "bulk furniture purchase",
            "hotel furniture supplier needed",
            "restaurant furniture wholesale",
            "outdoor furniture import",
            "custom furniture manufacturer",
            "furniture RFQ",
            "OEM furniture order",
            "wooden furniture wholesale buy",
            "upholstered furniture import",
            "采购家具",
            "求购沙发",
            "家具批发采购",
        ],
        "keywords_indirect": [
            '"new hotel project" furniture',
            '"office renovation" "furniture supplier"',
            '"furniture tender" government',
            '"new restaurant" "furniture"',
            '"interior project" "furniture procurement"',
            '"real estate development" "furniture"',
            '"school furniture" tender procurement',
            '"hospital furniture" tender',
        ],
        "sources": {
            "google_search": {
                "enabled": True,
                "max_results_per_keyword": 5,
            },
            "go4worldbusiness": {
                "enabled": True,
                "max_pages": 3,
            },
            "reddit": {
                "enabled": True,
                "subreddits": ["furniture", "InteriorDesign", "HomeImprovement", "woodworking"],
            },
            "tradekey": {
                "enabled": True,
                "max_pages": 2,
            },
            "rss": {
                "enabled": True,
                "feeds": [
                    "https://www.furniturelightingdecor.com/rss.xml",
                    "https://www.furnituretoday.com/feed/",
                ],
            },
            "apify_google": {
                "enabled": True,
                "actor_id": "apify/google-search-scraper",
                "max_results_per_keyword": 10,
            },
            "apify_linkedin": {
                "enabled": True,
                "actor_id": "curious_coder/linkedin-post-search-scraper",
                "max_results": 50,
            },
            "apify_facebook": {
                "enabled": True,
                "actor_id": "apify/facebook-posts-scraper",
                "pages": [
                    "FurnitureWholesale",
                    "FurnitureImporters",
                ],
                "max_results": 50,
            },
            "apify_alibaba": {
                "enabled": True,
                "actor_id": "epctex/alibaba-scraper",
                "max_results": 30,
            },
            "apify_b2b": {
                "enabled": True,
                "actor_id": "apify/web-scraper",
                "targets": [
                    {"url": "https://www.go4worldbusiness.com/buy-leads/furniture.html", "name": "go4world"},
                    {"url": "https://www.tradekey.com/buyoffer/furniture.htm", "name": "tradekey"},
                    {"url": "https://www.exportersindia.com/indian-buyers/furniture.htm", "name": "exportersindia"},
                ],
                "max_pages": 3,
            },
        },
    },
}


# =====================================================================
#  Runtime state — set by intent_monitor.py via load_industry()
# =====================================================================

# Current active industry profile (set at runtime)
_active_industry: str = "注塑机"
_active_profile: dict = INDUSTRY_PROFILES["注塑机"]

# Backward-compatible module-level variables (updated by load_industry)
LEADS_FILE = DB_DIR / "leads_注塑机.json"
KEYWORDS_DIRECT: list[str] = _active_profile["keywords_direct"]
KEYWORDS_INDIRECT: list[str] = _active_profile["keywords_indirect"]
SOURCES: dict = _active_profile["sources"]


def load_industry(industry: str) -> dict:
    """Activate an industry profile. Updates module-level variables."""
    global _active_industry, _active_profile
    global LEADS_FILE, KEYWORDS_DIRECT, KEYWORDS_INDIRECT, SOURCES

    if industry not in INDUSTRY_PROFILES:
        available = ", ".join(INDUSTRY_PROFILES.keys())
        raise ValueError(f"Unknown industry '{industry}'. Available: {available}")

    _active_industry = industry
    _active_profile = INDUSTRY_PROFILES[industry]
    LEADS_FILE = DB_DIR / _active_profile["leads_file"]
    KEYWORDS_DIRECT = _active_profile["keywords_direct"]
    KEYWORDS_INDIRECT = _active_profile["keywords_indirect"]
    SOURCES = _active_profile["sources"]

    return _active_profile


def get_active_profile() -> dict:
    """Return the currently active industry profile."""
    return _active_profile


def get_active_industry() -> str:
    """Return the currently active industry name."""
    return _active_industry


def list_industries() -> list[str]:
    """Return all available industry names."""
    return list(INDUSTRY_PROFILES.keys())
