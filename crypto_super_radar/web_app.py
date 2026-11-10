"""Dependency-light Persian web dashboard for on-demand research scans."""
from __future__ import annotations

import html
import hashlib
import json
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .integrated_engine import WhaleAwareConfirmationEngine
from .indicators import observed_price_ranges
from .models import AssetSpec
from .paper_trading import PAPER_ASSETS, PaperTradingBot
from .market_intelligence import search_dex_tokens
from .providers import _ohlcv_frame

ENGINE = WhaleAwareConfirmationEngine()
STORE = ENGINE.whale_store
PAPER_BOT = PaperTradingBot(ENGINE)
JOBS: dict[str, dict] = {}
ACTIVE_SCAN_KEYS: dict[str, str] = {}
JOB_FENCE_COUNTER = 0
JOB_LEASE_SECONDS = 1800
LOCK = threading.RLock()
SCAN_RUNNING = False
SCAN_QUEUE = queue.Queue(maxsize=50)
SCAN_WORKER_STARTED = False

COMMON_ASSET_NAMES = {
    "BTC": ("Bitcoin", "بیت‌کوین"), "ETH": ("Ethereum", "اتریوم"),
    "XRP": ("XRP / Ripple", "ریپل"), "SOL": ("Solana", "سولانا"),
    "BNB": ("BNB", "بی‌ان‌بی"), "DOGE": ("Dogecoin", "دوج‌کوین"),
    "ADA": ("Cardano", "کاردانو"), "AVAX": ("Avalanche", "آوالانچ"),
    "LINK": ("Chainlink", "چین‌لینک"), "DOT": ("Polkadot", "پولکادات"),
    "LTC": ("Litecoin", "لایت‌کوین"), "TRX": ("TRON", "ترون"),
    "TON": ("Toncoin", "تون‌کوین"), "PEPE": ("Pepe", "پپه"),
    "SHIB": ("Shiba Inu", "شیبا اینو"),
}
ASSET_NAME_ALIASES = {
    "BITCOIN": "BTCUSDT", "ETHEREUM": "ETHUSDT", "RIPPLE": "XRPUSDT",
    "SOLANA": "SOLUSDT", "DOGECOIN": "DOGEUSDT", "CARDANO": "ADAUSDT",
    "AVALANCHE": "AVAXUSDT", "CHAINLINK": "LINKUSDT", "POLKADOT": "DOTUSDT",
    "LITECOIN": "LTCUSDT", "TRON": "TRXUSDT", "TONCOIN": "TONUSDT",
}
TIMEFRAME_WINDOWS = (("1h", 1), ("6h", 6), ("12h", 12), ("1d", 24), ("1w", 168), ("1mo", 720))

# Small, explicitly curated public spot board; it is not a market-cap ranking.
CRYPTO_MARKET_UNIVERSE = (
    ("BTCUSDT", "Bitcoin", "بیت‌کوین"),
    ("ETHUSDT", "Ethereum", "اتریوم"),
    ("SOLUSDT", "Solana", "سولانا"),
    ("BNBUSDT", "BNB", "بی‌ان‌بی"),
    ("XRPUSDT", "XRP", "ریپل"),
    ("DOGEUSDT", "Dogecoin", "دوج‌کوین"),
    ("ADAUSDT", "Cardano", "کاردانو"),
    ("AVAXUSDT", "Avalanche", "آوالانچ"),
    ("LINKUSDT", "Chainlink", "چین‌لینک"),
    ("DOTUSDT", "Polkadot", "پولکادات"),
    ("LTCUSDT", "Litecoin", "لایت‌کوین"),
    ("TRXUSDT", "TRON", "ترون"),
    ("TONUSDT", "Toncoin", "تون‌کوین"),
    ("PEPEUSDT", "Pepe", "پپه"),
    ("SHIBUSDT", "Shiba Inu", "شیبا اینو"),
    ("BCHUSDT", "Bitcoin Cash", "بیت‌کوین کش"),
    ("SUIUSDT", "Sui", "سویی"),
    ("HBARUSDT", "Hedera", "هدرا"),
    ("NEARUSDT", "NEAR Protocol", "نیر"),
    ("UNIUSDT", "Uniswap", "یونی‌سواپ"),
)
CRYPTO_MARKET_CACHE_TTL_SECONDS = 60
CRYPTO_MARKET_CACHE = {"expires_at": 0.0, "payload": None}
CRYPTO_MARKET_FETCH_LOCK = threading.Lock()


