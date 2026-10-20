# Reelflow

**A self-hosted Instagram posting desk. Drop footage in a folder. Keep it on rotation.**

Built for TRADES, the DJ duo; configurable for any artist, creator, or business. Reelflow imports the connected account's recent captions and available engagement rather than imposing a generic brand voice.

## What it does

- Watches a read-only footage folder, including a TrueNAS dataset or mounted share. Also accepts browser uploads.
- Waits for files to settle, deduplicates by SHA-256, validates video, prepares 1080×1920 H.264/AAC MP4s, and generates previews. Original footage stays untouched. Nonvertical footage is letterboxed rather than cropped.
- Writes one- or two-sentence captions from three silent video frames, account context, operator notes, and up to 30 previous captions with available engagement counts. OpenRouter OAuth PKCE or an OpenAI-compatible API key; choose an actual model from the live catalog.
- Creates **one regular Reel and one Trial Reel per unique clip**. Regular Reels share to feed; Trials use `trial_params.graduation_strategy=MANUAL` and never silently fall back to regular Reels. No automatic Trial graduation.
- Schedules one regular Reel daily and up to three Trials daily. With conservation enabled, low stock or an upcoming footage drought slows both lanes to one post every second calendar day. Normal cadence returns when sufficient new footage arrives.
- Mixes new clips into flexible queue positions, avoids adjacent scene categories where practical, and keeps edited captions and pinned times intact. Default same-clip separation across lanes: one day, editable down to zero.
- Starts with editable posting windows for every weekday. After at least five mature, same-weekday observations in a lane, smoothed engagement per reach guides time selection. Every fifth calendar date explores a window. These are experiments, not a promise of optimal reach.
- Shows a queue, playable footage library, caption/time editor, approve/reject/restore actions, published history, metrics, errors, and a durable activity log.
- Sends ntfy stock, shortage, connection and publishing alerts with delivery retries. Enter a "no new footage until" date in Settings. If alternate-day pacing cannot cover it, Reelflow tells you; it does not invent footage or recycle clips secretly.
- Persists containers before publishing. A timeout or crash at the publish boundary produces a reconciliation hold; it does **not** trigger an automatic duplicate post.

## Run it: one Compose file, all Docker

```sh
git clone https://github.com/palermostest25/reelflow.git
cd reelflow
docker compose up -d --build
```

Open [the local desk](http://localhost:8080), or port 8080 on your Docker host. Get the generated admin password with `docker compose logs web`. You can instead set your own `ADMIN_PASSWORD` in **compose.yaml** before starting.

**compose.yaml is the only deployment configuration.** No host Python installation, setup script, environment files, or external cron service is required. It starts the web UI and posting worker together. Installation keys are created automatically and persist with your database and media in a Docker volume.

For your actual account, edit the marked entries in that file: `PUBLIC_URL`, `META_APP_ID`, `META_APP_SECRET`, and the footage mount's host path. Connect OpenRouter and ntfy through the UI. The app starts paused in dry run; live publishing uses the **official Instagram API**, creating a Reel container with `POST /{ig_id}/media`, letting Instagram fetch the signed video URL, then publishing with `POST /{ig_id}/media_publish`.

An optional HTTPS gateway is also included in the same file. Set `PUBLIC_URL` to your real HTTPS domain, configure DNS and ports 80/443, then run:

```sh
docker compose --profile https up -d --build
```

No Caddyfile or second Compose file is needed. Skip that profile when using an existing reverse proxy. A first-time Meta app setup and eligible professional account are still required by Instagram. See [deployment](docs/DEPLOYMENT.md) for those account steps and TrueNAS storage.

## What is verified

The automated suite exercises the real application and real FFmpeg conversion. It covers authentication/CSRF, encrypted credentials, signed media and Range requests, queue cadence and mixing, rejection persistence, conservation, account expiry, distinct Trial payloads, publishing holds, and saved-container recovery. GitHub Actions also builds the Docker image.

**Live integration is not verified until you connect your Meta app and Instagram account.** Trial availability depends on the account and API permissions. Meta's developer pages were not fully accessible during implementation; the connector uses the documented Graph container/publish flow and a separately tested Trial parameter contract. Validate one regular Reel and one eligible Trial on your account before unattended use. No footage or account credentials are bundled.

This is a single-account, single-admin deployment. Use a separate data directory for each account. It is not a multi-tenant SaaS or an Instagram password/session scraper.

## AI authentication

The implemented sign-in is [OpenRouter's official OAuth PKCE](https://openrouter.ai/docs/guides/overview/auth/oauth). The resulting user-controlled API key is encrypted at rest; inference uses the user's OpenRouter credit. Choose a model with image inputs for visual captions. Exact model IDs are loaded from the provider, rather than assuming an unverified model name.

You can instead set `AI_BASE_URL` and supply a key for an OpenAI-compatible Chat Completions provider. Changing the provider requires restarting the containers. Provider-specific request differences may require an adapter; this connector expects the standard `messages`/`choices` format.

OpenAI now documents [Sign in with ChatGPT plan usage](https://developers.openai.com/siwc/token-sharing-open-source). Its current registration flow uses a `127.0.0.1` loopback listener and its inference flow uses streamed Responses with explicit preview limitations. That separate host-login adapter is **not implemented here**. Reelflow does not reuse Codex or Claude Code session credentials. OpenRouter sign-in/API credentials are the supported unattended path.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
# Optional source development only; deployment uses Docker Compose above.
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080 --no-access-log
.venv/bin/python -m app.worker
.venv/bin/python -m pytest -q
```

Install FFmpeg and FFprobe on the host for local development. Docker includes them. The web server never starts an implicit scheduler; run the worker or cron explicitly. Run only one deployment per local data directory; a process lock prevents overlapping worker/cron passes.

## Architecture

FastAPI serves the interface and authenticated API. Session keys, encryption keys and a generated admin password are initialized under a process lock in the persistent data volume, so both containers and later restarts share the same installation. A separate Python worker handles media, captions, planning, account sync, publishing and notices. SQLite WAL stores application records; processed footage and thumbnails live beside it. Tokens are encrypted with Fernet using a separately retained key. Signed 48-hour, clip-specific media URLs allow Instagram to fetch only the intended MP4, including byte ranges, without exposing the dashboard or library.

See [operations and integration details](docs/OPERATIONS.md) for failure states, data retention and practical limits.
