# Dashboard design

## Purpose

A Persian-first public-data research dashboard that lets a visitor inspect a small set of cross-checked spot markets, then choose an asset for a separate research scan. The interface must make read-only behavior, data uncertainty, and the manual scan action clear.

## Visual direction

Use a calm dark market-terminal style: deep navy page background, slightly lighter panels, thin blue-gray borders, and restrained teal accents. Reserve green for successful data, amber for UNKNOWN or partial data, and red for failures. Pair color with explicit status text so color is never the only signal.

All colors, spacing, borders, and focus styles are implemented with CSS variables in `web_app.py`. Use the system font stack (Segoe UI, Tahoma, Arial) so Persian text works without an external font service. The whale mark is the existing emoji; no paid or remote imagery is needed.

## Page order

1. **Brand header:** product name, Persian description, and a read-only status pill.
2. **Crypto market board:** selected BTC, ETH, SOL, BNB, XRP and DOGE Spot pairs. Show price, 24-hour volume, cross-source discrepancy, status, and a scan selection action.
3. **Asset scan form:** the market board can prefill the asset field. The visitor starts the scan with the form's separate submit button.
4. **Scenario calculator and research gates:** retain the existing spot calculator and the fail-closed 12-gate presentation.

## Component behavior

- The market board reads the existing free Binance Spot and Bybit Spot adapters. The two sources must validate before a price is shown.
- Results are cached for 60 seconds. Missing data stays UNKNOWN and cannot be selected from the board.
- Selecting a verified coin scrolls to and fills the scan form. It does not submit the form or start a scan.
- The scan form remains available for other supported markets and addresses.
- On small screens, keep data readable with horizontal table scrolling rather than hiding price or status fields.

## Design tokens

| Role | Token | Value |
|---|---|---|
| Page | `--bg` | `#07111f` |
| Main panel | `--surface` | `#0e1b2c` |
| Raised panel | `--surface-raised` | `#14263c` |
| Borders | `--line` | `#24374f` |
| Main text | `--text` | `#edf4fb` |
| Muted text | `--muted` | `#98abc0` |
| Accent | `--teal` | `#54ded0` |
| Primary action | `--blue` | `#72a7ff` |
| Pass | `--green` | `#53d6a0` |
| Unknown | `--amber` | `#ffc66d` |
| Fail | `--red` | `#ff7b85` |

No external design reference was supplied; these tokens are the project's proposed visual system, not extracted brand measurements.
