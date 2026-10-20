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
One regular Reel daily, adaptive alternate-day pacing during a footage drought; every clip receives a Trial Reel, with an assumed cap of three per day pending user clarification. New clips mix into future queue positions. Upcoming posts may be edited, approved or rejected. Published history and measured engagement are visible. No footage, credentials or audience information has been supplied. Trial eligibility must be validated against the connected account. Best-time windows are initially experiments, not claims of measured audience behaviour.
## Open decisions
Confirmed: TRADES is a DJ duo; the app must serve any account and import prior captions on login. Every clip receives one Trial, capped at three daily. Audience location remains unknown. Use the previous account captions and editable business context and Australia/Perth as explicit initial configuration assumptions.
## Product Principles
Automatic routine work; visible reasons and honest states; durable posting history; no blind retry of an ambiguous publish; operator overrides survive replanning.

## Repository
The user explicitly requested publishing the finished source to GitHub. Create a private repository on the authenticated account.
