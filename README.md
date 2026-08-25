# AI Video Generator — Backend

FastAPI service that turns a one-line idea into a video:

```
"A cat flying in space"
        │
        ├─ POST /api/enhance-prompt   → an LLM writes a cinematic prompt
        ├─ POST /api/generate-video   → a video model starts rendering, returns task_id
        └─ GET  /api/tasks/{task_id}  → poll until COMPLETED, get the video URL
```

Python 3.10+ · FastAPI · Pydantic v2 · httpx. No vendor SDKs — every provider is
reached over plain HTTP, so swapping one out is a single new class.

## Providers

Two independent choices, each one variable:

| Stage        | Setting          | Options                      |
| ------------ | ---------------- | ---------------------------- |
| Prompt (LLM) | `LLM_PROVIDER`   | `openai` · `gemini`          |
| Video        | `VIDEO_PROVIDER` | `gemini` (Veo) · `replicate` |

The default is **`gemini` for both**, so a single `GEMINI_API_KEY` runs the whole
app. Only the selected provider's key is required.

> **Veo needs a billed Google Cloud project.** Video generation is not offered on
> the Gemini free tier; a free key enhances prompts fine but fails at
> `/api/generate-video` with a quota error.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt

copy .env.example .env          # cp on macOS/Linux — then fill in your key
```

| Variable              | Needed when                                | Where to get it                          |
| --------------------- | ------------------------------------------ | ---------------------------------------- |
| `GEMINI_API_KEY`      | `LLM_PROVIDER` or `VIDEO_PROVIDER`=`gemini`| https://aistudio.google.com/app/apikey   |
| `OPENAI_API_KEY`      | `LLM_PROVIDER=openai`                      | https://platform.openai.com/api-keys     |
| `REPLICATE_API_TOKEN` | `VIDEO_PROVIDER=replicate`                 | https://replicate.com/account/api-tokens |

See [.env.example](.env.example) for every option.

## Run

```bash
uvicorn main:app --reload
```

Then open <http://127.0.0.1:8000> — that's the whole app. The web UI is a single
static file that FastAPI serves from `static/`, so there is **no build step, no
npm install, and no second dev server**; because it comes off the same origin,
CORS never enters the picture.

Interactive docs: <http://127.0.0.1:8000/docs> ·
Config check: <http://127.0.0.1:8000/api/health> reports which providers have
keys — the UI shows a banner when one is missing.

### Using the UI

1. Type an idea, optionally set a style, aspect ratio, and duration.
2. **Enhance prompt** — see the cinematic rewrite and edit it if you like.
   (Skip it: leave *Auto-enhance* checked and the backend does it inline.)
3. **Generate video** — polls every 3s and shows the live status and a timer.
4. The player appears on `COMPLETED`; **Download MP4** saves the file.

`Ctrl`/`Cmd` + `Enter` in the prompt box starts a generation.

## API

### `POST /api/enhance-prompt`

```json
{ "prompt": "A cat flying in space", "style": "Pixar 3D" }
```

```json
{
  "original_prompt": "A cat flying in space",
  "enhanced_prompt": "A tabby cat in a scuffed orange spacesuit drifts through a violet nebula...",
  "provider": "openai",
  "model": "gpt-4o-mini"
}
```

### `POST /api/generate-video` → `202 Accepted`

```json
{ "prompt": "<enhanced prompt>", "aspect_ratio": "16:9", "duration_seconds": 6 }
```

Set `"enhance": true` to run the enhancer first and skip the separate call.
`duration_seconds` / `aspect_ratio` are forwarded only when supplied — Veo
accepts durations of 4, 6, or 8 seconds and is rejected early otherwise, while
Replicate input keys differ per model, so check the model's schema there.

```json
{ "task_id": "6f1c...", "status": "PENDING", "prompt": "..." }
```

Returns immediately; the render is submitted in a background task.

### `GET /api/tasks/{task_id}`

Refreshes from the provider on every call while the task is in flight.

```json
{
  "task_id": "6f1c...",
  "status": "COMPLETED",
  "prompt": "...",
  "video_url": "https://replicate.delivery/.../out.mp4",
  "error": null,
  "created_at": "2026-08-25T10:00:00Z",
  "updated_at": "2026-08-25T10:01:12Z"
}
```

Statuses: `PENDING` → `PROCESSING` → `COMPLETED` | `FAILED`.
On `FAILED`, `error` carries the reason. Poll every 3–5s; renders take 1–5 min.

`video_url` depends on the provider: Replicate returns a public
`replicate.delivery` link, while Veo files sit behind the API key, so the
response points at the proxy route below instead. Either way the client just
uses `video_url` — it never sees a credential.

### `GET /api/tasks/{task_id}/video`

Streams the finished MP4 through the backend (Veo only; Replicate links are
already public). `409` if the task isn't `COMPLETED` yet.

Errors use `{"detail": "..."}`: `404` unknown task, `409` video not ready,
`422` invalid body, `502` upstream provider error, `503` missing API key.

### Quick check

```bash
curl -X POST http://127.0.0.1:8000/api/enhance-prompt \
  -H "Content-Type: application/json" \
  -d "{\"prompt\": \"A cat flying in space\"}"
```

## Layout

```
main.py                       FastAPI app, routes, DI, error handling, static mount
config.py                     Settings (pydantic-settings, reads .env)
schemas.py                    Request/response models
static/index.html             The whole frontend (Tailwind CDN + vanilla JS)
services/
  prompt_enhancer.py          OpenAI + Gemini backends behind one protocol
  video_provider.py           Veo + Replicate backends, submit/fetch/stream
  video_service.py            Task lifecycle orchestration
  task_store.py               In-memory task registry
  errors.py                   Service exceptions → HTTP status codes
```

## Production notes

- **Task storage is in-memory.** Tasks vanish on restart and aren't shared
  between workers — run a single worker, or swap `TaskStore` for a Redis-backed
  class with the same coroutines.
- **Polling costs a request per call.** Replicate supports webhooks; for high
  traffic, add a `webhook` field in `ReplicateVideoProvider.submit()` and a
  receiving route that updates the store directly.
- **CORS defaults to `*`.** Irrelevant while the UI is served from this origin,
  but set `CORS_ORIGINS` if you ever host the frontend separately.
- **The UI loads Tailwind from a CDN**, which prints a "not for production"
  console warning and needs network access. For a real deployment, build a
  stylesheet with the Tailwind CLI and swap the `<script>` tag for a `<link>`.
- Add rate limiting on `/api/generate-video` — every call spends provider credit.
