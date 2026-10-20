# One-file Docker deployment

## Start

You only need Docker with Compose on the host. Save [compose.yaml](https://github.com/palermostest25/reelflow/blob/main/compose.yaml) into a folder and run:

```sh
docker compose up -d
```

Docker pulls the prebuilt GHCR image published by GitHub Actions for AMD64 and ARM64. The local folder needs only compose.yaml, with no source checkout or host Git. The one `compose.yaml` starts the UI and the scheduled posting worker. No host Python installation, setup script, environment files, or system cron entries are needed. Open `http://DOCKER-HOST:8080`. Read your generated admin password with:

```sh
docker compose logs reelflow
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

All deployment settings live in the single service. Recreate after changing them:

```sh
docker compose up -d
```

App preferences, provider keys and ntfy are configured in the web UI. To use a different OpenAI-compatible caption provider, add `AI_BASE_URL` in the same Compose file. No extra configuration file is required.

## Official Instagram API

Publishing is implemented in `app/instagram.py`; it uses Instagram Login OAuth and `graph.instagram.com`. Reelflow calls `POST /{ig_id}/media` with a signed HTTPS video URL and caption, checks the saved container status, then calls `POST /{ig_id}/media_publish`. Instagram fetches the MP4 from your server through its signed, expiring URL. The app does not use browser automation, Instagram password login, or a third-party posting service.

Regular Reels use `share_to_feed=true`. Trials use `trial_params={"graduation_strategy":"MANUAL"}` and `share_to_feed=false`. Trial eligibility is account-dependent; a rejected Trial is held rather than silently converted to a regular Reel.

Meta setup:

1. Create a Meta app with Instagram API and Instagram Login. Use an Instagram Business or Creator account.
2. Register `https://YOUR-ORIGIN/auth/instagram/callback` as the exact OAuth redirect.
3. Enable `instagram_business_basic`, `instagram_business_content_publish`, and `instagram_business_manage_insights`, with the applicable app roles/access/review in Meta's dashboard.
4. Put the app credentials and HTTPS origin in compose.yaml and recreate the container.
5. In Settings, choose **Connect Instagram**, authorize, and return to the desk. The worker imports recent captions and available performance.

Permissions/review requirements can change; confirm them against your Meta app dashboard. The connector targets Graph API v25.0. References: [Meta's official collection](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api), [publishing](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/content-publishing/), [insights](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/insights/).

## Cloudflare Tunnel and HTTPS

The app listens on HTTP port 8080. You do not need TLS certificates inside the container. Configure your own Cloudflare Tunnel with a public HTTPS hostname pointing to `http://DOCKER-HOST:8080`, then set `PUBLIC_URL` to that external HTTPS origin. OAuth redirects, Secure session cookies and signed media links use `PUBLIC_URL`; the app does not depend on proxy headers to decide its external address.

A tunnel running on the Docker host can use `http://localhost:8080`. A tunnel running in another container needs a reachable Docker-host address or a shared network; its `localhost` refers to itself. Only port 8080 is published. No ports 80/443, gateway container, Compose profiles or separate configuration files are included. If you want host-local access only, change the port mapping to `127.0.0.1:8080:8080` and run the tunnel on the host.

Instagram must fetch the signed `/media/*` MP4 URLs without a browser login or Cloudflare Access challenge. Keep those routes reachable; the URLs already require an expiring app-generated signature. The dashboard, previews and library use the app's admin login. Verify a signed URL from outside your LAN before publishing. A VPN-only origin cannot deliver footage to Instagram.

Avoid logging OAuth query strings or signed media URLs in your tunnel/proxy. Large browser uploads may exceed your Cloudflare plan's request limits; the watched footage folder avoids sending those uploads through the tunnel.

## Image builds and updates

GitHub Actions runs the test suite, builds and smoke-tests native AMD64 and ARM64 images, then publishes a combined `latest` tag to `ghcr.io/palermostest25/reelflow`. Only successful builds from `main` publish. Pull requests run checks without publishing. Each release also has an immutable-by-convention `sha-<full-commit>` tag; set that in Compose if you prefer to pin an update.

```sh
docker compose pull
docker compose up -d
```

The named data volume is retained across updates. Take a backup first. To roll back, set `image:` to a previously tested `sha-<full-commit>` tag and recreate. Database compatibility still matters; restore a matching backup when a release changes schema incompatibly.

## TrueNAS footage and data

The container runs as UID/GID 1000. Grant that identity read/traverse access to the watched footage dataset, or add a `user: "UID:GID"` setting to the service to an identity with suitable dataset permissions. If changing the UID/GID on an existing installation, update the application volume's ownership first.

The footage mount is read-only; originals are never modified. Browser uploads, processed videos, thumbnails, the database and installation credentials live in `reelflow-data`, a named Docker volume managed automatically. To use a local TrueNAS application-state dataset instead, replace `reelflow-data:/data` with a host path and make it writable by the container identity. Keep `/data` on local storage, not an SMB/NFS share, because SQLite WAL needs reliable local locking. The footage source can be a mounted share.

You can also upload footage through the UI. Inputs up to 5 GB are accepted; publishable clips must be 3 seconds to 15 minutes. Prepared output is 1080×1920 H.264/AAC with a 1 GB cap, letterboxed to preserve the frame, with source sound retained. Allow room for originals, converted clips and previews.

## Captions, notifications and first posts

Sign in with OpenRouter or paste a compatible provider key in Settings. Load the model catalog and select an image-capable model for visual captions. Your previous captions define the voice; factual operator notes add clip context. Missing or failed captions hold footage for review.

Enter an ntfy server/topic and optional token, save, and send a test. Confirm delivery in Activity. Use an authenticated or unguessable private topic. Set "No new footage until" when you expect a gap.

Check framing, source sound, captions, timezone and the queue in dry run. Then verify one actual regular Reel and one eligible Trial on your account before unattended use. The editor lets you approve, reject, edit or pin a post; approval is optional for routine operation. Turn off dry run and resume once checked.

## Backups

Create a consistent database snapshot inside Docker:

```sh
docker compose exec -T reelflow python scripts/backup.py
```

Back up the complete `reelflow-data` volume, including its hidden `.runtime.json`, processed media, uploads and database snapshots. This hidden internal file contains the encryption key for your saved Instagram and AI tokens; it is app state, not another file you need to configure. Back up source footage separately. Keep your locally configured Compose credentials private. Never use `docker compose down -v` unless you intend to delete the stored application data.

To restore, stop the app container, restore the media and `.runtime.json`, and restore a consistent snapshot to `/data/reelflow.sqlite3`. Remove old `-wal`/`-shm` sidecars only while all database users are stopped. Restart and inspect any unresolved publish jobs.

## Migrating the original setup

Stop the original containers. Preserve the old database/media directory and its original `APP_SECRET`, `TOKEN_KEY` and `ADMIN_PASSWORD`. Bind that same directory to `/data` in the service volumes. Temporarily add those original values to its `environment` mapping in compose.yaml. Start the container once; they persist those credentials internally. You may then remove those three entries and recreate the container. Your old environment file is no longer used. If you choose to keep an explicit admin password in Compose, leave that one entry.

Reelflow refuses to silently generate a replacement key over old encrypted credentials. Do not delete the internal credential state to force a reset.

## Migrating from the earlier web/worker Compose

Run `docker compose down` using the old file first, then replace it with the new one-container file and start normally. Keep the same Compose project name (`reelflow`) and data-volume name so your existing data is reused. Do not add `-v`. Removing the old web/worker containers avoids duplicate schedulers and port conflicts.

## Process lifecycle

The container starts both the web UI and continuous posting worker; no external cron is needed. If either exits, the supervisor stops the other and exits with failure, allowing `restart: unless-stopped` to restart the complete app. A normal stop forwards termination to both processes and their conversion children. Compose gives the supervisor 30 seconds for shutdown.

The UI and `/healthz` expose worker heartbeat status without a Docker healthcheck. A process lock prevents overlapping posting passes. Run only one deployment per data directory.
