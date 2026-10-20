# One-file Docker deployment

## Start

You only need Docker with Compose on the host. Clone the repository and run:

```sh
docker compose up -d --build
```

The one `compose.yaml` starts the UI and the scheduled posting worker. No host Python installation, setup script, environment files, or system cron entries are needed. Open `http://DOCKER-HOST:8080`. Read your generated admin password with:

```sh
docker compose logs web
```

Alternatively put a password of at least twelve characters in `ADMIN_PASSWORD` in compose.yaml before starting. Generated credentials persist in the named data volume; restarts do not regenerate them.

## Your account: edit the marked values in compose.yaml

| Setting | What goes here |
| --- | --- |
| `PUBLIC_URL` | Your reachable HTTPS origin, e.g. `https://reels.example.com`; no path or trailing slash. The localhost default is for exploring dry run. |
| `META_APP_ID` | The app ID for your Meta app's Instagram Login product. |
| `META_APP_SECRET` | The corresponding app secret. Keep your edited Compose file private. |
| Footage volume | Change `./footage:/footage:ro` to your source, e.g. `/mnt/pool/media/trades:/footage:ro`. |
| `ADMIN_PASSWORD` | Optional; blank generates one automatically. |

Settings are defined once in a shared YAML block and reused by both containers. Recreate after changing them:

```sh
docker compose up -d --build
```

App preferences, provider keys and ntfy are configured in the web UI. To use a different OpenAI-compatible caption provider, change `AI_BASE_URL` in the same Compose file. No extra configuration file is required.

## Official Instagram API

Publishing is implemented in `app/instagram.py`; it uses Instagram Login OAuth and `graph.instagram.com`. Reelflow calls `POST /{ig_id}/media` with a signed HTTPS video URL and caption, checks the saved container status, then calls `POST /{ig_id}/media_publish`. Instagram fetches the MP4 from your server through its signed, expiring URL. The app does not use browser automation, Instagram password login, or a third-party posting service.

Regular Reels use `share_to_feed=true`. Trials use `trial_params={"graduation_strategy":"MANUAL"}` and `share_to_feed=false`. Trial eligibility is account-dependent; a rejected Trial is held rather than silently converted to a regular Reel.

Meta setup:

1. Create a Meta app with Instagram API and Instagram Login. Use an Instagram Business or Creator account.
2. Register `https://YOUR-ORIGIN/auth/instagram/callback` as the exact OAuth redirect.
3. Enable `instagram_business_basic`, `instagram_business_content_publish`, and `instagram_business_manage_insights`, with the applicable app roles/access/review in Meta's dashboard.
4. Put the app credentials and HTTPS origin in compose.yaml and recreate the containers.
5. In Settings, choose **Connect Instagram**, authorize, and return to the desk. The worker imports recent captions and available performance.

Permissions/review requirements can change; confirm them against your Meta app dashboard. The connector targets Graph API v25.0. References: [Meta's official collection](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api), [publishing](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/content-publishing/), [insights](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/insights/).

## HTTPS in the same Compose file

Instagram must be able to fetch the signed MP4 from outside your LAN. OAuth also needs a stable public callback. A private VPN-only address will not work for media fetching.

If you already have Nginx Proxy Manager, Traefik, Caddy or another HTTPS proxy, point it at the web service's port 8080 and use the normal start command.

Otherwise the Compose file includes an optional Caddy container:

1. Set the one `PUBLIC_URL` entry to `https://YOUR-DOMAIN`. The same YAML value is reused by Caddy.
2. Point the domain's DNS at your host and forward TCP ports 80/443 (optional UDP 443 for HTTP/3). Ensure those host ports are available; TrueNAS's admin UI may already use them.
3. Start the included gateway:

```sh
docker compose --profile https up -d --build
```

Caddy obtains and renews a certificate for the domain. It uses command-line configuration from the same Compose file: no Caddyfile, environment file, or extra Compose override. Gateway certificate data persists in Docker volumes. Keep request logging disabled or redact OAuth codes and signed media URLs in any existing proxy's logs.

## TrueNAS footage and data

The containers run as UID/GID 1000. Grant that identity read/traverse access to the watched footage dataset, or change the shared `user` setting to an identity with suitable dataset permissions. If changing the UID/GID on an existing installation, update the application volume's ownership first.

The footage mount is read-only; originals are never modified. Browser uploads, processed videos, thumbnails, the database and installation credentials live in `reelflow-data`, a named Docker volume managed automatically. To use a local TrueNAS application-state dataset instead, replace `reelflow-data:/data` with a host path and make it writable by the container identity. Keep `/data` on local storage, not an SMB/NFS share, because SQLite WAL needs reliable local locking. The footage source can be a mounted share.

You can also upload footage through the UI. Inputs up to 5 GB are accepted; publishable clips must be 3 seconds to 15 minutes. Prepared output is 1080×1920 H.264/AAC with a 1 GB cap, letterboxed to preserve the frame, with source sound retained. Allow room for originals, converted clips and previews.

## Captions, notifications and first posts

Sign in with OpenRouter or paste a compatible provider key in Settings. Load the model catalog and select an image-capable model for visual captions. Your previous captions define the voice; factual operator notes add clip context. Missing or failed captions hold footage for review.

Enter an ntfy server/topic and optional token, save, and send a test. Confirm delivery in Activity. Use an authenticated or unguessable private topic. Set "No new footage until" when you expect a gap.

Check framing, source sound, captions, timezone and the queue in dry run. Then verify one actual regular Reel and one eligible Trial on your account before unattended use. The editor lets you approve, reject, edit or pin a post; approval is optional for routine operation. Turn off dry run and resume once checked.

## Backups

Create a consistent database snapshot inside Docker:

```sh
docker compose exec -T web python scripts/backup.py
```

Back up the complete `reelflow-data` volume, including its hidden `.runtime.json`, processed media, uploads and database snapshots. This hidden internal file contains the encryption key for your saved Instagram and AI tokens; it is app state, not another file you need to configure. Back up source footage separately. Keep your locally configured Compose credentials private. Never use `docker compose down -v` unless you intend to delete the stored application data.

To restore, stop both app containers, restore the media and `.runtime.json`, and restore a consistent snapshot to `/data/reelflow.sqlite3`. Remove old `-wal`/`-shm` sidecars only while all database users are stopped. Restart and inspect any unresolved publish jobs.

## Migrating the original setup

Stop the original containers. Preserve the old database/media directory and its original `APP_SECRET`, `TOKEN_KEY` and `ADMIN_PASSWORD`. Bind that same directory to `/data` in the shared Compose volumes block. Temporarily add those original values to the shared `environment` mapping in compose.yaml. Start the containers once; they persist those credentials internally. You may then remove those three entries and recreate the containers. Your old environment file is no longer used. If you choose to keep an explicit admin password in Compose, leave that one entry.

Reelflow refuses to silently generate a replacement key over old encrypted credentials. Do not delete the internal credential state to force a reset.

## Optional cron

The included worker already schedules everything in Docker. If you prefer cron, stop the continuous worker and run `docker compose run --rm -T --no-deps worker python -m app.worker --once` from your scheduler. A process lock prevents overlapping passes. Most installations should use the default continuous worker and no external cron.
