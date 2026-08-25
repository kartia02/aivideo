"""AI Video Generator — FastAPI backend.

Run with:  uvicorn main:app --reload
Docs at:   http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from config import Settings, get_settings
from schemas import (
    EnhancePromptRequest,
    EnhancePromptResponse,
    ErrorResponse,
    GenerateVideoRequest,
    GenerateVideoResponse,
    HealthResponse,
    TaskStatusResponse,
)
from services import (
    PromptEnhancer,
    ServiceError,
    TaskNotReadyError,
    TaskStore,
    VideoService,
    build_prompt_enhancer,
    build_video_provider,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ai_video_generator")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build one shared httpx client and the service singletons."""
    settings = get_settings()
    client = httpx.AsyncClient(timeout=httpx.Timeout(settings.http_timeout_seconds))

    app.state.settings = settings
    app.state.http_client = client
    app.state.enhancer = build_prompt_enhancer(settings, client)
    app.state.video_service = VideoService(
        provider=build_video_provider(settings, client),
        store=TaskStore(),
    )

    logger.info(
        "Started with llm_provider=%s video_provider=%s",
        settings.llm_provider,
        app.state.video_service.provider.name,
    )
    try:
        yield
    finally:
        await client.aclose()


app = FastAPI(
    title="AI Video Generator API",
    description=(
        "Enhance a short idea into a cinematic prompt, render it into a video, "
        "and poll the task until the clip is ready."
    ),
    version="1.0.0",
    lifespan=lifespan,
    responses={
        502: {"model": ErrorResponse, "description": "Upstream provider error"},
        503: {"model": ErrorResponse, "description": "Provider not configured"},
    },
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ServiceError)
async def service_error_handler(_: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #


def get_enhancer(request: Request) -> PromptEnhancer:
    return request.app.state.enhancer


def get_video_service(request: Request) -> VideoService:
    return request.app.state.video_service


EnhancerDep = Annotated[PromptEnhancer, Depends(get_enhancer)]
VideoServiceDep = Annotated[VideoService, Depends(get_video_service)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@app.get("/api/health", response_model=HealthResponse, tags=["system"])
async def health(
    settings: SettingsDep, enhancer: EnhancerDep, videos: VideoServiceDep
) -> HealthResponse:
    return HealthResponse(
        llm_provider=settings.llm_provider,
        llm_configured=enhancer.is_configured(),
        video_provider=videos.provider.name,
        video_configured=videos.provider.is_configured(),
        video_model=getattr(videos.provider, "model", None),
    )


@app.post(
    "/api/enhance-prompt",
    response_model=EnhancePromptResponse,
    tags=["prompt"],
    summary="Expand a short idea into a detailed cinematic video prompt",
)
async def enhance_prompt(
    payload: EnhancePromptRequest, enhancer: EnhancerDep
) -> EnhancePromptResponse:
    enhanced = await enhancer.enhance(payload.prompt, payload.style)
    return EnhancePromptResponse(
        original_prompt=payload.prompt,
        enhanced_prompt=enhanced,
        provider=enhancer.provider,
        model=enhancer.model,
    )


@app.post(
    "/api/generate-video",
    response_model=GenerateVideoResponse,
    status_code=202,
    tags=["video"],
    summary="Queue a video render and return its task id",
)
async def generate_video(
    payload: GenerateVideoRequest,
    background_tasks: BackgroundTasks,
    videos: VideoServiceDep,
    enhancer: EnhancerDep,
) -> GenerateVideoResponse:
    prompt = payload.prompt
    if payload.enhance:
        prompt = await enhancer.enhance(prompt)

    task = await videos.create_task(prompt)
    background_tasks.add_task(
        videos.submit_task,
        task.task_id,
        duration_seconds=payload.duration_seconds,
        aspect_ratio=payload.aspect_ratio,
    )
    return GenerateVideoResponse(
        task_id=task.task_id, status=task.status, prompt=task.prompt
    )


@app.get(
    "/api/tasks/{task_id}",
    response_model=TaskStatusResponse,
    tags=["video"],
    summary="Poll a task until it is COMPLETED or FAILED",
    responses={404: {"model": ErrorResponse, "description": "Unknown task id"}},
)
async def get_task(
    task_id: str, request: Request, videos: VideoServiceDep
) -> TaskStatusResponse:
    task = await videos.get_task(task_id)

    # Providers whose output sits behind an API key are served through our own
    # proxy route, so the client never needs the credential.
    video_url = task.video_url
    if video_url and videos.provider.requires_auth_download:
        video_url = str(request.url_for("download_video", task_id=task_id))

    return TaskStatusResponse(
        task_id=task.task_id,
        status=task.status,
        prompt=task.prompt,
        video_url=video_url,
        error=task.error,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


@app.get(
    "/api/tasks/{task_id}/video",
    tags=["video"],
    summary="Stream the finished video through the backend",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"video/mp4": {}}, "description": "The generated video"},
        404: {"model": ErrorResponse, "description": "Unknown task id"},
        409: {"model": ErrorResponse, "description": "Task is not COMPLETED yet"},
    },
)
async def download_video(task_id: str, videos: VideoServiceDep) -> StreamingResponse:
    task = await videos.get_task(task_id)
    if task.status != "COMPLETED" or not task.video_url:
        raise TaskNotReadyError(f"Task {task_id} is {task.status}, not COMPLETED.")

    return StreamingResponse(
        videos.stream_video(task.video_url),
        media_type="video/mp4",
        headers={"Content-Disposition": f'inline; filename="{task_id}.mp4"'},
    )


# --------------------------------------------------------------------------- #
# Frontend
#
# Mounted last so every /api/* route above wins the match. Serving the UI from
# this same origin means no CORS round-trips and no separate dev server.
# --------------------------------------------------------------------------- #

STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="frontend")
else:  # pragma: no cover
    logger.warning("No static/ directory found — the API runs without a UI.")


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
