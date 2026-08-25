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

## Cost

Every render is billed per second of output, so the UI shows the price before
you click. At the default `veo-3.1-lite-generate-preview` / `720p`:

| Length | Cost | ≈ KRW |
| ------ | ------ | ----- |
| 4s     | $0.20  | 280원 |
| 6s     | $0.30  | 420원 |
| 8s     | $0.40  | 560원 |

The length dropdown defaults to **6s**. Leaving it on *모델 기본값* sends no
length at all and Veo renders 8s, the priciest of the three. Other tiers, per
second at 720p: `fast` $0.10, full `veo-3.1-generate-preview` $0.40 — the same
8s clip costs $3.20 there. Rates live in `_VEO_PRICE_USD_PER_SECOND`
(`services/video_provider.py`) and come from the
[Gemini API pricing page](https://ai.google.dev/gemini-api/docs/pricing); update
them there if Google changes them. `USD_KRW_RATE` only affects the KRW figure.

## Where the videos go

**Veo deletes generated videos after two days.** So the backend downloads each
finished clip to `OUTPUT_DIR` (default `outputs/`) the moment it completes:

```
outputs/
  6f1c….mp4     the video
  6f1c….json    prompt, length, cost, timestamps — and the task's own state
```

The download is driven by a server-side watcher, not by the browser, so closing
the tab mid-render doesn't cost you the clip. That JSON is also the task store's
on-disk state: it's read back at startup, so a restart during a render resumes
the job instead of orphaning something you already paid for. Once a local copy
exists, `/api/tasks/{id}/video` serves it — which keeps working after Veo has
expired the original.

`outputs/` is gitignored.

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
accepts durations of 4, 6, or 8 seconds only, while Replicate input keys differ
per model, so check the model's schema there.

> Any other length passes schema validation (which allows 1–60) and is rejected
> by the provider check inside the background task, so you get a `202` and then
> a `FAILED` on the next poll rather than a `422`. Nothing is billed. See
> *Known rough edges*.

```json
{
  "task_id": "6f1c...",
  "status": "PENDING",
  "prompt": "...",
  "estimated_cost_usd": 0.3
}
```

Returns immediately; the render is submitted in a background task.

### `GET /api/tasks/{task_id}`

Refreshes from the provider on every call while the task is in flight.

```json
{
  "task_id": "6f1c...",
  "status": "COMPLETED",
  "prompt": "...",
  "video_url": "http://127.0.0.1:8000/api/tasks/6f1c.../video",
  "local_path": "outputs/6f1c....mp4",
  "estimated_cost_usd": 0.3,
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

Serves the finished MP4. Prefers the saved local copy — which supports range
requests, so the player can seek, and outlives the provider's own link — and
falls back to streaming from the provider. `409` if the task isn't `COMPLETED`.

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
  video_service.py            Task lifecycle, completion watcher, auto-save
  task_store.py               Task registry, mirrored to outputs/*.json
  errors.py                   Service exceptions → HTTP status codes
outputs/                      Saved videos + task state (gitignored)
```

## Production notes

- **Task storage is a directory of JSON files.** Fine for one process; it isn't
  shared between workers and every update rewrites a file. Run a single worker,
  or swap `TaskStore` for a Redis/Postgres class with the same coroutines.
- **Polling costs a request per call.** Replicate supports webhooks; for high
  traffic, add a `webhook` field in `ReplicateVideoProvider.submit()` and a
  receiving route that updates the store directly.
- **CORS defaults to `*`.** Irrelevant while the UI is served from this origin,
  but set `CORS_ORIGINS` if you ever host the frontend separately.
- **The UI loads Tailwind from a CDN**, which prints a "not for production"
  console warning and needs network access. For a real deployment, build a
  stylesheet with the Tailwind CLI and swap the `<script>` tag for a `<link>`.
- Add rate limiting on `/api/generate-video` — every call spends provider credit.

## Known rough edges

Not bugs that cost money, but worth knowing:

- **Veo length validation happens late.** `duration_seconds` is checked against
  `(4, 6, 8)` in `GeminiVeoVideoProvider.submit()`, which runs in the background
  task — so a bad value returns `202` and fails on the next poll instead of
  `422`. Nothing is billed, since the check precedes the upstream call. Moving
  it into the route would need a provider-aware validator, because Replicate
  accepts other lengths.
- **`CORS_ORIGINS=["*"]` with `allow_credentials=True` is a combination browsers
  reject.** Harmless while the UI is served from this origin — no preflight is
  ever made — but it will silently not work if you host the frontend separately.
  Set a concrete origin list before you do.
- **Nothing stops repeat clicks.** There is no confirmation and no rate limit;
  each `영상 생성` press spends real credit.
- **The UI has no gallery.** Past renders are on disk under `outputs/` but the
  page only ever shows the current one.
