import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
_owner_id = os.getenv("OWNER_ID", "").strip()
OWNER_ID = int(_owner_id) if _owner_id else min(ADMIN_IDS, default=0)
DB_PATH = os.getenv("DB_PATH", "pollbot.db")

INITIAL_GROUPS = [
    (-1003677249449, "Telugu Chat Street"),
    (-1002423629583, "Biscuit Pals"),
    (-1002394885553, "BLACK MOON"),
]