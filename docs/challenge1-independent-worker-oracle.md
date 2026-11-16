# Challenge 1: Independent BloFin Demo Worker (Oracle Cloud)

## Scope and safety boundary

This guide prepares a separate process for the existing Challenge 1 / BloFin Demo loop. The worker uses the same crypto_super_radar.web_app execution and safety code, but it does not start the dashboard's HTTP server.

It is not a claim that the service is already running 24/7. Oracle Always Free compute is best-effort capacity, not an uptime SLA. Oracle documents that Always Free VMs can be reclaimed when CPU, network, and (for Ampere A1) memory utilization remain below its idle thresholds during a seven-day period. Availability also depends on the tenancy's home region and available host capacity.

Official references:
- Oracle Always Free compute and idle-instance reclamation: https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm
- Render free-instance limitations: https://render.com/docs/free

## Target architecture

- Render Free Web Service: dashboard/API only, with BLOFIN_DEMO_AUTOPILOT_ROLE=API.
- One independent Linux VM: foreground process python -m crypto_super_radar.blofin_demo_worker, supervised by systemd, with BLOFIN_DEMO_AUTOPILOT_ROLE=WORKER.
- Turso Cloud SQLite: shared durable AutoPilot state and append-only Demo order/event ledger.
- BloFin Demo API only. No production API host or Live account is supported by this worker entry point.

Keep exactly one active worker. Do not enable the new worker while the Render web process is still running its local AutoPilot thread. The safe cutover order is: prepare VM with AutoPilot disabled; change Render's role to API and wait for that deploy to finish; then enable/start the VM worker. Keep the previous Render deployment available for rollback.

## Oracle VM selection

1. Sign in to Oracle Cloud Infrastructure. The Always Free compute capacity must be created in the tenancy's selected home region.
2. Select an Ubuntu ARM64 image and the VM.Standard.A1.Flex Ampere shape.
3. Stay strictly inside the Always Free allowance shown by the Console. Oracle currently documents a shared monthly allowance equivalent to up to 2 OCPUs and 12 GB RAM, with 200 GB total Always Free block-volume storage.
4. Do not select a paid shape, paid disk, paid public service, or upgrade solely to obtain capacity. If the shape is unavailable in the home region, do not substitute a paid resource.
5. Oracle may reclaim an idle Always Free VM. A small trading bot may not consistently exceed Oracle's utilization thresholds, so this option cannot guarantee uninterrupted operation.

## Install the worker on Ubuntu ARM64

Connect to the VM terminal (Oracle Cloud Shell or SSH) and run:

~~~bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv python3-pip build-essential
sudo useradd --system --create-home --shell /usr/sbin/nologin csrworker || true
sudo mkdir -p /opt/crypto-super-radar
sudo chown -R csrworker:csrworker /opt/crypto-super-radar
sudo -u csrworker git clone --branch feature/challenge1-independent-worker \
  https://github.com/Iman-Rashidi/crypto-super-radar-Visibility-Iman-Rashidi.git \
  /opt/crypto-super-radar/app
cd /opt/crypto-super-radar/app
sudo -u csrworker python3 -m venv /opt/crypto-super-radar/venv
sudo -u csrworker /opt/crypto-super-radar/venv/bin/pip install --upgrade pip
sudo -u csrworker /opt/crypto-super-radar/venv/bin/pip install -r requirements.txt
~~~

The source archive rebuild used by Render is not required for this worker because the worker runs from the repository package directly.

## Configure secrets locally on the VM

Create the environment file without posting any secret into a chat, issue, commit, or public repository:

~~~bash
sudo install -m 600 -o root -g root /dev/null /etc/crypto-super-radar-demo.env
sudo nano /etc/crypto-super-radar-demo.env
~~~

Add the following keys using values from the BloFin Demo account and the existing Turso database:

~~~ini
BLOFIN_DEMO_AUTOPILOT_ROLE=WORKER
BLOFIN_DEMO_AUTOPILOT_ENABLED=true
BLOFIN_DEMO_TRADING_ENABLED=true
BLOFIN_DEMO_AUTOPILOT_INSTRUMENTS=BTC-USDT,ETH-USDT,SOL-USDT,XRP-USDT,LINK-USDT
BLOFIN_DEMO_AUTOPILOT_INTERVAL_SECONDS=300