def _page(body: str, title: str = "CRYPTO SUPER RADAR") -> bytes:
    return f"""<!doctype html>
<html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root{{color-scheme:dark;--bg:#07111f;--surface:#0e1b2c;--surface-raised:#14263c;--line:#24374f;--text:#edf4fb;--muted:#98abc0;--teal:#54ded0;--blue:#72a7ff;--green:#53d6a0;--amber:#ffc66d;--red:#ff7b85;--radius:18px}}
*{{box-sizing:border-box}}
body{{font-family:"Segoe UI",Tahoma,Arial,sans-serif;background:radial-gradient(ellipse at 82% -10%,#14334a 0,transparent 34%),var(--bg);color:var(--text);margin:0;padding:32px 20px 48px;line-height:1.65}}
main{{max-width:1120px;margin:auto}}
.app-header{{display:flex;align-items:center;gap:16px;margin:0 0 22px;padding:8px 2px 18px;border-bottom:1px solid var(--line)}}
.brand-mark{{display:grid;place-items:center;width:54px;height:54px;border:1px solid #2a5263;border-radius:17px;background:linear-gradient(145deg,#153445,#102338);font-size:27px;box-shadow:0 10px 30px #0003}}
.brand-copy{{min-width:0;flex:1}}
.brand-copy h1{{margin:1px 0 0;font-size:clamp(1.3rem,3vw,1.85rem);letter-spacing:.035em;line-height:1.25}}
.brand-copy p{{margin:5px 0 0}}
.eyebrow{{color:var(--teal);font-size:.78rem;font-weight:700;letter-spacing:.04em}}
.mode-pill{{display:inline-flex;align-items:center;gap:8px;padding:7px 11px;border:1px solid #2e5360;border-radius:999px;background:#102a34;color:#a8f2e6;font-size:.82rem;white-space:nowrap}}
.mode-dot{{width:8px;height:8px;border-radius:50%;background:var(--teal);box-shadow:0 0 12px #54ded080}}
.card{{background:linear-gradient(145deg,#101e30,#0d1929);border:1px solid var(--line);border-radius:var(--radius);padding:22px;margin:16px 0;box-shadow:0 14px 34px #0002}}
.card h2{{margin:0 0 6px;font-size:1.25rem;letter-spacing:-.01em}}
.card h3{{margin-top:22px}}
.muted{{color:var(--muted)}}
input,button,select{{font:inherit;padding:12px 14px;border-radius:11px;border:1px solid #334a63;background:#091522;color:var(--text);width:100%;box-sizing:border-box}}
input:focus{{outline:2px solid #54ded080;border-color:var(--teal)}}
.market-search-activity{{display:none;align-items:center;gap:10px;margin:10px 2px 0;padding:8px 11px;border:1px solid #24556a;border-radius:14px;background:linear-gradient(100deg,#0c2634,#102c39);color:#9feee4;font-size:.84rem;font-weight:650;overflow:hidden}}
.market-search-activity.is-searching,.market-search-activity.is-found{{display:flex}}
.market-search-dog{{display:block;flex:0 0 82px;width:82px;height:58px;overflow:visible}}
.market-search-dog .shiba-body{{transform-box:fill-box;transform-origin:center;animation:shiba-sniff-body .48s ease-in-out infinite alternate}}
.market-search-dog .shiba-head{{transform-box:fill-box;transform-origin:72% 78%;animation:shiba-sniff-head .34s ease-in-out infinite alternate}}
.market-search-dog .shiba-tail{{transform-box:fill-box;transform-origin:10% 90%;animation:shiba-tail-wag .22s ease-in-out infinite alternate}}
.market-search-dog .shiba-leg-front{{transform-box:fill-box;transform-origin:50% 0;animation:shiba-paw .3s ease-in-out infinite alternate}}
.market-search-dog .shiba-leg-back{{transform-box:fill-box;transform-origin:50% 0;animation:shiba-paw .3s ease-in-out .12s infinite alternate}}
.market-search-treasure{{font-size:25px;opacity:0;transform:scale(.35) rotate(-18deg);transition:opacity .22s ease,transform .3s cubic-bezier(.2,1.5,.5,1)}}
.market-search-activity.is-found .market-search-treasure{{opacity:1;transform:scale(1) rotate(8deg);animation:shiba-treasure-pop .55s ease-in-out infinite alternate}}
.market-search-activity.is-found .market-search-dog .shiba-body{{animation:shiba-found-bounce .38s ease-in-out infinite alternate}}
.market-search-activity.is-found .market-search-dog .shiba-head{{animation:shiba-found-look .6s ease-in-out infinite alternate}}
.market-search-activity.is-found .market-search-dog .shiba-tail{{animation:shiba-tail-wag .1s ease-in-out infinite alternate}}
.market-search-found-copy{{display:none}}
.market-search-activity.is-found .market-search-searching-copy{{display:none}}
.market-search-activity.is-found .market-search-found-copy{{display:inline}}
@keyframes shiba-sniff-body{{from{{transform:translateY(1px)}}to{{transform:translateY(-2px)}}}}
@keyframes shiba-sniff-head{{from{{transform:rotate(5deg) translateY(2px)}}to{{transform:rotate(-8deg) translateY(5px)}}}}
@keyframes shiba-tail-wag{{from{{transform:rotate(-20deg)}}to{{transform:rotate(24deg)}}}}
@keyframes shiba-paw{{from{{transform:rotate(-9deg) translateY(0)}}to{{transform:rotate(10deg) translateY(2px)}}}}
@keyframes shiba-found-bounce{{from{{transform:translateY(0) scale(1)}}to{{transform:translateY(-5px) scale(1.025)}}}}
@keyframes shiba-found-look{{from{{transform:rotate(-12deg)}}to{{transform:rotate(7deg)}}}}
@keyframes shiba-treasure-pop{{from{{filter:drop-shadow(0 0 2px #ffc857)}}to{{filter:drop-shadow(0 0 10px #ffc857)}}}}
@media(prefers-reduced-motion:reduce){{.market-search-dog *,.market-search-treasure{{animation:none!important;transition:none!important}}}}
label{{display:block;margin-top:12px;font-size:.94rem;font-weight:600}}
button{{background:linear-gradient(135deg,#3179e8,#2862c7);border:0;margin-top:12px;cursor:pointer;font-weight:700;transition:filter .15s ease,transform .15s ease}}
button:hover{{filter:brightness(1.12);transform:translateY(-1px)}}
button:focus-visible{{outline:3px solid #54ded080;outline-offset:2px}}
button:disabled{{opacity:.45;cursor:not-allowed;transform:none}}
button.market-scan-button{{width:auto;min-width:132px;margin:0;padding:8px 12px;font-size:.88rem;border:1px solid #315b74;background:#123344;color:#b9f5ed}}
button.market-scan-button:hover{{background:#174355}}
#crypto-market-status{{padding:9px 12px;border:1px solid #1f3448;border-radius:10px;background:#0a1725;font-size:.88rem}}
table{{width:100%;border-collapse:collapse;font-size:.93rem}}
th{{color:var(--muted);font-size:.79rem;font-weight:650;background:#0a1725;white-space:nowrap}}
td,th{{padding:12px 10px;border-bottom:1px solid #203149;text-align:right;vertical-align:middle}}
tbody tr:hover{{background:#ffffff06}}
.numeric{{direction:ltr;text-align:left;font-variant-numeric:tabular-nums;white-space:nowrap}}
.status{{font-weight:bold}} .status-pass{{color:var(--green)}} .status-unknown{{color:var(--amber)}}
code{{direction:ltr;display:inline-block}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#08101d;padding:12px;border-radius:10px;direction:ltr;text-align:left}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}
.analysis-links-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-top:16px}}
.analysis-link-card{{display:flex;flex-direction:column;gap:8px;padding:18px;border:1px solid #2b4660;border-radius:15px;background:linear-gradient(145deg,#12283a,#0b1a2a);min-width:0}}
.analysis-link-card h3{{margin:0;font-size:1.08rem}}
.analysis-link-card p{{margin:0;color:var(--muted);font-size:.9rem}}
.analysis-link{{display:inline-flex;align-items:center;justify-content:center;gap:8px;margin-top:6px;padding:11px 14px;border:1px solid #37627b;border-radius:11px;background:#123344;color:#c8fff4;text-decoration:none;font-weight:700}}
.analysis-link:hover{{background:#19465a}}
@media(max-width:680px){{.analysis-links-grid{{grid-template-columns:1fr}}}}
.metric{{background:#0a1725;border:1px solid #263a53;border-radius:13px;padding:14px}}
.metric strong{{display:block;font-size:1.2rem;margin-top:6px;direction:ltr;text-align:right;font-variant-numeric:tabular-nums}}
canvas{{width:100%;height:300px;background:#08101d;border-radius:12px}}
.calc-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}}
@media(max-width:680px){{body{{padding:20px 12px 32px}}.app-header{{gap:11px;align-items:flex-start}}.brand-mark{{width:44px;height:44px;border-radius:14px;font-size:23px}}.mode-pill{{font-size:.72rem;padding:5px 8px}}.card{{padding:17px 14px;border-radius:15px}}table{{font-size:.84rem}}td,th{{padding:10px 8px}}button.market-scan-button{{min-width:116px;padding:8px;font-size:.8rem}}}}
</style></head><body><main>{body}<script>
(function(){
  const source=document.getElementById('crypto-market-search-activity');
  if(!source)return;
  const selector='input[type="search"], input[data-search], input[id*="search"], input[id*="query"], input[placeholder*="جست"]';
  const attach=input=>{
    if(input.id==='crypto-market-search'||input.dataset.shibaAttached==='1')return;
    input.dataset.shibaAttached='1';
    const panel=source.cloneNode(true);
    panel.removeAttribute('id');
    panel.classList.remove('is-searching','is-found');
    panel.classList.add('global-search-activity');
    panel.querySelector('.market-search-searching-copy').textContent='شیبا داره اینجا رو می‌گرده... 🐾';
    panel.querySelector('.market-search-found-copy').textContent='پیداش کرد! گنج اینجاست! 🎉';
    const svg=panel.querySelector('svg');
    if(svg)svg.setAttribute('aria-label','شیبا در حال جست‌وجوی نتیجه');
    const host=input.parentElement;
    if(host&&host.tagName==='LABEL')input.insertAdjacentElement('afterend',panel);
    else if(host)host.insertAdjacentElement('afterend',panel);
    let timer=null;
    const show=()=>{
      const term=(input.value||'').trim();
      if(!term){panel.classList.remove('is-searching','is-found');return;}
      panel.classList.add('is-searching');panel.classList.remove('is-found');
      if(timer)clearTimeout(timer);
      timer=setTimeout(()=>{
        panel.classList.remove('is-searching');
        const id=input.id==='dex-token-query'?'dex-search-results':input.dataset.results;
        const resultBox=id?document.getElementById(id):null;
        if(term.length>=2&&resultBox&&resultBox.children.length)panel.classList.add('is-found');
      },500);
    };
    input.addEventListener('input',show);
    input.addEventListener('search',show);
    input.addEventListener('keydown',event=>{if(event.key==='Enter')show();});
    show();
  };
  document.querySelectorAll(selector).forEach(attach);
  new MutationObserver(records=>records.forEach(record=>record.addedNodes.forEach(node=>{
    if(node.nodeType!==1)return;
    if(node.matches&&node.matches(selector))attach(node);
    node.querySelectorAll&&node.querySelectorAll(selector).forEach(attach);
  }))).observe(document.body,{childList:true,subtree:true});
})();
</script></main></body></html>""".encode("utf-8")


