import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()


def parse_user_ids(value):
    try:
        return {int(item.strip()) for item in value.split(",") if item.strip()}
    except ValueError as exc:
        raise RuntimeError("ADMIN_IDS must contain comma-separated numeric Telegram user IDs") from exc


ADMIN_IDS = parse_user_ids(os.getenv("ADMIN_IDS", ""))
_owner_id = os.getenv("OWNER_ID", "").strip()
try:
    OWNER_ID = int(_owner_id) if _owner_id else min(ADMIN_IDS, default=0)
except ValueError as exc:
    raise RuntimeError("OWNER_ID must be a numeric Telegram user ID") from exc
DB_PATH = os.getenv("DB_PATH", "pollbot.db").strip() or "pollbot.db"

INITIAL_GROUPS = [
    (-1003677249449, "Telugu Chat Street"),
    (-1002423629583, "Biscuit Pals"),
    (-1002394885553, "BLACK MOON"),
]