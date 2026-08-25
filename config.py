"""Application settings, loaded from environment variables / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- App ---
    app_name: str = "AI Video Generator API"
    debug: bool = False
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    http_timeout_seconds: float = 60.0

    # Finished videos land here as <task_id>.mp4 beside a <task_id>.json record.
    # The JSON doubles as the task store's on-disk state, so a restart mid-render
    # doesn't orphan a clip you already paid for.
    output_dir: Path = Path("outputs")

    # Only used to show an approximate KRW figure next to the USD estimate.
    usd_krw_rate: float = 1400.0

    # --- Prompt enhancer (LLM) ---
    llm_provider: Literal["openai", "gemini"] = "gemini"

    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"

    gemini_api_key: str | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_model: str = "gemini-2.5-flash-lite"

    llm_temperature: float = 0.9
    llm_max_output_tokens: int = 600

    # --- Video generation ---
    # "gemini" reuses GEMINI_API_KEY (Veo); "replicate" needs its own token.
    video_provider: Literal["gemini", "replicate"] = "gemini"

    # Veo, via the Gemini API. Shares gemini_api_key / gemini_base_url above.
    veo_model: str = "veo-3.1-lite-generate-preview"
    veo_resolution: str = "720p"

    replicate_api_token: str | None = None
    replicate_base_url: str = "https://api.replicate.com/v1"
    # Either "owner/name" (official-model endpoint) or "owner/name:<version_id>".
    replicate_model: str = "minimax/video-01"


@lru_cache
def get_settings() -> Settings:
    return Settings()
