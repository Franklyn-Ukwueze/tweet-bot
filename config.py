"""
Configuration — reads from environment variables.
"""

import os
from pathlib import Path


def _load_dotenv(path: str = ".env"):
    env_path = Path(path)
    if not env_path.exists():
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class Config:
    def __init__(self, env_file: str = ".env"):
        _load_dotenv(env_file)

        self.telegram_token: str = self._require("TELEGRAM_BOT_TOKEN")
        self.rapidapi_key: str   = self._require("RAPIDAPI_KEY")

        # Tweet API (twitter-api47 on RapidAPI)
        self.rapidapi_host: str        = "twitter-api47.p.rapidapi.com"
        self.rapidapi_tweets_url: str  = "https://twitter-api47.p.rapidapi.com/v3/user/tweets"
        self.rapidapi_profile_url: str = "https://twitter-api47.p.rapidapi.com/v3/user/by-username"

        # Render injects REDIS_URL automatically via the Blueprint Redis service
        self.redis_url: str = (
            os.getenv("REDIS_URL")
            or os.getenv("REDIS_TLS_URL")
            or os.getenv("REDISCLOUD_URL")
            or "redis://localhost:6379"
        )

        raw = os.getenv("ADMIN_USER_IDS", "")
        self.admin_ids: list[int] = (
            [int(x.strip()) for x in raw.split(",") if x.strip()] if raw else []
        )

        self.poll_interval: int = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))

    @staticmethod
    def _require(key: str) -> str:
        value = os.getenv(key)
        if not value:
            raise EnvironmentError(f"Required env var '{key}' is not set.")
        return value
