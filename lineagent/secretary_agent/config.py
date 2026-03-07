import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    line_channel_access_token: str
    line_channel_secret: str
    dify_api_key: str
    dify_base_url: str
    dify_user_prefix: str
    database_path: str
    worker_poll_seconds: float
    short_context_ttl_days: int

    @classmethod
    def from_env(cls) -> "Settings":
        base_dir = os.path.dirname(os.path.dirname(__file__))
        settings = cls(
            line_channel_access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip(),
            line_channel_secret=os.getenv("LINE_CHANNEL_SECRET", "").strip(),
            dify_api_key=os.getenv("DIFY_API_KEY", "").strip(),
            dify_base_url=os.getenv("DIFY_BASE_URL", "https://api.dify.ai/v1").strip(),
            dify_user_prefix=os.getenv("DIFY_USER_PREFIX", "line").strip(),
            database_path=os.getenv(
                "BOT_DB_PATH",
                os.path.join(base_dir, "bot_memory.sqlite3"),
            ).strip(),
            worker_poll_seconds=float(os.getenv("WORKER_POLL_SECONDS", "1.5")),
            short_context_ttl_days=int(os.getenv("SHORT_CONTEXT_TTL_DAYS", "7")),
        )
        missing = [
            name
            for name, value in (
                ("LINE_CHANNEL_ACCESS_TOKEN", settings.line_channel_access_token),
                ("LINE_CHANNEL_SECRET", settings.line_channel_secret),
                ("DIFY_API_KEY", settings.dify_api_key),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
        return settings
