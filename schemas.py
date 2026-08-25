"""Public request/response models (Pydantic v2)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TaskStatus = Literal["PENDING", "PROCESSING", "COMPLETED", "FAILED"]


class EnhancePromptRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"prompt": "A cat flying in space"}]}
    )

    prompt: str = Field(min_length=3, max_length=2000, description="Raw user idea.")
    style: str | None = Field(
        default=None,
        max_length=200,
        description="Optional style hint, e.g. 'Pixar 3D', 'film noir', 'anime'.",
    )


class EnhancePromptResponse(BaseModel):
    original_prompt: str
    enhanced_prompt: str
    provider: str
    model: str


class GenerateVideoRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "prompt": (
                        "Cinematic 3D shot of an astronaut cat drifting through a "
                        "nebula, slow dolly-in, volumetric rim light, awe-struck mood."
                    ),
                    "aspect_ratio": "16:9",
                }
            ]
        }
    )

    prompt: str = Field(
        min_length=3,
        max_length=4000,
        description="Enhanced (or raw) prompt to render.",
    )
    enhance: bool = Field(
        default=False,
        description="Run the prompt through the enhancer before generating.",
    )
    duration_seconds: int | None = Field(default=None, ge=1, le=60)
    aspect_ratio: str | None = Field(default=None, max_length=16, examples=["16:9"])


class GenerateVideoResponse(BaseModel):
    task_id: str
    status: TaskStatus = "PENDING"
    prompt: str
    estimated_cost_usd: float | None = Field(
        default=None, description="List price for this render, if the provider quotes one."
    )


class TaskStatusResponse(BaseModel):
    task_id: str
    status: TaskStatus
    prompt: str
    video_url: str | None = None
    local_path: str | None = Field(
        default=None,
        description="Where the finished mp4 was saved on the server, once downloaded.",
    )
    estimated_cost_usd: float | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    llm_provider: str
    llm_configured: bool
    video_provider: str
    video_configured: bool
    video_model: str | None = None
    price_per_second_usd: float | None = Field(
        default=None, description="Published rate for the selected model/resolution."
    )
    default_duration_seconds: int | None = Field(
        default=None, description="Clip length used when the caller doesn't pick one."
    )
    usd_krw_rate: float = Field(description="Rate used for the approximate KRW figure.")


class ErrorResponse(BaseModel):
    detail: str
