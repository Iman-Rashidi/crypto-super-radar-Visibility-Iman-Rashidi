# Notion event sync setup (BloFin Demo)

This service stores robot and AI events in the configured Turso database first. A background worker retries Notion delivery. Missing Notion credentials must not block market analysis or risk checks.

## Required Render environment variable

On the Render service **crypto-super-radar-blofin-demo** only:

- Key: `NOTION_API_TOKEN`
- Value: the secret Internal Integration token created in Notion.

Do not put this token in GitHub, source files, issue comments, chat messages, or a public workflow.

## Notion integration access

1. In Notion, open Settings → Connections / Integrations and create an Internal Integration for this project.
2. Grant it read content and insert content permissions (update content is only needed if the integration later updates pages).
3. Open the data source **دفتر رویدادهای عملیاتی ربات و ناظر هوش مصنوعی** and share it with the integration.
4. Also share the activity/trade data source if the same integration will be used there.
5. Copy the integration secret once and keep it private.

## Render configuration

1. Open https://dashboard.render.com/web/srv-dbrl25bbc2fs73cdmov0
2. Open Environment.
3. Add or update `NOTION_API_TOKEN` with the integration secret.
4. Save changes and allow Render to restart the service.

Do not replace the service's other environment variables. Merge/add this one variable only.

## Verification

Read these endpoints after the deploy:

- `/api/notion-sync/status`
- `/api/blofin/demo/autopilot/status`

Expected Notion status after configuration:

- `token_configured: true`
- `storage_backend: TURSO_CLOUD_SQLITE`
- pending decreases as sync succeeds
- synced increases above zero

A successful HTTP response from the status endpoint alone does not prove sync. Verify that the Notion data source contains pages with matching `شناسه رویداد` values and that `وضعیت Sync` is `SYNCED`.

## Safety

- Demo only. Never add Live API keys or withdrawal/transfer permissions.
- Do not run a test order to verify Notion.
- Outbox sync is independent of order submission and must not weaken trade/risk gates.
- Challenge 1 remains NOT PASSED until real Demo analysis, AI review, any eligible Demo execution/fill, durable Turso persistence, and Notion page reconciliation are all evidenced.
