# Render Paper Trading Cycle / چرخه زمان‌بندی‌شده Paper Trading

## Purpose

Run exactly one validated Crypto Super Radar Spot paper-trading cycle per Cron invocation. This is separate from the public web dashboard and does not submit exchange orders or sign blockchain transactions.

## Required persistent storage

Render Cron Jobs do not provide persistent disks. The scheduled entry point therefore refuses to run unless both of these environment variables are configured on the Cron service:

- `TURSO_DATABASE_URL`
- `TURSO_AUTH_TOKEN`

Set the same two values on the `crypto-super-radar` web service if the dashboard must display the same paper account and trade history. Do not paste credentials into Git, Notion, logs, or chat. Add them through the Render Dashboard's Environment page.

The guard intentionally rejects local SQLite. Without this check, each independent Cron container could start with a fresh paper account and produce misleading performance records.

## Proposed Render Cron configuration

Do not create the Cron service until the Turso database exists, the environment variables are configured, and the current Feature branch passes its tests.

- Repository: `https://github.com/Iman-Rashidi/crypto-super-radar-Visibility-Iman-Rashidi`
- Branch: `feature/multi-stage-confirmation-engine`
- Runtime: Python
- Build command: `pip install -r requirements.txt`
- Start command: `python scripts/render_paper_cycle.py --asset cex:BTCUSDT`
- Proposed schedule: `*/15 * * * *` (one cycle every 15 minutes)
- Proposed region: Frankfurt
- Suggested minimum Cron compute plan: `0.5c-512mb`

The selected Cron compute plan is paid usage, not a free Render service. Render's pricing page currently lists Cron compute at $0.00016 per active minute for this plan and a $1/month minimum per Cron service. Actual cost depends on execution duration and usage; verify the current Render billing page before creating the service.

## Validation and safety

- Run `python -m unittest tests.test_render_paper_cycle -v` locally or through CI.
- A cycle must return a structured result. `NO_TRADE` is a valid outcome when evidence does not pass.
- `UNKNOWN`, stale data, incomplete evidence, or any failed mandatory gate must not create a paper position.
- The Cron entry point requires `TURSO_CLOUD_SQLITE` and reports `PAPER_ONLY`, `orders_submitted=false`, and `transactions_signed=false`.
- Do not add live exchange credentials to the Cron service. The scheduled job uses public market data and the paper ledger only.
- The web dashboard and the Cron job should share the same Turso database to display consistent account state.

## Operational boundary

This Cron process performs one scan and exits. It is not a continuous WebSocket worker. Continuous Binance/Bybit Spot order-flow ingestion remains a separate process using `python -m crypto_super_radar.order_flow_service`; its event ledger is not yet a mandatory input to the 12-gate decision engine. Do not claim continuous CVD confirmation until the order-flow data has durable history, freshness/coverage validation, and explicit integration into the signal gates.
