"""Turns a short user idea into a detailed cinematic video prompt.

Two interchangeable backends (OpenAI and Gemini) behind one `PromptEnhancer`
protocol, both spoken to over plain HTTP with httpx — no vendor SDKs.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from config import Settings
from services.errors import ConfigurationError, UpstreamError

SYSTEM_PROMPT = """\
You are a prompt engineer for text-to-video generation models.

Rewrite the user's short idea into ONE richly detailed cinematic prompt for a
3D/photoreal video shot of roughly 5-10 seconds. Always specify, woven into
natural prose rather than a list:

- Subject and action, with concrete visual detail.
- Camera: shot size, lens/focal length, angle, and one deliberate movement
  (dolly-in, slow orbit, crane up, handheld push, static locked-off, ...).
- Lighting: key/rim/practical sources, time of day, colour temperature,
  volumetrics or atmosphere.
- Mood, colour palette, and film-look references (film stock, grade, grain).
- Environment and background depth cues.

Rules:
- Output ONLY the prompt text. No preamble, no headings, no quotes, no markdown.
- Keep it under 150 words, present tense, one flowing paragraph.
- Never include text overlays, watermarks, logos, or dialogue.
"""


def _build_user_message(prompt: str, style: str | None) -> str:
    if style:
        return f"Idea: {prompt}\nPreferred style: {style}"
    return f"Idea: {prompt}"


class PromptEnhancer(Protocol):
    """Common surface for every LLM backend."""

    provider: str
    model: str

    async def enhance(self, prompt: str, style: str | None = None) -> str: ...

    def is_configured(self) -> bool: ...


class OpenAIPromptEnhancer:
    provider = "openai"

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self.model = settings.openai_model

    def is_configured(self) -> bool:
        return bool(self._settings.openai_api_key)

    async def enhance(self, prompt: str, style: str | None = None) -> str:
        if not self.is_configured():
            raise ConfigurationError("OPENAI_API_KEY is not set.")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_message(prompt, style)},
            ],
            "temperature": self._settings.llm_temperature,
            "max_tokens": self._settings.llm_max_output_tokens,
        }
        data = await _post_json(
            self._client,
            f"{self._settings.openai_base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {self._settings.openai_api_key}"},
            provider="OpenAI",
        )

        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise UpstreamError(f"Unexpected OpenAI response shape: {data}") from exc

        return _require_text(text, "OpenAI")


class GeminiPromptEnhancer:
    provider = "gemini"

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self.model = settings.gemini_model

    def is_configured(self) -> bool:
        return bool(self._settings.gemini_api_key)

    async def enhance(self, prompt: str, style: str | None = None) -> str:
        if not self.is_configured():
            raise ConfigurationError("GEMINI_API_KEY is not set.")

        payload: dict[str, Any] = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": _build_user_message(prompt, style)}],
                }
            ],
            "generationConfig": {
                "temperature": self._settings.llm_temperature,
                "maxOutputTokens": self._settings.llm_max_output_tokens,
            },
        }
        url = f"{self._settings.gemini_base_url}/models/{self.model}:generateContent"
        data = await _post_json(
            self._client,
            url,
            json=payload,
            headers={"x-goog-api-key": self._settings.gemini_api_key or ""},
            provider="Gemini",
        )

        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(part.get("text", "") for part in parts)
        except (KeyError, IndexError, TypeError) as exc:
            raise UpstreamError(f"Unexpected Gemini response shape: {data}") from exc

        return _require_text(text, "Gemini")


async def _post_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    json: dict[str, Any],
    headers: dict[str, str],
    provider: str,
) -> dict[str, Any]:
    try:
        response = await client.post(url, json=json, headers=headers)
    except httpx.HTTPError as exc:
        raise UpstreamError(f"{provider} request failed: {exc}") from exc

    if response.status_code >= 400:
        raise UpstreamError(
            f"{provider} returned {response.status_code}: {response.text[:500]}"
        )

    try:
        return response.json()
    except ValueError as exc:
        raise UpstreamError(f"{provider} returned non-JSON body.") from exc


def _require_text(text: str | None, provider: str) -> str:
    cleaned = (text or "").strip().strip('"')
    if not cleaned:
        raise UpstreamError(f"{provider} returned an empty completion.")
    return cleaned


def build_prompt_enhancer(
    settings: Settings, client: httpx.AsyncClient
) -> PromptEnhancer:
    if settings.llm_provider == "gemini":
        return GeminiPromptEnhancer(settings, client)
    return OpenAIPromptEnhancer(settings, client)
