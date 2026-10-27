"""App configuration: paths, timers, limits and Gemini prices. Env values come from .env."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"
AUDIO_DIR = DATA_DIR / "audio"
LOG_DIR = BASE_DIR / "logs"
PROMPTS_DIR = BASE_DIR / "prompts"
STATIC_DIR = BASE_DIR / "static"
TESTS_DIR = BASE_DIR / "tests"
DB_PATH = DATA_DIR / "sparring.db"
OBJECTIONS_PATH = DATA_DIR / "objections.json"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite").strip()
GEMINI_TIER = os.getenv("GEMINI_TIER", "free").strip().lower()
# Opt-in: on the free tier Google may use and human-review recordings (see README).
VOICE_ON_FREE_TIER = os.getenv("VOICE_ON_FREE_TIER", "").strip() == "1"
# Streamlit only: when set, the app asks for this password first. Set it for any web deploy.
APP_PASSWORD = os.getenv("APP_PASSWORD", "").strip()
# Streamlit public mode (password required, History and calibration hidden). Unset: public unless served to
# localhost. "1" forces it on, "0" off (ignored on Streamlit Cloud, which is always public).
PUBLIC_DEPLOY = os.getenv("PUBLIC_DEPLOY", "").strip()
# No new Gemini calls once today's (UTC) spend reaches this. Both front ends.
DAILY_SPEND_LIMIT_USD = float(os.getenv("DAILY_SPEND_LIMIT_USD", "1.0") or 1.0)

ROUND_SIZE = 5
MAX_DIFFICULTY_3_PER_ROUND = 2
CATEGORIES = ["nature", "health", "ethics", "environment", "personal"]
SOURCED_CATEGORIES = {"health", "environment"}

# Seconds. Follow-up values are illustrative - confirm after the first few rounds (PRD M4).
TIMERS = {
    "opener": {"type": 90, "speak": 45},
    "followup": {"type": 60, "speak": 30},
}
GRACE_S = 10

MAX_UPLOAD_MB = 5
MAX_ANSWER_CHARS = 2000
SESSION_CALL_LIMIT = 30  # Streamlit public mode: Gemini-spending actions per browser session
MAX_OUTPUT_TOKENS = {"followup": 2048, "judge": 8192, "transcribe": 8192}  # includes thinking tokens
CALL_TIMEOUT_MS = 30_000
MAX_CALLS = 3
FOLLOWUP_MAX_WORDS = 60
MODEL_ANSWER_MAX_WORDS = 120

# USD per 1M tokens, Standard tier. From gemini_pricing_final.json fetched 2026-10-22.
PRICES = {
    "gemini-3.8-flash": {"input": 0.75, "output": 3.75},
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50},
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50},
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40},
}

STREAM_CLIP_URL = "https://www.youtube.com/watch?v=kkw6ozdxli0&t=11700s"
