"""Environment + paths. Single place that reads .env — nothing else touches os.environ."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

load_dotenv(ROOT / ".env")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MAPBOX_TOKEN = os.environ.get("MAPBOX_TOKEN", "")


def require_db() -> str:
    """Return DATABASE_URL or fail with a actionable message."""
    if not DATABASE_URL or "REPLACE_ME" in DATABASE_URL:
        raise SystemExit(
            "DATABASE_URL is not set. Create the Supabase project, then put its "
            "connection string in .env (see .env placeholders / README Phase 1)."
        )
    return DATABASE_URL
