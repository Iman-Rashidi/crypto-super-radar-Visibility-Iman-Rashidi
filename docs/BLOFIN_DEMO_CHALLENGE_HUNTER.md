# BloFin Demo Challenge Hunter

The first implementation is a **read-only, report-only discovery module**.

Run from the repository root:

```bash
python scripts/scan_blofin_demo_challenges.py --output reports/blofin-demo-challenges.json
```

It reads the official BloFin Activity Center and follows only HTTPS links on
BloFin-owned domains. It never logs in, registers for a campaign, accesses
private account information, deposits funds, or submits trades.

## Classification

- `POTENTIALLY_DEMO_ONLY`: explicit Demo evidence and no detected Live/deposit terms.
  This is a candidate for manual terms review, not proof of eligibility.
- `LIVE_REQUIREMENTS_FOUND`: Demo is mentioned, but the campaign also contains
  Live trading, deposit, or taker-volume requirements.
- `NOT_DEMO_CONFIRMED`: no explicit Demo evidence was retrieved.
- Campaign status is `POSSIBLY_ACTIVE`, `ENDED`, or `UNKNOWN`. The scanner
  deliberately avoids inferring active status from incomplete date fragments.

The module is intentionally not wired to the trading executor. It does not
automatically register, follow traders, or execute orders. A later dashboard
integration should display source links, evidence, uncertainty, region
restrictions, registration requirements, reward type, and last scan time.