TURSO_DATABASE_URL=
TURSO_AUTH_TOKEN=
BLOFIN_DASHBOARD_TOKEN=

# Prefer the dedicated Demo Trade key group:
BLOFIN_DEMO_TRADE_API_KEY=
BLOFIN_DEMO_TRADE_API_SECRET=
BLOFIN_DEMO_TRADE_PASSPHRASE=

# Optional separate Demo read-key group, if already configured:
# BLOFIN_DEMO_API_KEY=
# BLOFIN_DEMO_API_SECRET=
# BLOFIN_DEMO_PASSPHRASE=

PYTHONUNBUFFERED=1
~~~

Use a BloFin Demo key with Read and Trade permissions only. Transfer/withdrawal permissions must remain disabled. Never copy a production API key into this file. The worker validates that the role is WORKER, both activation flags are true, Turso is configured, the dashboard token exists, and at least one complete Demo credential group exists before starting.

Protect the file:

~~~bash
sudo chmod 600 /etc/crypto-super-radar-demo.env
sudo chown root:root /etc/crypto-super-radar-demo.env
~~~

## systemd service

Create /etc/systemd/system/crypto-super-radar-demo-worker.service:

~~~ini
[Unit]
Description=CRYPTO SUPER RADAR BloFin Demo Challenge 1 Worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=csrworker
Group=csrworker
WorkingDirectory=/opt/crypto-super-radar/app
EnvironmentFile=/etc/crypto-super-radar-demo.env
ExecStart=/opt/crypto-super-radar/venv/bin/python -m crypto_super_radar.blofin_demo_worker
Restart=always
RestartSec=30
TimeoutStopSec=30
KillSignal=SIGTERM
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
~~~

Then install the service but leave it disabled until the Render cutover is complete:

~~~bash
sudo systemctl daemon-reload
sudo systemctl enable crypto-super-radar-demo-worker
sudo systemctl status crypto-super-radar-demo-worker --no-pager
~~~

## Safe cutover

1. Confirm the VM package imports and environment file are present. Keep BLOFIN_DEMO_AUTOPILOT_ENABLED=false during initial installation.
2. In the Render service crypto-super-radar-blofin-demo only, set BLOFIN_DEMO_AUTOPILOT_ROLE=API. Do not change any Production service.
3. Wait until the Render API-only deploy is live. Confirm the web service no longer launches the local AutoPilot thread.
4. Change the VM environment setting to BLOFIN_DEMO_AUTOPILOT_ENABLED=true.
5. Start the service: sudo systemctl start crypto-super-radar-demo-worker.
6. Check sudo journalctl -u crypto-super-radar-demo-worker -n 100 --no-pager and the Render status endpoint. Require a fresh worker heartbeat and execution_profile=CHALLENGE_1.
7. Do not treat a successful process start as proof of a trade. Verify readiness, signal evidence, order reservation, BloFin Demo acceptance, fill, exchange-side TP/SL, close, fees, and realized P/L independently.

## Rollback

Stop the VM worker first:

~~~bash
sudo systemctl stop crypto-super-radar-demo-worker
~~~

Only after it is stopped may the Render service be changed back to BLOFIN_DEMO_AUTOPILOT_ROLE=WEB. Do not run both roles concurrently. The event ledger remains in Turso; never delete or reset it during rollback.

## Acceptance checklist

- [ ] VM is an Always Free eligible A1 shape, and no paid resource was selected.
- [ ] Worker starts only with role WORKER and durable Turso settings.
- [ ] Render dashboard starts with role API and does not run the AutoPilot thread.
- [ ] Only one worker process is active.
- [ ] Five configured symbols are scanned using Strategy 1 only.
- [ ] Readiness blocks orders when key permissions, storage, or risk checks fail.
- [ ] Restart/reboot recovers from the durable event store without duplicate orders.
- [ ] BloFin Demo order lifecycle is proven with exchange-side evidence.
- [ ] Oracle reclamation and GitHub/VM outage risks are recorded; no 24/7 SLA is claimed.
