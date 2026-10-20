# Operations and reliability

## The posting state machine

A clip moves through `processing → captioning → ready`. Failed preparation is `error`; a missing key or invalid AI result is `needs_caption`. These clips cannot publish. Retry failed preparation from the library after fixing the source. A manual caption can release a prepared clip without AI.

A lane job moves through `queued → processing → publishing → published`. Upload/container failures can retry with exponential backoff, at most five failures before `failed`. The container ID is persisted separately. A lost response or restart at the publish boundary becomes `uncertain`. Reelflow first queries the saved container; `PUBLISHED` resolves success without posting again. If it cannot establish the outcome, the operator checks Instagram and explicitly confirms posted or not posted. This is deliberate duplicate prevention: Instagram does not provide a general exactly-once idempotency contract for this endpoint.

Rejecting a job affects that lane only. Restore rejected jobs from Published → Rejected. Excluding a source clip prevents both unposted lanes from publishing. Pausing is checked again immediately before the publish request; it cannot retract a request Instagram has already accepted. Source/caption edits are refused while the job is processing. Never assume a pause can undo an external action already in flight.

Regular jobs have a 20-hour minimum publish spacing in addition to calendar-day planning. Trial jobs use a daily cap, with a rolling check that prevents a burst of overdue posts. Scheduling windows missed by more than 30 minutes are moved into future flexible slots rather than posted in a catch-up flood. A near-due post is held stable across replanning. Fixed manual times reserve slots; avoid pinning long sequences because fixed times intentionally override adaptive replanning.

## Observability

`/healthz` reports web/database readiness and recent worker heartbeat. Docker has separate web and worker healthchecks. The heartbeat is updated at worker-pass boundaries; long conversion batches may make the UI report the worker as offline temporarily. The container healthcheck allows up to 35 minutes for a conversion pass; inspect logs if preparation is stuck. Docker does not automatically restart a merely unhealthy container; arrange external monitoring if required by your hosting environment.

Activity is the durable application log. Container logs intentionally avoid provider response bodies, query strings, access tokens and signed footage URLs. Publish and ntfy failures are visible. Notification records remain pending on delivery failures and retry at five-minute intervals, bounded per pass. Low-footage alerts deduplicate by UTC day. Subscribe to the selected ntfy topic on your phone.

## Timing and voice learning

The app imports recent captions immediately after connection and again every six hours. It attempts views, reach, likes, comments, saves and shares for recent videos. Metrics unavailable because of permission, age, media type or API changes remain unknown. Available likes/comments can still inform the caption reference.

Time selection uses mature app-origin Reels only, separated by regular versus Trial lane and weekday. Engagement is `(likes + comments + saves + shares) / reach`, capped at 1 and shrunk toward a neutral prior with weight 5. At least five observations on that weekday unlock score-based hour selection; otherwise the configured baseline leads. Every fifth date explores among candidate windows. This is a practical heuristic, not a causal experiment, follower-online model or guarantee of best performance. You can always pin a specific post time.

Caption generation sees three sampled frames, not the whole video/audio. It can miss a moment between frames; factual notes improve results. Previous posts and filenames are treated as reference data, never trusted instructions. A model can still make mistakes. Optional approvals and caption edits are available. AI output is not a substitute for checking rights to source footage/audio.

## Footage conservation

The low-watermark is a count of pending, usable regular posts. Conservation selects alternate calendar days when stock is at or below the threshold, or when pending stock cannot cover a supplied drought date at daily cadence. Trials also slow to one per alternate calendar day. A shortage notice is created if this still cannot cover the period. Posted clips are never silently reused. New clips enter the shuffled flexible queue; a SHA-256 uniqueness constraint stops renamed or repeated source files from producing duplicate lane jobs.

Default lane gap is one day between the same footage appearing as a regular and Trial Reel. The planner assigns regular slots first, then Trial slots respecting that gap; a Trial need not precede its regular Reel. If you want initial Trials sooner, reduce the gap or pin selected slots. Automatic promotion/graduation is not implemented. Each lane publishes its own Reel.

## Security and scope

Single admin, one Instagram account per data directory. Admin sessions expire after twelve hours, use HTTP-only signed cookies, and require CSRF tokens for mutations. HTTPS origins use Secure cookies. Login has an address-based attempt limit. Tokens and OAuth PKCE verifiers are Fernet-encrypted in SQLite; keep `TOKEN_KEY` and backups private. No Instagram password is stored. The browser never receives saved provider keys.

The default port binds only to `127.0.0.1`. A public deployment needs a trusted reverse proxy and TLS. Protect `.env` and application-state ownership, do not enable `DEMO_MODE` publicly, and do not expose the Python server without the intended proxy. Demo mode is only a loopback development preview and blocks provider credentials and live publishing.

The media endpoint uses HMAC-SHA256 with a clip ID and 48-hour expiry. A valid URL grants temporary access to that specific video; anyone possessing it can retrieve it until expiry. Preview images, queue data, and library videos require admin authentication.

SQLite is appropriate for this single-account, one-host scheduler. This release does not include multi-user RBAC, multiple Instagram accounts in one database, cloud object storage, transcoding farms, custom music overlays, guaranteed exactly-once delivery across Meta failures, or fully autonomous resolution of ambiguous publishes. Add those deliberately if the deployment grows.

## Integration acceptance record

Before declaring your installation production-ready, record the real outcome of: Instagram authorization and token refresh; recent-caption import; your account's insight permissions; a public signed video URL fetched outside your LAN; one published regular Reel; one eligible Trial Reel; a delivered ntfy alert; source-folder permissions; and backup restoration. Automated tests cannot prove these account/network-specific conditions.
