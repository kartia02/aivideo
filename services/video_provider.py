"""Video generation backend.

`VideoProvider` is the seam: `submit()` kicks off a render and returns the
provider's own job id, `fetch()` reports on it. Swapping Replicate for
Runway/Luma means writing one more class here and nothing else.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

import httpx
from pydantic import BaseModel

from config import Settings
from schemas import TaskStatus
from services.errors import ConfigurationError, UpstreamError


class ProviderResult(BaseModel):
    status: TaskStatus
    video_url: str | None = None
    error: str | None = None


class VideoProvider(Protocol):
    name: str
    # True when the finished video sits behind an API key and therefore has to
    # be streamed through this backend instead of handed straight to a browser.
    requires_auth_download: bool

    async def submit(
        self,
        prompt: str,
        *,
        duration_seconds: int | None = None,
        aspect_ratio: str | None = None,
    ) -> str: ...

    async def fetch(self, provider_task_id: str) -> ProviderResult: ...

    def stream(self, video_url: str) -> AsyncIterator[bytes]: ...

    def is_configured(self) -> bool: ...


async def _stream_bytes(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], provider: str
) -> AsyncIterator[bytes]:
    """Proxy a remote video to the caller without buffering it in memory."""
    try:
        async with client.stream(
            "GET", url, headers=headers, follow_redirects=True
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                raise UpstreamError(
                    f"{provider} download returned {response.status_code}."
                )
            async for chunk in response.aiter_bytes():
                yield chunk
    except httpx.HTTPError as exc:
        raise UpstreamError(f"{provider} download failed: {exc}") from exc


# Replicate prediction states -> our public vocabulary.
_STATUS_MAP: dict[str, TaskStatus] = {
    "starting": "PENDING",
    "processing": "PROCESSING",
    "succeeded": "COMPLETED",
    "failed": "FAILED",
    "canceled": "FAILED",
}


class ReplicateVideoProvider:
    """Talks to the Replicate predictions REST API."""

    name = "replicate"
    requires_auth_download = False  # replicate.delivery URLs are public

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self.model = settings.replicate_model

    def is_configured(self) -> bool:
        return bool(self._settings.replicate_api_token)

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.replicate_api_token}",
            "Content-Type": "application/json",
        }

    def _create_endpoint(self) -> tuple[str, dict[str, Any]]:
        """Resolve REPLICATE_MODEL into (url, extra body fields).

        "owner/name:<version>" uses the generic predictions endpoint;
        "owner/name" uses the official-model endpoint, which always runs the
        model's current version.
        """
        base = self._settings.replicate_base_url.rstrip("/")
        model = self.model.strip()

        if ":" in model:
            _, version = model.split(":", 1)
            return f"{base}/predictions", {"version": version}

        if model.count("/") != 1:
            raise ConfigurationError(
                "REPLICATE_MODEL must look like 'owner/name' or 'owner/name:version', "
                f"got {model!r}."
            )
        return f"{base}/models/{model}/predictions", {}

    async def submit(
        self,
        prompt: str,
        *,
        duration_seconds: int | None = None,
        aspect_ratio: str | None = None,
    ) -> str:
        if not self.is_configured():
            raise ConfigurationError("REPLICATE_API_TOKEN is not set.")

        url, body = self._create_endpoint()

        # Input keys vary per model; only send what the caller asked for.
        model_input: dict[str, Any] = {"prompt": prompt}
        if duration_seconds is not None:
            model_input["duration"] = duration_seconds
        if aspect_ratio is not None:
            model_input["aspect_ratio"] = aspect_ratio

        data = await self._request("POST", url, json={**body, "input": model_input})

        prediction_id = data.get("id")
        if not prediction_id:
            raise UpstreamError(f"Replicate response had no prediction id: {data}")
        return str(prediction_id)

    async def fetch(self, provider_task_id: str) -> ProviderResult:
        if not self.is_configured():
            raise ConfigurationError("REPLICATE_API_TOKEN is not set.")

        base = self._settings.replicate_base_url.rstrip("/")
        data = await self._request("GET", f"{base}/predictions/{provider_task_id}")

        raw_status = str(data.get("status", "")).lower()
        status = _STATUS_MAP.get(raw_status, "PROCESSING")

        if status == "FAILED":
            return ProviderResult(
                status=status,
                error=str(data.get("error") or f"Prediction {raw_status}."),
            )

        if status == "COMPLETED":
            video_url = _extract_video_url(data.get("output"))
            if video_url is None:
                return ProviderResult(
                    status="FAILED",
                    error=f"Prediction succeeded but returned no video URL: "
                    f"{data.get('output')!r}",
                )
            return ProviderResult(status=status, video_url=video_url)

        return ProviderResult(status=status)

    def stream(self, video_url: str) -> AsyncIterator[bytes]:
        return _stream_bytes(self._client, video_url, {}, "Replicate")

    async def _request(
        self, method: str, url: str, *, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method, url, json=json, headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(f"Replicate request failed: {exc}") from exc

        if response.status_code >= 400:
            raise UpstreamError(
                f"Replicate returned {response.status_code}: {response.text[:500]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError("Replicate returned a non-JSON body.") from exc

        if not isinstance(payload, dict):
            raise UpstreamError(f"Unexpected Replicate payload: {payload!r}")
        return payload


class GeminiVeoVideoProvider:
    """Runs Veo through the Gemini API, reusing GEMINI_API_KEY.

    Generation is a long-running operation: `predictLongRunning` hands back an
    operation name, which we poll until `done`. The resulting file needs the API
    key to download, so `requires_auth_download` is True and the video is served
    back through this backend rather than exposing the key to the browser.
    """

    name = "gemini-veo"
    requires_auth_download = True

    # Veo accepts only these clip lengths.
    _ALLOWED_DURATIONS = (4, 6, 8)

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self.model = settings.veo_model

    def is_configured(self) -> bool:
        return bool(self._settings.gemini_api_key)

    @property
    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self._settings.gemini_api_key or ""}

    async def submit(
        self,
        prompt: str,
        *,
        duration_seconds: int | None = None,
        aspect_ratio: str | None = None,
    ) -> str:
        if not self.is_configured():
            raise ConfigurationError("GEMINI_API_KEY is not set.")
        if duration_seconds is not None and duration_seconds not in self._ALLOWED_DURATIONS:
            raise ConfigurationError(
                f"Veo supports duration_seconds of {self._ALLOWED_DURATIONS}, "
                f"got {duration_seconds}."
            )

        parameters: dict[str, Any] = {
            "resolution": self._settings.veo_resolution,
            "numberOfVideos": 1,
        }
        if duration_seconds is not None:
            parameters["durationSeconds"] = str(duration_seconds)
        if aspect_ratio is not None:
            parameters["aspectRatio"] = aspect_ratio

        base = self._settings.gemini_base_url.rstrip("/")
        data = await self._request(
            "POST",
            f"{base}/models/{self.model}:predictLongRunning",
            json={"instances": [{"prompt": prompt}], "parameters": parameters},
        )

        operation_name = data.get("name")
        if not operation_name:
            raise UpstreamError(f"Veo response had no operation name: {data}")
        return str(operation_name)

    async def fetch(self, provider_task_id: str) -> ProviderResult:
        if not self.is_configured():
            raise ConfigurationError("GEMINI_API_KEY is not set.")

        base = self._settings.gemini_base_url.rstrip("/")
        data = await self._request("GET", f"{base}/{provider_task_id.lstrip('/')}")

        if not data.get("done"):
            return ProviderResult(status="PROCESSING")

        if error := data.get("error"):
            message = error.get("message") if isinstance(error, dict) else str(error)
            return ProviderResult(status="FAILED", error=message or "Veo failed.")

        video_url = _extract_veo_uri(data.get("response"))
        if video_url is None:
            # Usually a safety filter: the operation finishes with no sample.
            return ProviderResult(
                status="FAILED",
                error=_veo_filter_reason(data.get("response"))
                or "Veo finished without returning a video.",
            )
        return ProviderResult(status="COMPLETED", video_url=video_url)

    def stream(self, video_url: str) -> AsyncIterator[bytes]:
        return _stream_bytes(self._client, video_url, self._headers, "Veo")

    async def _request(
        self, method: str, url: str, *, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method, url, json=json, headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(f"Veo request failed: {exc}") from exc

        if response.status_code >= 400:
            raise UpstreamError(
                f"Veo returned {response.status_code}: {response.text[:500]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError("Veo returned a non-JSON body.") from exc

        if not isinstance(payload, dict):
            raise UpstreamError(f"Unexpected Veo payload: {payload!r}")
        return payload


def _extract_veo_uri(response: Any) -> str | None:
    """response.generateVideoResponse.generatedSamples[0].video.uri"""
    if not isinstance(response, dict):
        return None
    samples = response.get("generateVideoResponse", {}).get("generatedSamples")
    if not isinstance(samples, list):
        return None
    for sample in samples:
        if isinstance(sample, dict):
            uri = sample.get("video", {}).get("uri")
            if isinstance(uri, str) and uri:
                return uri
    return None


def _veo_filter_reason(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    reasons = response.get("generateVideoResponse", {}).get("raiMediaFilteredReasons")
    if isinstance(reasons, list) and reasons:
        return f"Blocked by safety filter: {'; '.join(str(r) for r in reasons)}"
    return None


def _extract_video_url(output: Any) -> str | None:
    """Models return a URL string, a list of URLs, or a dict wrapping one."""
    if isinstance(output, str):
        return output or None
    if isinstance(output, list):
        for item in reversed(output):
            url = _extract_video_url(item)
            if url:
                return url
        return None
    if isinstance(output, dict):
        for key in ("video", "url", "output"):
            url = _extract_video_url(output.get(key))
            if url:
                return url
    return None


def build_video_provider(
    settings: Settings, client: httpx.AsyncClient
) -> VideoProvider:
    if settings.video_provider == "replicate":
        return ReplicateVideoProvider(settings, client)
    return GeminiVeoVideoProvider(settings, client)
