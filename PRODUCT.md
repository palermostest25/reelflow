# Product
<!-- impeccable:product-schema 1 -->
## Platform
web
## Stack
Delegated by the user's request to set up the whole app: Python, FastAPI, SQLite, plain browser JavaScript, Docker Compose. Local Docker deployment is required, not Sites hosting.
## Users
TRADES, a DJ duo, wants daily Instagram content without daily manual operation. The operator drops footage into a TrueNAS folder and occasionally reviews the queue.
## Product Purpose
Turn a folder of real footage into captions and scheduled regular and Trial Reels, with visibility, control and low-footage alerts.
## Operating Context
Self hosted, durable local database and processed files, bind-mounted footage, unattended worker or cron. Instagram login for a professional account. ntfy notifications.
## Capabilities and Constraints
Default one grid Reel and up to five Trial Reels daily, confirmed on 24 October 2026; both limits (0–10), preview days (1–90), posting window, timing learning and conservation cadence/counts are editable in the UI. Every clip receives one job per destination. The day-by-day preview shows the persisted schedule before publishing, including empty days and held jobs. Custom caption instructions in Settings steer all new captions and rewrites. New clips mix into future queue positions. Upcoming posts may be edited, approved or rejected. Published history and measured engagement are visible. No footage, credentials or audience information has been supplied. Trial eligibility must be validated against the connected account. Best-time windows are initially experiments, not claims of measured audience behaviour.
## Open decisions
Confirmed: TRADES is a DJ duo; the app must serve any account and import prior captions on login. Every clip receives one Trial, now capped at five daily by default; updates retain existing saved limits until edited in the UI. Audience location remains unknown. Use the previous account captions and editable business context and Australia/Perth as explicit initial configuration assumptions.
## Product Principles
Automatic routine work; visible reasons and honest states; durable posting history; no blind retry of an ambiguous publish; operator overrides survive replanning.

## Repository
The user explicitly requested publishing the finished source to GitHub. The repository is public at https://github.com/palermostest25/reelflow, as explicitly requested by the user.

## Deployment commitment
One compose.yaml configures and runs the whole app in Docker. No host Python setup or separate environment files. Installation keys and the optional generated admin password persist internally in the application data volume. Publishing uses the official Instagram API.

## Container delivery
GitHub Actions builds and publishes ghcr.io/palermostest25/reelflow. Compose pulls that prebuilt image and runs one container with the UI and worker supervised together. No build directive, x-common, Compose healthchecks, Caddy, or TLS ports. The operator manages a Cloudflare Tunnel; PUBLIC_URL is its HTTPS origin, forwarded to the app over HTTP on port 8080.
