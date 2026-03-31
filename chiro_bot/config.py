import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# AI API
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.openai.com/v1")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")

# Bot Settings
TIMEZONE = os.getenv("TIMEZONE", "Asia/Seoul")
MORNING_HOUR = int(os.getenv("MORNING_HOUR", "8"))
EVENING_HOUR = int(os.getenv("EVENING_HOUR", "22"))
PROACTIVE_INTERVAL_MINUTES = int(os.getenv("PROACTIVE_INTERVAL_MINUTES", "15"))

# DB
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "chiro.db")
