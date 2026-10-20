# Reelflow

**A self-hosted Instagram posting desk. Drop footage in a folder. Keep it on rotation.**

Built for TRADES, the DJ duo; configurable for any artist, creator, or business. Reelflow imports the connected account's recent captions and available engagement rather than imposing a generic brand voice.

Start with [Docker deployment](#run-it-one-container-one-compose-file), follow the [exact Meta setup](#first-time-setup-meta-and-instagram), then complete [live acceptance and operations](#finish-setup-and-verify-unattended-operation).

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

## Run it: one container, one Compose file

Save [compose.yaml](https://github.com/palermostest25/reelflow/blob/main/compose.yaml) into a folder. Edit `PUBLIC_URL`, `META_APP_ID`, `META_APP_SECRET`, and the footage mount's host path, then run:

```sh
docker compose up -d
```

Docker pulls the prebuilt `ghcr.io/palermostest25/reelflow:latest` image. GitHub Actions builds and tests native AMD64 and ARM64 images before publishing; your server needs no application source, build tools, environment files or external cron.

Open [the local desk](http://localhost:8080), or port 8080 on your Docker host. Get the generated admin password with `docker compose logs reelflow`. Installation keys and your password persist alongside the database and media in the Docker volume. Optionally add `ADMIN_PASSWORD` to the same Compose environment mapping to choose your own password.

The one container runs the web UI and posting worker together. A supervisor stops the container if either process exits; Docker restarts it. Compose exposes only port 8080 and contains no gateway, shared YAML blocks or healthchecks.

**Cloudflare Tunnel:** point your tunnel at `http://DOCKER-HOST:8080` and set `PUBLIC_URL` to its public `https://reels.example.com` address. The container uses HTTP; Cloudflare provides public HTTPS for Instagram login callbacks and signed video downloads. If the tunnel runs in another container, use a reachable host address or shared Docker network rather than that container's `localhost`. Allow Instagram to fetch signed `/media/*` URLs without an interactive Cloudflare Access challenge. The dashboard has its own login.

Connect OpenRouter and ntfy through the UI. The app starts paused in dry run; live publishing uses the **official Instagram API**, creating a Reel container with `POST /{ig_id}/media`, letting Instagram fetch the signed video URL, then publishing with `POST /{ig_id}/media_publish`. A Meta app and eligible professional account are required. See [deployment](docs/DEPLOYMENT.md) for account setup, TrueNAS permissions, updates and backups.

To update:

```sh
docker compose pull
docker compose up -d
```

## First-time setup: Meta and Instagram

Follow these steps for TRADES or another professional account you own/manage. This uses **Instagram API with Instagram Login**. A linked Facebook Page is not required. For your own managed account added in the dashboard, **Standard Access does not require App Review**. Serving other businesses without app roles requires Advanced Access, App Review and Business Verification. Publishing the source on GitHub does not grant Meta permissions or make this a multi-account service. See Meta's [access overview](https://developers.facebook.com/documentation/instagram-platform/overview) and [review scenarios](https://developers.facebook.com/documentation/instagram-platform/app-review).

### 1. Get your public address working

Choose a hostname such as `reels.example.com`. Configure your own Cloudflare Tunnel to send it to `http://DOCKER-HOST:8080`. Use that hostname throughout the following steps, replacing the example everywhere.

In your local `compose.yaml`, set:

```yaml
    environment:
      PUBLIC_URL: "https://reels.example.com"
      META_APP_ID: ""     # Instagram App ID, copied in step 4
      META_APP_SECRET: "" # Instagram App Secret, copied in step 4
```

Keep `PUBLIC_URL` as an origin: no extra path or trailing slash. Open the app through this HTTPS address after configuring it; HTTPS settings produce Secure login cookies, so plain HTTP LAN access will no longer retain your login. The container itself still uses HTTP.

### 2. Prepare the Instagram account and your Meta developer login

- Confirm TRADES is a **public Business or Creator account**. If it is Personal, switch it to a professional account in Instagram's account settings.
- Open the [Meta App Dashboard](https://developers.facebook.com/apps/), sign in with your Facebook developer account, and finish developer registration if prompted. The Facebook account owns the developer app; the later Instagram authorization uses TRADES' Instagram credentials.

### 3. Create the developer app

Meta's [current app creation guide](https://developers.facebook.com/documentation/instagram-platform/create-an-instagram-app) describes this route:

1. Choose **Create App**. For business association, select your portfolio if you have one, or the option to connect one later if offered.
2. Select the **Other** use case, then the **Business** app type.
3. Name the app `Reelflow` and supply a contact email you monitor. Finish creation.
4. On its dashboard, find **Instagram** under available products and choose **Set up**.
5. Open **Instagram → API setup with Instagram login**. Stay on this login configuration for Reelflow.

Dashboard layouts can vary by account. The destination to look for is **API setup with Instagram login**. Do not choose the Facebook Login setup or the retired Basic Display API.

### 4. Configure business login and copy the right credentials

On the Instagram setup page, find **Set up Instagram business login** and choose **Set up** / **Business login settings**.

Save this exact OAuth redirect URI, replacing the hostname:

```text
https://reels.example.com/auth/instagram/callback
```

Check the saved address: the path, HTTPS scheme and trailing slash must match exactly. Reelflow's callback has **no trailing slash**.

Copy the **Instagram App ID** and reveal/copy the **Instagram App Secret** from this Instagram setup/business-login section into `META_APP_ID` and `META_APP_SECRET` in your local Compose file. Despite those environment variable names, they need the **Instagram-specific credentials**, not the parent Meta/Facebook app credentials. Reelflow already implements the login button; you do not need to paste Meta's embed snippet into its source. [Official business-login instructions](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/business-login).

Keep the configured Compose file private. The public repository contains blank placeholders only.

### 5. Add TRADES and enable the permissions Reelflow uses

In the Instagram setup page's account/token section, choose **Add account**, sign in to TRADES, and complete the requested account association. If Meta creates a pending Instagram tester invitation, accept it while signed in to TRADES under Instagram's **Apps and websites → Tester invites** settings. Accounts can also be managed under **App roles → Roles**; confirm the invitation is accepted before attempting Reelflow login. The account should appear as an added account, rather than a pending invite.

Check these permissions in the Instagram setup / **App Review → Permissions and features** area:

| Permission | Reelflow uses it for |
| --- | --- |
| `instagram_business_basic` | Account identity and recent media/caption import |
| `instagram_business_content_publish` | Regular and Trial Reel publishing |
| `instagram_business_manage_insights` | Available media engagement metrics |

The publishing permissions are documented in [Content Publishing](https://developers.facebook.com/documentation/instagram-platform/content-publishing); the insights permission is documented in [Insights](https://developers.facebook.com/documentation/instagram-platform/insights). Reelflow requests all three when you connect. It does not need comment moderation, messaging or Human Agent permissions for its current features.

For this own-account setup, retain Standard Access and the required account roles. You do not need to open the app to everyone to post to TRADES. Development/Standard Access does **not** make publishing a sandbox: authorized live requests can create real posts.

Reelflow obtains and stores its token during login. There is no access-token field to fill in Compose and no need to copy a dashboard-generated token. It also does not use Instagram webhooks: leave webhook configuration out of this deployment, and do not enter the OAuth callback as a webhook URL.

### 6. Restart and connect from Reelflow

Save Compose, including the TrueNAS footage path, then run:

```sh
docker compose up -d
docker compose logs reelflow
```

Use the generated password from the logs to sign in at your public HTTPS hostname. In **Settings**, choose **Connect Instagram**, sign in to TRADES, and approve all requested permissions. Complete the return in the same browser within ten minutes.

Confirm Settings shows the correct Instagram username and that recent captions appear after the worker's next pass. Reelflow exchanges the login code for a long-lived token and attempts refresh when fewer than ten days remain. Meta permits refresh only for valid tokens older than 24 hours; expiration/revocation can require reconnecting. [Token lifecycle](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/business-login).

### 7. If you later offer this to other businesses

Follow Meta's [App Review guide](https://developers.facebook.com/documentation/instagram-platform/app-review), request Advanced Access only for the three permissions used, complete Business Verification, and supply the requested reviewer instructions, test access and recordings demonstrating each permission. Finish the dashboard's publishing requirements before admitting users outside the allowed roles.

Provide genuine privacy and data-deletion information and any required deauthorization handling. This release does **not** include hosted privacy/deletion pages or Meta deauthorization/deletion webhook handlers. Never enter invented URLs, the home page, or the OAuth callback to bypass those fields. Those additional release requirements and a multi-account architecture need implementing before using this as a public service. Your self-hosted TRADES installation remains the own-account path above.

### Meta connection troubleshooting

| What you see | What to check |
| --- | --- |
| Invalid platform app / invalid client ID | Use the Instagram App ID and matching Instagram secret from the Instagram setup page. |
| Redirect URI mismatch | Save exactly `PUBLIC_URL` + `/auth/instagram/callback`, including scheme and slash handling. Recreate the container after editing Compose. |
| Insufficient developer role / app unavailable | Add the target account and accept its pending invitation. Standard Access is limited to the permitted accounts/roles. |
| Invalid scope / missing permission | Confirm the Instagram Login product and the three permissions above. If a documented permission is unavailable for your app, record the exact error and stop setup for investigation; do not substitute a Facebook Login permission. |
| Login state expired / repeated login screen | Start again from the app's HTTPS hostname in the same browser, allow cookies, and finish within ten minutes. |
| Account connects but publishing fails | Check Activity, signed-video reachability, account restrictions and content eligibility. A connected account alone does not prove publishing works. |
| Insights unavailable | Confirm the insights permission was granted; some metrics depend on account size/media eligibility. Unknown metrics remain unknown. |
| Trial fails | Validate Trial eligibility on the account. Reelflow holds the failure and does not turn it into a regular Reel. |

## Finish setup and verify unattended operation

### AI, alerts, footage and schedule

1. In Settings, connect OpenRouter, fund its credits and choose a model supporting image inputs. Alternatively enter a supported provider API key. Check generated captions against a few real clips; generation samples three silent frames and can miss audio/context.
2. Enter your ntfy server, topic and optional access token. Send a test and confirm receipt on the phone subscribed to that topic. Use a private/authenticated topic for operational alerts.
3. Mount the footage folder read-only. The container runs as UID/GID 1000: grant read/traverse access, and keep application storage writable. Keep `/data` on local storage; SQLite WAL should not live on SMB/NFS. Source footage can come from a mounted share.
4. Set the audience timezone and weekday posting windows. Set the low-footage threshold and, when needed, **No new footage until**. Keep the default one regular per day and up to three Trials, with every clip used once in each lane. Timing improves only after sufficient measured history; it is not immediately a proven best time.
5. Leave dry run enabled while checking imports, captions, framing, source audio, rejection and queue mixing. Enable **Require approval** for the pilot and inspect the approval state of existing jobs too.

### Live acceptance: finish this before leaving it unattended

- [ ] Public HTTPS hostname works and the correct account completes OAuth.
- [ ] Historical captions import and permitted engagement metrics load.
- [ ] A signed `/media/*` URL downloads from outside the LAN without an admin login or Cloudflare challenge. The dashboard/library remain protected.
- [ ] A deliberately approved regular Reel publishes and appears correctly in Instagram.
- [ ] A deliberately approved Trial publishes and is identified as a **Trial inside Instagram**, rather than merely being absent from the grid. Meta documents `trial_params.graduation_strategy=MANUAL`; Reelflow does not auto-graduate Trials. [Trial publishing](https://developers.facebook.com/documentation/instagram-platform/content-publishing#trial-reels-posts).
- [ ] ntfy test and low-stock/error alerts reach the subscribed device; footage drought changes the queue's pacing.
- [ ] New footage mixes into flexible positions; denied posts remain denied; edits and pinned times survive replanning.
- [ ] Restarting the deployment retains credentials and queue state. Test network interruptions with disposable footage; reconcile any uncertain publish against Instagram before retrying.
- [ ] Token refresh succeeds when eligible, and connection failures are visible. Initial login alone does not verify refresh.
- [ ] A backup restores successfully into a separate, paused test deployment before any live worker is started there.

To publish the pilot, reject/exclude unwanted queued clips, approve only the intended jobs, disable dry run and resume. Observe several days of real posting and alerts before removing routine approval. A publish already accepted by Instagram cannot be recalled by pausing the app.

### Backups, monitoring and updates

Create a consistent database snapshot:

```sh
docker compose exec -T reelflow python scripts/backup.py
```

Schedule this on your host/NAS and copy backups off the Docker host. Back up the entire persistent data volume, including hidden `/data/.runtime.json` (the token encryption key), media/uploads and database snapshots; back up watched originals separately. A database copy without its original encryption key cannot restore saved connections. Do not use `docker compose down -v` when preserving the installation.

Restore with the app stopped, retaining `.runtime.json` and media and using a consistent snapshot for `reelflow.sqlite3`. Clear stale SQLite sidecars only while all database users are stopped. Keep restored test installations paused and disconnected from live publishing. See [restore details](docs/DEPLOYMENT.md#backups).

Configure an external uptime monitor against your deployment, plus NAS disk-space alerts. A dead host cannot deliver its own ntfy alert. `/healthz` reports `status` and `worker_alive`; check both if your monitor supports JSON assertions. A long conversion pass can temporarily age the heartbeat. Process crashes trigger Docker restart; a hung worker needs investigation.

Back up before updates. Pull and recreate using the commands above; for controlled upgrades, replace `:latest` with a tested `:sha-<full-commit>` tag. Keep the configured Compose file, admin password, Docker logs and backups private. Automated checks cover application logic and container recovery; live account/network checks remain deployment-specific.

## What is verified

The automated suite exercises the real application and real FFmpeg conversion. It covers authentication/CSRF, encrypted credentials, signed media and Range requests, queue cadence and mixing, rejection persistence, conservation, account expiry, distinct Trial payloads, publishing holds, and saved-container recovery. GitHub Actions also builds and starts the Docker image on AMD64 and ARM64, checks login and persistent credentials after a restart, and verifies that a crashed worker triggers a container restart before publishing to GHCR.

**Live integration is not verified until you connect your Meta app and Instagram account.** Trial availability depends on the account and API permissions. The connector follows Meta's documented Graph container/publish flow and Trial parameter contract. The setup above was checked against Meta's current documentation on 20 October 2026; account-specific live acceptance remains outstanding. Validate one regular Reel and one eligible Trial on your account before unattended use. No footage or account credentials are bundled.

This is a single-account, single-admin deployment. Use a separate data directory for each account. It is not a multi-tenant SaaS or an Instagram password/session scraper.

## AI authentication

The implemented sign-in is [OpenRouter's official OAuth PKCE](https://openrouter.ai/docs/guides/overview/auth/oauth). The resulting user-controlled API key is encrypted at rest; inference uses the user's OpenRouter credit. Choose a model with image inputs for visual captions. Exact model IDs are loaded from the provider, rather than assuming an unverified model name.

You can instead set `AI_BASE_URL` and supply a key for an OpenAI-compatible Chat Completions provider. Changing the provider requires restarting the container. Provider-specific request differences may require an adapter; this connector expects the standard `messages`/`choices` format.

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

Install FFmpeg and FFprobe on the host for local development. Docker includes them. For source development, run the web and worker commands in separate terminals; the Docker launcher starts both automatically. Run only one deployment per local data directory; a process lock prevents overlapping worker/cron passes.

## Architecture

FastAPI serves the interface and authenticated API. Session keys, encryption keys and a generated admin password are initialized under a process lock in the persistent data volume, so both processes and later restarts share the same installation. The supervised Python worker handles media, captions, planning, account sync, publishing and notices. SQLite WAL stores application records; processed footage and thumbnails live beside it. Tokens are encrypted with Fernet using a separately retained key. Signed 48-hour, clip-specific media URLs allow Instagram to fetch only the intended MP4, including byte ranges, without exposing the dashboard or library.

See [operations and integration details](docs/OPERATIONS.md) for failure states, data retention and practical limits.
