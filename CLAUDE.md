# Reelflow: working notes for Claude

## Always publish the latest version

The owner updates their TrueNAS server from `ghcr.io/palermostest25/reelflow:latest`. That image is only built and published when `main` changes. So **every time a piece of work is finished**:

1. Run the checks: `python -m pytest -q`, `node --check app/static/app.js`, `node --test tests/frontend.test.cjs`.
2. Commit, then push to `main` (fast-forward from the working branch). Do not ask first; this is standing permission from the owner.
3. Watch the "Test and publish Reelflow image" GitHub Actions run until the `publish` job succeeds. If a step hangs for over ~12 minutes, cancel and re-run it. (The FFmpeg install step now times out and retries on its own; it used to stall.) If tests fail, fix them and push again.
4. Tell the owner the new version (`1.0.<run number>`), the short commit shown in the app footer, and the update command:

```bash
sudo midclt call -j app.pull_images reelflow '{"redeploy": true}'
```

Fallback if that method isn't available on their TrueNAS version:

```bash
sudo midclt call -j app.redeploy reelflow
```

Never leave finished work only on a feature branch.
