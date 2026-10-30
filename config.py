"""Central configuration, loaded from the .env file (or real environment variables)."""
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"[config] Missing required setting {name}. Fill it in your .env file.")
    return value


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        sys.exit(f"[config] {name} must be a whole number, got: {raw!r}")


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        sys.exit(f"[config] {name} must be a number, got: {raw!r}")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _id_list(name: str) -> set:
    ids = set()
    for part in os.getenv(name, "").replace(" ", "").split(","):
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            sys.exit(f"[config] {name} must be comma-separated numeric Telegram IDs, got: {part!r}")
    return ids


def _inbounds(name: str) -> list:
    """Parse 'port:inbound_id,port:inbound_id' -> [(port, inbound_id), ...] (order kept)."""
    raw = _required(name).replace(" ", "")
    pairs = []
    for part in raw.split(","):
        if not part:
            continue
        try:
            port, inbound_id = part.split(":")
            pairs.append((int(port), int(inbound_id)))
        except ValueError:
            sys.exit(f"[config] {name} must look like 8443:1,443:2 (port:inbound_id), got: {part!r}")
    if not pairs:
        sys.exit(f"[config] {name} is empty.")
    return pairs


# --- Telegram ---------------------------------------------------------------
BOT_TOKEN = _required("BOT_TOKEN")
ADMIN_IDS = _id_list("ADMIN_IDS")
if not ADMIN_IDS:
    sys.exit("[config] ADMIN_IDS is empty. Add at least your own numeric Telegram ID.")

# --- 3x-ui panel ------------------------------------------------------------
# Full panel URL INCLUDING the web base path, e.g. https://panel.example.com:2053/mySecretPath
XUI_URL = _required("XUI_URL").rstrip("/")
XUI_USERNAME = _required("XUI_USERNAME")
XUI_PASSWORD = _required("XUI_PASSWORD")
XUI_VERIFY_SSL = _bool("XUI_VERIFY_SSL", True)
XUI_TIMEOUT = _int("XUI_TIMEOUT", 15)
XUI_FLOW = os.getenv("XUI_FLOW", "").strip()  # e.g. xtls-rprx-vision (VLESS+Reality only)

# Ports the reseller can choose from: [(port, inbound_id), ...]
INBOUNDS = _inbounds("INBOUNDS")
INBOUND_BY_ID = {inbound_id: port for port, inbound_id in INBOUNDS}
PORT_BY_INBOUND = INBOUND_BY_ID  # alias: inbound_id -> port

# Base of your panel's subscription URL, e.g. https://domain:2096/sub/  (optional)
SUB_URL_BASE = os.getenv("SUB_URL_BASE", "").strip()

# --- Reseller rules ---------------------------------------------------------
DEFAULT_DAYS = _int("DEFAULT_DAYS", 30)          # 0 = no expiry
MAX_GB_PER_CLIENT = _float("MAX_GB_PER_CLIENT", 500)
LIMIT_IP = _int("LIMIT_IP", 0)                   # 0 = unlimited devices
REFUND_UNUSED_ON_DELETE = _bool("REFUND_UNUSED_ON_DELETE", False)
PENDING_TTL = _int("PENDING_TTL", 300)           # seconds a port menu stays valid

# --- Storage ----------------------------------------------------------------
DB_PATH = os.getenv("DB_PATH", "reseller.db")
