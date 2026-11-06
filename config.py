import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

if sys.platform == "win32":
    # psycopg's async mode refuses Windows' default ProactorEventLoop. Render is
    # Linux so this only matters for local runs, but it must happen before any loop.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _req(name: str) -> str:
    v = os.getenv(name)
    if not v:
        sys.exit(f"Missing required environment variable: {name}")
    return v


TELEGRAM_TOKEN = _req("TELEGRAM_TOKEN")
GROQ_API_KEY = _req("GROQ_API_KEY")
DATABASE_URL = _req("DATABASE_URL")
ADMIN_CHAT_ID = int(_req("ADMIN_CHAT_ID"))

# Both verified against this account with client.models.list(); the Llama models
# are Enterprise-only here, and gpt-oss is text-only, so vision goes to qwen.
GROQ_TEXT_MODEL = os.getenv("GROQ_TEXT_MODEL", "openai/gpt-oss-120b")
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "8080"))

TMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp")

BATCH_WINDOW = 20          # seconds collected before one batch is processed
MAX_PHOTOS = 2
VISION_PAGES = 2           # <= this many pages goes straight to the vision model
ADMIN_PAGES = 8            # > this many needs an admin decision
MAX_VISION_IMAGES = 5      # cap per Groq vision request
MSG_PER_MIN = 10
PDF_PER_MIN = 5
PNG_COOLDOWN = 600         # one explicit "review it as images" request per 10 min
TMP_RETENTION_DAYS = 7
SESSION_DAYS = 3
CV_CHARS = 24000           # stored CV ceiling, keeps the context window sane

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
