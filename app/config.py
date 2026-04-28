import os

def load_env(path="/home/deanrex/.env"):
    """Minimal .env loader — no dependencies."""
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

load_env()

POCKETBASE_URL      = os.environ["POCKETBASE_URL"]
POCKETBASE_EMAIL    = os.environ["POCKETBASE_EMAIL"]
POCKETBASE_PASSWORD = os.environ["POCKETBASE_PASSWORD"]
TAPO_IP        = os.environ["TAPO_IP"]
TAPO_EMAIL     = os.environ["TAPO_EMAIL"]
TAPO_PASS      = os.environ["TAPO_PASS"]
PM25_THRESHOLD = float(os.environ.get("PM25_THRESHOLD", 35))
LOG_INTERVAL   = int(os.environ.get("LOG_INTERVAL",    300))
SQLITE_PATH    = os.environ.get("SQLITE_PATH",         "/home/deanrex/aqi_readings.db")