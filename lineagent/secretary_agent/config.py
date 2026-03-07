import os
from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class Settings:
    line_channel_access_token: str
    line_channel_secret: str
    dify_api_key: str
    dify_base_url: str
    dify_user_prefix: str
    stt_provider: str
    assemblyai_api_key: str
    stt_language_code: str
    stt_speech_models: Tuple[str, ...]
    stt_poll_seconds: float
    stt_timeout_seconds: float
    stt_upload_timeout_seconds: float
    diarization_speakers_expected: int
    public_base_url: str
    artifact_output_dir: str
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
            stt_provider=os.getenv("STT_PROVIDER", "assemblyai").strip().lower(),
            assemblyai_api_key=os.getenv("ASSEMBLYAI_API_KEY", "").strip(),
            stt_language_code=os.getenv("STT_LANGUAGE_CODE", "zh").strip(),
            stt_speech_models=tuple(
                item.strip()
                for item in os.getenv("STT_SPEECH_MODELS", "universal-3-pro,universal-2").split(",")
                if item.strip()
            ),
            stt_poll_seconds=float(os.getenv("STT_POLL_SECONDS", "2.5")),
            stt_timeout_seconds=float(os.getenv("STT_TIMEOUT_SECONDS", "120")),
            stt_upload_timeout_seconds=float(os.getenv("STT_UPLOAD_TIMEOUT_SECONDS", "600")),
            diarization_speakers_expected=int(os.getenv("DIARIZATION_SPEAKERS_EXPECTED", "0")),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/"),
            artifact_output_dir=os.getenv(
                "ARTIFACT_OUTPUT_DIR",
                os.path.join(base_dir, "output", "doc"),
            ).strip(),
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
