"""
Intent Monitor Configuration
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
LEADS_FILE = DB_DIR / "leads.json"
OUTPUT_DIR = BASE_DIR / "monitor" / "output"

# --- Keywords ---
KEYWORDS_DIRECT = [
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
]

KEYWORDS_INDIRECT = [
    '"new plastic factory" Vietnam',
    '"new plastic factory" India',
    '"new plastic factory" Mexico',
    '"new plastic factory" Indonesia',
    '"injection molding factory setup"',
    '"factory manager" "injection molding"',
    '"plant manager" "plastics manufacturing"',
]

# --- Source Config ---
SOURCES = {
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
}

# --- Analysis ---
MIN_INTENT_SCORE = 3  # Only keep leads with score >= this
LLM_BATCH_SIZE = 10   # Signals per LLM call

# --- Rate Limiting ---
REQUEST_DELAY_SECONDS = 2  # Delay between HTTP requests
