# Deployment and first connection

## 1. Storage and permissions

Run `python3 scripts/configure.py` on the host. It creates a mode-0600 `.env` with independent session/encryption secrets and your admin password. It refuses to overwrite existing configuration.

For TrueNAS, set `FOOTAGE_PATH` to the host path of the dataset, for example `/mnt/pool/media/trades`, and `DATA_PATH` to a dedicated application-state dataset. Set `PUID`/`PGID` to an account with read access to footage and write access to the data directory. Precreate both directories with the correct ownership. The footage bind mount is read-only; browser uploads go to the data directory. Subfolders are watched recursively; hidden files and symlinks are ignored.

**Keep SQLite application state on the Docker host's local filesystem.** Do not place `DATA_PATH` on an SMB/NFS share; SQLite WAL locking is not suitable there. A TrueNAS local dataset bind-mounted into the container is suitable. The footage folder may be a mounted share.

Allow room for the original upload, a prepared MP4, three sampled frames and a thumbnail per clip. Processed files remain on disk after posting for review and recovery. Inputs up to 5 GB are accepted; publishable clips must be 3 seconds to 15 minutes, and prepared output must remain below 1 GB. Sound is preserved from the source; the app does not add licensed Instagram music or infer track names from audio.

## 2. Public HTTPS

Instagram's servers need a publicly retrievable HTTPS video URL, and OAuth needs a stable callback address. Set `PUBLIC_URL=https://reels.example.com` to your actual origin, without a path or trailing slash. Put a reverse proxy with a valid certificate in front of port 8080. Point your domain at that proxy. The dashboard requires its own admin login; the only public footage endpoint validates a signed, expiring clip URL.

For Caddy running on the same host, a minimal Caddyfile is:

```caddyfile
reels.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

If the proxy runs in another container, attach it to the same Docker network and proxy `web:8080`; do not use its own `127.0.0.1`. Alternatively bind the web port only to a trusted LAN address using `LISTEN_HOST`. Configure your router/firewall and TLS at the proxy. A private-only VPN URL will not work for Instagram media fetching. Configure the proxy's upload limit for your intended file size, up to 5 GB. Keep access logging disabled or redact callback queries and signed media query strings.

`http://localhost:8080` is sufficient to explore dry-run setup. Live publishing stays blocked until a public HTTPS origin and valid Instagram connection are configured.

## 3. Configure a Meta app

1. Create a Meta developer app and add Instagram API with Instagram Login. Use an Instagram **Business or Creator** account. This flow does not require a linked Facebook Page.
2. Configure Instagram business login with this exact redirect URL:
   `https://YOUR-ORIGIN/auth/instagram/callback`
3. Request `instagram_business_basic`, `instagram_business_content_publish`, and `instagram_business_manage_insights`. Enable the required publishing and insights features in the app dashboard. Permissions and review requirements can change; verify them against Meta's current app dashboard.
4. For development-mode testing, add/accept the relevant app/tester roles. For accounts outside your app roles, Meta may require advanced access, app review, business verification and a privacy/data-deletion URL.
5. Put your Instagram app ID and secret in `META_APP_ID` and `META_APP_SECRET` in `.env`. These are the credentials for the configured Instagram login product. Keep `META_API_VERSION` at a supported version; the shipped connector targets `v25.0`.
6. Restart with `docker compose up -d`. In Reelflow Settings, choose **Connect Instagram**, authorize the account and return to the desk.
7. The worker imports up to 100 recent posts and their captions, plus available counts, and attempts insights. Failed insight permissions are shown rather than represented as zeros. Long-lived tokens refresh when less than ten days remain; an expired or revoked connection requires reconnection.

References: [Meta's official Instagram collection](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api), [Instagram Login publishing](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/content-publishing/), [Instagram insights](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/insights/). Meta developer documentation may require a developer login.

## 4. Connect AI and notifications

Choose **Sign in with OpenRouter** in Settings. Authorize the app using your OpenRouter account and credit. The server exchanges the PKCE authorization code and encrypts the resulting key. Load the model catalog and select a model accepting image inputs. Save Settings. Alternatively paste a compatible API key; use `.env` to change the provider base URL.

Previous captions define the voice automatically. Adjust the brand name, account description and guardrails for any business or artist. New media frames and reference captions are sent to your chosen provider for caption generation; disable visual inputs if needed. Clips without valid captions stay out of the posting queue until a provider succeeds or you write one manually in the library.

For ntfy, enter the server and topic, optionally an access token, save, and send a test. Use an authenticated topic or an unguessable private topic. Confirm delivery in Activity. [ntfy publishing docs](https://docs.ntfy.sh/publish/).

## 5. Validate before unattended posting

Keep dry run on while checking timezone, footage framing, source audio, captions, the Trial lane and the next posting times. Enable approval requirements if you want a supervised launch. To validate a live post, use the editor to pin its time a few minutes ahead and approve it; resolve conflicts by moving other flexible posts first. Disable Trials temporarily to validate the regular lane, or reject/hold regular posts to validate the Trial lane separately.

Turn off dry run and resume only after checking the queue. First verify one regular Reel and one Trial on Instagram. Trial posts must appear as Trials to an eligible account. A Trial error is held; the app does not silently convert it to a regular Reel. Keep `MANUAL` Trial graduation so Instagram does not also share additional Trials to followers beyond the regular schedule.

## Cron instead of the continuous worker

The included worker loops every 60 seconds. To use cron, stop the continuous worker and leave only the web service running:

```sh
docker compose up -d web
# Add this to cron, replacing the directory with the checkout location:
* * * * * cd /opt/reelflow && docker compose run --rm -T --no-deps worker python -m app.worker --once >> /opt/reelflow/worker.log 2>&1
```

A filesystem lock makes overlapping passes exit without duplicating work. Do not run a second app instance against the same SQLite database on a different host. Add log rotation for the cron log. The continuous worker is simpler and avoids starting a container every minute.

## Backups and restoration

Create an online consistent database snapshot:

```sh
docker compose exec -T web python scripts/backup.py
```

Back up `data/backups`, all `data/media`, `data/thumbs`, `data/frames`, `data/uploads`, and `.env` to protected storage. You need the original `TOKEN_KEY` to decrypt saved provider tokens. Never commit these files. Back up your watched source folder separately.

To restore, stop both containers, restore the snapshot to `DATA_PATH/reelflow.sqlite3`, and remove stale `reelflow.sqlite3-wal`/`-shm` files only while all database users are stopped. Restore media and original secrets, then restart. Any job recorded at the publish boundary is reconciled or held; inspect uncertain posts before choosing to requeue them.