def _parse_scan_asset(value: str) -> AssetSpec:
    """Accept simple coin names/tickers as USDT spot pairs; keep explicit DEX contracts."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("نام ارز یا نماد را وارد کن؛ نمونه: XRP یا BTCUSDT.")
    if ":" in raw:
        return AssetSpec.parse(raw)
    compact = re.sub(r"[\s/_-]+", "", raw).upper()
    if not compact.isalnum() or len(compact) > 24:
        raise ValueError("نماد ارز معتبر نیست. نمونه: XRP، XRPUSDT یا eth:آدرس‌قرارداد.")
    if compact in ASSET_NAME_ALIASES:
        compact = ASSET_NAME_ALIASES[compact]
    else:
        quote_suffixes = ("USDT", "USDC", "FDUSD", "BUSD", "TUSD", "BTC", "ETH", "EUR")
        # A bare ticker such as BTC or ETH is a base asset, not a quote suffix.
        has_quote = any(
            len(compact) > len(quote) and compact.endswith(quote)
            for quote in quote_suffixes
        )
        if not has_quote:
            compact += "USDT"
    return AssetSpec.parse("cex:" + compact)


def _coin_display(asset: AssetSpec, market_intelligence: dict) -> dict:
    dex = (market_intelligence or {}).get("dex_screener") or {}
    raw_symbol = (asset.symbol or "").upper()
    for quote in ("USDT", "USDC", "FDUSD", "BUSD", "TUSD", "EUR", "BTC", "ETH"):
        if raw_symbol.endswith(quote) and len(raw_symbol) > len(quote):
            raw_symbol = raw_symbol[:-len(quote)]
            break
    symbol = dex.get("token_symbol") or raw_symbol or asset.address or asset.raw
    names = COMMON_ASSET_NAMES.get(str(symbol).upper(), (str(dex.get("token_name") or symbol), str(dex.get("token_name") or symbol)))
    name_en = dex.get("token_name") or names[0]
    name_fa = names[1]
    if asset.market_type == "dex" and dex.get("token_name"):
        name_en = dex["token_name"]
        name_fa = dex["token_name"]
    return {"symbol": symbol, "name_en": name_en, "name_fa": name_fa}


def _timeframe_ranges(asset: AssetSpec, snapshot=None) -> dict:
    """Fetch actual 1h candles and derive truthful rolling high/low windows."""
    empty = {
        label: {"status": "UNKNOWN", "high": None, "low": None, "bar_count": 0,
                "expected_bars": hours, "coverage_pct": 0.0, "age_hours": None}
        for label, hours in TIMEFRAME_WINDOWS
    }
    try:
        if asset.market_type == "cex":
            frame = ENGINE.data.binance.candles(asset.symbol, interval="1h", limit=750)
            source = "Binance Spot 1h candles"
        elif asset.market_type == "perp":
            provider = ENGINE.data.perpetual
            rows = provider.http.get(
                f"{provider.BINANCE}/fapi/v1/klines",
                {"symbol": asset.symbol, "interval": "1h", "limit": 750},
            )
            frame = _ohlcv_frame(rows)
            source = "Binance USD-M perpetual 1h candles"
        elif asset.market_type == "dex":
            snapshot = snapshot or getattr(ENGINE.data, "snapshots", {}).get(asset.raw)
            if snapshot is None or not snapshot.pool_address:
                raise ValueError("No validated DEX pool is available for timeframe candles.")
            frame = ENGINE.data.gecko.candles(
                asset.chain, snapshot.pool_address, token_address=asset.address,
                timeframe="hour", aggregate=1, limit=750,
            )
            source = "GeckoTerminal pool 1h candles"
        else:
            raise ValueError("Unsupported asset market type.")
        if frame is None or frame.empty:
            raise ValueError("The provider returned no 1h candles.")
        if getattr(frame.index, "tz", None) is None:
            frame = frame.copy()
            frame.index = frame.index.tz_localize(timezone.utc)
        # Exclude the still-forming current hour so high/low values use closed candles only.
        closed_before = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        frame = frame.loc[frame.index < closed_before]
        if frame.empty:
            raise ValueError("No completed 1h candles are available yet.")
        required = {"high", "low"}
        if not required.issubset(frame.columns):
            raise ValueError("The provider returned an incomplete OHLC schema.")
        latest = frame.index.max()
        if hasattr(latest, "to_pydatetime"):
            latest = latest.to_pydatetime()
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        age_hours = max(0.0, (datetime.now(timezone.utc) - latest).total_seconds() / 3600.0)
        output = {}
        for label, hours in TIMEFRAME_WINDOWS:
            subset = frame.tail(min(hours, len(frame)))
            count = len(subset)
            coverage = min(100.0, count / float(hours) * 100.0)
            status = "KNOWN" if coverage >= 95.0 else "PARTIAL"
            output[label] = {
                "status": status,
                "high": float(subset["high"].max()) if count else None,
                "low": float(subset["low"].min()) if count else None,
                "bar_count": count,
                "expected_bars": hours,
                "coverage_pct": round(coverage, 2),
                "age_hours": round(age_hours, 2),
                "source": source,
                "window_hours": hours,
                "note": None if status == "KNOWN" else f"فقط {coverage:.1f}% از کندل‌های این بازه دریافت شده است.",
            }
        return output
    except Exception as exc:
        reason = f"{type(exc).__name__}: {str(exc)[:160]}"
        return {label: {**row, "reason": reason} for label, row in empty.items()}


def _build_user_diagnosis(result, asset: AssetSpec, timeframe_ranges: dict, market_overview: dict) -> dict:
    """Turn engine output into a plain-language, gate-backed decision summary."""
    mi = result.metrics.get("market_intelligence") or {}
    coin = _coin_display(asset, mi)
    gates = list(result.gates or [])
    failed = [g for g in gates if getattr(g.status, "value", g.status) == "FAIL"]
    unknown = [g for g in gates if getattr(g.status, "value", g.status) == "UNKNOWN"]
    signal = getattr(result.signal, "value", str(result.signal))
    direction = "BUY" if signal == "LONG" else "SELL" if signal == "SHORT" else "NEUTRAL"
    analysis_incomplete = bool(unknown) or market_overview.get("current_price") is None
    if analysis_incomplete:
        direction = "ANALYSIS_INCOMPLETE"
    thresholds = getattr(getattr(ENGINE, "config", None), "thresholds", None)
    min_confirmation = float(getattr(thresholds, "min_confirmation_score", 75.0))
    max_risk = float(getattr(thresholds, "max_estimated_risk_score", 45.0))
    confirmation = result.confirmation_score
    risk = result.estimated_risk_score
    all_pass = len(gates) >= 12 and all(
        getattr(g.status, "value", g.status) == "PASS" for g in gates
    )
    score_pass = confirmation is not None and float(confirmation) >= min_confirmation
    risk_pass = risk is not None and float(risk) < max_risk
    tradable = direction in {"BUY", "SELL"} and all_pass and score_pass and risk_pass
    buy_allowed = direction == "BUY" and tradable

    gate_reasons = []
    for gate in failed + unknown:
        status = getattr(gate.status, "value", gate.status)
        gate_reasons.append({
            "id": gate.gate_id,
            "name": gate.name_fa,
            "status": status,
            "reason": gate.reason_fa,
        })
    if not score_pass:
        score_status = "NOT_COMPUTABLE" if confirmation is None else "FAIL"
        gate_reasons.append({
            "id": None, "name": "امتیاز نهایی",
            "status": score_status,
            "reason": f"امتیاز تأیید {confirmation if confirmation is not None else 'قابل محاسبه نیست'} است؛ حداقل لازم {min_confirmation:g} است.",
        })
    if not risk_pass:
        risk_status = "NOT_COMPUTABLE" if risk is None else "FAIL"
        gate_reasons.append({
            "id": None, "name": "ریسک معامله",
            "status": risk_status,
            "reason": f"ریسک {risk if risk is not None else 'قابل محاسبه نیست'} است؛ سقف مجاز کمتر از {max_risk:g} است.",
        })
    if direction == "SELL" and tradable:
        gate_reasons.append({
            "id": None, "name": "جهت بازار",
            "status": "PASS",
            "reason": "جهت تأییدشدهٔ موتور فروش است؛ بنابراین خرید تازه توصیه نمی‌شود و فقط کاهش موقعیت موجود قابل بررسی است.",
        })
    elif direction == "NEUTRAL" and not gate_reasons:
        gate_reasons.append({
            "id": None, "name": "جهت بازار",
            "status": "UNKNOWN",
            "reason": result.summary_fa or "جهت خرید یا فروش مشخص نشده است؛ فعلاً صبر کن.",
        })
    elif analysis_incomplete and not gate_reasons:
        gate_reasons.append({
            "id": None, "name": "کفایت داده",
            "status": "UNKNOWN",
            "reason": "قیمت معتبر یا شواهد کافی برای تکمیل تحلیل در دسترس نیست.",
        })
    elif not gate_reasons:
        gate_reasons.append({
            "id": None, "name": "وضعیت فعلی",
            "status": "PASS",
            "reason": "همهٔ فیلترهای اجباری و حدود امتیاز تأیید و ریسک رعایت شده‌اند.",
        })

    if buy_allowed:
        short_action = "خرید کوتاه‌مدت قابل بررسی است؛ ورود فقط با حدضرر و اندازه موقعیت کنترل‌شده."
        long_action = "خرید پله‌ای و نگهداری بلندمدت قابل بررسی است؛ این اسکن ارزش بنیادی یا تضمین سود را تأیید نمی‌کند."
        final_title = "خرید قابل بررسی است"
    elif analysis_incomplete:
        short_action = "تحلیل ناقص است؛ تا دریافت قیمت و شواهد معتبر، هیچ تصمیم معاملاتی صادر نمی‌شود."
        long_action = "دادهٔ تاریخی یا Snapshot معتبر کافی نیست؛ ارزیابی بلندمدت نیز متوقف است."
        final_title = "تحلیل ناقص است؛ تصمیم‌گیری متوقف شد"
    elif direction == "SELL" and tradable:
        short_action = "فروش یا کاهش موقعیت کوتاه‌مدت قابل بررسی است؛ در بازار Spot این به معنی فروش دارایی موجود است، نه فروش استقراضی."
        long_action = "برای خرید و نگهداری بلندمدت فعلاً صبر کن؛ روند و داده‌های فعلی از خرید تازه پشتیبانی نمی‌کنند."
        final_title = "خرید توصیه نمی‌شود؛ فشار فروش تأیید شده است"
    else:
        short_action = "فعلاً صبر؛ خرید یا فروش فوری تأیید نشده است."
        long_action = "فعلاً خرید بلندمدت و نگهداری جدید توصیه نمی‌شود؛ ابتدا روند و آزمون‌های آماری باید تأیید شوند."
        final_title = "فعلاً خرید توصیه نمی‌شود"

    short_reasons = []
    for gate_id in (1, 2, 3, 4, 5, 7, 10):
        gate = next((g for g in gates if g.gate_id == gate_id), None)
        if gate is not None:
            status = getattr(gate.status, "value", gate.status)
            short_reasons.append(f"{gate.name_fa}: {gate.reason_fa} (وضعیت: {status})")
    if not short_reasons:
        short_reasons = [result.summary_fa]

    long_reasons = []
    for gate_id in (2, 3, 6, 8, 12):
        gate = next((g for g in gates if g.gate_id == gate_id), None)
        if gate is not None:
            status = getattr(gate.status, "value", gate.status)
            long_reasons.append(f"{gate.name_fa}: {gate.reason_fa} (وضعیت: {status})")
    month = timeframe_ranges.get("1mo") or {}
    week = timeframe_ranges.get("1w") or {}
    if month.get("status") != "KNOWN":
        long_reasons.append("دادهٔ یک‌ماهه کامل نیست؛ کف و سقف این بازه با پوشش ناقص نمایش داده می‌شود.")
    if week.get("status") != "KNOWN":
        long_reasons.append("دادهٔ یک‌هفته کامل نیست؛ برای تصمیم بلندمدت به تاریخچهٔ بیشتری نیاز است.")
    if not long_reasons:
        long_reasons = ["روند، ریسک و اعتبارسنجی زمانی بررسی شده‌اند."]

    age_hours = market_overview.get("candle_age_hours")
    if age_hours is None:
        candle_age_text = "سن کندل روزانه در دسترس نیست."
    else:
        candle_age_text = (
            f"آخرین کندل روزانه حدود {float(age_hours):.1f} ساعت پیش ثبت شده است. "
            "برای تصمیم کوتاه‌مدت، دادهٔ ۱ساعتهٔ تازه‌تر ملاک این جدول است."
        )
    return {
        "coin_name": coin["name_fa"],
        "coin_name_en": coin["name_en"],
        "symbol": coin["symbol"],
        "overall_signal": direction,
        "analysis_status": "INCOMPLETE" if analysis_incomplete else "COMPLETE",
        "overall_signal_fa": {"BUY": "خرید", "SELL": "فروش", "NEUTRAL": "خنثی",
                              "ANALYSIS_INCOMPLETE": "تحلیل ناقص · داده معتبر کافی نیست"}[direction],
        "short_term": {"action_fa": short_action, "reasons": short_reasons},
        "long_term": {"action_fa": long_action, "reasons": long_reasons},
        "final_recommendation": final_title,
        "buy_recommended": buy_allowed,
        "score_status": "NOT_COMPUTABLE" if confirmation is None else ("PASS" if score_pass else "FAIL"),
        "risk_status": "NOT_COMPUTABLE" if risk is None else ("PASS" if risk_pass else "FAIL"),
        "final_reasons": [row["reason"] for row in gate_reasons],
        "blockers": gate_reasons,
        "candle_age_text": candle_age_text,
        "explanation": "نتیجه فقط بر پایهٔ فیلترهای همین اسکن است؛ در وضعیت خنثی یا با فیلتر ردشده، سیستم ورود را تأیید نمی‌کند. این خروجی سفارش واقعی ارسال نمی‌کند.",
    }


def _run_scan(job_id: str, asset: AssetSpec, headline: str, social_post: str) -> None:
    global SCAN_RUNNING
    try:
        result = ENGINE.analyze(
            asset,
            headlines=[headline] if headline else [],
            social_posts=[social_post] if social_post else [],
        )
        candle_frame = getattr(ENGINE.data, "candle_frames", {}).get(asset.raw)
        range_evidence = observed_price_ranges(candle_frame)
        trend_gate = next((gate for gate in result.gates if gate.gate_id == 3), None)
        snapshot = getattr(ENGINE.data, "snapshots", {}).get(asset.raw)
        timeframe_ranges = _timeframe_ranges(asset, snapshot)
        market_intelligence = result.metrics.get("market_intelligence", {}) or {}
        coin_display = _coin_display(asset, market_intelligence)
        regime_gate = next((gate for gate in result.gates if gate.gate_id == 2), None)
        candle_records = []
        if candle_frame is not None and not candle_frame.empty:
            required_ohlc = {"open", "high", "low", "close"}
            if required_ohlc.issubset(candle_frame.columns):
                for timestamp, row in candle_frame.tail(120).iterrows():
                    values = [float(row[key]) for key in ("open", "high", "low", "close")]
                    if all(value > 0 for value in values):
                        candle_records.append({
                            "timestamp": timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp),
                            "open": values[0], "high": values[1], "low": values[2], "close": values[3],
                        })
        payload = {
            "safety": {
                "read_only": True,
                "public_data_only": True,
                "wallet_credentials_used": False,
                "private_exchange_credentials_used": False,
                "orders_submitted": False,
                "transactions_signed": False,
            },
            "asset": asset.raw,
            "coin_name": coin_display["name_fa"],
            "coin_name_en": coin_display["name_en"],
            "symbol": coin_display["symbol"],
            "timeframe_ranges": timeframe_ranges,
            "diagnosis": _build_user_diagnosis(result, asset, timeframe_ranges, range_evidence),
            "market_overview": {
                "status": range_evidence.get("status", "UNKNOWN"),
                "reason": range_evidence.get("reason"),
                "current_price": range_evidence.get("current_price"),
                "candle_age_hours": range_evidence.get("candle_age_hours"),
                "ranges": range_evidence.get("windows", {}),
                "candles": candle_records,
                "chart_pattern": result.metrics.get("chart_pattern"),
                "asset_regime": (trend_gate.evidence or {}).get("asset_regime") if trend_gate else None,
                "benchmark_regime": (regime_gate.evidence or {}).get("regime") if regime_gate else None,
            },
            "signal": result.signal.value,
            "confirmation_score": result.confirmation_score,
            "estimated_risk_score": result.estimated_risk_score,
            "risk_score": result.metrics.get("risk_score"),
            "opportunity_rank": result.opportunity_rank,
            "confidence_level_pct": result.metrics.get("confidence_level_pct"),
            "evidence_confidence_pct": result.metrics.get("evidence_confidence_pct"),
            "summary_fa": result.summary_fa,
            "market_intelligence": result.metrics.get("market_intelligence", {}),
            "gates": [
                {"id": gate.gate_id, "name": gate.name_fa, "name_en": gate.name_en,
                 "status": gate.status.value, "reason": gate.reason_fa,
                 "reason_en": gate.reason_en, "evidence": gate.evidence}
                for gate in result.gates
            ],
            "metrics": {
                key: result.metrics.get(key) for key in (
                    "whale_status", "whale_bias", "whale_history_count",
                    "whale_recent_history_bias", "whale_alignment", "sentiment_alignment",
                    "market_sentiment_score", "fear_greed_value", "fear_greed_attribution",
                    "walk_forward_status", "walk_forward_fold_count", "risk_score",
                    "opportunity_rank", "confidence_level_pct", "evidence_confidence_pct",
                    "elliott_wave",
                )
            },
        }
        with LOCK:
            JOBS[job_id] = {"status": "completed", "result": payload}
    except Exception as exc:
        with LOCK:
            JOBS[job_id] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        with LOCK:
            SCAN_RUNNING = False



def _run_paper_tick(job_id: str, asset: AssetSpec) -> None:
    global SCAN_RUNNING
    try:
        result = PAPER_BOT.run_once(asset)
        payload = {
            "safety": {
                "read_only": True,
                "paper_only": True,
                "wallet_credentials_used": False,
                "private_exchange_credentials_used": False,
                "orders_submitted": False,
                "transactions_signed": False,
            },
            "cycle": result,
            "paper_state": PAPER_BOT.state(),
        }
        with LOCK:
            JOBS[job_id] = {"status": "completed", "result": payload}
    except Exception as exc:
        with LOCK:
            JOBS[job_id] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        with LOCK:
            SCAN_RUNNING = False


def _crypto_market_board() -> dict:
    """Return a short-lived, free public spot-market board cross-checked by both venues."""
    with CRYPTO_MARKET_FETCH_LOCK:
        now = time.monotonic()
        cached = CRYPTO_MARKET_CACHE["payload"]
        if cached is not None and CRYPTO_MARKET_CACHE["expires_at"] > now:
            return cached

        items = []
        for symbol, name_en, name_fa in CRYPTO_MARKET_UNIVERSE:
            try:
                asset = AssetSpec.parse(f"cex:{symbol}")
                snapshot = ENGINE.data.snapshot(asset)
                items.append({
                    "symbol": symbol,
                    "name_en": name_en,
                    "name_fa": name_fa,
                    "status": "PASS",
                    "price_usd": snapshot.price_usd,
                    "volume_24h_usd": snapshot.volume_24h_usd,
                    "price_discrepancy_pct": snapshot.price_discrepancy_pct,
                    "providers": [snapshot.provider_a, snapshot.provider_b],
                    "received_at_utc": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as exc:
                items.append({
                    "symbol": symbol,
                    "name_en": name_en,
                    "name_fa": name_fa,
                    "status": "UNKNOWN",
                    "reason": f"{type(exc).__name__}: {str(exc)[:180]}",
                })

        payload = {
            "status": "PASS" if all(item["status"] == "PASS" for item in items) else "PARTIAL",
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "cache_ttl_seconds": CRYPTO_MARKET_CACHE_TTL_SECONDS,
            "sources": ["Binance Spot", "Bybit Spot"],
            "items": items,
        }
        CRYPTO_MARKET_CACHE["payload"] = payload
        CRYPTO_MARKET_CACHE["expires_at"] = time.monotonic() + CRYPTO_MARKET_CACHE_TTL_SECONDS
        return payload


def _scan_queue_worker():
    """Run stateful scan and paper-bot jobs serially with lease and fencing checks."""
    global SCAN_RUNNING
    while True:
        task = SCAN_QUEUE.get()
        job_id = task["job_id"]
        try:
            with LOCK:
                SCAN_RUNNING = True
                if job_id in JOBS:
                    current = JOBS[job_id]
                    expired = False
                    lease_text = current.get("valid_until_utc")
                    if lease_text:
                        try:
                            expired = datetime.now(timezone.utc) >= datetime.fromisoformat(lease_text)
                        except ValueError:
                            expired = True
                    if current.get("fencing_token") != task.get("fencing_token"):
                        current["status"] = "superseded"
                    elif expired:
                        current["status"] = "expired"
                        current["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
                    else:
                        current["status"] = "running"
                        current["worker_started_at_utc"] = datetime.now(timezone.utc).isoformat()
            with LOCK:
                current = JOBS.get(job_id) or {}
                should_run = (
                    current.get("status") == "running"
                    and current.get("fencing_token") == task.get("fencing_token")
                )
            if should_run:
                if task["route"] == "/paper-bot/tick":
                    _run_paper_tick(job_id, task["asset"])
                else:
                    _run_scan(job_id, task["asset"], task["headline"], task["social_post"])
        except Exception as exc:
            with LOCK:
                JOBS[job_id] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        finally:
            with LOCK:
                SCAN_RUNNING = False
                key = task.get("idempotency_key")
                if key and ACTIVE_SCAN_KEYS.get(key) == job_id:
                    ACTIVE_SCAN_KEYS.pop(key, None)
            SCAN_QUEUE.task_done()


def _ensure_scan_worker():
    global SCAN_WORKER_STARTED
    with LOCK:
        if SCAN_WORKER_STARTED:
            return
        threading.Thread(target=_scan_queue_worker, daemon=True, name="crypto-scan-queue").start()
        SCAN_WORKER_STARTED = True


class Handler(BaseHTTPRequestHandler):
    server_version = "CryptoSuperRadar/0.2"

    def _send(self, status: int, content: bytes, content_type: str = "text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            health = {"status": "ok", "commit": os.getenv("RENDER_GIT_COMMIT") or None}
            return self._send(200, json.dumps(health, separators=(",", ":")).encode(), "application/json")
        if parsed.path == "/api/dex-search":
            query = parse_qs(parsed.query).get("q", [""])[0]
            payload = search_dex_tokens(getattr(ENGINE.data, "http", None), query)
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/api/crypto-markets":
            payload = _crypto_market_board()
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if parsed.path.startswith("/jobs/"):
            job_id = parsed.path.rsplit("/", 1)[-1]
            with LOCK:
                job = JOBS.get(job_id)
            if job is None:
                return self._send(404, b'{"error":"job not found"}', "application/json")
            return self._send(200, json.dumps(job, ensure_ascii=False, default=str).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/paper-bot/status":
            payload = PAPER_BOT.state()
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/history":
            asset = parse_qs(parsed.query).get("asset", [""])[0]
            try:
                AssetSpec.parse(asset)
                data = STORE.summary(asset)
                return self._send(200, json.dumps(data, ensure_ascii=False, default=str).encode(),
                                  "application/json; charset=utf-8")
            except Exception:
                return self._send(400, b'{"error":"invalid asset"}', "application/json")
        if parsed.path != "/":
            return self._send(404, b"Not found", "text/plain; charset=utf-8")
        body = """
        <header class="app-header">
          <div class="brand-mark" aria-hidden="true">🐋</div>
          <div class="brand-copy">
            <span class="eyebrow">پایش بازار · دادهٔ عمومی و رایگان</span>
            <h1>CRYPTO SUPER RADAR</h1>
            <p class="muted">تحلیل کمی و Paper Research؛ بدون اتصال کیف‌پول یا ارسال سفارش</p>
          </div>
          <span class="mode-pill"><span class="mode-dot"></span>فقط خواندنی</span>
        </header>
        <section class="card" id="external-analysis-tools">
          <h2>ابزارهای تحلیل بازار</h2>
          <p class="muted">دو منبع تحلیلی را هم‌زمان و در کنار داشبورد اصلی باز کنید؛ هرکدام در زبانهٔ جداگانه باز می‌شود.</p>
          <div class="analysis-links-grid">
            <article class="analysis-link-card">
              <h3>CoinGlass · مشتقات و لیکوییدیشن</h3>
              <p>Open Interest، Funding Rate، نسبت Long/Short و نقشهٔ لیکوییدیشن‌ها.</p>
              <a class="analysis-link" href="https://www.coinglass.com/" target="_blank" rel="noopener noreferrer">باز کردن CoinGlass ↗</a>
            </article>
            <article class="analysis-link-card">
              <h3>DEX Screener · بازارهای غیرمتمرکز</h3>
              <p>قیمت، حجم، نقدینگی، جفت‌ارزها و فعالیت توکن‌های DEX.</p>
              <a class="analysis-link" href="https://dexscreener.com/" target="_blank" rel="noopener noreferrer">باز کردن DEX Screener ↗</a>
            </article>
          </div>
        </section>
        <section class="card" id="crypto-markets">
          <h2>بازار رمزارزها</h2>
          <p class="muted">قیمت‌های Spot منتخب از APIهای عمومی و رایگان Binance و Bybit؛ نرخ‌ها فقط زمانی نمایش داده می‌شوند که هر دو منبع پاسخ معتبر بدهند. رتبه‌بندی ارزش بازار نیست.</p>
          <p id="crypto-market-status" class="muted" aria-live="polite">در حال دریافت دادهٔ عمومی بازار...</p>
          <div class="grid" style="margin:12px 0">
            <label>جست‌وجوی نام یا نماد
              <input id="crypto-market-search" type="search" placeholder="مثلاً BTC، بیت‌کوین یا SUI" autocomplete="off">
              <span id="crypto-market-search-activity" class="market-search-activity" role="status" aria-live="polite">
                <svg class="market-search-dog" viewBox="0 0 120 82" role="img" aria-label="سگ شیبا در حال بو کشیدن و جست‌وجو">
                  <g class="shiba-body">
                    <path class="shiba-tail" d="M25 37 C3 35 8 5 27 9 C43 12 36 29 24 25" fill="none" stroke="#d9782d" stroke-width="11" stroke-linecap="round"/>
                    <path d="M26 44 Q36 26 68 34 Q91 39 91 57 Q83 72 48 67 Q23 65 26 44" fill="#d9782d" stroke="#8c451f" stroke-width="2"/>
                    <path d="M43 55 Q62 65 83 56 Q81 70 57 70 Q42 67 43 55" fill="#fff0d7"/>
                    <g class="shiba-leg-back"><path d="M36 59 L32 73 Q31 78 43 77 L49 74 L48 64" fill="#d9782d" stroke="#8c451f" stroke-width="1.5"/><path d="M31 74 Q39 71 48 75 Q49 80 35 80 Q28 79 31 74" fill="#fff0d7"/></g>
                    <g class="shiba-leg-front"><path d="M72 57 L70 73 Q69 78 82 77 L88 74 L84 59" fill="#d9782d" stroke="#8c451f" stroke-width="1.5"/><path d="M69 74 Q78 71 87 75 Q89 80 75 80 Q67 79 69 74" fill="#fff0d7"/></g>
                  </g>
                  <g class="shiba-head">
                    <path d="M72 39 L74 9 Q77 2 86 15 L91 39" fill="#d9782d" stroke="#8c451f" stroke-width="2"/>
                    <path d="M76 15 L78 31 L86 22" fill="#f8c7a0"/>
                    <path d="M92 36 L104 12 Q111 7 113 23 L109 47" fill="#d9782d" stroke="#8c451f" stroke-width="2"/>
                    <path d="M101 23 L108 18 L107 37" fill="#f8c7a0"/>
                    <path d="M69 35 Q80 20 102 34 Q117 45 107 60 Q96 72 76 61 Q62 53 69 35" fill="#e9893b" stroke="#8c451f" stroke-width="2"/>
                    <path d="M71 49 Q80 39 91 49 Q101 55 111 47 Q113 62 98 66 Q79 67 71 49" fill="#fff0d7"/>
                    <ellipse cx="104" cy="49" rx="3" ry="2.5" fill="#241b18"/>
                    <path d="M108 57 Q112 60 115 56" fill="none" stroke="#40251d" stroke-width="2" stroke-linecap="round"/>
                    <circle cx="87" cy="40" r="2.5" fill="#241b18"/>
                    <path d="M74 30 L88 26 L101 31" fill="none" stroke="#ffc36b" stroke-width="2" stroke-linecap="round"/>
                  </g>
                </svg>
                <span class="market-search-searching-copy">شیبا داره بو می‌کشه... چیزی پیدا می‌کنه؟ 🐾</span>
                <span class="market-search-found-copy">پیداش کرد! گنج اینجاست! 🎉</span>
                <span class="market-search-treasure" aria-hidden="true">💰</span>
              </span>
            </label>
            <label>مرتب‌سازی
              <select id="crypto-market-sort">
                <option value="volume">بیشترین حجم ۲۴ساعته</option>
                <option value="name">نام رمزارز</option>
                <option value="symbol">نماد</option>
              </select>
            </label>
          </div>
          <p id="crypto-market-count" class="muted">۰ رمزارز</p>
          <div style="overflow-x:auto"><table>
            <thead><tr><th>رمزارز</th><th>نماد</th><th>قیمت مرجع (USD)</th><th>حجم ۲۴ساعته (USD)</th><th>اختلاف منابع</th><th>وضعیت</th><th>اقدام</th></tr></thead>
            <tbody id="crypto-market-rows"><tr><td colspan="7">در حال بارگذاری...</td></tr></tbody>
          </table></div>
          <button type="button" id="crypto-market-refresh">به‌روزرسانی بازار</button>
          <p class="muted">برای آماده‌کردن اسکن، «انتخاب برای اسکن» را بزنید؛ اجرای اسکن فقط با دکمهٔ فرم انجام می‌شود. به‌روزرسانی سمت سرور هر ۶۰ ثانیه cache می‌شود. این بخش هیچ API پولی یا کلید API نمی‌خواهد؛ محدودیت منطقه‌ای و قطع سرویس عمومی ممکن است بعضی ردیف‌ها را UNKNOWN کند.</p>
        </section>
        <script>
        (function(){
          const status=document.getElementById('crypto-market-status');
          const rows=document.getElementById('crypto-market-rows');
          const button=document.getElementById('crypto-market-refresh');
          const search=document.getElementById('crypto-market-search');
          const sort=document.getElementById('crypto-market-sort');
          const count=document.getElementById('crypto-market-count');
          const searchActivity=document.getElementById('crypto-market-search-activity');
          let marketItems=[];
          let searchTimer=null;
          const fmt=(value,max=8)=>Number(value).toLocaleString(undefined,{maximumFractionDigits:max});
          function cell(tag,value){const node=document.createElement(tag);node.textContent=value??'—';return node;}
          function renderRows(){
            const query=search.value.trim().toLocaleLowerCase();
            const visible=marketItems.filter(item=>
              (item.name_fa+' '+item.name_en+' '+item.symbol).toLocaleLowerCase().includes(query)
            );
            visible.sort((a,b)=>{
              if(sort.value==='name')return (a.name_en||'').localeCompare(b.name_en||'');
              if(sort.value==='symbol')return (a.symbol||'').localeCompare(b.symbol||'');
              return (Number(b.volume_24h_usd)||-1)-(Number(a.volume_24h_usd)||-1);
            });
            const tableRows=visible.map(item=>{
              const tr=document.createElement('tr');
              const values=[item.name_fa+' · '+item.name_en,item.symbol,
                item.status==='PASS'?fmt(item.price_usd):'—',
                item.status==='PASS'?fmt(item.volume_24h_usd,2):'—',
                item.status==='PASS'?fmt(item.price_discrepancy_pct,4)+'%':'—',
                item.status==='PASS'?'تأیید دو منبع':'نامشخص · UNKNOWN'];
              values.forEach((value,index)=>{const itemCell=cell('td',value);if(index>=2&&index<=4)itemCell.className='numeric';tr.appendChild(itemCell);});
              const actionCell=document.createElement('td');
              const scanButton=document.createElement('button');
              scanButton.type='button';scanButton.className='market-scan-button';
              scanButton.textContent='انتخاب برای اسکن';
              scanButton.disabled=item.status!=='PASS';
              scanButton.addEventListener('click',()=>{
                const input=document.getElementById('scan-asset');
                input.value='cex:'+item.symbol;
                document.getElementById('scan-section').scrollIntoView({behavior:'smooth',block:'start'});
                input.focus();
              });
              actionCell.appendChild(scanButton);tr.appendChild(actionCell);
              if(item.status!=='PASS')tr.className='status-unknown';
              return tr;
            });
            rows.replaceChildren(...tableRows);
            count.textContent='نمایش '+visible.length+' از '+marketItems.length+' رمزارز';
            searchActivity.classList.toggle('is-found',query.length>0&&visible.length>0);
            if(!visible.length){const empty=cell('td','موردی با این جست‌وجو پیدا نشد');empty.colSpan=7;const tr=document.createElement('tr');tr.appendChild(empty);rows.replaceChildren(tr);}
          }
          async function refresh(){
            button.disabled=true;status.textContent='در حال دریافت قیمت‌های عمومی...';
            try{
              const response=await fetch('/api/crypto-markets');
              const data=await response.json();
              if(!response.ok)throw new Error('HTTP '+response.status);
              marketItems=data.items||[];
              renderRows();
              const received=new Date(data.updated_at_utc).toLocaleString();
              status.textContent='منابع: '+(data.sources||[]).join(' و ')+' · دریافت: '+received+
                ' · وضعیت کلی: '+data.status+' · cache: '+data.cache_ttl_seconds+' ثانیه';
            }catch(error){
              status.textContent='دادهٔ بازار در دسترس نیست: '+String(error.message||error);
            }finally{button.disabled=false;}
          }
          search.addEventListener('input',()=>{
            searchActivity.classList.add('is-searching');
            if(searchTimer!==null)clearTimeout(searchTimer);
            searchTimer=setTimeout(()=>{
              renderRows();
              searchActivity.classList.remove('is-searching');
              searchTimer=null;
            },650);
          });
          sort.addEventListener('change',renderRows);
          button.addEventListener('click',refresh);refresh();
        })();
        </script>
        <section class="card" id="paper-bot">
          <h2>ربات معامله‌گر آزمایشی (Paper Trading)</h2>
          <p class="muted">ربات فقط LONG در بازار Spot را با همان ۱۲ فیلتر ارزیابی می‌کند؛ هر بار یک چرخه اجرا می‌شود. ریسک هر موقعیت حداکثر ۰٫۵٪ سرمایه، ارزش موقعیت حداکثر ۱۰٪، بدون اهرم و بدون سفارش واقعی است. کارمزد و لغزش اعمال می‌شوند؛ حد سربه‌سر متحرک، خروج زمان‌دار، سقف زیان روزانه، افت سرمایه و وقفه پس از زیان هم ورودهای تازه را کنترل می‌کنند.</p>
          <form method="post" action="/paper-bot/tick">
            <label>دارایی</label>
            <select name="asset">
              <option value="cex:BTCUSDT">BTC / USDT</option>
              <option value="cex:ETHUSDT">ETH / USDT</option>
              <option value="cex:SOLUSDT">SOL / USDT</option>
            </select>
            <button type="submit">اجرای یک چرخهٔ ربات آزمایشی</button>
          </form>
          <div id="paper-bot-status" class="muted" style="margin-top:12px" aria-live="polite">در حال دریافت وضعیت حساب آزمایشی...</div>
          <p class="muted">برای اجرای دوره‌ای رایگان روی رایانهٔ خود: <code>python scripts/paper_trading_bot.py --asset cex:BTCUSDT --interval-seconds 900</code>. فایل SQLite محلی است؛ سرویس رایگان Render ممکن است هنگام خواب یا راه‌اندازی مجدد فایل‌های محلی را پاک کند.</p>
        </section>
        <script>
        (async function(){
          const box=document.getElementById('paper-bot-status');
          try{
            const r=await fetch('/paper-bot/status');const s=await r.json();
            const a=s.account||{};
            box.textContent='حالت: Paper فقط · موجودی نقدی: '+Number(a.cash_balance_usd||0).toFixed(2)+' USD · ارزش کل حساب: '+Number(a.equity_usd||0).toFixed(2)+' USD · سود/زیان تحقق‌یافته: '+Number(a.realized_pnl_usd||0).toFixed(2)+' USD · موقعیت باز: '+((s.open_position||{}).asset||'ندارد')+' · ترمز ریسک: '+(s.risk_controls&&s.risk_controls.entry_allowed?'فعال':'متوقف')+' · زیان روز: '+Number((s.risk_controls||{}).daily_realized_loss_pct||0).toFixed(3)+'%';
          }catch(e){box.textContent='وضعیت حساب آزمایشی در دسترس نیست.';}
        })();
        </script>
        <section class="card" id="scan-section">
          <h2>اسکن دارایی</h2>
          <div style="margin:12px 0 18px;padding:14px;border:1px solid #2b4660;border-radius:12px;background:#0a1725">
            <label for="dex-token-query">جست‌وجوی نام یا نماد آلت‌کوین در DEX</label>
            <div style="display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px">
              <input id="dex-token-query" placeholder="مثلاً PEPE یا BONK">
              <button id="dex-token-search" type="button" style="width:auto">جست‌وجو</button>
            </div>
            <p class="muted">توکن را با نام، نماد، شبکه و آدرس قرارداد انتخاب کن. شبکه‌های جست‌وجو: Ethereum، Base و Solana.</p>
            <div id="dex-search-status" class="muted" aria-live="polite">نمادهای تکراری ممکن است قراردادهای متفاوت داشته باشند.</div>
            <div id="dex-search-results"></div>
          </div>
          <form method="post" action="/scan">
            <label>بازار و نماد / آدرس</label>
            <input id="scan-asset" name="asset" required value="BTC" placeholder="XRP، BTCUSDT یا eth:آدرس‌قرارداد">
            <p class="muted">نمونه‌ها: XRP، BTC، XRPUSDT · برای توکن DEX آدرس قرارداد را با شبکه وارد کن؛ مثل base:0x... یا solana:...</p>
            <label>تیتر خبری تکمیلی (اختیاری)</label>
            <input name="headline" placeholder="تیتر خبر">
            <label>متن اجتماعی تکمیلی (اختیاری)</label>
            <input name="social_post" placeholder="متن عمومی شبکه اجتماعی">
            <button type="submit">اجرای اسکن پژوهشی</button>
          </form>
        </section>
        <script>
        (function(){
          const q=document.getElementById('dex-token-query'),b=document.getElementById('dex-token-search');
          const st=document.getElementById('dex-search-status'),out=document.getElementById('dex-search-results');
          const asset=document.getElementById('scan-asset');
          b.addEventListener('click',async()=>{
            const term=(q.value||'').trim();if(term.length<2){st.textContent='حداقل دو نویسه وارد کن.';return;}
            b.disabled=true;st.textContent='در حال جست‌وجو...';out.replaceChildren();
            try{const r=await fetch('/api/dex-search?q='+encodeURIComponent(term));const data=await r.json();
              if(!Array.isArray(data.results)||!data.results.length){st.textContent=data.reason||'نتیجه‌ای پیدا نشد.';return;}
              st.textContent='قرارداد دقیق و شبکه موردنظر را انتخاب کن.';
              data.results.forEach(item=>{const opt=document.createElement('button');opt.type='button';opt.textContent=item.name+' ('+item.symbol+') · '+item.chain+' · '+item.address;opt.addEventListener('click',()=>{asset.value=item.asset;st.textContent='انتخاب شد: '+item.chain+' / '+item.address;out.replaceChildren();});out.appendChild(opt);});
            }catch(e){st.textContent='سرویس جست‌وجوی DEX در دسترس نیست.';}finally{b.disabled=false;}
          });
          q.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();b.click();}});
        })();
        </script>
        <section class="card" id="market-tools">
          <h2>ماشین‌حساب حرفه‌ای سناریوی Spot</h2>
          <p class="muted">محاسبهٔ سود/زیان فرضی Long بدون اهرم؛ زمان انتخابی فقط افق سناریو است، نه پیش‌بینی زمان سود.</p>
          <div class="calc-grid">
            <div><label for="calc-amount">سرمایه (EUR)</label><input id="calc-amount" type="number" min="0.01" step="0.01" value="10"></div>
            <div><label for="calc-move">تغییر فرضی قیمت (%)</label><input id="calc-move" type="number" min="-99.9" step="0.1" value="10"></div>
            <div><label for="calc-days">افق سناریو (روز)</label><input id="calc-days" type="number" min="1" max="3650" value="14"></div>
            <div><label for="calc-buy-fee">کارمزد خرید (%)</label><input id="calc-buy-fee" type="number" min="0" max="20" step="0.01" value="0.1"></div>
            <div><label for="calc-sell-fee">کارمزد فروش (%)</label><input id="calc-sell-fee" type="number" min="0" max="20" step="0.01" value="0.1"></div>
            <div><label for="calc-buy-slip">لغزش خرید (%)</label><input id="calc-buy-slip" type="number" min="0" max="20" step="0.01" value="0.2"></div>
            <div><label for="calc-sell-slip">لغزش فروش (%)</label><input id="calc-sell-slip" type="number" min="0" max="20" step="0.01" value="0.2"></div>
          </div>
          <div class="grid" style="margin-top:12px">
            <div class="metric">سود/زیان خالص<strong id="calc-pnl">—</strong></div>
            <div class="metric">ارزش نهایی<strong id="calc-value">—</strong></div>
            <div class="metric">بازده خالص<strong id="calc-return">—</strong></div>
            <div class="metric">رشد سربه‌سر<strong id="calc-break-even">—</strong></div>
          </div>
          <h3>مقایسهٔ سناریوهای قیمت</h3><div id="calc-scenarios"></div>
          <p class="muted">فرمول: Vnet = I(1−fb)(1−sb)(1+r)(1−ss)(1−fs). مالیات، تغییر EUR/USD، هزینه برداشت/شبکه و اثر اندازه سفارش خارج از محاسبه‌اند.</p>
          <p class="muted">این ابزار پیش‌بینی، توصیه سرمایه‌گذاری یا سفارش معاملاتی نیست.</p>
        </section>
        <script>
        (function(){
          const ids=['calc-amount','calc-move','calc-days','calc-buy-fee','calc-sell-fee','calc-buy-slip','calc-sell-slip'];
          const el=id=>document.getElementById(id);
          function calc(move){
            const amount=Number(el('calc-amount').value), change=Number(move);
            const bf=Number(el('calc-buy-fee').value)/100, sf=Number(el('calc-sell-fee').value)/100;
            const bs=Number(el('calc-buy-slip').value)/100, ss=Number(el('calc-sell-slip').value)/100;
            const factor=(1-bf)*(1-bs)*(1+change/100)*(1-ss)*(1-sf);
            const net=amount*factor, pnl=net-amount;
            const base=(1-bf)*(1-bs)*(1-ss)*(1-sf);
            return {net,pnl,ret:pnl/amount*100,be:(1/base-1)*100};
          }
          function update(){
            const amount=Number(el('calc-amount').value),move=Number(el('calc-move').value);
            const days=Number(el('calc-days').value);
            const rates=ids.slice(3).map(id=>Number(el(id).value));
            if(!Number.isFinite(amount)||amount<=0||!Number.isFinite(move)||move<=-100||!Number.isFinite(days)||days<1||rates.some(x=>!Number.isFinite(x)||x<0||x>=100)){
              el('calc-pnl').textContent='ورودی نامعتبر';return;
            }
            const out=calc(move),eur=x=>x.toFixed(4)+' €';
            el('calc-pnl').textContent=(out.pnl>=0?'+':'')+eur(out.pnl);
            el('calc-value').textContent=eur(out.net);
            el('calc-return').textContent=(out.ret>=0?'+':'')+out.ret.toFixed(3)+'%';
            el('calc-break-even').textContent=out.be.toFixed(3)+'%';
            const rows=[-20,-10,0,5,10,20].map(v=>{const x=calc(v);return '<tr><td>'+v.toFixed(1)+'%</td><td>'+eur(x.pnl)+'</td><td>'+x.ret.toFixed(3)+'%</td><td>'+days+' روز</td></tr>';}).join('');
            el('calc-scenarios').innerHTML='<table><thead><tr><th>تغییر قیمت</th><th>سود/زیان خالص</th><th>بازده خالص</th><th>افق</th></tr></thead><tbody>'+rows+'</tbody></table>';
          }
          ids.forEach(id=>el(id).addEventListener('input',update));update();
        })();
        </script>
        <section class="card">
          <h3>۱۲ فیلتر اجباری</h3>
          <p>داده بازار · رژیم · روند · مومنتوم · جریان سرمایه · امنیت · احساسات · بک‌تست · ریسک · نهنگ‌ها · پوشش احساسات · Walk-forward</p>
          <p class="muted">هر فیلتر FAIL یا UNKNOWN باشد، خروجی NO TRADE است. سولانا در نسخه فعلی فقط نمونه‌برداری محدود از تراکنش‌های اخیر دارد.</p>
        </section>
        <p class="muted">تاریخچه: <code>/history?asset=cex:BTCUSDT</code></p>
        """
        return self._send(200, _page(body))

    def do_POST(self):
        global SCAN_RUNNING
        route = urlparse(self.path).path
        if route not in {"/scan", "/paper-bot/tick"}:
            return self._send(404, b"Not found", "text/plain; charset=utf-8")
        origin = self.headers.get("Origin")
        request_host = self.headers.get("Host", "")
        if origin and urlparse(origin).netloc.lower() != request_host.lower():
            return self._send(403, b"Cross-origin scan requests are not allowed", "text/plain; charset=utf-8")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            return self._send(400, b"Invalid Content-Length", "text/plain; charset=utf-8")
        if length <= 0 or length > 16_384:
            return self._send(413, b"Invalid request size", "text/plain; charset=utf-8")
        try:
            form = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
            asset = _parse_scan_asset(form.get("asset", [""])[0].strip())
            if route == "/paper-bot/tick" and (
                asset.market_type != "cex" or asset.symbol not in PAPER_ASSETS
            ):
                raise ValueError("Paper trading accepts only BTC, ETH or SOL spot.")
            headline = form.get("headline", [""])[0].strip() if route == "/scan" else ""
            social_post = form.get("social_post", [""])[0].strip() if route == "/scan" else ""
            global JOB_FENCE_COUNTER
            canonical_request = json.dumps(
                {"route": route, "asset": asset.raw, "headline": headline, "social_post": social_post},
                sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            )
            idempotency_key = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
            job_id = uuid.uuid4().hex
            task = {"job_id": job_id, "route": route, "asset": asset,
                    "headline": headline, "social_post": social_post,
                    "idempotency_key": idempotency_key}
            _ensure_scan_worker()
            with LOCK:
                existing_id = ACTIVE_SCAN_KEYS.get(idempotency_key)
                existing = JOBS.get(existing_id) if existing_id else None
                if existing and existing.get("status") in {"queued", "running"}:
                    job_id = existing_id
                else:
                    if SCAN_QUEUE.full():
                        return self._send(429, _page("<h2>صف اسکن پر است</h2><p>۵۰ درخواست در صف هستند. کمی بعد دوباره تلاش کن.</p><a href='/'>بازگشت</a>"))
                    ACTIVE_SCAN_KEYS[idempotency_key] = job_id
                    JOB_FENCE_COUNTER += 1
                    created_at = datetime.now(timezone.utc)
                    JOBS[job_id] = {
                        "status": "queued",
                        "queue_position": SCAN_QUEUE.qsize() + 1,
                        "created_at_utc": created_at.isoformat(),
                        "valid_until_utc": (created_at + timedelta(seconds=JOB_LEASE_SECONDS)).isoformat(),
                        "idempotency_key": idempotency_key,
                        "fencing_token": JOB_FENCE_COUNTER,
                    }
                    task["fencing_token"] = JOBS[job_id]["fencing_token"]
                    while len(JOBS) > 100:
                        oldest = next(iter(JOBS))
                        if JOBS[oldest].get("status") in {"queued", "running"}:
                            break
                        JOBS.pop(oldest)
                    SCAN_QUEUE.put_nowait(task)
            if route == "/paper-bot/tick":
                body = (
                    "<h2>چرخهٔ ربات آزمایشی آغاز شد</h2><p>شناسه: <code>" + job_id + "</code></p>"
                    "<p id='state'>در حال ارزیابی دادهٔ بازار و مدیریت موقعیت کاغذی...</p>"
                    "<pre id='result'>هیچ سفارش واقعی ارسال نمی‌شود.</pre><p><a href='/'>بازگشت</a></p>"
                    "<script>const job='" + job_id + "';async function poll(){try{const r=await fetch('/jobs/'+job);const j=await r.json();document.getElementById('state').textContent=j.status;if(j.status==='completed'){document.getElementById('result').textContent=JSON.stringify(j.result,null,2);return;}if(j.status==='failed'){document.getElementById('result').textContent=j.error;return;}}catch(e){document.getElementById('state').textContent='ارتباط قطع شد؛ وضعیت در /paper-bot/status موجود است';return;}setTimeout(poll,2500);}poll();</script>"
                )
                return self._send(202, _page(body, "Paper Bot Cycle"))
            body = (
                "<h2>اسکن آغاز شد</h2><p>شناسه کار: <code>" + job_id + "</code></p>"
                "<p id='state'>در حال جمع‌آوری داده‌های بازار، نهنگ‌ها و احساسات...</p>"
                "<section class='card'><h3 id='diagnosis-title'>تشخیص نهایی</h3><div id='diagnosis-headline' class='metric'></div><div id='diagnosis-short' class='grid'></div><div id='diagnosis-long' class='grid'></div><div id='diagnosis-reasons'></div></section>"
                "<section class='card'><h3>کف و سقف در بازه‌های مختلف</h3><div id='timeframe-range-table'></div><p id='candle-age-note' class='muted'></p></section>"
                "<section class='card'><h3>نمای کلی بازار و نمودار</h3><div id='market-summary' class='grid'></div>"
                "<div id='range-table'></div><canvas id='candles' width='1000' height='320'></canvas></section>"
                "<section class='card'><h3>Market Intelligence · DEX و مشتقات</h3><div id='mi-summary' class='grid'></div><div id='mi-notes' class='muted'></div></section>"
                "<section class='card'><h3>۱۲ فیلتر</h3><div id='gate-table'></div></section>"
                "<section class='card'><h3>خلاصه نتیجه اسکن</h3><div id='scan-summary' class='grid'></div></section><p><a href='/'>بازگشت</a></p>"
                "<script>const job='" + job_id + "';"
                "function cell(tag,text){const e=document.createElement(tag);e.textContent=text??'UNKNOWN';return e;}"
                "function table(container,headers,rows){const t=document.createElement('table'),h=document.createElement('thead'),hr=document.createElement('tr');headers.forEach(v=>hr.appendChild(cell('th',v)));h.appendChild(hr);t.appendChild(h);const b=document.createElement('tbody');rows.forEach(row=>{const tr=document.createElement('tr');row.forEach(v=>tr.appendChild(cell('td',v)));b.appendChild(tr);});t.appendChild(b);container.replaceChildren(t);}"
                "function renderMarket(m){const box=document.getElementById('market-summary');"
                "const items=[['وضعیت دارایی',m.asset_regime],['رژیم معیار',m.benchmark_regime],['قیمت آخر',m.current_price],['سن کندل (ساعت)',m.candle_age_hours],['وضعیت کف/سقف',m.status],['الگوی هندسی',(m.chart_pattern||{}).pattern||'نامشخص'],['وضعیت الگو',(m.chart_pattern||{}).status||'UNKNOWN'],['تأیید شکست با حجم',(m.chart_pattern||{}).breakout_confirmation||'UNKNOWN'],['حجم شکست / میانه ۲۰ کندل',(m.chart_pattern||{}).breakout_volume_ratio||'UNKNOWN']];"
                "box.replaceChildren();items.forEach(([k,v])=>{const d=document.createElement('div');d.className='metric';d.appendChild(cell('span',k));const strong=cell('strong',v);d.appendChild(strong);box.appendChild(d);});"
                "const labels={'7':'۷ روز','30':'۳۰ روز','90':'۹۰ روز','365':'۳۶۵ روز'};"
                "const rows=Object.entries(m.ranges||{}).map(([k,v])=>[labels[k]||k,v.status,v.low,v.high,v.candle_count,v.coverage_pct]);"
                "table(document.getElementById('range-table'),['بازه','وضعیت','کف','سقف','تعداد کندل','پوشش %'],rows);"
                "const canvas=document.getElementById('candles'),ctx=canvas.getContext('2d'),bars=m.candles||[];ctx.clearRect(0,0,canvas.width,canvas.height);"
                "if(!bars.length){ctx.fillStyle='#f59e0b';ctx.fillText('تاریخچه OHLC برای نمودار موجود نیست',20,30);return;}"
                "const lo=Math.min(...bars.map(x=>x.low)),hi=Math.max(...bars.map(x=>x.high)),span=Math.max(hi-lo,1e-12),pad=20,w=(canvas.width-2*pad)/bars.length;"
                "bars.forEach((x,i)=>{const cx=pad+i*w+w/2,y=v=>canvas.height-pad-(v-lo)/span*(canvas.height-2*pad);const up=x.close>=x.open;ctx.strokeStyle=up?'#22c55e':'#ef4444';ctx.fillStyle=ctx.strokeStyle;ctx.beginPath();ctx.moveTo(cx,y(x.high));ctx.lineTo(cx,y(x.low));ctx.stroke();const top=y(Math.max(x.open,x.close)),bottom=y(Math.min(x.open,x.close));ctx.fillRect(cx-w*.32,top,Math.max(1,w*.64),Math.max(1,bottom-top));});const shape=m.chart_pattern||{};if(shape.status==='FORMING'||shape.status==='BREAKOUT_UP'||shape.status==='BREAKOUT_DOWN'){const drawLine=(line,color)=>{if(!line)return;ctx.save();ctx.strokeStyle=color;ctx.lineWidth=2;ctx.setLineDash([7,5]);ctx.beginPath();ctx.moveTo(pad+(line.start_index+.5)*w,y(line.start_price));ctx.lineTo(pad+(line.end_index+.5)*w,y(line.end_price));ctx.stroke();ctx.restore();};drawLine(shape.upper_line,'#ffc66d');drawLine(shape.lower_line,'#72a7ff');(shape.pivots||[]).forEach(p=>{ctx.fillStyle=p.kind==='HIGH'?'#ffc66d':'#72a7ff';ctx.beginPath();ctx.arc(pad+(p.index+.5)*w,y(p.price),4,0,Math.PI*2);ctx.fill();});ctx.fillStyle='#edf4fb';ctx.fillText('Triangle: '+(shape.status||'UNKNOWN')+' / '+(shape.breakout||'NO BREAKOUT'),pad,16);}}"
                "function renderTimeframes(rows){const labels={'1h':'۱ ساعت','6h':'۶ ساعت','12h':'۱۲ ساعت','1d':'۱ روز','1w':'۱ هفته','1mo':'۱ ماه'};const data=Object.entries(rows||{}).map(([k,v])=>[labels[k]||k,v.status==='KNOWN'?'کامل':v.status==='PARTIAL'?'ناقص':'ناموجود',v.low,v.high,v.bar_count+'/'+v.expected_bars,v.coverage_pct===undefined?'—':v.coverage_pct+'%',v.age_hours===null||v.age_hours===undefined?'—':v.age_hours+' ساعت']);table(document.getElementById('timeframe-range-table'),['بازه','وضعیت داده','کف','سقف','تعداد کندل','پوشش','سن آخرین کندل'],data);}"
                "function renderDiagnosis(d){const title=document.getElementById('diagnosis-title'),head=document.getElementById('diagnosis-headline');title.textContent='تشخیص نهایی · '+(d.coin_name||'رمزارز')+' ('+(d.symbol||'')+')';head.replaceChildren(cell('strong',d.final_recommendation||'تشخیص در دسترس نیست'),cell('div','وضعیت کلی: '+(d.overall_signal_fa||'خنثی')+' · '+(d.explanation||'')));const makeCard=(label,item)=>{const box=document.createElement('div');box.className='metric';box.appendChild(cell('strong',label));box.appendChild(cell('div',item.action_fa||'ناموجود'));(item.reasons||[]).forEach(reason=>{const p=cell('p','• '+reason);box.appendChild(p);});return box;};document.getElementById('diagnosis-short').replaceChildren(makeCard('تصمیم کوتاه‌مدت',d.short_term||{}));document.getElementById('diagnosis-long').replaceChildren(makeCard('تصمیم بلندمدت / نگهداری',d.long_term||{}));const list=document.createElement('ul');(d.blockers||[]).forEach(row=>{const li=cell('li',(row.name?row.name+' — ':'')+row.reason+' ['+row.status+']');list.appendChild(li);});const reasons=document.getElementById('diagnosis-reasons');reasons.replaceChildren(cell('h4','چرا خرید توصیه می‌شود یا نمی‌شود؟'),list);document.getElementById('candle-age-note').textContent=d.candle_age_text||'سن کندل در دسترس نیست.';}"
                "function renderMarketIntelligence(m){const d=m.dex_screener||{},c=m.coinglass||{},s=m.scoring||{};"
                "const items=[['وضعیت کلی',m.status],['توکن DEX',(d.token_name||'')+' '+(d.token_symbol||'')],['نقدینگی DEX (USD)',d.liquidity_usd],['حجم DEX در ۲۴ ساعت (USD)',d.volume_24h_usd],['سهم تعداد تراکنش خرید %',d.buy_transaction_share_pct],['تعداد تراکنش ۲۴ ساعت',d.total_transactions_24h],['عمر جفت‌ارز (ساعت)',d.pair_age_hours],['وضعیت CoinGlass',c.status],['Funding میانه %',c.funding_median_pct],['تعداد صرافی‌های Funding',c.funding_exchange_count],['Open Interest (USD)',c.open_interest_usd],['تغییر OI در ۲۴ ساعت %',c.open_interest_change_24h_pct],['تعدیل امتیاز تأیید',s.confirmation_adjustment],['تعدیل امتیاز ریسک',s.risk_adjustment],['فشار DEX',s.dex_flow_bias],['ازدحام مشتقات',s.coinglass_crowding]];const box=document.getElementById('mi-summary');box.replaceChildren();items.forEach(([k,v])=>{const d=document.createElement('div');d.className='metric';d.appendChild(cell('span',k));d.appendChild(cell('strong',v===null||v===undefined?'ناموجود':v));box.appendChild(d);});const notes=[...(s.notes||[]),d.reason,c.reason,m.disclaimer].filter(Boolean);document.getElementById('mi-notes').textContent=notes.join(' · ')||'دادهٔ تکمیلی خاصی از منابع بیرونی گزارش نشده است.';}"
                "async function poll(){try{const r=await fetch('/jobs/'+job);const j=await r.json();document.getElementById('state').textContent=j.status;"
                "if(j.status==='completed'){renderDiagnosis(j.result.diagnosis||{});renderTimeframes(j.result.timeframe_ranges||{});renderMarket(j.result.market_overview||{});renderMarketIntelligence(j.result.market_intelligence||{});renderGates(j.result.gates);const s=j.result,box=document.getElementById('scan-summary');const items=[['نام ارز',s.coin_name||s.asset],['نماد',s.symbol||'—'],['وضعیت',((s.diagnosis||{}).overall_signal_fa)||s.signal],['امتیاز تأیید',s.confirmation_score],['امتیاز ریسک',s.estimated_risk_score],['رتبه فرصت',s.opportunity_rank],['خلاصه',s.summary_fa]];box.replaceChildren();items.forEach(([k,v])=>{const d=document.createElement('div');d.className='metric';d.appendChild(cell('span',k));d.appendChild(cell('strong',v));box.appendChild(d);});return;}"
                "if(j.status==='failed'){document.getElementById('state').textContent='اسکن ناموفق بود: '+j.error;return;}"
                "}catch(e){document.getElementById('state').textContent='ارتباط قطع شد؛ بعداً /jobs/'+job+' را بررسی کنید';return;}"
                "setTimeout(poll,2500);}poll();</script>"
            )
            return self._send(202, _page(body, "Scan Running"))
        except Exception as exc:
            with LOCK:
                SCAN_RUNNING = False
            message = html.escape(f"{type(exc).__name__}: {exc}")
            return self._send(400, _page(f"<h2>خطا در ورودی</h2><p>{message}</p><a href='/'>بازگشت</a>"))

    def log_message(self, fmt, *args):
        super().log_message(fmt, *args)


def main():
    port = int(os.getenv("PORT", "10000"))
    host = os.getenv("HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"CRYPTO SUPER RADAR listening on {host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()